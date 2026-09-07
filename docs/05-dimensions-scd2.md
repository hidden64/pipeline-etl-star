# Phase 5 — Dimensions historisées (SCD2) et dimension météo

## 1. Résultat mesuré

```
dim_ligne ..... 155 lignes    (2 métros + 8 Chronostar structurants + 145 bus)
dim_arret ..... 1 804 arrêts
dim_meteo .....  36 combinaisons  (à partir de 408 relevés horaires)
```

La météo illustre à elle seule l'intérêt de la discrétisation : **408 relevés
continus deviennent 36 combinaisons distinctes** de tranches. Un axe d'analyse à
36 valeurs est exploitable dans un `GROUP BY` ; un axe à 408 ne l'est pas.

## 2. Le patron SCD2, en deux ordres

Tout le mécanisme d'historisation tient en deux ordres ensemblistes, dans cet
ordre impératif :

```sql
-- 1. FERMER les versions courantes dont le hash a changé
UPDATE dim SET date_fin_validite = date_effet - 1, est_courant = false
  FROM source WHERE dim.est_courant AND dim.cle = source.cle
              AND dim.hash <> source.hash;

-- 2. INSÉRER une version courante pour toute clé qui n'en a plus plus
INSERT INTO dim SELECT ... FROM source
  WHERE NOT EXISTS (SELECT 1 FROM dim WHERE est_courant AND cle = source.cle);
```

Pourquoi cet ordre fonctionne :

- l'étape 1 ferme les clés **modifiées** (elles n'ont plus de version courante) ;
- l'étape 2 insère les clés **sans version courante** : les nouvelles, et celles
  qu'on vient de fermer ;
- les clés **inchangées** gardent leur version courante avec le bon hash →
  l'étape 2 les ignore. C'est ce qui rend la procédure **idempotente**.

## 3. La détection de changement par hash

On calcule un `md5` de la concaténation des attributs suivis :

```sql
md5( coalesce(nom, '') || '|' || coalesce(mode, '') || '|' || ... )
```

Deux détails qui ne sont pas cosmétiques :

- **`coalesce(x, '')`** : sans lui, un seul attribut NULL rendrait tout le hash
  NULL, et deux lignes différentes pourraient sembler identiques.
- **le séparateur `|`** : sans lui, `('ab', 'c')` et `('a', 'bc')` produiraient
  la même concaténation `abc` et donc le même hash — une collision silencieuse.

Comparer un hash est plus rapide et plus sûr que douze `IS DISTINCT FROM`, et
insensible à l'ordre des colonnes une fois le hash calculé.

## 4. Démonstration du cycle de vie (vérifiée)

Renommage de l'arrêt « Anatole France » → « Anatole France - Centre », avec date
d'effet au 20/09 :

| sk_arret | nom | début | fin | courant |
|---:|---|---|---|:---:|
| 2 | Anatole France | 2026-09-04 | **2026-09-19** | **false** |
| 1806 | Anatole France - Centre | **2026-09-20** | 9999-12-31 | **true** |

Les deux versions **coexistent** (historique préservé), une seule est
**courante** (invariant respecté), et la continuité temporelle est exacte : la
nouvelle prend le relais le lendemain de la fermeture, sans trou ni chevauchement.

Le test `tests/test_scd2.py` rejoue ce cycle complet de façon automatisée et
reproductible, sur une clé dédiée qu'il nettoie après lui.

## 5. La date d'effet

Une nouvelle version SCD2 prend effet à une date précise. On la prend égale à
`feed_info.feed_start_date` — la date à laquelle la nouvelle description du
réseau devient officielle (ici 2026-09-04). On la LIT dans le staging plutôt que
de la coder en dur : elle change à chaque feed. À défaut, on retombe sur la date
du jour.

## 6. dim_meteo : une junk dimension

Contrairement aux deux autres, `dim_meteo` n'est **pas** historisée. Chaque ligne
est une combinaison distincte de conditions, insérée une fois
(`ON CONFLICT (hash) DO NOTHING`).

Deux points de conception :

- **La discrétisation porte le sens métier.** On ne stocke pas « 3,2 mm » mais la
  tranche `MODEREE`. La question analytique est « quand il pleut fort », pas
  « quand il tombe 3,2 mm ».
- **Un attribut précalculé, `est_conditions_degradees`.** Il condense la règle
  métier (pluie soutenue, neige, orage, ou vent fort) dans la dimension, pour
  qu'un analyste écrive `WHERE est_conditions_degradees` sans la réécrire.

Le dépliage du document météo (un seul `jsonb`) en lignes se fait avec
`jsonb_to_recordset`, l'outil idiomatique pour projeter un tableau d'objets JSON
en relationnel. La procédure construit aussi `staging.meteo_creneau`, la table
`(jour, heure) → sk_meteo` que le fait utilisera en phase 6.

## 7. Un bug révélé par le test : la logique ternaire SQL

Le test SCD2, en injectant une route **minimale** (sans `route_desc`), a fait
apparaître un défaut invisible en production :

```sql
-- FAUX : si route_desc est NULL, l'expression entière vaut NULL
mode = 'METRO' OR route_desc ILIKE '%chronostar%' OR code ~ '^C[0-9]'
--                ↑ NULL ILIKE ... = NULL, et false OR NULL OR false = NULL
```

`est_ligne_structurante` étant `NOT NULL`, l'insertion échouait — mais seulement
pour une ligne sans `route_desc`. Les 155 vraies lignes en ont toutes une, donc
la production n'a jamais planté. **Un test sur une donnée minimale a débusqué ce
que la donnée réelle masquait.**

Correctif : `coalesce(route_desc, '') ILIKE '%chronostar%'`. En SQL, tant qu'une
colonne peut être NULL, toute expression booléenne qui la touche doit être
protégée si son résultat alimente une contrainte `NOT NULL`.

## 8. Fichiers produits

```
sql/transform/30_dimensions_scd2.sql   dim_ligne + dim_arret (SCD2)
sql/transform/40_dim_meteo.sql         dim_meteo + table meteo_creneau
src/mobilite/dimensions.py             orchestration + date d'effet
tests/test_scd2.py                     cycle de vie SCD2 automatisé
```

62 tests passent.

**Prochaine étape (phase 6)** : construction de la table de faits. Résolution des
clés de substitution (date, créneau, ligne, arrêt, météo), rattachement au membre
inconnu pour les cas non résolus, et chargement partitionné.
