# Phase 8 — Savoir raconter le projet en entretien

Un projet qu'on ne sait pas présenter ne compte pas. Voici comment le raconter,
des questions probables aux réponses, et le vocabulaire à maîtriser.

---

## Le pitch en 30 secondes

> « J'ai construit un pipeline ETL qui charge trois sources hétérogènes — les
> horaires GTFS du réseau de bus de Rennes, un flux de passages réalisés, et la
> météo — vers un entrepôt décisionnel en schéma en étoile sur PostgreSQL. Il
> nettoie, dédoublonne, gère les rejets, historise les référentiels en SCD2, et
> produit un rapport automatisé. Sur 2,3 millions de passages, il mesure par
> exemple que la pluie fait passer le retard moyen de 66 à 91 secondes. »

Ce pitch contient les mots-clés qu'un recruteur écoute : *ETL, sources
hétérogènes, schéma en étoile, SCD2, rejets, rapport automatisé*, et un
**résultat chiffré**.

---

## Le vocabulaire à maîtriser (on peut te le demander)

| Terme | Réponse courte |
|---|---|
| **Grain** | Ce que représente une ligne de la table de faits. Ici : un passage à un arrêt. Première décision, la plus structurante. |
| **Schéma en étoile** | Une table de faits centrale entourée de dimensions. Une jointure par axe, jamais en cascade. Optimisé pour la lecture. |
| **Clé de substitution** | Clé artificielle entière (`sk_ligne`), indépendante de la source. Résiste aux renumérotations et permet le SCD2. |
| **SCD2** | Dimension à évolution lente : on ferme l'ancienne version et on en ouvre une nouvelle au lieu d'écraser. Garde l'historique fidèle. |
| **Membre inconnu** | Ligne `sk = -1` de chaque dimension. Un fait douteux y est rattaché au lieu d'être jeté : les totaux restent justes. |
| **Additivité** | Une mesure additive se somme partout ; un ratio jamais. On stocke numérateur et dénominateur, on calcule le ratio à la lecture. |
| **Dimension dégénérée** | Un identifiant conservé dans le fait sans dimension propre (le numéro de course). |
| **Junk dimension** | Dimension regroupant des attributs qualitatifs de faible cardinalité (les tranches météo). |
| **ELT vs ETL** | On charge d'abord le brut (staging en `text`), on transforme ensuite. Rend le pipeline rejouable et le chargement increvable. |
| **Idempotence** | Rejouer le pipeline donne le même résultat, sans doublon. Assurée par empreintes SHA-256 et purges ciblées. |

---

## Questions probables, et comment y répondre

### « Pourquoi un schéma en étoile et pas une base normalisée ? »

Une base applicative (3NF) optimise l'écriture et évite la redondance. Un
entrepôt optimise la **lecture** : on écrit une fois par nuit, on lit des
millions de fois. L'étoile assume la redondance (le nom de ligne répété) pour
qu'une question métier se traduise en **une jointure par axe**, lisible, au lieu
de dix jointures en cascade.

### « Comment tu gères une donnée qui change, comme un arrêt renommé ? »

En **SCD2**. Je ne l'écrase pas : je ferme l'ancienne version (date de fin,
`est_courant = false`) et j'insère la nouvelle. Comme ça, un rapport de janvier
affiche encore le nom de janvier. Je détecte le changement avec un **hash md5**
des attributs suivis, plus rapide et plus sûr que comparer chaque colonne.

### « Que fais-tu d'une ligne invalide ? »

Ça dépend de si elle est **récupérable**. Un retard de capteur à 99999 secondes
est irrécupérable : je le **rejette** (sévérité ERREUR), en gardant la ligne
brute et le motif dans une table de rejets pour pouvoir la corriger et la
rejouer. Un arrêt introuvable est récupérable : je **conserve** le passage en le
rattachant au membre inconnu (AVERTISSEMENT), pour ne pas fausser les totaux.
J'ai aussi un **seuil d'alerte** : si plus de 5 % des lignes sont rejetées, le
pipeline s'arrête — c'est le signe d'une source cassée, pas d'un nettoyage.

