-- =============================================================================
--  20 : Table de faits
-- =============================================================================
--  GRAIN : une ligne = le passage d'un véhicule à un arrêt, pour une course
--          donnée, à une date de service donnée.
--
--  Trois familles de colonnes, et rien d'autre :
--    1. les CLÉS ÉTRANGÈRES vers les dimensions  (les axes d'analyse)
--    2. les DIMENSIONS DÉGÉNÉRÉES                (identifiants sans attributs)
--    3. les MESURES                              (ce qu'on agrège)
--
--  Ce qui n'est PAS ici, volontairement :
--    - aucun libellé (nom d'arrêt, nom de ligne) → ils vivent dans les dimensions ;
--    - aucun ratio (taux de ponctualité) → non additif, se calcule à la volée.
-- =============================================================================

CREATE TABLE IF NOT EXISTS entrepot.fait_passage (

    -- ==== 1. Clés étrangères vers les dimensions =============================
    --  Toutes NOT NULL : c'est le membre inconnu (-1) qui absorbe les cas non
    --  résolus, jamais un NULL. Une FK nulle ferait disparaître silencieusement
    --  des lignes lors d'une jointure interne, et fausserait tous les totaux.
    sk_date            integer  NOT NULL REFERENCES entrepot.dim_date(sk_date),
    sk_creneau         smallint NOT NULL REFERENCES entrepot.dim_creneau(sk_creneau),
    sk_ligne           integer  NOT NULL REFERENCES entrepot.dim_ligne(sk_ligne),
    sk_arret           integer  NOT NULL REFERENCES entrepot.dim_arret(sk_arret),
    sk_meteo           integer  NOT NULL REFERENCES entrepot.dim_meteo(sk_meteo),

    -- ==== 2. Dimensions dégénérées ===========================================
    --  Identifiants métier conservés dans le fait parce qu'ils n'ont aucun
    --  attribut descriptif à porter. Ils servent au dénombrement de courses
    --  distinctes et à la traçabilité vers la source.
    code_source        text     NOT NULL DEFAULT 'STAR',
    id_course_source   text     NOT NULL,   -- trip_id GTFS
    rang_arret         smallint NOT NULL,   -- stop_sequence : position dans la course
    sens               smallint NOT NULL DEFAULT 0,  -- direction_id : 0 aller, 1 retour
    destination        text,                -- trip_headsign, utile en restitution

    -- ==== 3. Contexte temporel exact =========================================
    --  timestamptz et non timestamp : le passage à l'heure d'été/hiver crée une
    --  nuit de 23 h et une de 25 h. Un timestamp naïf y produit soit un doublon,
    --  soit une heure inexistante. C'est le piège n°8 du document de conception.
    horodate_theorique timestamptz NOT NULL,
    horodate_reelle    timestamptz,          -- NULL si la course est supprimée

    -- ==== 4. Mesures =========================================================
    --  retard_secondes : SEMI-ADDITIVE. AVG / MAX / percentiles ont du sens ;
    --  SUM n'en a aucun. Signé : négatif = en avance.
    retard_secondes    integer,

    --  Compteur toujours à 1. Permet d'écrire SUM(nb_passages) partout, y
    --  compris sur des agrégats intermédiaires, là où COUNT(*) deviendrait faux.
    nb_passages        smallint NOT NULL DEFAULT 1 CHECK (nb_passages = 1),

    --  Booléens de mesure : additifs une fois castés en entier.
    --  Ponctualité : convention française des AOM, volontairement asymétrique
    --  (une minute d'avance maximum, trois minutes de retard maximum) car un
    --  véhicule en avance fait rater le bus à l'usager.
    est_ponctuel       boolean  NOT NULL DEFAULT false,
    est_supprime       boolean  NOT NULL DEFAULT false,
    --  Trace que la ligne a été conservée malgré une anomalie non bloquante
    --  (ex. arrêt orphelin rattaché au membre inconnu). Permet de mesurer la
    --  part de faits « dégradés » dans le rapport de qualité.
    est_degrade        boolean  NOT NULL DEFAULT false,

    -- ==== 5. Traçabilité =====================================================
    id_execution       bigint   NOT NULL,
    horodate_chargement timestamptz NOT NULL DEFAULT now(),

    -- ==== Contraintes ========================================================
    --  La clé primaire DOIT contenir la clé de partitionnement (sk_date) :
    --  PostgreSQL ne peut pas garantir l'unicité entre partitions autrement.
    --  L'ordre des colonnes compte : sk_date en tête sert aussi l'élagage.
    PRIMARY KEY (sk_date, code_source, id_course_source, rang_arret),

    --  Cohérence interne : soit la course est supprimée et il n'y a pas
    --  d'horodatage réel, soit elle a eu lieu et le retard est renseigné.
    CONSTRAINT ck_fait_passage_suppression CHECK (
        (est_supprime AND horodate_reelle IS NULL AND retard_secondes IS NULL)
        OR (NOT est_supprime AND horodate_reelle IS NOT NULL AND retard_secondes IS NOT NULL)
    ),

    --  Garde-fou métier : au-delà de ces bornes, c'est une erreur de capteur,
    --  pas un retard. Les valeurs hors bornes doivent partir en rejet AVANT
    --  d'arriver ici : cette contrainte est le filet de sécurité qui garantit
    --  qu'aucune valeur aberrante ne pollue les moyennes.
    CONSTRAINT ck_fait_passage_retard CHECK (
        retard_secondes IS NULL OR retard_secondes BETWEEN -1800 AND 7200
    ),

    CONSTRAINT ck_fait_passage_rang CHECK (rang_arret >= 0)

) PARTITION BY RANGE (sk_date);

COMMENT ON TABLE entrepot.fait_passage IS
    'Grain : un passage de véhicule à un arrêt, pour une course, à une date de service. '
    'Partitionnée par mois sur sk_date.';


-- =============================================================================
--  Partition par défaut
-- =============================================================================
--  Filet de sécurité : sans elle, une insertion dont la date n'a pas encore de
--  partition ÉCHOUE. Avec elle, la ligne atterrit ici et reste détectable :
--      SELECT count(*) FROM entrepot.fait_passage_defaut;
--  Un compteur non nul est un signal d'alerte pour le rapport de qualité.
--
--  Contrepartie à connaître : attacher une nouvelle partition oblige PostgreSQL
--  à scanner la partition par défaut pour vérifier qu'elle ne contient aucune
--  ligne qui devrait y aller. On la garde donc vide.
-- =============================================================================
CREATE TABLE IF NOT EXISTS entrepot.fait_passage_defaut
    PARTITION OF entrepot.fait_passage DEFAULT;


-- =============================================================================
--  Création automatisée des partitions mensuelles
-- =============================================================================
--  Appelée par le pipeline avant chaque chargement, pour la période traitée.
--  Idempotente : deux appels successifs ne produisent aucune erreur.
-- =============================================================================
CREATE OR REPLACE FUNCTION entrepot.creer_partition_mois(p_mois date)
RETURNS text
LANGUAGE plpgsql
AS $$
DECLARE
    v_debut_mois  date := date_trunc('month', p_mois)::date;
    v_fin_mois    date := (date_trunc('month', p_mois) + interval '1 month')::date;
    v_nom         text := format('fait_passage_%s', to_char(v_debut_mois, 'YYYYMM'));
    -- Les bornes sont des sk_date (AAAAMMJJ), pas des dates : c'est le type de
    -- la colonne de partitionnement. FROM inclusif, TO exclusif.
    v_borne_min   integer := to_char(v_debut_mois, 'YYYYMMDD')::integer;
    v_borne_max   integer := to_char(v_fin_mois,   'YYYYMMDD')::integer;
BEGIN
    IF to_regclass('entrepot.' || quote_ident(v_nom)) IS NOT NULL THEN
        RETURN format('Partition %s déjà présente.', v_nom);
    END IF;

    EXECUTE format(
        'CREATE TABLE entrepot.%I PARTITION OF entrepot.fait_passage
             FOR VALUES FROM (%s) TO (%s)',
        v_nom, v_borne_min, v_borne_max
    );

    RETURN format('Partition %s créée sur [%s, %s).', v_nom, v_borne_min, v_borne_max);
END;
$$;

COMMENT ON FUNCTION entrepot.creer_partition_mois(date) IS
    'Crée si besoin la partition mensuelle de fait_passage couvrant le mois du paramètre.';


-- =============================================================================
--  Index
-- =============================================================================
--  Sur une table partitionnée, un index déclaré au niveau du parent est
--  automatiquement propagé à chaque partition, présente et future. On ne les
--  gère donc jamais partition par partition.
--
--  Choix des index : dans un schéma en étoile, PostgreSQL exécute les requêtes
--  par jointure de hachage sur les dimensions filtrées. Un index par clé
--  étrangère suffit ; inutile de multiplier les index composites tant qu'aucun
--  plan d'exécution réel n'en a démontré le besoin.
-- =============================================================================
CREATE INDEX IF NOT EXISTS idx_fait_passage_ligne   ON entrepot.fait_passage (sk_ligne);
CREATE INDEX IF NOT EXISTS idx_fait_passage_arret   ON entrepot.fait_passage (sk_arret);
CREATE INDEX IF NOT EXISTS idx_fait_passage_meteo   ON entrepot.fait_passage (sk_meteo);
CREATE INDEX IF NOT EXISTS idx_fait_passage_creneau ON entrepot.fait_passage (sk_creneau);

-- Index partiel : les passages en retard sont minoritaires mais ce sont eux
-- qu'on interroge. L'index ne couvre que ces lignes, donc il est petit.
CREATE INDEX IF NOT EXISTS idx_fait_passage_non_ponctuel
    ON entrepot.fait_passage (sk_date, sk_ligne)
    WHERE NOT est_ponctuel;
