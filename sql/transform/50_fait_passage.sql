-- =============================================================================
--  TRANSFORM 50 — Construction de la table de faits
-- =============================================================================
--  Tout converge ici. Pour chaque passage propre (staging.realisation_valide),
--  on résout les CINQ clés de substitution vers les dimensions, puis on insère
--  dans fait_passage (partitionnée par mois).
--
--  LE point technique de cette phase : la résolution SCD2. Une dimension
--  historisée contient plusieurs versions d'une même clé. Il faut joindre le
--  fait à la version VALIDE À LA DATE DU PASSAGE, pas à la version courante.
--  C'est ce qui donne son sens à tout le SCD2 : un passage du 10 janvier doit
--  pointer vers le nom d'arrêt du 10 janvier.
--
--    JOIN dim_arret d
--      ON d.id_arret_source = r.id_arret
--     AND r.date_service BETWEEN d.date_debut_validite AND d.date_fin_validite
--                                ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
--                                sélectionne la bonne version historique
--
--  Les cas non résolus (arrêt orphelin, créneau sans météo) tombent sur le
--  MEMBRE INCONNU (sk = -1) via coalesce, jamais sur NULL. Le fait est conservé,
--  les totaux restent justes, l'anomalie reste mesurable (est_degrade).
-- =============================================================================

CREATE OR REPLACE PROCEDURE staging.charger_fait_passage(p_id_execution bigint)
LANGUAGE plpgsql
AS $$
DECLARE
    v_mois   date;
    v_insere bigint;
    v_degrade bigint;
