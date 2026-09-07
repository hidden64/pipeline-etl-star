# Phase 4 : Nettoyage, validation et gestion des rejets

C'est le cœur du sujet. On part de 2 355 913 lignes brutes en `text` et on
produit une table propre, typée, dédoublonnée, en traçant chaque anomalie.

## 1. Résultat mesuré

```
lignes lues ............ 2 355 913
lignes propres ......... 2 342 951   (99,45 %)
doublons absorbés ......     9 345
rejets ERREUR ..........     3 617   (0,15 %)
```

Concordance avec les anomalies injectées (phase 2) : la validation retrouve
exactement ce qui a été semé :

| Règle | Détecté | Injecté |
|---|---:|---:|
| R003 retard aberrant | 3 588 | 3 580 |
| R006 incohérence suppression | 29 | 29 |
| A103 doublon exact (groupes) | 7 025 | 6 977 |
| A104 doublon divergent (groupes) | 2 320 | 2 376 |

Les petits écarts viennent des collisions : une ligne peut cumuler deux
anomalies (un doublon qui est aussi un retard aberrant), et l'ordre de priorité
tranche.

## 2. Les deux sévérités

| | ERREUR | AVERTISSEMENT |
|---|---|---|
| Sort du flux ? | Oui, écartée du fait | Non, conservée |
| Exemples | retard aberrant, date illisible, incohérence | arrêt orphelin, doublon, météo absente |
| Principe | ce qu'on ne peut **pas** récupérer sans inventer | ce qui est récupérable de façon déterministe |

Règle : on ne rejette que ce qui est irrécupérable. Perdre 3 588 lignes de
capteur en vrac est moins grave que fausser toutes les moyennes de retard avec
des `99999`.

## 3. Architecture : le SQL décide, Python arbitre

Toute la logique est **ensembliste**, en SQL : appliquer une règle à 2,3 M
lignes se fait en un ordre, jamais en une boucle Python. Le module Python
`validation.py` se contente d'appeler la procédure, de lire le bilan et
d'appliquer la politique de seuil (arrêt si le taux de rejet dépasse le seuil :
au-delà, ce n'est plus une donnée à nettoyer mais une source cassée).

Trois destinations depuis `staging.realisation` :

```
                         ┌───────────────────────────────┐
                         │  staging.v_realisation_        │
  staging.realisation ──▶│  normalisee (vue)             │
  (2,3 M, tout en text)  │  parse + drapeaux par règle    │
                         └───────────────┬───────────────┘
                                         │ router_realisations()
                 ┌───────────────────────┼───────────────────────┐
                 ▼                       ▼                       ▼
     staging.realisation_valide     rejet.rejet            rejet.rejet
     (propre, typé, dédoublonné)    (ERREUR + charge)      (doublons tracés)
```

## 4. Les traitements clés

### 4.1 Heures GTFS > 24 h : résolues nativement

`date_service + heure::interval`. PostgreSQL sait que `'25:14:00'::interval` vaut
`1 jour 01:14:30`, donc `04/09 + 25:14` bascule au `05/09 01:14`. Vérifié dans la
table propre : les courses de nuit apparaissent bien au lendemain. `AT TIME ZONE
'Europe/Paris'` ancre le tout en `timestamptz`, correct au changement d'heure.

### 4.2 Dédoublonnage : « dernière observation gagne »

Clé naturelle `(date_service, id_course, rang_arret)`. Un `row_number()` par
groupe, trié par `numero_ligne` décroissant, garde le rang 1. Justification
métier : dans un flux temps réel, la dernière estimation est la plus proche du
réel ; dans un rejeu de fichier, la dernière écriture prime. Même règle dans les
deux cas.

Les doublons sont **tracés**, pas silencieux. On distingue exact et divergent en
comptant les combinaisons de mesures distinctes dans chaque groupe :
`[98, 156]` dans la charge utile d'un A104 montre les deux retards concurrents.

### 4.3 Charge utile des rejets

