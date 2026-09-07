-- =============================================================================
--  TRANSFORM 40 : Dimension météo (dimension « poubelle » / junk)
-- =============================================================================
--  Rappel (phase 1) : dim_meteo ne stocke PAS des mesures continues (12,4 °C)
--  mais des TRANCHES métier (10-15). Raisons :
--    - un axe d'analyse doit avoir peu de valeurs pour être lisible dans un
--      GROUP BY ;
--    - la question métier est « quand il pleut fort », pas « quand il tombe
--      3,2 mm ». La tranche porte le sens.
--
--  Ce n'est pas une dimension historisée : chaque ligne est une COMBINAISON
--  distincte de conditions, insérée une fois (dédup par hash). C'est le propre
--  d'une junk dimension : on y range des attributs qualitatifs de faible
--  cardinalité qui ne méritent pas chacun leur table.
-- =============================================================================


-- -----------------------------------------------------------------------------
--  Discrétisation : mesure continue → tranche
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION staging.tranche_temperature(p_c numeric)
RETURNS text LANGUAGE sql IMMUTABLE PARALLEL SAFE
RETURN CASE
    WHEN p_c IS NULL      THEN 'INCONNU'
    WHEN p_c < 0          THEN 'NEGATIF'
    WHEN p_c < 5          THEN '0_5'
    WHEN p_c < 10         THEN '5_10'
    WHEN p_c < 15         THEN '10_15'
    WHEN p_c < 20         THEN '15_20'
    WHEN p_c < 25         THEN '20_25'
    ELSE                       '25_PLUS'
END;

CREATE OR REPLACE FUNCTION staging.tranche_precipitation(p_mm numeric)
RETURNS text LANGUAGE sql IMMUTABLE PARALLEL SAFE
RETURN CASE
    WHEN p_mm IS NULL     THEN 'INCONNU'
    WHEN p_mm = 0         THEN 'AUCUNE'
    WHEN p_mm < 1.0       THEN 'FAIBLE'
    WHEN p_mm < 4.0       THEN 'MODEREE'
    ELSE                       'FORTE'
END;

-- Vent en km/h, seuils inspirés de l'échelle de Beaufort.
CREATE OR REPLACE FUNCTION staging.tranche_vent(p_kmh numeric)
RETURNS text LANGUAGE sql IMMUTABLE PARALLEL SAFE
RETURN CASE
    WHEN p_kmh IS NULL    THEN 'INCONNU'
    WHEN p_kmh < 12       THEN 'CALME'
    WHEN p_kmh < 30       THEN 'LEGER'
    WHEN p_kmh < 50       THEN 'MODERE'
    WHEN p_kmh < 62       THEN 'FORT'
    ELSE                       'TEMPETE'
END;

-- Code WMO → (libellé, famille). Le code météo standard de l'OMM, tel que
-- renvoyé par Open-Meteo. On regroupe en familles pour l'analyse à gros grain.
CREATE OR REPLACE FUNCTION staging.famille_wmo(p_code integer)
RETURNS text LANGUAGE sql IMMUTABLE PARALLEL SAFE
RETURN CASE
    WHEN p_code IS NULL           THEN 'INCONNU'
    WHEN p_code = 0               THEN 'CLAIR'
    WHEN p_code BETWEEN 1 AND 3   THEN 'NUAGEUX'
    WHEN p_code IN (45, 48)       THEN 'BROUILLARD'
    WHEN p_code BETWEEN 51 AND 67 THEN 'PLUIE'
    WHEN p_code BETWEEN 71 AND 77 THEN 'NEIGE'
    WHEN p_code BETWEEN 80 AND 82 THEN 'PLUIE'
    WHEN p_code BETWEEN 85 AND 86 THEN 'NEIGE'
    WHEN p_code BETWEEN 95 AND 99 THEN 'ORAGE'
    ELSE                               'INCONNU'
END;

CREATE OR REPLACE FUNCTION staging.libelle_wmo(p_code integer)
RETURNS text LANGUAGE sql IMMUTABLE PARALLEL SAFE
RETURN CASE
    WHEN p_code = 0               THEN 'Ciel clair'
    WHEN p_code = 1               THEN 'Principalement clair'
    WHEN p_code = 2               THEN 'Partiellement nuageux'
    WHEN p_code = 3               THEN 'Couvert'
    WHEN p_code IN (45, 48)       THEN 'Brouillard'
    WHEN p_code BETWEEN 51 AND 55 THEN 'Bruine'
    WHEN p_code BETWEEN 56 AND 57 THEN 'Bruine verglaçante'
    WHEN p_code BETWEEN 61 AND 65 THEN 'Pluie'
    WHEN p_code BETWEEN 66 AND 67 THEN 'Pluie verglaçante'
    WHEN p_code BETWEEN 71 AND 77 THEN 'Neige'
    WHEN p_code BETWEEN 80 AND 82 THEN 'Averses de pluie'
    WHEN p_code BETWEEN 85 AND 86 THEN 'Averses de neige'
    WHEN p_code BETWEEN 95 AND 99 THEN 'Orage'
    ELSE                               'Inconnu'
END;


