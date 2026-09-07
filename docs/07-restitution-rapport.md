# Phase 7 — Restitution et rapport automatisé

C'est le dernier maillon technique : rendre le travail exploitable, par un outil
de BI comme par un humain.

## 1. Deux livrables

| Livrable | Pour qui | Forme |
|---|---|---|
| Vues `restitution.*` | outil de BI, analyste SQL | contrat SQL stable |
| Rapport HTML | humain, archive, comparaison inter-runs | fichier autonome |

## 2. Les vues de restitution : un contrat, pas un accès direct

Le schéma `restitution` est la **seule** porte d'entrée pour l'analyse. On
n'expose jamais `entrepot` directement. Bénéfice : les vues isolent les
utilisateurs des détails d'implémentation (partitionnement, membres inconnus,
colonnes techniques). On peut réorganiser l'entrepôt sans casser les tableaux de
bord, tant que les vues gardent la même forme.

Sept vues couvrent les axes du projet :

```
v_kpi_globaux           indicateurs de synthèse (une ligne)
v_ponctualite_meteo     l'effet de la pluie — la question fondatrice
v_ponctualite_creneau   par heure, heures de pointe repérées
v_ponctualite_ligne     par ligne
v_ponctualite_jour      série temporelle, avec type de jour
v_qualite_rejets        rejets par règle et par exécution
v_journal_executions    durées et statuts par étape
```

### La règle d'or : jamais un ratio stocké, jamais AVG d'un ratio

Toutes les vues recalculent les taux par `SUM(numérateur) / SUM(dénominateur)` :

```sql
100.0 * sum(est_ponctuel::int)
      / nullif(sum(nb_passages) FILTER (WHERE NOT est_supprime), 0)
```

Le `FILTER (WHERE NOT est_supprime)` exclut les courses supprimées **des deux
côtés** du ratio — sinon le dénominateur serait faux. Et l'on n'écrit **jamais**
`avg(taux)` : une moyenne de moyennes ne pondère pas par l'effectif, elle est
fausse. C'est l'additivité de la phase 1, appliquée jusqu'à la dernière requête.

Au-delà de la moyenne, `v_kpi_globaux` expose **médiane et P90** du retard :
`percentile_cont`. Sur des retards, la moyenne masque les extrêmes ; le P90 dit
« 10 % des passages dépassent tant », ce qui parle bien plus à un exploitant.

## 3. Le rapport HTML

À chaque exécution, le pipeline produit un rapport HTML **autonome** (aucune
dépendance, tout le style est embarqué). Il présente d'un coup d'œil : bilan de
l'exécution, déroulé des étapes, qualité des données, et les indicateurs métier
(effet météo, heures de pointe, ponctualité par ligne).

### Séparation logique / présentation

Le rendu passe par **Jinja2** : le module Python interroge la base (la logique),
le gabarit `.html.j2` met en forme (la présentation). Le même jeu de données
pourrait être rendu en Markdown ou en courriel sans toucher aux requêtes. Le
Python ne fait aucun calcul : tout vient des vues, il ne fait que transporter.

### Généré après clôture, et tolérant à l'échec

Le rapport se génère **après** la clôture de l'exécution, pour que sa durée
totale y figure. Et un échec de génération de rapport **n'invalide pas** un
pipeline par ailleurs réussi : on l'attrape et on avertit. Le rapport est un
compte rendu, pas une étape critique du chargement.

## 4. Trois bugs, encore attrapés par les tests

Cette phase illustre une dernière fois la valeur des tests — et de la
vérification systématique du résultat rendu, pas seulement du code.

1. **Double pourcentage.** La requête météo du rapport faisait
   `100 * sum(passages * taux) / sum(passages)`, alors que `taux` est déjà un
   pourcentage. Résultat affiché : **9102 %**. Repéré en relisant le HTML rendu,
   corrigé en retirant le `100 *`. Leçon : vérifier la **sortie**, pas seulement
   que le code s'exécute.

2. **Séparateur de milliers non déterministe.** Le filtre contenait, par
   inadvertance, une espace fine insécable (` `) au lieu d'une espace
   normale. Un test comparant `2 342 951` a forcé à rendre le filtre explicite
   et stable d'une machine à l'autre (regex sur les non-chiffres).

3. **Faille XSS — la plus importante.** `select_autoescape(["html"])` regarde
   l'extension **finale** du gabarit. Or celle-ci est `.j2`, pas `.html` :
   l'échappement n'était donc **pas activé**. Un nom d'arrêt contenant
   `<script>` serait passé tel quel dans la page. Un test injectant une charge
   hostile l'a révélé ; correctif : `autoescape=True`, inconditionnel, puisque
   ce module ne produit que du HTML. **On n'accorde jamais confiance à une donnée
   externe pour construire du HTML.**

## 5. Fichiers produits

```
sql/restitution/00_vues.sql           les 7 vues d'analyse
src/mobilite/rapport.py               collecte + rendu Jinja2
src/mobilite/gabarits/rapport.html.j2 le gabarit HTML autonome
tests/test_rapport.py                 filtres + rendu + anti-XSS
```

70 tests passent. Le rapport est intégré au pipeline : `python -m mobilite` le
génère automatiquement à la fin de chaque exécution réussie.

**Prochaine étape (phase 8)** : le README et l'argumentaire d'entretien — savoir
raconter le projet, ses choix, et ce que chaque partie démontre.