Chaque rejet ERREUR porte la ligne source **intégrale** en `jsonb`
(`to_jsonb(r.*)`), reconstruite depuis le vrai brut. On peut corriger puis
rejouer un rejet sans retourner au fichier d'origine.

## 5. La bataille de la performance : mesurer, pas deviner

C'est la partie la plus instructive. Le routage est passé par **trois** versions.

| Version | Temps | Cause |
|---|---:|---|
| Fonctions PL/pgSQL + `EXCEPTION` | 285 s | savepoint par ligne (~9 M) |
| Réécriture SQL pur, naïve | 372 s | **pire** : hypothèse fausse |
| CTE `base` MATERIALIZED + `work_mem` | ~110 s | réévaluation supprimée |

### Ce qui s'est passé

1. **Première hypothèse : les blocs `EXCEPTION`.** Vraie en partie (un bloc
   `EXCEPTION` crée un savepoint par appel), mais les réécrire en SQL pur a
   *aggravé* le temps. **On ne devine pas la performance, on la mesure.**

2. **Mesure ciblée.** Parse d'une colonne sur 200 k lignes : 1,1 s. La vue
   complète sur 200 k lignes : 40,4 s, soit 37× plus lent. Le parsing n'était donc
   pas le coupable.

3. **Vraie cause : l'inlining des CTE.** PostgreSQL fond les CTE dans la requête
   et **réévalue** les fonctions de parsing à chaque référence de leur colonne.
   `date_service` et `heure_theorique` sont référencés 5-6 fois → ~14 M appels au
   lieu de 2,3 M.

4. **Remède : `WITH base AS MATERIALIZED`.** Force le calcul une seule fois,
   stocké puis relu. Seul `base` (le parsing cher) est matérialisé ; `typee`
   reste en flux, car il ne lit que des colonnes déjà calculées. Plus
   `SET LOCAL work_mem = '256MB'` pour que le tri de dédoublonnage ne déborde pas
   sur disque.

## 6. Trois bugs révélés par les tests

Les tests SQL contre le moteur ont attrapé ce que la lecture n'avait pas vu.

1. **`parse_decimal_fr('18.4')` renvoyait NULL** alors que son commentaire
   promettait de gérer le point. La regex n'acceptait que la virgule. *Le
   commentaire mentait ; le test a tranché.*

2. **`to_date('31/13/2026')` lève une exception** en PostgreSQL 18 (mois hors
   champ). Ma fonction SQL pure, sans `EXCEPTION`, aurait fait **échouer tout le
   routage** sur une seule date à mois 13 dans 2,3 M lignes. Bombe à retardement
   invisible tant qu'aucune donnée ne la déclenche.

3. **`30/02` lève aussi** (débordement de jour), et l'aller-retour `to_char` ne
   pouvait pas l'attraper puisque `to_date` plante avant. Pire : une fois la
   fonction inlinée, PostgreSQL évalue `make_date` **avant** le `WHERE` censé
   filtrer.

**Solution finale** : valider le nombre de jours du mois soi-même (avec la règle
bissextile grégorienne), et n'appeler `make_date` que sur une combinaison
prouvée valide, via des **`CASE` imbriqués**, dont le court-circuit est garanti
par PostgreSQL, contrairement à `AND`. Robuste face à n'importe quelle entrée,
sans bloc `EXCEPTION`, donc sans coût.

## 7. Fichiers produits

```
sql/transform/00_catalogue_regles.sql     règles + sévérités
sql/transform/10_normalisation.sql        fonctions de parsing + vue à drapeaux
sql/transform/20_router_propre_rejet.sql  procédure de routage
src/mobilite/validation.py                orchestration + politique de seuil
tests/test_normalisation_sql.py           tests des fonctions contre le moteur
tests/conftest.py                         fixtures base, skip propre si absente
```

61 tests passent.

**Prochaine étape (phase 5)** : alimentation des dimensions SCD2 (`dim_ligne`,
`dim_arret`) avec détection de changement par hash, et gestion des versions.
