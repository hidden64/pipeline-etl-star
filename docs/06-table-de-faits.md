# Phase 6 — Construction de la table de faits

Tout converge ici : les 2 342 951 passages propres deviennent des faits, chacun
relié à ses cinq dimensions.

## 1. Résultat mesuré

```
faits chargés .......... 2 342 951   (= exactement les lignes propres)
  dont supprimés ....... 19 188
  dont dégradés ........ 1 345 414   (rattachés à un membre inconnu)
    · arrêt inconnu .... 4 664        (les arrêts orphelins)
    · météo inconnue ... 1 343 368    (jours hors horizon de prévision)

partition 202609 ....... 1 629 037
partition 202610 .......   713 914
partition par défaut ... 0            ✓ aucun fait égaré
retards hors bornes .... 0            ✓ la validation a tout filtré

ponctualité globale .... 90,7 %
retard moyen ........... 67,5 s
```

Le nombre de faits égale **exactement** le nombre de lignes propres : aucune
perte, aucune duplication. C'est le premier contrôle qu'on vérifie.

## 2. La résolution des cinq clés

Pour chaque passage, cinq jointures traduisent les valeurs métier en clés de
substitution :

| Clé | Résolue par | Cas non résolu |
|---|---|---|
| `sk_date` | jointure sur la date exacte | membre inconnu `-1` |
| `sk_creneau` | heure locale du passage (0-23) | membre inconnu `-1` |
| `sk_ligne` | **SCD2** : code de ligne + validité | membre inconnu `-1` |
| `sk_arret` | **SCD2** : identifiant + validité | membre inconnu `-1` (orphelins) |
| `sk_meteo` | table `(jour, heure) → sk_meteo` | membre inconnu `-1` (hors horizon) |

### La résolution SCD2 — le point technique de la phase

Une dimension historisée a plusieurs versions d'une même clé. Le fait doit
pointer vers la version **valide à la date du passage**, pas vers la version
courante :

```sql
LEFT JOIN entrepot.dim_arret da
       ON da.id_arret_source = r.id_arret
      AND r.date_service BETWEEN da.date_debut_validite AND da.date_fin_validite
--        ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
--        c'est CETTE ligne qui sélectionne la bonne version historique
```

Sans le `BETWEEN`, un passage de septembre pourrait se voir attribuer un nom
d'arrêt d'octobre. Avec lui, chaque passage récupère l'état du réseau tel qu'il
était ce jour-là. C'est l'aboutissement du SCD2 construit en phase 5.

## 3. Le membre inconnu à grande échelle

57 % des faits sont « dégradés », presque tous à cause de `sk_meteo = -1`. Ce
n'est pas un défaut : ce sont les ~22 jours situés au-delà de l'horizon de
prévision météo (décision documentée en phase 2). Aucune météo n'existe pour ces
dates, alors ces faits sont rattachés au membre inconnu.

Ce choix montre toute sa valeur au moment de l'analyse :

```sql
-- Analyse par ligne : on garde TOUS les faits, l'axe météo n'intervient pas.
SELECT code_ligne, avg(retard_secondes) ...   -- 2,3 M faits, total juste

-- Analyse par météo : on écarte l'inconnu, car il n'a pas de sens sur cet axe.
... WHERE sk_meteo <> -1                       -- ~990 k faits météo-connus
```

Si on avait **rejeté** ces 1,34 M passages faute de météo, les totaux par ligne
et par créneau seraient faux de 57 %. En les **conservant** sous membre inconnu,
chaque axe d'analyse reste exact : on filtre l'inconnu seulement là où il n'a pas
de sens. **C'est exactement le raisonnement du membre inconnu, à l'échelle du
million de lignes.**

## 4. Ce que l'entrepôt sait maintenant répondre

Les mesures sont conformes au réel encodé, et surtout **cohérentes entre elles** :

**Pluie → retard** (la question fondatrice du projet)

| Pluie | Passages | Retard moyen | Ponctualité |
|---|---:|---:|---:|
| Aucune | 824 241 | 66,0 s | 91,0 % |
| Faible | 158 256 | 83,9 s | 87,0 % |
| Modérée | 8 237 | 90,7 s | 88,2 % |

**Heure de pointe → retard**

| Créneau | Retard moyen |
|---|---:|
| Nuit | 15,6 s |
| Pointe matin | **106,7 s** |
| Pointe soir | **103,8 s** |
| Heures creuses | ~33 s |

L'heure de pointe multiplie le retard par trois. Ces réponses ne sortent pas
d'un script isolé : elles sont le produit de **cinq jointures dimensionnelles**
sur une table de faits partitionnée — l'architecture en étoile à l'œuvre.

## 5. Le partitionnement, vérifié

Les faits se répartissent d'eux-mêmes entre `fait_passage_202609` (1,63 M) et
`fait_passage_202610` (0,71 M) selon `sk_date`. La partition par défaut est
**vide** : aucun fait n'a échappé au routage. Rejouer un seul mois ne touchera
que sa partition.

## 6. Contrôles d'intégrité après chargement

Un pipeline sérieux vérifie ce qu'il a produit. Trois contrôles, tous à zéro :

- aucun fait dans la partition par défaut (toutes les dates ont une partition) ;
- aucune clé étrangère nulle (le membre inconnu absorbe tout) ;
- aucun retard hors bornes (la validation de la phase 4 a tout filtré en amont).

## 7. Performance et piste d'amélioration

L'étape de faits a pris **467 s**. Le coût vient des deux jointures SCD2 par
plage (`BETWEEN date_debut AND date_fin`), difficiles à indexer aussi bien qu'une
égalité. C'est acceptable pour ce volume et ce cadre, et honnêtement mesuré.

Piste connue si le volume grossissait : pré-résoudre les clés SCD2 dans une table
intermédiaire indexée avant l'insertion, ou remplacer la plage par une jointure
d'égalité sur une clé de version pré-calculée. On ne l'implémente pas ici — le
gain ne se justifie qu'au-delà de plusieurs dizaines de millions de faits, et la
règle reste : n'optimiser qu'après avoir mesuré un besoin réel.

## 8. Fichiers produits

```
sql/transform/50_fait_passage.sql      résolution des 5 clés + chargement partitionné
sql/transform/60_reinitialisation.sql  remise à zéro propre (rejeu complet)
src/mobilite/faits.py                  orchestration + contrôles d'intégrité
src/mobilite/pipeline.py               orchestrateur bout-en-bout
src/mobilite/__main__.py               python -m mobilite
```

**Prochaine étape (phase 7)** : vues de restitution (schéma `restitution`) et
rapport automatisé — HTML généré à partir de `meta.*` et des agrégats, pour
présenter le bilan d'exécution et la qualité des données d'un coup d'œil.
