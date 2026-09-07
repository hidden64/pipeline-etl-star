-- =============================================================================
--  TRANSFORM 30 : Dimensions historisées (SCD type 2) : lignes et arrêts
-- =============================================================================
--  Rappel du principe (phase 1) : quand un attribut suivi change (un arrêt est
--  renommé, une ligne change de mode), on ne L'ÉCRASE PAS. On FERME la version
--  courante (date_fin_validite + est_courant=false) et on OUVRE une nouvelle
--  version courante. L'historique reste fidèle : un rapport de janvier continue
--  d'afficher le nom de janvier.
--
--  Détection du changement : un HASH md5 des attributs suivis. Comparer deux
--  hash est plus rapide et plus sûr que comparer douze colonnes une à une avec
--  des IS DISTINCT FROM (qui, en plus, se piègent sur les NULL).
--
--  Le patron SCD2 tient en DEUX ordres ensemblistes, dans cet ordre :
--    1. FERMER les versions courantes dont le hash a changé ;
--    2. INSÉRER une version courante pour toute clé qui n'en a plus (clés
--       nouvelles, et clés qu'on vient de fermer à l'étape 1).
--  Les clés inchangées ne sont pas touchées : leur version courante existe
--  déjà avec le bon hash, donc l'étape 2 les ignore.
-- =============================================================================


-- -----------------------------------------------------------------------------
--  Mapping route_type (GTFS) → mode de transport métier
-- -----------------------------------------------------------------------------
--  Les codes route_type sont numériques dans le GTFS. On les traduit en une
--  énumération lisible, qui est ce qu'un analyste veut voir dans un rapport.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION staging.mode_transport_gtfs(p_route_type text)
RETURNS text
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
RETURN CASE btrim(p_route_type)
    WHEN '0' THEN 'TRAMWAY'
    WHEN '1' THEN 'METRO'
    WHEN '2' THEN 'AUTRE'        -- train régional : hors périmètre STAR
    WHEN '3' THEN 'BUS'
    WHEN '5' THEN 'FUNICULAIRE'
    WHEN '6' THEN 'AUTRE'        -- téléphérique
    WHEN '7' THEN 'FUNICULAIRE'
    ELSE 'INCONNU'
END;


-- =============================================================================
--  Procédure : charger dim_ligne en SCD2
-- =============================================================================
CREATE OR REPLACE PROCEDURE staging.charger_dim_ligne(
    p_id_execution bigint,
    p_date_effet   date
)
LANGUAGE plpgsql
AS $$
DECLARE
    v_fermees  bigint;
    v_creees   bigint;
BEGIN
    -- --- Source normalisée : une ligne par route, avec son hash d'attributs ---
    --  On lit routes.txt (staging) et on y joint agency.txt pour l'exploitant.
    --  Le hash concatène les attributs SUIVIS, chacun protégé par coalesce('')
    --  pour qu'un NULL ne rende pas tout le hash NULL. Le séparateur « | » évite
    --  les collisions du type ('ab','c') vs ('a','bc').
    DROP TABLE IF EXISTS tmp_ligne_source;
    CREATE TEMP TABLE tmp_ligne_source ON COMMIT DROP AS
    SELECT
        r.route_id                                       AS id_ligne_source,
        'STAR'::text                                     AS code_source,
        btrim(r.route_short_name)                        AS code_ligne,
        btrim(r.route_long_name)                         AS nom_ligne,
        staging.mode_transport_gtfs(r.route_type)        AS mode_transport,
        btrim(a.agency_name)                             AS exploitant,
        NULLIF(btrim(r.route_color), '')                 AS couleur_ligne,
        -- Structurante : métro, ou ligne Chronostar (C1..C7), qui portent des
        -- exigences de ponctualité renforcées.
        -- coalesce(route_desc,'') est INDISPENSABLE : sans lui, un route_desc
        -- NULL rend « NULL ILIKE ... » = NULL, et « false OR NULL OR false »
        -- = NULL (logique ternaire SQL). La colonne cible étant NOT NULL,
        -- l'insertion échouerait. Un test sur une route sans desc l'a révélé.
        (staging.mode_transport_gtfs(r.route_type) = 'METRO'
            OR coalesce(r.route_desc, '') ILIKE '%chronostar%'
            OR btrim(r.route_short_name) ~ '^C[0-9]')    AS est_structurante,
        md5(
            coalesce(btrim(r.route_short_name), '') || '|' ||
            coalesce(btrim(r.route_long_name), '')  || '|' ||
            coalesce(staging.mode_transport_gtfs(r.route_type), '') || '|' ||
            coalesce(btrim(a.agency_name), '')      || '|' ||
            coalesce(NULLIF(btrim(r.route_color), ''), '')
        )                                                AS hash_attributs
    FROM staging.gtfs_routes r
    LEFT JOIN staging.gtfs_agency a ON a.agency_id = r.agency_id;

    -- --- Étape 1 : fermer les versions courantes dont le hash a changé --------
    UPDATE entrepot.dim_ligne d
       SET date_fin_validite = p_date_effet - 1,
           est_courant       = false
    FROM tmp_ligne_source s
    WHERE d.est_courant
      AND d.code_source = s.code_source
      AND d.id_ligne_source = s.id_ligne_source
      AND d.hash_attributs <> s.hash_attributs;   -- <> : le changement réel
    GET DIAGNOSTICS v_fermees = ROW_COUNT;

    -- --- Étape 2 : insérer une version courante pour toute clé sans courante ---
    INSERT INTO entrepot.dim_ligne (
        id_ligne_source, code_source, code_ligne, nom_ligne, mode_transport,
        exploitant, couleur_ligne, est_ligne_structurante,
        date_debut_validite, date_fin_validite, est_courant,
        hash_attributs, id_execution_creation
    )
    SELECT
        s.id_ligne_source, s.code_source, s.code_ligne, s.nom_ligne,
        s.mode_transport, s.exploitant, s.couleur_ligne, s.est_structurante,
        p_date_effet, DATE '9999-12-31', true,
        s.hash_attributs, p_id_execution
    FROM tmp_ligne_source s
    WHERE NOT EXISTS (
        SELECT 1 FROM entrepot.dim_ligne d
         WHERE d.est_courant
           AND d.code_source = s.code_source
           AND d.id_ligne_source = s.id_ligne_source
    );
    GET DIAGNOSTICS v_creees = ROW_COUNT;

    RAISE NOTICE 'dim_ligne : % version(s) fermée(s), % version(s) créée(s)',
        v_fermees, v_creees;
