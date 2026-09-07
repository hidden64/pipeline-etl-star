-- =============================================================================
--  TRANSFORM 10 — Normalisation de l'export d'exploitation
-- =============================================================================
--  On construit une VUE qui présente le staging brut (tout en text) sous une
--  forme typée et normalisée, SANS encore rien rejeter. Chaque ligne y gagne :
--    - ses valeurs converties (date, horodatages, retard, vitesse) ;
--    - des DRAPEAUX booléens qui disent, pour chaque règle, si elle est violée.
--
--  Pourquoi une vue et pas une table ? Parce que la normalisation est une
--  fonction PURE de l'entrée : la recalculer coûte moins cher que de la stocker
--  et de la maintenir cohérente. La matérialisation viendra seulement à l'étape
--  de routage (20), là où on veut figer le résultat pour le lire deux fois
--  (une pour le propre, une pour le rejet) sans tout recalculer.
--
--  Principe directeur : la normalisation ne DÉCIDE rien. Elle expose les faits
--  (« cette date est illisible », « cet arrêt est orphelin »). C'est l'étape 20
--  qui tranche entre correction, rattachement et rejet.
-- =============================================================================


-- -----------------------------------------------------------------------------
--  Fonctions de conversion tolérantes
-- -----------------------------------------------------------------------------
--  Elles renvoient NULL au lieu d'échouer sur une entrée invalide : une seule
--  ligne fautive ne doit pas casser la transformation de 2,3 millions d'autres.
--
--  POINT DE PERFORMANCE — leçon centrale de cette phase.
--  Une première version utilisait PL/pgSQL avec `EXCEPTION WHEN OTHERS`. Le
--  routage prenait 285 secondes. Deux raisons cumulées :
--    1. un bloc EXCEPTION crée un SAVEPOINT à CHAQUE appel. Sur 2,3 M lignes ×
--       4 fonctions, cela fait ~9 millions de savepoints — un coût énorme ;
--    2. une fonction PL/pgSQL n'est JAMAIS « inlinée » par le planificateur :
--       elle reste une boîte noire appelée ligne à ligne.
--
--  Réécrites en SQL pur (un seul SELECT, sans EXCEPTION), elles deviennent
--  INLINABLES : le planificateur les fond dans la requête, comme si le CASE
--  était écrit à la main. La regex de garde suffit à éviter tout cast fautif,
--  donc l'EXCEPTION est superflue. Résultat : le même routage passe à quelques
--  secondes. C'est la règle à retenir — sur de gros volumes, préférer le SQL
--  pur inlinable au PL/pgSQL, et bannir EXCEPTION dans le chemin chaud.
--
--  Toutes IMMUTABLE : même entrée → même sortie, sans effet de bord.
-- -----------------------------------------------------------------------------

-- Date française « JJ/MM/AAAA » → date. NULL si le format ou le calendrier est
-- invalide.
--
-- SUBTILITÉ ATTRAPÉE PAR LES TESTS. En PostgreSQL 18, to_date('31/13/2026')
-- LÈVE une exception (mois 13 hors champ), tandis que to_date('30/02/2026')
-- DÉBORDE silencieusement en 02/03. Comme cette fonction est en SQL pur (sans
-- gestionnaire d'exception, retiré pour la performance), une exception non
-- interceptée ferait échouer le routage des 2,3 M lignes à cause d'UNE seule
-- date à mois 13. La parade tient en deux temps :
--   1. la regex BORNE le jour (01-31) et le mois (01-12) : « 31/13 » et
--      « 32/.. » sont écartés AVANT to_date, donc plus aucune exception ;
--   2. l'aller-retour to_char(to_date(...)) rattrape les débordements de jour
--      qui, eux, ne lèvent pas d'exception (« 30/02 » → « 02/03 » ≠ entrée
--      → NULL).
-- On obtient la robustesse d'un bloc EXCEPTION sans son coût.
-- Nombre de jours d'un mois, année bissextile comprise (règle grégorienne).
-- 0 pour un mois hors [1,12], ce qui rend tout jour invalide sans lever.
CREATE OR REPLACE FUNCTION staging.jours_dans_mois(p_mois int, p_annee int)
RETURNS int
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
RETURN CASE
    WHEN p_mois < 1 OR p_mois > 12 THEN 0
    ELSE (ARRAY[31,28,31,30,31,30,31,31,30,31,30,31])[p_mois]
         + CASE WHEN p_mois = 2
                 AND (p_annee % 4 = 0 AND (p_annee % 100 <> 0 OR p_annee % 400 = 0))
                THEN 1 ELSE 0 END
END;

