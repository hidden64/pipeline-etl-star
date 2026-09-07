-- =============================================================================
--  05 — Zone de staging (zone brute)
-- =============================================================================
--  DEUX PARTIS PRIS, tous deux délibérés :
--
--  1. TOUT EN `text`
--     Un chargement ne doit JAMAIS échouer sur un problème de format. Si on
--     déclarait `arrival_time TIME`, le GTFS ferait exploser le COPY sur ses
--     40 131 valeurs `>= 24:00:00` — et on perdrait le lot entier pour une
--     donnée pourtant parfaitement légale.
--     En `text`, l'ingestion réussit toujours. La validation devient une étape
--     explicite qui produit des rejets exploitables au lieu d'une erreur psql.
--     C'est le principe « charger d'abord, valider ensuite » (ELT).
--
--     EXCEPTION ASSUMÉE : la météo est chargée en `jsonb`. Pour une source
--     JSON, la forme brute EST le JSON — le stocker en jsonb ne perd rien et
--     rend la donnée interrogeable. La règle du `text` vise les formats plats
--     délimités, où le typage casse le chargement.
--
--  2. TABLES `UNLOGGED`
--     Elles n'écrivent pas dans le journal de transactions (WAL). Gain mesuré
--     sur ce volume : chargement environ deux fois plus rapide, et pas de
--     saturation du WAL sur 188 Mo d'un coup.
--     Contrepartie : le contenu est PERDU en cas d'arrêt brutal du serveur, et
--     les tables ne sont pas répliquées. C'est acceptable ici, et seulement
--     ici : le staging est intégralement reconstructible depuis les fichiers
--     sources, qui eux sont conservés avec leur empreinte SHA-256.
--     Ne jamais faire cela sur `entrepot`.
-- =============================================================================


-- -----------------------------------------------------------------------------
--  Valeur par défaut de l'identifiant d'exécution
-- -----------------------------------------------------------------------------
--  COPY ne sait pas calculer de colonne. Or on veut que chaque ligne chargée
--  porte l'exécution qui l'a produite, sans repasser derrière avec un UPDATE
--  sur 2,3 millions de lignes.
--
--  Astuce : COPY applique les DEFAULT des colonnes absentes de sa liste. On
--  fait donc lire au DEFAULT un paramètre de session, positionné juste avant
--  le chargement par `SET mobilite.id_execution = '...'`.
--  Le second argument `true` de current_setting signifie « ne pas lever
--  d'erreur si le paramètre est absent » — sinon un chargement hors pipeline
--  échouerait.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION staging.execution_courante()
RETURNS bigint
LANGUAGE sql
STABLE
AS $$
    SELECT COALESCE(NULLIF(current_setting('mobilite.id_execution', true), ''), '-1')::bigint;
$$;

COMMENT ON FUNCTION staging.execution_courante() IS
    'Identifiant de l''exécution en cours, lu dans la session. Sert de DEFAULT '
    'aux tables de staging pour que COPY trace la provenance sans UPDATE.';


-- =============================================================================
--  GTFS — un fichier, une table, colonnes à l'identique de la source
-- =============================================================================
--  On conserve les NOMS DE COLONNES DE LA SOURCE (route_id, et non id_ligne).
--  Renommer en zone brute romprait la traçabilité : quand on compare la table
--  au fichier d'origine pour comprendre une anomalie, on veut lire la même
--  chose des deux côtés. Le renommage métier a lieu en phase de transformation.
-- =============================================================================

DROP TABLE IF EXISTS staging.gtfs_agency;
CREATE UNLOGGED TABLE staging.gtfs_agency (
    numero_ligne     bigint GENERATED ALWAYS AS IDENTITY,
    id_execution     bigint NOT NULL DEFAULT staging.execution_courante(),
    agency_id        text, agency_name      text, agency_url      text,
    agency_timezone  text, agency_lang      text, agency_phone    text,
    agency_fare_url  text, agency_email     text
);

DROP TABLE IF EXISTS staging.gtfs_routes;
CREATE UNLOGGED TABLE staging.gtfs_routes (
    numero_ligne     bigint GENERATED ALWAYS AS IDENTITY,
    id_execution     bigint NOT NULL DEFAULT staging.execution_courante(),
    route_id         text, agency_id        text, route_short_name text,
    route_long_name  text, route_desc       text, route_type       text,
    route_url        text, route_color      text, route_text_color text,
    route_sort_order text
);

DROP TABLE IF EXISTS staging.gtfs_stops;
CREATE UNLOGGED TABLE staging.gtfs_stops (
    numero_ligne     bigint GENERATED ALWAYS AS IDENTITY,
    id_execution     bigint NOT NULL DEFAULT staging.execution_courante(),
    stop_id          text, stop_code        text, stop_name        text,
    stop_desc        text, stop_lat         text, stop_lon         text,
    zone_id          text, stop_url         text, location_type    text,
    parent_station   text, wheelchair_boarding text, stop_timezone text
);

