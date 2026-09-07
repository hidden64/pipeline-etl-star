# Phase 2 : Extraction des sources hétérogènes

## 1. Les trois sources

| # | Source | Format | Protocole | Volume réel | Ce qu'elle apporte |
|---|---|---|---|---|---|
| 1 | GTFS statique STAR | ZIP de CSV | HTTP | 12,4 Mo → 138 Mo décompressés | Référentiel (lignes, arrêts) + horaires théoriques |
| 2 | Open-Meteo | JSON | API REST | 408 relevés horaires | Conditions météo, axe d'analyse |
| 3 | Export d'exploitation | CSV latin-1 `;` | Fichier | 2 355 913 lignes, 188 Mo | Passages réalisés, donc les retards |

Trois formats, trois encodages, trois protocoles : c'est la définition même de
sources hétérogènes, et chacune impose son propre traitement.

## 2. Volumétrie mesurée (pas estimée)

Mesurer avant de coder évite de découvrir en production qu'on génère 40 millions
de lignes.

```
routes GTFS totales ................... 155
routes retenues (a, b, C1→C7) .........   9
courses retenues ...................... 31 195
passages théoriques distincts ......... 790 221
fenêtre du calendrier GTFS ............ 2026-09-04 → 2026-10-12 (39 jours de service)
passages / jour ....................... ~60 200
TOTAL de faits attendus ............... 2 355 913
```

## 3. Décisions et leurs justifications

### 3.1 La période est imposée par le calendrier GTFS

Première période envisagée : juin–août 2026, choisie pour que la météo provienne
de l'archive **observée** plutôt que d'une prévision.

Elle s'est révélée inapplicable. `calendar.txt` ne décrit que la fenêtre de
validité du feed : **2026-09-04 → 2026-10-12**. En dehors, aucun `service_id`
n'est actif, donc aucune course : le pipeline aurait produit zéro ligne.

> **Leçon générale : un référentiel temporel ne s'extrapole pas.** Un calendrier
> GTFS n'est pas un motif hebdomadaire qu'on peut appliquer à n'importe quelle
> date ; c'est une description bornée d'une période donnée. La seule alternative
> aurait été un « calendrier de substitution » (rejouer le lundi de la fenêtre
> sur chaque lundi de juin), une approximation supplémentaire dont on se passe.

Période retenue : **la fenêtre du feed**. Bénéfice secondaire : elle chevauche
deux mois, ce qui exerce réellement le partitionnement mensuel.

### 3.2 Horizon météo : une lacune assumée et signalée

Open-Meteo expose deux points d'entrée aux propriétés opposées :

| Point d'entrée | Couverture | Fiabilité |
|---|---|---|
| `archive-api` | passé, jusqu'à J-5 environ | Réanalyse consolidée |
| `api/forecast` | J-92 → J+16 via `past_days` / `forecast_days` | Prévision |

Sur la période retenue et à la date d'exécution, **17 jours sur 39** sont
couverts (2026-09-04 → 2026-09-20). Les 22 restants sont dans le futur, au-delà
de l'horizon de prévision : **aucune donnée n'existe**, ni chez Open-Meteo ni
ailleurs.

Trois réactions possibles, et une seule est correcte :

1. Rétrécir la période pour masquer le problème → on cache une limite réelle ;
2. Inventer une valeur par défaut → on fabrique de la donnée fausse ;
3. **Rattacher ces faits au membre inconnu de `dim_meteo` (`sk_meteo = -1`),
   émettre un avertissement, et laisser les exécutions quotidiennes combler le
   trou.** ← retenu

C'est exactement le cas d'usage du membre inconnu défini en phase 1 : le fait est
conservé, les totaux de passages restent justes, et l'absence de météo devient
une grandeur mesurable au lieu d'un trou silencieux.

### 3.3 Deux producteurs, un seul format

Le temps réel ne donne qu'un instantané : **on ne peut pas remonter dans le
passé**. Constituer trois mois d'historique par collecte demanderait trois mois.

D'où deux producteurs écrivant le **même format d'export** :

