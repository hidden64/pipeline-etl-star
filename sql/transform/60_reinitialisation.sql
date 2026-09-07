-- =============================================================================
--  TRANSFORM 60 — Réinitialisation de l'entrepôt (reconstruction complète)
-- =============================================================================
--  Vide les faits et les dimensions RECONSTRUCTIBLES, puis restaure leurs
--  membres inconnus. À n'utiliser que pour repartir d'un état propre (rejeu
--  complet, développement) — le fonctionnement normal du pipeline est
--  idempotent et n'en a pas besoin.
--
--  Ce qu'on NE touche PAS :
--    - dim_date et dim_creneau : dimensions de référence figées, peuplées une
--      fois pour toutes. Les vider obligerait à les régénérer sans aucun gain.
--    - le catalogue rejet.regle et l'historique meta.* : ce sont des archives.
-- =============================================================================

CREATE OR REPLACE PROCEDURE staging.reinitialiser_entrepot()
LANGUAGE plpgsql
AS $$
BEGIN
    -- Un SEUL TRUNCATE pour le fait ET les dimensions qu'il référence.
    -- PostgreSQL refuse de tronquer une table cible d'une clé étrangère sans
    -- tronquer la table référençante DANS LE MÊME ordre : les deux doivent
    -- figurer ensemble (l'alternative serait CASCADE, plus dangereux car il
    -- s'étendrait à d'éventuelles autres tables dépendantes sans les nommer).
    -- Sur une table partitionnée, TRUNCATE vide toutes les partitions d'un coup.
    -- RESTART IDENTITY remet les compteurs de clés de substitution à zéro.
    TRUNCATE TABLE entrepot.fait_passage,
                   entrepot.dim_ligne, entrepot.dim_arret, entrepot.dim_meteo
        RESTART IDENTITY;

    -- Restaurer les membres inconnus (sk = -1), indispensables aux jointures.
    INSERT INTO entrepot.dim_ligne (sk_ligne, id_ligne_source, code_source, code_ligne,
                                    nom_ligne, mode_transport, hash_attributs,
                                    date_debut_validite, date_fin_validite, est_courant)
    VALUES (-1, '@INCONNU', 'SYSTEME', 'N/D', 'Ligne inconnue', 'INCONNU',
            md5('inconnu'), DATE '1900-01-01', DATE '9999-12-31', true);

    INSERT INTO entrepot.dim_arret (sk_arret, id_arret_source, code_source, nom_arret,
                                    type_arret, hash_attributs,
                                    date_debut_validite, date_fin_validite, est_courant)
    VALUES (-1, '@INCONNU', 'SYSTEME', 'Arrêt inconnu', 'INCONNU', md5('inconnu'),
            DATE '1900-01-01', DATE '9999-12-31', true);

    INSERT INTO entrepot.dim_meteo (sk_meteo, condition_libelle, famille_condition,
                                    tranche_temperature, tranche_precipitation,
                                    tranche_vent, hash_attributs)
    VALUES (-1, 'Météo inconnue', 'INCONNU', 'INCONNU', 'INCONNU', 'INCONNU', md5('inconnu'));

    RAISE NOTICE 'Entrepôt réinitialisé : faits vidés, dimensions reconstructibles remises à zéro.';
END;
$$;
