-- =============================================================================
--  10 : Dimensions de l'entrepôt
-- =============================================================================
--  Convention du projet :
--    sk_*         clé de substitution (surrogate key), entière, sans sens métier
--    id_*_source  clé naturelle telle que fournie par la source
--    Chaque dimension possède un MEMBRE INCONNU à sk = -1.
-- =============================================================================


-- =============================================================================
--  dim_date : dimension calendaire
-- =============================================================================
--  Sa clé est un entier AAAAMMJJ plutôt qu'une séquence. C'est la seule
--  exception à la règle « clé sans signification », et elle est délibérée :
--    - on lit une table de faits sans jointure (sk_date = 20260315 se déchiffre) ;
--    - le partitionnement par plage devient trivial et ordonné ;
--    - la valeur ne changera jamais, contrairement à un identifiant de source.
--
--  Cette table est PEUPLÉE PAR AVANCE (cf. sql/seed/10_peupler_dim_date.sql),
--  pas au fil de l'eau : un entrepôt doit pouvoir répondre « 0 passage le
--  25 décembre » plutôt que de n'avoir aucune ligne pour cette date.
-- =============================================================================
CREATE TABLE IF NOT EXISTS entrepot.dim_date (
    sk_date                integer   PRIMARY KEY,          -- AAAAMMJJ
    jour                   date      NOT NULL,
    annee                  smallint  NOT NULL,
    trimestre              smallint  NOT NULL,
    mois                   smallint  NOT NULL,
    libelle_mois           text      NOT NULL,
    numero_semaine_iso     smallint  NOT NULL,
    jour_du_mois           smallint  NOT NULL,
    -- ISO : 1 = lundi … 7 = dimanche. On fixe la convention explicitement
    -- parce que PostgreSQL propose aussi `dow` (0 = dimanche), source classique
    -- de bugs d'un jour de décalage.
    jour_semaine_iso       smallint  NOT NULL,
    libelle_jour_semaine   text      NOT NULL,
    est_weekend            boolean   NOT NULL,
    est_ferie              boolean   NOT NULL DEFAULT false,
    libelle_ferie          text,
    est_vacances_scolaires boolean   NOT NULL DEFAULT false,
    -- Type de jour au sens exploitation transport : le niveau d'offre en dépend.
    type_jour_reseau       text      NOT NULL DEFAULT 'OUVRABLE'
                                     CHECK (type_jour_reseau IN
                                            ('OUVRABLE','SAMEDI','DIMANCHE_FERIE','VACANCES'))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_dim_date_jour ON entrepot.dim_date (jour);

-- Membre inconnu. `ON CONFLICT DO NOTHING` rend le script rejouable sans erreur :
-- c'est l'idempotence appliquée au DDL.
INSERT INTO entrepot.dim_date (sk_date, jour, annee, trimestre, mois, libelle_mois,
                               numero_semaine_iso, jour_du_mois, jour_semaine_iso,
                               libelle_jour_semaine, est_weekend, type_jour_reseau)
VALUES (-1, '1900-01-01', 1900, 1, 1, 'Inconnu', 1, 1, 1, 'Inconnu', false, 'OUVRABLE')
ON CONFLICT (sk_date) DO NOTHING;


-- =============================================================================
--  dim_creneau : dimension horaire (granularité : l'heure)
-- =============================================================================
--  Pourquoi une dimension séparée de dim_date, et pas une colonne `heure` dans
--  le fait ?
--    1. Sinon dim_date aurait 24 × 365 lignes par an au lieu de 365, et perdrait
--       sa lisibilité.
--    2. Les attributs horaires (heure de pointe, période d'exploitation) sont
--       INDÉPENDANTS de la date : 8 h est une heure de pointe le 3 mars comme le
--       12 juillet. Croiser les deux dimensions dupliquerait l'information.
--
--  C'est la règle : deux axes indépendants → deux dimensions.
-- =============================================================================
CREATE TABLE IF NOT EXISTS entrepot.dim_creneau (
    sk_creneau        smallint PRIMARY KEY,   -- 0 à 23, ou -1
    heure             smallint NOT NULL,
    libelle_heure     text     NOT NULL,      -- '08h - 09h'
    -- Périodes d'exploitation d'un réseau urbain.
    libelle_creneau   text     NOT NULL
                               CHECK (libelle_creneau IN
                                      ('NUIT','POINTE_MATIN','CREUSE_MATIN',
                                       'MIDI','CREUSE_APRES_MIDI','POINTE_SOIR','SOIREE','INCONNU')),
    est_heure_pointe  boolean  NOT NULL DEFAULT false,
    -- Service de nuit : l'offre et les critères de ponctualité y diffèrent.
    est_service_nuit  boolean  NOT NULL DEFAULT false
);

INSERT INTO entrepot.dim_creneau (sk_creneau, heure, libelle_heure, libelle_creneau)
VALUES (-1, -1, 'Inconnu', 'INCONNU')
ON CONFLICT (sk_creneau) DO NOTHING;


-- =============================================================================
--  dim_ligne : les lignes de transport (SCD type 2)
-- =============================================================================
--  Source : routes.txt du GTFS.
--
--  Historisée en SCD2 : une ligne peut être renommée, changer de mode
--  (bus → BHNS), ou de couleur d'identité visuelle entre deux versions du GTFS.
--  On veut que le rapport de janvier continue d'afficher le nom de janvier.
-- =============================================================================
CREATE TABLE IF NOT EXISTS entrepot.dim_ligne (
    sk_ligne            integer GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,

    -- --- Clé naturelle -------------------------------------------------------
    id_ligne_source     text    NOT NULL,          -- route_id GTFS
    code_source         text    NOT NULL DEFAULT 'STAR',  -- prépare le multi-réseau

    -- --- Attributs descriptifs (ceux qui déclenchent une nouvelle version) ----
    code_ligne          text    NOT NULL,          -- route_short_name : 'C1', '12'
    nom_ligne           text    NOT NULL,          -- route_long_name
    mode_transport      text    NOT NULL           -- dérivé de route_type
                                CHECK (mode_transport IN
                                       ('METRO','BUS','BHNS','TRAMWAY','FUNICULAIRE','AUTRE','INCONNU')),
    exploitant          text,                      -- agency_name
    couleur_ligne       text,                      -- route_color, '#RRGGBB'
    -- Les lignes structurantes (métro, C1-C7) ont des exigences de ponctualité
    -- différentes : attribut utile pour segmenter l'analyse.
    est_ligne_structurante boolean NOT NULL DEFAULT false,

    -- --- Attributs SCD2 ------------------------------------------------------
    date_debut_validite date    NOT NULL DEFAULT CURRENT_DATE,
    -- '9999-12-31' plutôt que NULL : rend les prédicats BETWEEN simples et
    -- indexables. Un NULL obligerait à écrire `(fin IS NULL OR fin >= x)`.
    date_fin_validite   date    NOT NULL DEFAULT DATE '9999-12-31',
    est_courant         boolean NOT NULL DEFAULT true,
    -- Empreinte des attributs descriptifs. Comparer un md5 est plus rapide et
    -- moins bavard que 7 comparaisons `IS DISTINCT FROM`.
    hash_attributs      text    NOT NULL,

    id_execution_creation bigint,
    horodate_creation   timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT ck_dim_ligne_periode CHECK (date_fin_validite >= date_debut_validite)
);

-- Une seule version COURANTE par clé naturelle. L'index partiel est la façon
-- idiomatique d'exprimer cette contrainte en PostgreSQL : il n'indexe que les
-- lignes courantes, donc il reste petit même avec beaucoup d'historique.
CREATE UNIQUE INDEX IF NOT EXISTS uq_dim_ligne_courante
    ON entrepot.dim_ligne (code_source, id_ligne_source)
    WHERE est_courant;

-- Deux versions ne doivent pas se chevaucher dans le temps.
CREATE UNIQUE INDEX IF NOT EXISTS uq_dim_ligne_version
    ON entrepot.dim_ligne (code_source, id_ligne_source, date_debut_validite);

CREATE INDEX IF NOT EXISTS idx_dim_ligne_naturelle
    ON entrepot.dim_ligne (id_ligne_source, date_debut_validite, date_fin_validite);

INSERT INTO entrepot.dim_ligne (sk_ligne, id_ligne_source, code_source, code_ligne,
                                nom_ligne, mode_transport, hash_attributs,
                                date_debut_validite, date_fin_validite, est_courant)
VALUES (-1, '@INCONNU', 'SYSTEME', 'N/D', 'Ligne inconnue', 'INCONNU',
        md5('inconnu'), DATE '1900-01-01', DATE '9999-12-31', true)
ON CONFLICT (sk_ligne) DO NOTHING;


-- =============================================================================
--  dim_arret : les points d'arrêt (SCD type 2)
-- =============================================================================
--  Source : stops.txt du GTFS.
--  Les arrêts sont renommés, déplacés de quelques mètres, ou rendus accessibles
--  PMR au fil des travaux : c'est exactement le cas d'école du SCD2.
-- =============================================================================
CREATE TABLE IF NOT EXISTS entrepot.dim_arret (
    sk_arret            integer GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,

    id_arret_source     text    NOT NULL,          -- stop_id GTFS
    code_source         text    NOT NULL DEFAULT 'STAR',

    nom_arret           text    NOT NULL,          -- stop_name
    commune             text,
    -- numeric et non float : une coordonnée est une donnée de référence, on ne
    -- veut aucune dérive de représentation binaire entre deux chargements
    -- (sinon le hash SCD2 changerait sans qu'aucun attribut n'ait bougé).
    latitude            numeric(9,6),
    longitude           numeric(9,6),
    accessible_pmr      boolean,
    -- Un « quai » appartient à une « station » : hiérarchie GTFS parent_station.
    -- On la garde à plat dans la dimension (principe de l'étoile) plutôt que
    -- d'ajouter une table dim_station qui en ferait un flocon.
    id_station_parente  text,
    nom_station_parente text,
    type_arret          text    NOT NULL DEFAULT 'ARRET'
                                CHECK (type_arret IN ('ARRET','STATION','ACCES','INCONNU')),

    date_debut_validite date    NOT NULL DEFAULT CURRENT_DATE,
    date_fin_validite   date    NOT NULL DEFAULT DATE '9999-12-31',
    est_courant         boolean NOT NULL DEFAULT true,
    hash_attributs      text    NOT NULL,

    id_execution_creation bigint,
    horodate_creation   timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT ck_dim_arret_periode CHECK (date_fin_validite >= date_debut_validite),
    -- Garde-fou géographique : la Bretagne, généreusement bornée. Une coordonnée
    -- hors de cette boîte trahit une inversion lat/lon ou un séparateur décimal
    -- mal interprété : deux erreurs très fréquentes à l'import.
    CONSTRAINT ck_dim_arret_coordonnees CHECK (
        (latitude IS NULL AND longitude IS NULL)
        OR (latitude BETWEEN 46.5 AND 49.5 AND longitude BETWEEN -5.5 AND -0.5)
        OR sk_arret = -1
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_dim_arret_courant
    ON entrepot.dim_arret (code_source, id_arret_source)
    WHERE est_courant;

CREATE UNIQUE INDEX IF NOT EXISTS uq_dim_arret_version
    ON entrepot.dim_arret (code_source, id_arret_source, date_debut_validite);

CREATE INDEX IF NOT EXISTS idx_dim_arret_naturelle
    ON entrepot.dim_arret (id_arret_source, date_debut_validite, date_fin_validite);

INSERT INTO entrepot.dim_arret (sk_arret, id_arret_source, code_source, nom_arret,
                                type_arret, hash_attributs,
                                date_debut_validite, date_fin_validite, est_courant)
VALUES (-1, '@INCONNU', 'SYSTEME', 'Arrêt inconnu', 'INCONNU', md5('inconnu'),
        DATE '1900-01-01', DATE '9999-12-31', true)
ON CONFLICT (sk_arret) DO NOTHING;


-- =============================================================================
--  dim_meteo : conditions météorologiques (dimension « poubelle » / junk)
-- =============================================================================
--  Source : API Open-Meteo (archive horaire, station de Rennes).
--
--  Ce n'est PAS une dimension historisée : c'est une dimension de RÉFÉRENCE
--  dont chaque ligne est une COMBINAISON distincte de conditions.
--  On ne stocke pas « 12,4 °C et 3,2 mm » (ce seraient des mesures, et il y en
--  aurait des millions de combinaisons) mais des TRANCHES.
--
--  Pourquoi discrétiser ?
--    - un axe d'analyse doit avoir peu de valeurs (~200 lignes ici) pour être
--      utilisable dans un GROUP BY lisible ;
--    - la question métier est « quand il pleut fort », pas « quand il tombe
--      3,2 mm ». La tranche porte le sens métier.
--
--  Les valeurs continues brutes restent disponibles pour vérification, mais
--  l'axe d'analyse est la tranche.
-- =============================================================================
CREATE TABLE IF NOT EXISTS entrepot.dim_meteo (
    sk_meteo              integer GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,

    -- Code WMO renvoyé par Open-Meteo (0 = ciel clair, 61 = pluie faible, …)
    code_condition_wmo    smallint,
    condition_libelle     text    NOT NULL,     -- 'Pluie faible', 'Brouillard', …
    -- Regroupement grossier, pour les analyses à gros grain.
    famille_condition     text    NOT NULL
                                  CHECK (famille_condition IN
                                         ('CLAIR','NUAGEUX','BROUILLARD','PLUIE',
                                          'NEIGE','ORAGE','INCONNU')),

    tranche_temperature   text    NOT NULL
                                  CHECK (tranche_temperature IN
                                         ('NEGATIF','0_5','5_10','10_15','15_20',
                                          '20_25','25_PLUS','INCONNU')),
    tranche_precipitation text    NOT NULL
                                  CHECK (tranche_precipitation IN
                                         ('AUCUNE','FAIBLE','MODEREE','FORTE','INCONNU')),
    tranche_vent          text    NOT NULL
                                  CHECK (tranche_vent IN
                                         ('CALME','LEGER','MODERE','FORT','TEMPETE','INCONNU')),
    -- Indicateur synthétique : conditions susceptibles de dégrader l'exploitation.
    -- C'est un attribut PRÉCALCULÉ dans la dimension, pas une mesure : il permet
    -- d'écrire `WHERE est_conditions_degradees` sans réécrire la logique métier
    -- dans chaque requête. C'est tout l'intérêt d'une dimension riche.
    est_conditions_degradees boolean NOT NULL DEFAULT false,

    hash_attributs        text    NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_dim_meteo_hash
    ON entrepot.dim_meteo (hash_attributs);

INSERT INTO entrepot.dim_meteo (sk_meteo, condition_libelle, famille_condition,
                                tranche_temperature, tranche_precipitation,
                                tranche_vent, hash_attributs)
VALUES (-1, 'Météo inconnue', 'INCONNU', 'INCONNU', 'INCONNU', 'INCONNU', md5('inconnu'))
ON CONFLICT (sk_meteo) DO NOTHING;


-- =============================================================================
--  Recalage des séquences
-- =============================================================================
--  On a inséré les membres inconnus avec un sk EXPLICITE (-1), ce que permet
--  `GENERATED BY DEFAULT AS IDENTITY` (contrairement à `GENERATED ALWAYS`).
--  Les séquences ne le savent pas ; on s'assure qu'elles repartent à 1.
-- =============================================================================
SELECT setval(pg_get_serial_sequence('entrepot.dim_ligne', 'sk_ligne'),
              GREATEST((SELECT COALESCE(MAX(sk_ligne), 0) FROM entrepot.dim_ligne), 1));
SELECT setval(pg_get_serial_sequence('entrepot.dim_arret', 'sk_arret'),
              GREATEST((SELECT COALESCE(MAX(sk_arret), 0) FROM entrepot.dim_arret), 1));
SELECT setval(pg_get_serial_sequence('entrepot.dim_meteo', 'sk_meteo'),
              GREATEST((SELECT COALESCE(MAX(sk_meteo), 0) FROM entrepot.dim_meteo), 1));