```
                    ┌────────────────────────┐
  GTFS statique ───▶│  realisations.py       │──┐
  + météo           │  (simulateur, calibré) │  │
                    └────────────────────────┘  │   export CSV
                                                ├──  d'exploitation  ──▶  PIPELINE
                    ┌────────────────────────┐  │   (format unique)
  flux GTFS-RT ────▶│  collecteur_rt.py      │──┘
  (protobuf)        │  (collecteur, réel)    │
                    └────────────────────────┘
```

Le contrat entre producteur et consommateur est **le format de fichier, pas le
code**. Conséquences :

* tout le pipeline est démontrable aujourd'hui, sur un historique complet ;
* passer au réel ne change pas une ligne d'ETL, seulement l'ordonnancement ;
* les deux peuvent tourner en parallèle et être comparés.

C'est un motif d'architecture courant, et il se raconte bien en entretien.

### 3.4 Le collecteur ne dédoublonne pas : volontairement

Le flux GTFS-RT republie le même passage toutes les 60 secondes, avec une
estimation qui s'affine à l'approche du véhicule. Le collecteur écrit **toutes**
les observations.

Le dédoublonnage appartient au pipeline (phase 5), avec une règle explicite et
traçable : garder la dernière observation. Si le collecteur tranchait, la
décision serait enfouie dans un script d'acquisition, invisible et non
auditable. **Un collecteur collecte, il ne décide pas.**

## 4. Ce que le simulateur injecte

Anomalies réellement produites sur les 2 355 913 lignes :

| Anomalie | Nombre | Ce qu'elle teste en phase 4 |
|---|---:|---|
| Espaces parasites (`"  C1 "`) | 23 375 | Normalisation des chaînes |
| Casse incohérente (`realise` / `Realise`) | 18 772 | Normalisation des énumérations |
| Valeurs manquantes | 7 054 | Gestion des champs vides |
| Doublons exacts | 6 977 | Dédoublonnage sur clé naturelle |
| Arrêts orphelins | 4 671 | Intégrité référentielle → membre inconnu |
| Retards aberrants (`99999`, `-5000`) | 3 580 | Bornes métier → rejet |
| Doublons divergents | 2 376 | Arbitrage entre versions concurrentes |
| États incohérents | 29 | Contrôle de cohérence inter-colonnes |

Plus les caractéristiques structurelles du fichier : encodage **latin-1**,
séparateur **`;`**, fins de ligne **CRLF**, dates **`JJ/MM/AAAA`**, décimales à
**virgule**, et heures GTFS **supérieures à 24 h** conservées telles quelles.

Le générateur est **déterministe** (graine fixée) : deux exécutions produisent le
même fichier. Sans cela, aucun test ne pourrait affirmer « ce lot doit produire
exactement N rejets ».

## 5. Règles d'extraction appliquées

* **Ne rien transformer.** L'extraction dépose l'octet reçu, tel quel. Toute
  normalisation appartient à la transformation, sinon on perd la capacité de
  rejouer sans retélécharger.
* **Écriture atomique.** Téléchargement vers `.partiel`, puis renommage. Aucun
  fichier tronqué ne peut être pris pour un succès.
* **Temporisation exponentielle** (2 s → 4 s → 8 s). Marteler un service saturé
  ne le débloque pas.
* **Empreinte SHA-256 systématique**, pour l'idempotence et l'audit. Le JSON
  météo est écrit trié, sinon deux exécutions identiques donneraient deux
  empreintes différentes.
* **Contrôle d'intégrité avant confiance.** `zipfile.is_zipfile` et vérification
  des fichiers GTFS obligatoires : une page d'erreur HTML servie en HTTP 200
  échoue ici, pas trois étapes plus loin.
* **Protection contre le « Zip Slip ».** Un membre d'archive nommé
  `../../autre.txt` écrirait hors du répertoire cible. Les noms non plats sont
  refusés.

## 6. Fichiers produits

```
src/mobilite/config.py         configuration typée, validée au démarrage
src/mobilite/journal.py        journalisation + masquage des secrets
src/mobilite/extraction.py     GTFS (ZIP/CSV) et météo (API JSON)
src/mobilite/realisations.py   simulateur d'export d'exploitation
src/mobilite/collecteur_rt.py  collecteur GTFS-RT (protobuf)
```

**Prochaine étape (phase 3)** : zone de staging et chargement brut par `COPY`,
avec journalisation des exécutions dans `meta`.
