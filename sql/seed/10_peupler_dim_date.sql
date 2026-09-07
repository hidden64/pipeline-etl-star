-- =============================================================================
--  SEED 10 — Peuplement de entrepot.dim_date
-- =============================================================================
--  Une dimension calendaire se génère, elle ne se charge pas depuis une source.
--  On la peuple sur une plage large et par avance (typiquement 2020 → 2035),
--  une fois pour toutes.
--
--  Pourquoi par avance et pas au fil de l'eau ?
--    Pour pouvoir répondre « 0 passage le 25 décembre ». Si la date n'existe pas
--    dans la dimension, une jointure la fait disparaître du rapport : l'absence
--    de données devient indiscernable de l'absence de la date elle-même.
--    Une dimension calendaire complète permet les jointures externes et donc les
--    séries temporelles sans trous.
-- =============================================================================


-- -----------------------------------------------------------------------------
--  Table de référence : vacances scolaires
-- -----------------------------------------------------------------------------
--  Rennes dépend de l'académie de Rennes → ZONE B.
--  Les vacances scolaires font chuter la fréquentation et modifient l'offre :
--  c'est un axe d'analyse indispensable pour interpréter la ponctualité.
--
--  /!\ Ces dates sont saisies manuellement. À vérifier et compléter depuis le
--      calendrier officiel : https://www.education.gouv.fr/calendrier-scolaire
--      C'est assumé : une petite table de référence tenue à la main est plus
--      honnête et plus maintenable qu'un algorithme qui devinerait mal.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS entrepot.ref_vacances_scolaires (
    zone        text NOT NULL DEFAULT 'B',
    libelle     text NOT NULL,
    date_debut  date NOT NULL,
    date_fin    date NOT NULL,      -- incluse
    PRIMARY KEY (zone, date_debut),
    CONSTRAINT ck_ref_vacances_periode CHECK (date_fin >= date_debut)
);

INSERT INTO entrepot.ref_vacances_scolaires (zone, libelle, date_debut, date_fin) VALUES
    ('B', 'Toussaint 2025',  '2025-10-18', '2025-11-02'),
    ('B', 'Noël 2025',       '2025-12-20', '2026-01-04'),
    ('B', 'Hiver 2026',      '2026-02-07', '2026-02-22'),
    ('B', 'Printemps 2026',  '2026-04-04', '2026-04-19'),
    ('B', 'Été 2026',        '2026-07-04', '2026-08-31'),
    ('B', 'Toussaint 2026',  '2026-10-17', '2026-11-01'),
    ('B', 'Noël 2026',       '2026-12-19', '2027-01-03')
ON CONFLICT (zone, date_debut) DO NOTHING;


-- -----------------------------------------------------------------------------
--  Calcul du dimanche de Pâques (algorithme de Meeus, version grégorienne)
-- -----------------------------------------------------------------------------
--  Nécessaire parce que quatre jours fériés français en dépendent : lundi de
--  Pâques (+1), Ascension (+39), lundi de Pentecôte (+50).
--  C'est de l'arithmétique entière pure, donc IMMUTABLE : PostgreSQL peut mettre
--  le résultat en cache et l'utiliser dans un index si besoin.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION entrepot.dimanche_paques(p_annee integer)
RETURNS date
LANGUAGE plpgsql
IMMUTABLE
AS $$
DECLARE
    a int; b int; c int; d int; e int; f int; g int;
    h int; i int; k int; l int; m int;
    v_mois int; v_jour int;
BEGIN
    a := p_annee % 19;
    b := p_annee / 100;
    c := p_annee % 100;
    d := b / 4;
    e := b % 4;
    f := (b + 8) / 25;
    g := (b - f + 1) / 3;
    h := (19 * a + b - d - g + 15) % 30;
    i := c / 4;
    k := c % 4;
    l := (32 + 2 * e + 2 * i - h - k) % 7;
    m := (a + 11 * h + 22 * l) / 451;
    v_mois := (h + l - 7 * m + 114) / 31;
    v_jour := ((h + l - 7 * m + 114) % 31) + 1;

    RETURN make_date(p_annee, v_mois, v_jour);
END;
$$;