-- =============================================================================
--  Vue : relevés météo dépliés et discrétisés
-- =============================================================================
--  Le staging météo tient dans UN document jsonb (le tableau « releves »). On le
--  déplie avec jsonb_to_recordset, qui projette un tableau d'objets JSON en
--  lignes typées : l'outil idiomatique pour transformer du JSON en relationnel.
-- =============================================================================
DROP VIEW IF EXISTS staging.v_meteo_normalisee CASCADE;
CREATE VIEW staging.v_meteo_normalisee AS
WITH releves AS (
    SELECT rel.*
    FROM staging.meteo_brut m,
         jsonb_to_recordset(m.charge -> 'releves') AS rel(
             horodate          text,
             temperature_c     numeric,
             precipitation_mm  numeric,
             vent_kmh          numeric,
             code_wmo          integer
         )
)
SELECT
    -- L'horodatage météo est en heure locale sans fuseau (« 2026-09-04T08:00 »).
    -- On l'ancre explicitement en Europe/Paris pour l'aligner sur les passages.
    (r.horodate::timestamp AT TIME ZONE 'Europe/Paris')   AS horodate,
    (r.horodate::timestamp)::date                         AS jour,
    EXTRACT(HOUR FROM r.horodate::timestamp)::smallint    AS heure,
    r.code_wmo,
    staging.libelle_wmo(r.code_wmo)                       AS condition_libelle,
    staging.famille_wmo(r.code_wmo)                       AS famille_condition,
    staging.tranche_temperature(r.temperature_c)          AS tranche_temperature,
    staging.tranche_precipitation(r.precipitation_mm)     AS tranche_precipitation,
    staging.tranche_vent(r.vent_kmh)                      AS tranche_vent,
    -- Conditions dégradant l'exploitation : pluie soutenue, neige, orage, ou
    -- vent fort. Attribut précalculé dans la dimension, pour écrire
    -- « WHERE est_conditions_degradees » sans répéter la logique métier.
    (staging.tranche_precipitation(r.precipitation_mm) IN ('MODEREE', 'FORTE')
        OR staging.famille_wmo(r.code_wmo) IN ('NEIGE', 'ORAGE')
        OR staging.tranche_vent(r.vent_kmh) IN ('FORT', 'TEMPETE'))
                                                          AS est_degrade,
    md5(
        staging.famille_wmo(r.code_wmo) || '|' ||
        staging.libelle_wmo(r.code_wmo) || '|' ||
        staging.tranche_temperature(r.temperature_c) || '|' ||
        staging.tranche_precipitation(r.precipitation_mm) || '|' ||
        staging.tranche_vent(r.vent_kmh)
    )                                                     AS hash_attributs
FROM releves r;


-- =============================================================================
--  Procédure : charger dim_meteo + table de correspondance créneau → sk_meteo
-- =============================================================================
CREATE OR REPLACE PROCEDURE staging.charger_dim_meteo(p_id_execution bigint)
LANGUAGE plpgsql
AS $$
DECLARE
    v_combinaisons bigint;
    v_creneaux     bigint;
BEGIN
    -- --- 1. Insérer les combinaisons météo distinctes encore absentes --------
    --  ON CONFLICT (hash_attributs) DO NOTHING : deux créneaux aux conditions
    --  identiques partagent la même ligne de dimension. C'est tout l'intérêt de
    --  la discrétisation : on passe de ~400 relevés à quelques dizaines de
    --  combinaisons distinctes.
    INSERT INTO entrepot.dim_meteo (
        code_condition_wmo, condition_libelle, famille_condition,
        tranche_temperature, tranche_precipitation, tranche_vent,
        est_conditions_degradees, hash_attributs
    )
    SELECT DISTINCT ON (hash_attributs)
        code_wmo, condition_libelle, famille_condition,
        tranche_temperature, tranche_precipitation, tranche_vent,
        est_degrade, hash_attributs
    FROM staging.v_meteo_normalisee
    ON CONFLICT (hash_attributs) DO NOTHING;
    GET DIAGNOSTICS v_combinaisons = ROW_COUNT;

    -- --- 2. Table de correspondance (jour, heure) → sk_meteo -----------------
    --  Le fait (phase 6) devra retrouver la météo du créneau d'un passage. On
    --  précalcule ici la correspondance, en joignant chaque relevé horaire à la
    --  ligne de dimension qui porte son hash. Un index sur (jour, heure) rend la
    --  jointure du fait immédiate.
    DROP TABLE IF EXISTS staging.meteo_creneau;
    CREATE UNLOGGED TABLE staging.meteo_creneau AS
    SELECT DISTINCT
        v.jour,
        v.heure,
        dm.sk_meteo
    FROM staging.v_meteo_normalisee v
    JOIN entrepot.dim_meteo dm ON dm.hash_attributs = v.hash_attributs;

    CREATE UNIQUE INDEX ON staging.meteo_creneau (jour, heure);
    GET DIAGNOSTICS v_creneaux = ROW_COUNT;
    ANALYZE staging.meteo_creneau;

    RAISE NOTICE 'dim_meteo : % combinaison(s) ajoutée(s), % créneau(x) horaires mappés',
        v_combinaisons, v_creneaux;
END;
$$;

COMMENT ON PROCEDURE staging.charger_dim_meteo(bigint) IS
    'Alimente entrepot.dim_meteo (combinaisons distinctes) et construit '
    'staging.meteo_creneau, la table (jour, heure) -> sk_meteo utilisée par le fait.';