DROP TABLE IF EXISTS staging.gtfs_trips;
CREATE UNLOGGED TABLE staging.gtfs_trips (
    numero_ligne     bigint GENERATED ALWAYS AS IDENTITY,
    id_execution     bigint NOT NULL DEFAULT staging.execution_courante(),
    route_id         text, service_id       text, trip_id          text,
    trip_headsign    text, trip_short_name  text, direction_id     text,
    block_id         text, shape_id         text, wheelchair_accessible text,
    bikes_allowed    text
);

DROP TABLE IF EXISTS staging.gtfs_stop_times;
CREATE UNLOGGED TABLE staging.gtfs_stop_times (
    numero_ligne     bigint GENERATED ALWAYS AS IDENTITY,
    id_execution     bigint NOT NULL DEFAULT staging.execution_courante(),
    trip_id          text, arrival_time     text, departure_time   text,
    stop_id          text, stop_sequence    text, stop_headsign    text,
    pickup_type      text, drop_off_type    text, shape_dist_traveled text,
    timepoint        text
);

DROP TABLE IF EXISTS staging.gtfs_calendar;
CREATE UNLOGGED TABLE staging.gtfs_calendar (
    numero_ligne     bigint GENERATED ALWAYS AS IDENTITY,
    id_execution     bigint NOT NULL DEFAULT staging.execution_courante(),
    service_id       text, monday   text, tuesday  text, wednesday text,
    thursday         text, friday   text, saturday text, sunday    text,
    start_date       text, end_date text
);

DROP TABLE IF EXISTS staging.gtfs_calendar_dates;
CREATE UNLOGGED TABLE staging.gtfs_calendar_dates (
    numero_ligne     bigint GENERATED ALWAYS AS IDENTITY,
    id_execution     bigint NOT NULL DEFAULT staging.execution_courante(),
    service_id       text,
    -- « date » est un nom de type SQL. En staging on garde le nom de la source,
    -- quitte à le mettre entre guillemets partout. La fidélité prime.
    "date"           text,
    exception_type   text
);

DROP TABLE IF EXISTS staging.gtfs_feed_info;
CREATE UNLOGGED TABLE staging.gtfs_feed_info (
    numero_ligne     bigint GENERATED ALWAYS AS IDENTITY,
    id_execution     bigint NOT NULL DEFAULT staging.execution_courante(),
    feed_publisher_name text, feed_publisher_url text, feed_lang text,
    feed_start_date     text, feed_end_date      text, feed_version text,
    feed_contact_email  text, feed_contact_url   text
);


-- =============================================================================
--  Météo — chargée en jsonb (voir l'exception documentée en tête de fichier)
-- =============================================================================
DROP TABLE IF EXISTS staging.meteo_brut;
CREATE UNLOGGED TABLE staging.meteo_brut (
    id_execution  bigint NOT NULL DEFAULT staging.execution_courante(),
    nom_fichier   text   NOT NULL,
    -- Le document complet, tel que reçu de l'API. Les relevés seront dépliés en
    -- phase 4 avec jsonb_to_recordset : la structure reste auditable jusqu'au
    -- bout, et un champ ajouté par l'API n'oblige pas à modifier le staging.
    charge        jsonb  NOT NULL
);


-- =============================================================================
--  Export d'exploitation — la source « sale »
-- =============================================================================
--  Colonnes en minuscules et sans accent : c'est la seule normalisation faite
--  à ce stade, et elle est purement syntaxique (PostgreSQL replie les
--  identifiants non quotés en minuscules). Aucune valeur n'est touchée.
-- =============================================================================
DROP TABLE IF EXISTS staging.realisation;
CREATE UNLOGGED TABLE staging.realisation (
    numero_ligne      bigint GENERATED ALWAYS AS IDENTITY,
    id_execution      bigint NOT NULL DEFAULT staging.execution_courante(),
    date_exploitation text,   -- « 15/09/2026 » : format français
    ligne             text,   -- « ␣␣C1␣ » : espaces parasites possibles
    course            text,
    sequence          text,
    code_arret        text,   -- peut désigner un arrêt inexistant
    libelle_arret     text,   -- accents transcodés depuis latin-1
    h_theorique       text,   -- « 25:14:00 » : heure GTFS > 24 h possible
    h_reelle          text,   -- vide si la course est supprimée
    etat_course       text,   -- « REALISE » / « realise » / « Realise »
    retard_sec        text,   -- « 99999 » possible : capteur défaillant
    vitesse_moy_kmh   text    -- « 18,4 » : décimale à virgule
);

COMMENT ON TABLE staging.realisation IS
    'Export d''exploitation brut. Aucune valeur n''est normalisée : tout est '
    'conservé tel quel pour que la validation de la phase 4 puisse tracer '
    'chaque rejet jusqu''à sa ligne d''origine.';


-- =============================================================================
--  Pas d'index sur le staging — volontairement
-- =============================================================================
--  Un index ralentit chaque insertion et n'a aucune utilité ici : les tables
--  sont lues intégralement une seule fois par la phase de transformation, ce
--  qui donne toujours un parcours séquentiel. Les index se justifient dans
--  `entrepot`, où les données sont lues des milliers de fois.
-- =============================================================================
