-- =============================================================================
--  30 — Métadonnées d'exécution et table de rejets
-- =============================================================================
--  À écrire AVANT les tables métier : tout le reste du pipeline y fait référence
--  via `id_execution`. Sans ce journal, un pipeline est une boîte noire — on ne
--  peut ni auditer, ni reprendre après incident, ni prouver l'idempotence.
-- =============================================================================


-- -----------------------------------------------------------------------------
--  meta.execution : une ligne par lancement du pipeline
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS meta.execution (
    id_execution    bigint       GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- Nom logique du traitement (permet de faire cohabiter plusieurs pipelines).
    nom_pipeline    text         NOT NULL DEFAULT 'mobilite_star',
    horodate_debut  timestamptz  NOT NULL DEFAULT now(),
    horodate_fin    timestamptz,
    statut          text         NOT NULL DEFAULT 'EN_COURS'
                                 CHECK (statut IN ('EN_COURS','SUCCES','ECHEC','INTERROMPU')),
    -- Paramètres du lancement (période traitée, sources, mode). En JSONB pour
    -- pouvoir les interroger : WHERE parametres->>'date_debut' = '2026-03-01'
    parametres      jsonb        NOT NULL DEFAULT '{}'::jsonb,
    message_erreur  text
);

COMMENT ON TABLE meta.execution IS
    'Journal des lancements du pipeline. Toute ligne insérée en staging, en rejet '
    'ou dans un fait porte l''id_execution qui l''a produite.';


-- -----------------------------------------------------------------------------
--  meta.etape : une ligne par étape à l'intérieur d'une exécution
-- -----------------------------------------------------------------------------
--  C'est cette table qui alimente le rapport automatisé de la phase 7 :
--  volumétries lues / insérées / rejetées, durées, taux de rejet.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS meta.etape (
    id_etape        bigint       GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    id_execution    bigint       NOT NULL REFERENCES meta.execution(id_execution) ON DELETE CASCADE,
    nom_etape       text         NOT NULL,
    -- Phase du pipeline, pour regrouper dans le rapport.
    phase           text         NOT NULL
                                 CHECK (phase IN ('EXTRACTION','CHARGEMENT','VALIDATION',
                                                  'DIMENSION','FAIT','RESTITUTION')),
    horodate_debut  timestamptz  NOT NULL DEFAULT now(),
    horodate_fin    timestamptz,
    lignes_lues     bigint       NOT NULL DEFAULT 0,
    lignes_inserees bigint       NOT NULL DEFAULT 0,
    lignes_rejetees bigint       NOT NULL DEFAULT 0,
    statut          text         NOT NULL DEFAULT 'EN_COURS'
                                 CHECK (statut IN ('EN_COURS','SUCCES','ECHEC')),
    message         text
);

CREATE INDEX IF NOT EXISTS idx_etape_execution ON meta.etape (id_execution);

-- Durée calculée : colonne générée, jamais désynchronisée d'un UPDATE oublié.
ALTER TABLE meta.etape
    ADD COLUMN IF NOT EXISTS duree_ms bigint
    GENERATED ALWAYS AS (
        (EXTRACT(EPOCH FROM (horodate_fin - horodate_debut)) * 1000)::bigint
    ) STORED;


-- -----------------------------------------------------------------------------
--  meta.source_fichier : traçabilité des fichiers ingérés
-- -----------------------------------------------------------------------------
--  Le hash SHA-256 sert à deux choses :
--    1. l'IDEMPOTENCE : si le fichier a déjà été chargé avec succès, on saute
--       l'étape au lieu de créer des doublons ;
--    2. l'AUDIT : « d'où vient cette ligne ? » a toujours une réponse.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS meta.source_fichier (
    id_source_fichier bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    id_execution      bigint      NOT NULL REFERENCES meta.execution(id_execution),
    code_source       text        NOT NULL,   -- 'GTFS', 'METEO', 'EXPLOITATION'
    nom_fichier       text        NOT NULL,
    chemin_local      text        NOT NULL,
    url_origine       text,
    taille_octets     bigint,
    hash_sha256       text        NOT NULL,
    horodate_ingestion timestamptz NOT NULL DEFAULT now(),
    -- Date de publication annoncée par la source (feed_info.txt pour le GTFS).
    version_source    text
);

-- Un même contenu ne doit être ingéré qu'une fois par source.
CREATE UNIQUE INDEX IF NOT EXISTS uq_source_fichier_hash
    ON meta.source_fichier (code_source, hash_sha256);


-- -----------------------------------------------------------------------------
--  rejet.regle : le catalogue des règles de qualité
-- -----------------------------------------------------------------------------
--  Externaliser les règles dans une table (plutôt que de les laisser en dur dans
--  le code) permet d'en changer la sévérité sans redéployer, et de produire un
--  rapport de qualité lisible par un métier.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS rejet.regle (
    code_regle  text  PRIMARY KEY,
    libelle     text  NOT NULL,
    -- ERREUR       : la ligne est écartée du fait.
    -- AVERTISSEMENT: la ligne est conservée, mais rattachée à un membre inconnu
    --                ou corrigée automatiquement. Elle est tracée quand même.
    severite    text  NOT NULL CHECK (severite IN ('ERREUR','AVERTISSEMENT')),
    entite      text  NOT NULL,   -- table/flux concerné
    actif       boolean NOT NULL DEFAULT true
);


-- -----------------------------------------------------------------------------
--  rejet.rejet : les lignes refusées
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS rejet.rejet (
    id_rejet       bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    id_execution   bigint      NOT NULL REFERENCES meta.execution(id_execution),
    code_source    text        NOT NULL,
    nom_fichier    text,
    numero_ligne   bigint,                    -- position dans le fichier source
    cle_naturelle  text,                      -- ce qui identifie la ligne côté métier
    code_regle     text        NOT NULL REFERENCES rejet.regle(code_regle),
    valeur_fautive text,                      -- la valeur exacte qui a déclenché le rejet
    -- La ligne brute complète, pour pouvoir la rejouer après correction.
    charge_utile   jsonb       NOT NULL,
    horodate_rejet timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_rejet_execution ON rejet.rejet (id_execution);
CREATE INDEX IF NOT EXISTS idx_rejet_regle     ON rejet.rejet (code_regle);
-- Index GIN sur le JSONB : permet de retrouver tous les rejets concernant un
-- arrêt donné sans connaître à l'avance la structure de la charge utile.
CREATE INDEX IF NOT EXISTS idx_rejet_charge_utile
    ON rejet.rejet USING gin (charge_utile jsonb_path_ops);

COMMENT ON COLUMN rejet.rejet.charge_utile IS
    'Ligne source intégrale au format clé/valeur. Permet de corriger puis rejouer '
    'un rejet sans retourner au fichier d''origine.';
