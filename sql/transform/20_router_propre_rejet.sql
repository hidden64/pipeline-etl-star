-- =============================================================================
--  TRANSFORM 20 — Routage : propre d'un côté, rejets de l'autre
-- =============================================================================
--  Procédure appelée par le pipeline. Elle prend le staging normalisé et le
--  répartit en trois destinations :
--
--    staging.realisation_valide   les lignes propres, typées, dédoublonnées
--    rejet.rejet                  les lignes ERREUR, avec motif et charge utile
--    (traces A1xx)                les avertissements, conservés mais signalés
--
--  Ordre des opérations, et il n'est pas négociable :
--    1. matérialiser la vue normalisée (on va la lire plusieurs fois) ;
--    2. router les ERREURS vers rejet.rejet (une ligne = un rejet, règle
--       prioritaire) ;
--    3. dédoublonner ce qui reste, en traçant les doublons ;
--    4. rattacher les arrêts orphelins au membre inconnu (avertissement).
--
--  Idempotence : la procédure vide ses propres cibles au début. La relancer sur
--  le même staging produit exactement le même résultat.
-- =============================================================================

CREATE OR REPLACE PROCEDURE staging.router_realisations(p_id_execution bigint)
LANGUAGE plpgsql
AS $$
DECLARE
    v_lues     bigint;
    v_rejets   bigint;
    v_doublons bigint;
    v_propres  bigint;