### « Ton pipeline est lent ? Comment tu l'optimises ? »

Je **mesure avant d'optimiser**. Exemple vécu : mon étape de validation prenait
285 secondes. J'ai d'abord cru que c'étaient les blocs `EXCEPTION` des fonctions
SQL ; je les ai réécrits… et c'est devenu **pire**. Alors j'ai profilé : le vrai
coupable était la **réévaluation des fonctions de parsing** causée par l'inlining
des CTE par PostgreSQL — 14 millions d'appels au lieu de 2,3 millions. Corrigé
avec `WITH … AS MATERIALIZED`, on est repassé à ~110 secondes. La leçon : on ne
devine pas la performance, on la mesure.

### « Pourquoi `COPY` et pas des `INSERT` ? »

Parce que `INSERT` refait pour chaque ligne un aller-retour réseau, une analyse
et une transaction. `COPY` fait tout en un ordre. Je l'ai **benchmarké** : 77
fois plus rapide sur mes données. Sur 2,3 millions de lignes, c'est la différence
entre 4 secondes et 5 minutes.

### « Comment tu assures que rejouer le pipeline ne crée pas de doublons ? »

Trois mécanismes. Le staging est vidé avant chaque `COPY`. Chaque fichier ingéré
est tracé par empreinte SHA-256 : un même contenu n'est pas retraité. Et la table
de faits est purgée par date avant rechargement, ce que le partitionnement rend
instantané.

### « Et le changement d'heure ? »

Je stocke tous les horodatages en `timestamptz`, pas en timestamp naïf. La nuit
du passage à l'heure d'hiver a 25 heures ; un timestamp naïf y produirait un
doublon de clé ou une heure inexistante. C'est un piège que beaucoup oublient.

---

## Ce qui distingue ce projet (à mettre en avant)

1. **Des données réelles et locales.** Le vrai réseau de Rennes, pas un jeu de
   données d'exercice. On peut en parler concrètement, et c'est pertinent pour un
   poste à Rennes.
2. **Le découplage producteur/consommateur.** Un simulateur et un collecteur
   temps réel écrivent le même format ; le pipeline ne les distingue pas. On
   démontre tout aujourd'hui, on branche le réel sans changer l'ETL.
3. **La qualité traitée sérieusement.** Neuf types d'anomalies réelles injectées
   et gérées, une table de rejets auditable, un seuil d'alerte.
4. **L'honnêteté sur les limites.** L'horizon météo, la période imposée par le
   calendrier GTFS, le coût de l'étape de faits : tout est documenté, rien n'est
   caché. Un recruteur préfère un candidat qui connaît les limites de son travail.
5. **Les bugs racontés.** Les docs montrent les vrais bugs rencontrés (logique
   ternaire SQL, faille XSS, réévaluation de CTE) et comment les tests les ont
   attrapés. Ça montre une vraie démarche d'ingénieur, pas une démo lissée.

---

## Les chiffres à retenir par cœur

```
2,3 millions       de faits chargés
5 dimensions       date, créneau, ligne (SCD2), arrêt (SCD2), météo (junk)
77×                accélération de COPY vs INSERT
285 s → 110 s      optimisation de la validation (mesurée, pas devinée)
66 → 91 s          retard moyen : beau temps → pluie modérée
90,7 %             ponctualité globale
70                 tests
```

---

## Pour aller plus loin (si on te demande « et ensuite ? »)

- Laisser tourner le **collecteur GTFS-RT** plusieurs semaines pour un historique
  100 % réel, et comparer au simulateur.
- Ajouter un second réseau (BreizhGo) pour exercer le multi-source et les clés de
  substitution face aux collisions d'identifiants.
- Pré-résoudre les clés SCD2 pour accélérer l'étape de faits au-delà de quelques
  dizaines de millions de lignes.
- Exposer les vues de restitution dans un outil de BI (Metabase, Superset) pour
  des tableaux de bord interactifs.