-- Date française « JJ/MM/AAAA » → date, ou NULL.
--
-- Pourquoi des CASE IMBRIQUÉS et pas un simple WHERE ? Parce que PostgreSQL,
-- une fois cette fonction inlinée dans la grande requête de normalisation,
-- peut évaluer make_date AVANT le filtre — et make_date LÈVE sur une date
-- impossible. Un CASE, lui, garantit de ne pas évaluer les branches non
-- retenues. On s'appuie sur cette garantie en imbriquant :
--   - le CASE externe protège les casts ::int (une entrée non conforme à la
--     regex ne doit jamais être castée : « abc »::int lèverait) ;
--   - le CASE interne protège make_date (jamais appelé sur un jour hors bornes).
-- Résultat : robuste face à n'importe quelle entrée, sans bloc EXCEPTION.
CREATE OR REPLACE FUNCTION staging.parse_date_fr(p_texte text)
RETURNS date
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
RETURN CASE
    WHEN p_texte ~ '^\d{2}/\d{2}/\d{4}$' THEN
        CASE
            WHEN substring(p_texte, 1, 2)::int BETWEEN 1 AND
                 staging.jours_dans_mois(substring(p_texte, 4, 2)::int,
                                         substring(p_texte, 7, 4)::int)
            THEN make_date(substring(p_texte, 7, 4)::int,
                           substring(p_texte, 4, 2)::int,
                           substring(p_texte, 1, 2)::int)
        END
END;

-- Heure GTFS « HH:MM:SS », éventuellement > 24 h, → interval. Le pattern garantit
-- un cast valide : « 25:14:00 »::interval est légal (1 j 01:14:30).
CREATE OR REPLACE FUNCTION staging.parse_heure_gtfs(p_texte text)
RETURNS interval
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
RETURN CASE
    WHEN btrim(p_texte) ~ '^\d{1,2}:\d{2}:\d{2}$'
    THEN btrim(p_texte)::interval
END;

-- Entier signé. La borne de 15 chiffres exclut tout dépassement de bigint,
-- ce qui rend le cast infaillible sans bloc EXCEPTION.
CREATE OR REPLACE FUNCTION staging.parse_entier(p_texte text)
RETURNS bigint
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
RETURN CASE
    WHEN btrim(p_texte) ~ '^-?\d{1,15}$'
    THEN btrim(p_texte)::bigint
END;

-- Décimale « 18,4 » ou « 18.4 » → numeric. La source française écrit à la
-- virgule, mais le flux temps réel peut écrire au point : on accepte les deux.
-- (Le commentaire promettait déjà le point ; un test a révélé que la regex ne
--  l'acceptait pas. Code et commentaire sont désormais d'accord.)
CREATE OR REPLACE FUNCTION staging.parse_decimal_fr(p_texte text)
RETURNS numeric
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
RETURN CASE
    WHEN btrim(p_texte) ~ '^-?\d+([.,]\d+)?$'
    THEN replace(btrim(p_texte), ',', '.')::numeric
END;


-- -----------------------------------------------------------------------------
--  Bornes métier, centralisées
-- -----------------------------------------------------------------------------
--  Un retard hors de [-30 min, +2 h] n'est pas un retard : c'est un capteur en
--  panne. Ces bornes sont les MÊMES que la contrainte CHECK de fait_passage :
--  la validation applicative et la contrainte base doivent toujours s'accorder,
--  sinon une ligne « validée » se ferait refuser à l'insertion.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION staging.retard_min_sec() RETURNS integer
    LANGUAGE sql IMMUTABLE AS $$ SELECT -1800 $$;   -- -30 min
CREATE OR REPLACE FUNCTION staging.retard_max_sec() RETURNS integer
    LANGUAGE sql IMMUTABLE AS $$ SELECT 7200 $$;    -- +2 h