BEGIN
    -- Mémoire de tri relevée pour cette transaction seulement. Le dédoublonnage
    -- trie 2,3 M lignes par fenêtrage ; avec les 4 Mo par défaut, ce tri
    -- déborderait sur disque en multiples passes. SET LOCAL limite la hausse à
    -- la durée de la procédure, sans impacter les autres sessions.
    SET LOCAL work_mem = '256MB';

    -- =========================================================================
    --  1. Matérialisation de la vue normalisée
    -- =========================================================================
    --  On la lit trois fois (rejets, dédoublonnage, propre). Recalculer la
    --  normalisation à chaque lecture triplerait le coût. On la fige donc une
    --  fois dans une table temporaire, indexée sur la clé naturelle.
    DROP TABLE IF EXISTS tmp_normalisee;
    CREATE TEMP TABLE tmp_normalisee ON COMMIT DROP AS
        SELECT * FROM staging.v_realisation_normalisee;

    SELECT count(*) INTO v_lues FROM tmp_normalisee;

    -- Un « code de rejet prioritaire » par ligne : la première règle ERREUR
    -- violée, dans l'ordre du catalogue. Une ligne peut cumuler les défauts ;
    -- on n'en trace qu'un pour que « nombre de rejets = nombre de lignes
    -- rejetées » reste vrai et lisible dans le rapport.
    ALTER TABLE tmp_normalisee ADD COLUMN code_rejet text;
    UPDATE tmp_normalisee SET code_rejet =
        CASE
            WHEN viole_R004_course              THEN 'R004_COURSE_MANQUANTE'
            WHEN viole_R001_date                THEN 'R001_DATE_ILLISIBLE'
            WHEN viole_R002_heure_theo          THEN 'R002_HEURE_THEORIQUE_ILLISIBLE'
            WHEN viole_R008_sequence            THEN 'R008_SEQUENCE_INVALIDE'
            WHEN viole_R005_etat                THEN 'R005_ETAT_INCONNU'
            WHEN viole_R006_suppr               THEN 'R006_INCOHERENCE_SUPPRESSION'
            WHEN viole_R003_retard              THEN 'R003_RETARD_ABERRANT'
            WHEN viole_R007_realise_sans_heure  THEN 'R007_REALISE_SANS_HEURE'
            ELSE NULL
        END;

    -- =========================================================================
    --  2. Rejets ERREUR → rejet.rejet
    -- =========================================================================
    --  La charge utile reconstruit la ligne source complète en jsonb, pour
    --  pouvoir corriger puis rejouer sans retourner au fichier d'origine.
    --  On repart de staging.realisation (le VRAI brut) et non de la vue, afin de
    --  tracer les valeurs telles qu'elles étaient AVANT toute normalisation.
    INSERT INTO rejet.rejet
        (id_execution, code_source, nom_fichier, numero_ligne, cle_naturelle,
         code_regle, valeur_fautive, charge_utile)
    SELECT
        p_id_execution,
        'EXPLOITATION',
        NULL,
        n.numero_ligne,
        -- Clé naturelle lisible, même partielle : elle aide au diagnostic.
        coalesce(n.brut_date,'?') || '|' || coalesce(n.id_course,'?')
            || '|' || coalesce(n.brut_sequence,'?'),
        n.code_rejet,
        -- La valeur précise qui a déclenché la règle prioritaire.
        CASE n.code_rejet
            WHEN 'R001_DATE_ILLISIBLE'             THEN n.brut_date
            WHEN 'R002_HEURE_THEORIQUE_ILLISIBLE'  THEN n.brut_h_theo
            WHEN 'R003_RETARD_ABERRANT'            THEN n.brut_retard
            WHEN 'R005_ETAT_INCONNU'               THEN n.brut_etat
            WHEN 'R008_SEQUENCE_INVALIDE'          THEN n.brut_sequence
            ELSE NULL
        END,
        to_jsonb(r.*)  -- ligne brute intégrale
    FROM tmp_normalisee n
    JOIN staging.realisation r ON r.numero_ligne = n.numero_ligne
    WHERE n.code_rejet IS NOT NULL;

    GET DIAGNOSTICS v_rejets = ROW_COUNT;

    -- =========================================================================
    --  3. Dédoublonnage des lignes propres
    -- =========================================================================
    --  Clé naturelle du passage : (date_service, id_course, rang_arret).
    --  Politique : on garde la DERNIÈRE observation, celle de plus grand
    --  numero_ligne. Justification métier — dans un flux temps réel, la dernière
    --  estimation d'un passage est la plus proche de la réalité observée ; dans
    --  un rejeu de fichier, la dernière écriture prime. La règle est la même
    --  dans les deux cas, ce qui est exactement ce qu'on veut.
    DROP TABLE IF EXISTS staging.realisation_valide;
    CREATE UNLOGGED TABLE staging.realisation_valide AS
    WITH propres AS (
        SELECT *
        FROM tmp_normalisee
        WHERE code_rejet IS NULL          -- aucune règle ERREUR violée
    ),
    numerotees AS (
        SELECT
            p.*,
            -- Rang au sein d'un groupe de doublons : 1 = la ligne conservée.
            row_number() OVER (
                PARTITION BY p.date_service, p.id_course, p.rang_arret
                ORDER BY p.numero_ligne DESC
            ) AS rang_dans_doublon,
            count(*) OVER (
                PARTITION BY p.date_service, p.id_course, p.rang_arret
            ) AS nb_dans_groupe
        FROM propres p
    )
    SELECT
        date_service, code_ligne, id_course, rang_arret, id_arret, libelle_arret,
        horodate_theorique, horodate_reelle, est_supprime, retard_sec, vitesse_kmh,
        nb_dans_groupe,
        id_execution, numero_ligne
    FROM numerotees
    WHERE rang_dans_doublon = 1;         -- un seul exemplaire par clé

    GET DIAGNOSTICS v_propres = ROW_COUNT;

    -- Combien de lignes ont été absorbées comme doublons ?
    SELECT count(*) - v_propres INTO v_doublons
    FROM tmp_normalisee WHERE code_rejet IS NULL;

    -- Trace des doublons : on distingue exact et divergent en comparant, dans
    -- chaque groupe, le nombre de combinaisons de mesures distinctes.
    --   1 combinaison  → toutes les copies sont identiques      → A103 exact
    --   ≥ 2            → les copies divergent sur une mesure     → A104 divergent
    INSERT INTO rejet.rejet
        (id_execution, code_source, numero_ligne, cle_naturelle,
         code_regle, charge_utile)
    SELECT
        p_id_execution, 'EXPLOITATION', min(numero_ligne),
        date_service || '|' || id_course || '|' || rang_arret,
        CASE WHEN count(DISTINCT (horodate_reelle, retard_sec, est_supprime)) = 1
             THEN 'A103_DOUBLON_EXACT' ELSE 'A104_DOUBLON_DIVERGENT' END,
        jsonb_build_object(
            'cle', date_service || '|' || id_course || '|' || rang_arret,
            'nb_copies', count(*),
            'retards_vus', array_agg(DISTINCT retard_sec))
    FROM tmp_normalisee
    WHERE code_rejet IS NULL
    GROUP BY date_service, id_course, rang_arret
    HAVING count(*) > 1;

    -- =========================================================================
    --  4. Index sur la table propre
    -- =========================================================================
    --  La phase 6 (construction des faits) joindra cette table au référentiel
    --  d'arrêts et aux dimensions. On indexe les clés de jointure maintenant.
    CREATE INDEX ON staging.realisation_valide (id_arret);
    CREATE INDEX ON staging.realisation_valide (code_ligne);
    ANALYZE staging.realisation_valide;

    -- =========================================================================
    --  Journalisation du bilan
    -- =========================================================================
    RAISE NOTICE 'Routage : % lues | % propres | % doublons absorbés | % rejets ERREUR',
        v_lues, v_propres, v_doublons, v_rejets;
END;
$$;

COMMENT ON PROCEDURE staging.router_realisations(bigint) IS
    'Répartit staging.realisation en lignes propres (realisation_valide), '
    'rejets ERREUR (rejet.rejet) et doublons tracés. Idempotente.';
