# Phase 3 : Staging et chargement brut

## 1. Résultat mesuré

```
4 301 863 lignes chargées en ~18 s (exécution complète)
  staging.realisation ......... 2 355 913 lignes   296 Mo
  staging.gtfs_stop_times ..... 1 857 606 lignes   209 Mo
  staging.gtfs_trips ..........    85 891 lignes    12 Mo
  staging.gtfs_stops ..........     1 804 lignes
  staging.meteo_brut ..........         1 document  (408 relevés en jsonb)
  … + routes, calendar, agency, feed_info
```

## 2. Le débit de COPY, mesuré

| Stratégie | Débit | Facteur | Durée extrapolée sur 2,36 M lignes |
|---|---:|---:|---:|
| `INSERT` un par un | 7 710 l/s | ×1 | 5,1 min |
| `executemany` (lots) | 22 790 l/s | ×3 | 1,7 min |
| **`COPY FROM STDIN`** | **597 749 l/s** | **×77,5** | **3,9 s** |

Ce n'est pas une optimisation marginale : c'est la différence entre un pipeline
qui tourne en quelques secondes et un qui tourne en heures. La raison est
structurelle : `INSERT` refait pour **chaque** ligne un aller-retour réseau, une
analyse syntaxique, une planification et une transaction ; `COPY` fait le tout
une fois, avec un analyseur spécialisé.

## 3. Décisions et justifications

### 3.1 Tout en `text` (sauf la météo en `jsonb`)

Un chargement ne doit **jamais** échouer sur un format. La preuve est dans les
données : `staging.realisation` contient 60 375 passages dont l'heure théorique
commence par `24`, `25` ou `26`. Si la colonne était typée `TIME`, le `COPY`
aurait échoué sur la première, et ces heures sont **légales** en GTFS.

En `text`, l'ingestion réussit toujours ; la validation devient une étape
explicite (phase 4) qui produit des rejets exploitables. C'est le principe
**« charger d'abord, valider ensuite »**.

Exception assumée : la météo est en `jsonb`. Pour une source JSON, la forme brute
*est* le JSON. Le stocker tel quel ne perd rien, le rend interrogeable, et
surtout : si l'API ajoute un champ demain, le chargement continue sans
modification du schéma.

### 3.2 Tables `UNLOGGED`

Elles n'écrivent pas dans le journal de transactions (WAL) : chargement plus
rapide, pas de saturation du WAL sur 296 Mo d'un coup. Contrepartie : le contenu
est perdu en cas d'arrêt brutal du serveur.

C'est acceptable **uniquement** parce que le staging est intégralement
reconstructible depuis les fichiers sources, eux conservés avec leur empreinte.
Jamais sur `entrepot`.

### 3.3 Transcodage pendant le COPY

L'export d'exploitation est en latin-1. Plutôt que de relire 188 Mo en Python
pour les réencoder, on passe `ENCODING 'LATIN1'` à `COPY` : PostgreSQL transcode
en flux. Preuve : « Beaulieu - Université », « Aéroport », « Abbé Grimault »
ressortent avec leurs accents intacts.

### 3.4 Provenance tracée par empreinte, pas par nom

`id_execution` se remplit tout seul : `COPY` applique le `DEFAULT` des colonnes
absentes de sa liste, et ce défaut lit un paramètre de session
(`staging.execution_courante()`). Aucun `UPDATE` de masse après coup.

`meta.source_fichier` porte l'empreinte SHA-256 de chaque fichier chargé, avec
unicité sur `(code_source, hash_sha256)`. Au second passage, les trois sources
sont reconnues : « contenu déjà ingéré ». C'est le **contenu** qui fait foi, pas
le nom : un fichier renommé reste le même fichier.

### 3.5 L'en-tête est lu dans le fichier, pas codé en dur

`COPY` reçoit la liste des colonnes lue dans la première ligne du fichier. Le
GTFS étant une spécification ouverte, un producteur peut ajouter une colonne
facultative. Un chargeur qui suppose un ordre fixe décalerait silencieusement
toutes les valeurs : un bug bien pire qu'une erreur franche. Une colonne inconnue
lève ici une erreur explicite avec son remède.

## 4. Le runner de migrations

`meta.migration` trace chaque script appliqué par nom et empreinte. Trois cas :
inconnu → appliqué ; connu, même hash → sauté ; connu, hash changé → réappliqué
avec avertissement.

Ce dernier cas rappelle une règle de production : **on ne modifie jamais une
migration déjà passée**, on en ajoute une nouvelle, parce que les autres
environnements ont déjà appliqué l'ancienne. Ici tous les scripts sont
rejouables (`IF NOT EXISTS`, `ON CONFLICT DO NOTHING`), donc réappliquer est sans
risque, mais ce confort de développement n'est pas transposable tel quel.

## 5. Deux bugs rencontrés, et ce qu'ils apprennent

* **`ON COMMIT DROP` en mode autocommit** : la table temporaire du benchmark
  était détruite dès sa création, car `CREATE` validait immédiatement. Leçon :
  `ON COMMIT DROP` suppose une transaction ouverte ; en autocommit il faut gérer
  la suppression explicitement.
* **Idempotence non branchée** : le mécanisme `meta.source_fichier` existait mais
  n'était appelé nulle part : la table restait vide. Écrire un mécanisme ne suffit
  pas ; il faut vérifier qu'il est réellement invoqué dans le flux. C'est
  exactement ce que la requête de contrôle a révélé.

## 6. Fichiers produits

```
sql/ddl/05_staging.sql       tables de staging (UNLOGGED, text + jsonb)
src/mobilite/base.py         connexion, migrations, journal d'exécution
src/mobilite/chargement.py   COPY, orchestration, benchmark
tests/test_temps_gtfs.py     conversion des heures > 24 h
tests/test_config_et_journal.py  validation + masquage des secrets
```

30 tests passent.

**Prochaine étape (phase 4)** : nettoyage, validation et gestion des rejets :
c'est là que les 60 000 anomalies injectées sont détectées et traitées.
