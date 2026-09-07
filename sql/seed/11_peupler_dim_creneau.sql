-- =============================================================================
--  SEED 11 : Peuplement de entrepot.dim_creneau
-- =============================================================================
--  24 lignes, figées. Les tranches correspondent aux périodes d'exploitation
--  usuelles d'un réseau urbain français :
--
--    00h-04h  NUIT               service de nuit, offre très réduite
--    05h-06h  CREUSE_MATIN       montée en charge
--    07h-09h  POINTE_MATIN       heure de pointe domicile → travail/études
--    10h-11h  CREUSE_MATIN
--    12h-13h  MIDI               pointe secondaire (pause méridienne)
--    14h-15h  CREUSE_APRES_MIDI
--    16h-19h  POINTE_SOIR        heure de pointe travail/études → domicile
--    20h-23h  SOIREE             offre décroissante
--
--  Ces découpages sont un CHOIX MÉTIER, pas une donnée. Les inscrire dans la
--  dimension plutôt que dans chaque requête garantit que tout le monde analyse
--  avec la même définition d'« heure de pointe » : c'est le rôle même d'une
--  dimension conforme.
-- =============================================================================

INSERT INTO entrepot.dim_creneau
    (sk_creneau, heure, libelle_heure, libelle_creneau, est_heure_pointe, est_service_nuit)
SELECT
    h::smallint                                        AS sk_creneau,
    h::smallint                                        AS heure,
    lpad(h::text, 2, '0') || 'h - ' || lpad(((h + 1) % 24)::text, 2, '0') || 'h'
                                                       AS libelle_heure,
    CASE
        WHEN h BETWEEN  0 AND  4 THEN 'NUIT'
        WHEN h BETWEEN  5 AND  6 THEN 'CREUSE_MATIN'
        WHEN h BETWEEN  7 AND  9 THEN 'POINTE_MATIN'
        WHEN h BETWEEN 10 AND 11 THEN 'CREUSE_MATIN'
        WHEN h BETWEEN 12 AND 13 THEN 'MIDI'
        WHEN h BETWEEN 14 AND 15 THEN 'CREUSE_APRES_MIDI'
        WHEN h BETWEEN 16 AND 19 THEN 'POINTE_SOIR'
        ELSE                          'SOIREE'
    END                                                AS libelle_creneau,
    (h BETWEEN 7 AND 9 OR h BETWEEN 16 AND 19)         AS est_heure_pointe,
    (h BETWEEN 0 AND 4)                                AS est_service_nuit
FROM generate_series(0, 23) AS g(h)
ON CONFLICT (sk_creneau) DO NOTHING;