-- -----------------------------------------------------------------------------
--  Jours fériés français d'une année
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION entrepot.jours_feries(p_annee integer)
RETURNS TABLE (jour date, libelle text)
LANGUAGE sql
IMMUTABLE
AS $$
    WITH paques AS (SELECT entrepot.dimanche_paques(p_annee) AS d)
    SELECT make_date(p_annee,  1,  1), 'Jour de l''An'
    UNION ALL SELECT (SELECT d FROM paques) + 1,  'Lundi de Pâques'
    UNION ALL SELECT make_date(p_annee,  5,  1), 'Fête du Travail'
    UNION ALL SELECT make_date(p_annee,  5,  8), 'Victoire 1945'
    UNION ALL SELECT (SELECT d FROM paques) + 39, 'Ascension'
    UNION ALL SELECT (SELECT d FROM paques) + 50, 'Lundi de Pentecôte'
    UNION ALL SELECT make_date(p_annee,  7, 14), 'Fête nationale'
    UNION ALL SELECT make_date(p_annee,  8, 15), 'Assomption'
    UNION ALL SELECT make_date(p_annee, 11,  1), 'Toussaint'
    UNION ALL SELECT make_date(p_annee, 11, 11), 'Armistice 1918'
    UNION ALL SELECT make_date(p_annee, 12, 25), 'Noël';
$$;


-- -----------------------------------------------------------------------------
--  Peuplement
-- -----------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE entrepot.peupler_dim_date(p_debut date, p_fin date)
LANGUAGE plpgsql
AS $$
BEGIN
    INSERT INTO entrepot.dim_date (
        sk_date, jour, annee, trimestre, mois, libelle_mois,
        numero_semaine_iso, jour_du_mois, jour_semaine_iso, libelle_jour_semaine,
        est_weekend, est_ferie, libelle_ferie, est_vacances_scolaires, type_jour_reseau
    )
    SELECT
        to_char(j.jour, 'YYYYMMDD')::integer                      AS sk_date,
        j.jour,
        EXTRACT(YEAR    FROM j.jour)::smallint                    AS annee,
        EXTRACT(QUARTER FROM j.jour)::smallint                    AS trimestre,
        EXTRACT(MONTH   FROM j.jour)::smallint                    AS mois,
        -- Libellés en français : on force la locale plutôt que de dépendre de
        -- celle du serveur, qui varie d'une machine à l'autre.
        (ARRAY['janvier','février','mars','avril','mai','juin','juillet',
               'août','septembre','octobre','novembre','décembre']
        )[EXTRACT(MONTH FROM j.jour)::int]                        AS libelle_mois,
        EXTRACT(WEEK    FROM j.jour)::smallint                    AS numero_semaine_iso,
        EXTRACT(DAY     FROM j.jour)::smallint                    AS jour_du_mois,
        -- ISODOW : 1 = lundi … 7 = dimanche. On n'utilise JAMAIS `DOW` ici, qui
        -- démarre le dimanche à 0 — décalage d'un jour classique.
        EXTRACT(ISODOW  FROM j.jour)::smallint                    AS jour_semaine_iso,
        (ARRAY['lundi','mardi','mercredi','jeudi',
               'vendredi','samedi','dimanche']
        )[EXTRACT(ISODOW FROM j.jour)::int]                       AS libelle_jour_semaine,
        EXTRACT(ISODOW FROM j.jour) >= 6                          AS est_weekend,
        (f.jour IS NOT NULL)                                      AS est_ferie,
        f.libelle                                                 AS libelle_ferie,
        (v.date_debut IS NOT NULL)                                AS est_vacances_scolaires,
        -- Type de jour réseau : l'ordre des conditions porte la règle métier.
        -- Un dimanche férié reste un dimanche ; les vacances ne priment que sur
        -- un jour ouvrable.
        CASE
            WHEN EXTRACT(ISODOW FROM j.jour) = 7 OR f.jour IS NOT NULL THEN 'DIMANCHE_FERIE'
            WHEN EXTRACT(ISODOW FROM j.jour) = 6                       THEN 'SAMEDI'
            WHEN v.date_debut IS NOT NULL                              THEN 'VACANCES'
            ELSE 'OUVRABLE'
        END                                                       AS type_jour_reseau
    FROM generate_series(p_debut, p_fin, interval '1 day') AS j(jour)

    -- Jours fériés : on génère la table des fériés pour chaque année de la plage,
    -- puis on rattache. LATERAL n'est pas nécessaire, une jointure suffit.
    LEFT JOIN LATERAL (
        SELECT jf.jour, jf.libelle
        FROM entrepot.jours_feries(EXTRACT(YEAR FROM j.jour)::integer) AS jf
        WHERE jf.jour = j.jour::date
    ) AS f ON true

    LEFT JOIN entrepot.ref_vacances_scolaires AS v
           ON v.zone = 'B'
          AND j.jour::date BETWEEN v.date_debut AND v.date_fin

    -- Rejouable : relancer la procédure sur une plage déjà couverte ne fait rien.
    ON CONFLICT (sk_date) DO NOTHING;

    RAISE NOTICE 'dim_date peuplée de % à % (% lignes au total).',
        p_debut, p_fin, (SELECT count(*) FROM entrepot.dim_date);
END;
$$;

-- Plage par défaut du projet.
CALL entrepot.peupler_dim_date(DATE '2020-01-01', DATE '2030-12-31');