END;
$$;


-- =============================================================================
--  Procédure : charger dim_arret en SCD2 (même patron)
-- =============================================================================
CREATE OR REPLACE PROCEDURE staging.charger_dim_arret(
    p_id_execution bigint,
    p_date_effet   date
)
LANGUAGE plpgsql
AS $$
DECLARE
    v_fermees  bigint;
    v_creees   bigint;
BEGIN
    DROP TABLE IF EXISTS tmp_arret_source;
    CREATE TEMP TABLE tmp_arret_source ON COMMIT DROP AS
    SELECT
        s.stop_id                                        AS id_arret_source,
        'STAR'::text                                     AS code_source,
        btrim(s.stop_name)                               AS nom_arret,
        NULLIF(btrim(s.stop_desc), '')                   AS commune,
        -- Coordonnées : NULL si illisibles, plutôt que de laisser passer un
        -- texte qui ferait échouer le cast numeric. La contrainte CHECK de
        -- dim_arret (bornes Bretagne) rattrapera une éventuelle inversion.
        staging.parse_decimal_fr(s.stop_lat)             AS latitude,
        staging.parse_decimal_fr(s.stop_lon)             AS longitude,
        CASE btrim(s.wheelchair_boarding)
            WHEN '1' THEN true WHEN '2' THEN false ELSE NULL
        END                                              AS accessible_pmr,
        NULLIF(btrim(s.parent_station), '')              AS id_station_parente,
        CASE btrim(s.location_type)
            WHEN '1' THEN 'STATION' WHEN '2' THEN 'ACCES' ELSE 'ARRET'
        END                                              AS type_arret,
        md5(
            coalesce(btrim(s.stop_name), '')      || '|' ||
            coalesce(NULLIF(btrim(s.stop_desc),''), '') || '|' ||
            coalesce(btrim(s.stop_lat), '')       || '|' ||
            coalesce(btrim(s.stop_lon), '')       || '|' ||
            coalesce(btrim(s.wheelchair_boarding),'') || '|' ||
            coalesce(btrim(s.location_type), '')
        )                                                AS hash_attributs
    FROM staging.gtfs_stops s;

    UPDATE entrepot.dim_arret d
       SET date_fin_validite = p_date_effet - 1,
           est_courant       = false
    FROM tmp_arret_source s
    WHERE d.est_courant
      AND d.code_source = s.code_source
      AND d.id_arret_source = s.id_arret_source
      AND d.hash_attributs <> s.hash_attributs;
    GET DIAGNOSTICS v_fermees = ROW_COUNT;

    INSERT INTO entrepot.dim_arret (
        id_arret_source, code_source, nom_arret, commune, latitude, longitude,
        accessible_pmr, id_station_parente, type_arret,
        date_debut_validite, date_fin_validite, est_courant,
        hash_attributs, id_execution_creation
    )
    SELECT
        s.id_arret_source, s.code_source, s.nom_arret, s.commune,
        s.latitude, s.longitude, s.accessible_pmr, s.id_station_parente,
        s.type_arret, p_date_effet, DATE '9999-12-31', true,
        s.hash_attributs, p_id_execution
    FROM tmp_arret_source s
    WHERE NOT EXISTS (
        SELECT 1 FROM entrepot.dim_arret d
         WHERE d.est_courant
           AND d.code_source = s.code_source
           AND d.id_arret_source = s.id_arret_source
    );
    GET DIAGNOSTICS v_creees = ROW_COUNT;

    RAISE NOTICE 'dim_arret : % version(s) fermée(s), % version(s) créée(s)',
        v_fermees, v_creees;
END;
$$;

COMMENT ON PROCEDURE staging.charger_dim_ligne(bigint, date) IS
    'Alimente entrepot.dim_ligne en SCD2 depuis staging.gtfs_routes. Idempotente : '
    'sans changement d''attribut, un second appel ne crée aucune version.';