BEGIN
    SET LOCAL work_mem = '256MB';

    -- --- 1. Créer les partitions mensuelles couvrant les données -------------
    --  On balaie les mois réellement présents dans les données propres et on
    --  crée la partition de chacun. Idempotent (la fonction ignore l'existant).
    FOR v_mois IN
        SELECT DISTINCT date_trunc('month', date_service)::date
        FROM staging.realisation_valide
        WHERE date_service IS NOT NULL
        ORDER BY 1
    LOOP
        PERFORM entrepot.creer_partition_mois(v_mois);
    END LOOP;

    -- --- 2. Purge idempotente des faits de cette période ---------------------
    --  On retire les faits des dates qu'on s'apprête à recharger, pour que
    --  rejouer le pipeline ne crée pas de doublons. Grâce au partitionnement,
    --  PostgreSQL n'attaque que les partitions concernées (élagage).
    DELETE FROM entrepot.fait_passage
    WHERE sk_date IN (
        SELECT DISTINCT to_char(date_service, 'YYYYMMDD')::int
        FROM staging.realisation_valide WHERE date_service IS NOT NULL
    );

    -- --- 3. Insertion des faits, clés résolues -------------------------------
    INSERT INTO entrepot.fait_passage (
        sk_date, sk_creneau, sk_ligne, sk_arret, sk_meteo,
        code_source, id_course_source, rang_arret, sens, destination,
        horodate_theorique, horodate_reelle,
        retard_secondes, nb_passages, est_ponctuel, est_supprime, est_degrade,
        id_execution
    )
    SELECT
        -- ---- sk_date : la clé calendaire EST l'entier AAAAMMJJ --------------
        --  On joint quand même dim_date pour garantir l'intégrité (une date hors
        --  du calendrier peuplé tomberait sur le membre inconnu au lieu de créer
        --  une clé étrangère orpheline).
        coalesce(dd.sk_date, -1),

        -- ---- sk_creneau : l'heure locale du passage (0..23) ----------------
        --  EXTRACT(HOUR) sur un timestamptz renvoie l'heure dans le fuseau de
        --  session (Europe/Paris). dim_creneau a sk = heure, d'où la jointure
        --  directe ; coalesce protège le cas (théorique) d'une heure absente.
        coalesce(dc.sk_creneau, -1),

        -- ---- sk_ligne : résolution SCD2 par CODE de ligne + validité --------
        --  Les réalisations désignent la ligne par son code court ('C1'), pas
        --  par route_id. On joint donc sur code_ligne, en filtrant sur la
        --  version valide à la date. Orpheline → membre inconnu.
        coalesce(dl.sk_ligne, -1),

        -- ---- sk_arret : résolution SCD2 par identifiant d'arrêt + validité --
        coalesce(da.sk_arret, -1),

        -- ---- sk_meteo : créneau (jour, heure) → météo ----------------------
        --  meteo_creneau ne couvre que les créneaux pour lesquels une météo
        --  existe. Au-delà de l'horizon de prévision, pas de ligne → membre
        --  inconnu. C'est la matérialisation du choix de la phase 2.
        coalesce(mc.sk_meteo, -1),

        -- ---- Dimensions dégénérées ----------------------------------------
        'STAR',
        r.id_course,
        r.rang_arret,
        coalesce(staging.parse_entier(t.direction_id), 0)::smallint,
        NULLIF(btrim(t.trip_headsign), ''),

        -- ---- Contexte temporel --------------------------------------------
        r.horodate_theorique,
        r.horodate_reelle,

        -- ---- Mesures -------------------------------------------------------
        r.retard_sec,
        1,  -- nb_passages : toujours 1, permet SUM() partout
        -- Ponctualité : convention AOM, asymétrique (1 min d'avance, 3 de
        -- retard). Un passage supprimé n'est jamais ponctuel.
        (NOT r.est_supprime AND r.retard_sec BETWEEN -60 AND 180),
        r.est_supprime,
        -- Dégradé : le fait a été conservé malgré un rattachement au membre
        -- inconnu (arrêt orphelin ou météo manquante). Sert au rapport qualité.
        (da.sk_arret IS NULL OR mc.sk_meteo IS NULL),

        p_id_execution
    FROM staging.realisation_valide r

    -- dim_date : jointure sur la date exacte (clé naturelle = jour).
    LEFT JOIN entrepot.dim_date dd
           ON dd.jour = r.date_service

    -- dim_creneau : heure locale du passage.
    LEFT JOIN entrepot.dim_creneau dc
           ON dc.sk_creneau = EXTRACT(HOUR FROM r.horodate_theorique)::smallint

    -- dim_ligne : version SCD2 valide à la date du passage.
    LEFT JOIN entrepot.dim_ligne dl
           ON dl.code_source = 'STAR'
          AND dl.code_ligne = r.code_ligne
          AND r.date_service BETWEEN dl.date_debut_validite AND dl.date_fin_validite

    -- dim_arret : version SCD2 valide à la date du passage.
    LEFT JOIN entrepot.dim_arret da
           ON da.code_source = 'STAR'
          AND da.id_arret_source = r.id_arret
          AND r.date_service BETWEEN da.date_debut_validite AND da.date_fin_validite

    -- Météo du créneau (jour + heure).
    LEFT JOIN staging.meteo_creneau mc
           ON mc.jour = r.date_service
          AND mc.heure = EXTRACT(HOUR FROM r.horodate_theorique)::smallint

    -- trips : pour le sens et la destination (dimensions dégénérées enrichies).
    LEFT JOIN staging.gtfs_trips t
           ON t.trip_id = r.id_course;

    GET DIAGNOSTICS v_insere = ROW_COUNT;

    -- --- 4. Statistiques pour le planificateur -------------------------------
    --  Après un chargement massif, les statistiques des partitions sont
    --  périmées. ANALYZE les rafraîchit pour que les requêtes de restitution
    --  choisissent de bons plans.
    ANALYZE entrepot.fait_passage;

    SELECT count(*) INTO v_degrade FROM entrepot.fait_passage
     WHERE id_execution = p_id_execution AND est_degrade;

    RAISE NOTICE 'fait_passage : % faits insérés (% dégradés / membre inconnu)',
        v_insere, v_degrade;
END;
$$;

COMMENT ON PROCEDURE staging.charger_fait_passage(bigint) IS
    'Résout les 5 clés de substitution (dont 2 en SCD2 par validité temporelle) '
    'et charge fait_passage. Idempotente par purge des dates rechargées.';