-- =============================================================================
--  La vue de normalisation
-- =============================================================================
DROP VIEW IF EXISTS staging.v_realisation_normalisee CASCADE;
CREATE VIEW staging.v_realisation_normalisee AS
-- `AS MATERIALIZED` est ici une décision de PERFORMANCE, pas de style.
-- Sans lui, PostgreSQL fond (inline) le CTE dans la requête et RÉÉVALUE chaque
-- fonction de parsing autant de fois que sa colonne est référencée plus loin —
-- date_service et heure_theorique le sont 5 à 6 fois, soit ~14 M appels au lieu
-- de 2,3 M. Mesuré : 40 s pour 200 k lignes en inline, contre ~1 s en une passe.
-- MATERIALIZED force le calcul UNE fois, stocké, puis relu. C'est le remède
-- exact au piège « fonction coûteuse réévaluée par l'inlining de CTE ».
WITH base AS MATERIALIZED (
    SELECT
        r.numero_ligne,
        r.id_execution,

        -- ---- Champs normalisés (nettoyés, mais pas encore validés) --------
        staging.parse_date_fr(r.date_exploitation)          AS date_service,
        -- btrim retire les espaces parasites « ␣␣C1␣ » → « C1 ».
        btrim(r.ligne)                                      AS code_ligne,
        btrim(r.course)                                     AS id_course,
        staging.parse_entier(r.sequence)                    AS rang_arret,
        btrim(r.code_arret)                                 AS id_arret,
        NULLIF(btrim(r.libelle_arret), '')                  AS libelle_arret,
        staging.parse_heure_gtfs(r.h_theorique)             AS heure_theorique,
        staging.parse_heure_gtfs(r.h_reelle)                AS heure_reelle,
        -- upper() absorbe « realise / Realise / REALISE » en une seule valeur.
        upper(btrim(r.etat_course))                         AS etat,
        staging.parse_entier(r.retard_sec)                  AS retard_sec,
        staging.parse_decimal_fr(r.vitesse_moy_kmh)         AS vitesse_kmh,

        -- Valeurs brutes conservées pour tracer les rejets jusqu'à la source.
        r.date_exploitation AS brut_date, r.h_theorique AS brut_h_theo,
        r.retard_sec AS brut_retard, r.etat_course AS brut_etat,
        r.code_arret AS brut_arret, r.sequence AS brut_sequence
    FROM staging.realisation r
),
-- PAS de MATERIALIZED ici, à dessein : `typee` ne fait que LIRE les colonnes
-- déjà calculées et stockées par `base` (date_service, heure_theorique). Les
-- expressions dérivées (horodate_theorique) sont de simples additions
-- d'intervalles sur des colonnes stockées — leur éventuelle réévaluation ne
-- coûte quasi rien, contrairement au parsing. Matérialiser ici n'apporterait
-- qu'une écriture de 2,3 M lignes en plus. On ne matérialise QUE ce qui est cher.
typee AS (
    SELECT
        b.*,
        -- « SUPPRIME » sous toutes ses casses est déjà replié par upper().
        (b.etat = 'SUPPRIME')                               AS est_supprime,
        -- Horodatage théorique absolu. date + interval règle nativement les
        -- heures > 24 h : « 04/09 » + « 25:14:00 » = « 05/09 01:14:00 ».
        -- AT TIME ZONE ancre en Europe/Paris → timestamptz correct au
        -- changement d'heure (piège n°8).
        CASE WHEN b.date_service IS NOT NULL AND b.heure_theorique IS NOT NULL
             THEN (b.date_service + b.heure_theorique) AT TIME ZONE 'Europe/Paris'
        END                                                 AS horodate_theorique
    FROM base b
)
SELECT
    t.*,

    -- Horodatage réel : reconstruit à partir du théorique et du retard, qui est
    -- la mesure de référence. On ne se fie pas à l'heure réelle brute, qui peut
    -- diverger de quelques secondes (arrondi du système source).
    CASE WHEN NOT t.est_supprime
              AND t.horodate_theorique IS NOT NULL
              AND t.retard_sec IS NOT NULL
         THEN t.horodate_theorique + make_interval(secs => t.retard_sec)
    END                                                     AS horodate_reelle,

    -- ================= DRAPEAUX DE VALIDATION ==============================
    -- Chacun correspond à une règle du catalogue. La vue CONSTATE, elle ne
    -- décide pas ; l'étape 20 traduit ces drapeaux en propre/rejet.

    (t.date_service IS NULL)                    AS viole_R001_date,
    (t.horodate_theorique IS NULL)              AS viole_R002_heure_theo,
    (t.retard_sec IS NOT NULL
        AND NOT t.est_supprime
        AND (t.retard_sec < staging.retard_min_sec()
             OR t.retard_sec > staging.retard_max_sec()))  AS viole_R003_retard,
    (t.id_course IS NULL OR t.id_course = '')   AS viole_R004_course,
    (t.etat NOT IN ('REALISE', 'SUPPRIME')
        OR t.etat IS NULL)                      AS viole_R005_etat,
    -- Course supprimée mais porteuse d'une heure réelle ou d'un retard : les
    -- deux informations se contredisent, la ligne n'est pas fiable.
    (t.est_supprime
        AND (t.heure_reelle IS NOT NULL OR t.retard_sec IS NOT NULL)) AS viole_R006_suppr,
    -- Réalisée mais sans moyen de situer le passage dans le temps.
    (NOT t.est_supprime
        AND t.retard_sec IS NULL)               AS viole_R007_realise_sans_heure,
    (t.rang_arret IS NULL OR t.rang_arret < 0)  AS viole_R008_sequence,

    -- Avertissements (récupérables) : évalués à l'étape 20 par jointure au
    -- référentiel. On expose ici seulement ce qui se calcule sans jointure.
    (t.vitesse_kmh IS NULL)                     AS avert_A105_vitesse
FROM typee t;

COMMENT ON VIEW staging.v_realisation_normalisee IS
    'Présentation typée et normalisée de staging.realisation, avec un drapeau '
    'booléen par règle de qualité. Ne rejette rien : constate seulement.';
