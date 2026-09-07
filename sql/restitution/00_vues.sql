-- =============================================================================
--  RESTITUTION 00 — Vues d'analyse
-- =============================================================================
--  Le schéma `restitution` est la SEULE porte d'entrée d'un outil de BI ou d'un
--  analyste. On n'expose jamais `entrepot` directement : les vues forment un
--  contrat stable, isolant les utilisateurs des détails d'implémentation
--  (partitionnement, membres inconnus, colonnes techniques).
--
--  RÈGLE ABSOLUE DE CES VUES : les ratios sont TOUJOURS recalculés à la volée,
--  par SUM(numérateur) / SUM(dénominateur). On ne stocke jamais un taux, et on
--  ne fait jamais AVG d'un taux — une moyenne de moyennes est fausse. C'est le
--  principe d'additivité vu en phase 1, appliqué jusqu'au bout.
-- =============================================================================


-- -----------------------------------------------------------------------------
--  KPI globaux : une seule ligne, la photo d'ensemble
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW restitution.v_kpi_globaux AS
SELECT
    count(*)                                                   AS total_passages,
    count(*) FILTER (WHERE est_supprime)                       AS passages_supprimes,
    count(*) FILTER (WHERE est_degrade)                        AS passages_degrades,
    -- Ponctualité = ponctuels / réalisés. Le FILTER exclut les supprimés des
    -- deux côtés du ratio, sinon le dénominateur serait faux.
    round(100.0 * sum(est_ponctuel::int)
          / nullif(sum(nb_passages) FILTER (WHERE NOT est_supprime), 0), 2)
                                                               AS taux_ponctualite_pct,
    round(avg(retard_secondes) FILTER (WHERE NOT est_supprime), 1)
                                                               AS retard_moyen_sec,
    -- La médiane et le 90e centile disent bien plus que la moyenne sur des
    -- retards : la moyenne masque les extrêmes, le P90 les révèle.
    round((percentile_cont(0.5) WITHIN GROUP (ORDER BY retard_secondes)
           FILTER (WHERE NOT est_supprime))::numeric, 0)       AS retard_median_sec,
    round((percentile_cont(0.9) WITHIN GROUP (ORDER BY retard_secondes)
           FILTER (WHERE NOT est_supprime))::numeric, 0)       AS retard_p90_sec
FROM entrepot.fait_passage;


-- -----------------------------------------------------------------------------
--  Ponctualité selon la météo — la question fondatrice du projet
-- -----------------------------------------------------------------------------
--  On écarte le membre inconnu (sk_meteo = -1) : sur l'AXE météo, un fait sans
--  météo n'a rien à dire. Il reste compté partout ailleurs.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW restitution.v_ponctualite_meteo AS
SELECT
    m.famille_condition,
    m.tranche_precipitation,
    m.est_conditions_degradees,
    count(*) FILTER (WHERE NOT f.est_supprime)                 AS passages,
    round(avg(f.retard_secondes) FILTER (WHERE NOT f.est_supprime), 1)
                                                               AS retard_moyen_sec,
    round(100.0 * sum(f.est_ponctuel::int)
          / nullif(sum(f.nb_passages) FILTER (WHERE NOT f.est_supprime), 0), 2)
                                                               AS taux_ponctualite_pct
FROM entrepot.fait_passage f
JOIN entrepot.dim_meteo m ON m.sk_meteo = f.sk_meteo
WHERE f.sk_meteo <> -1
GROUP BY m.famille_condition, m.tranche_precipitation, m.est_conditions_degradees;


-- -----------------------------------------------------------------------------
--  Ponctualité par créneau horaire
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW restitution.v_ponctualite_creneau AS
SELECT
    c.sk_creneau                                               AS heure,
    c.libelle_creneau,
    c.est_heure_pointe,
    count(*) FILTER (WHERE NOT f.est_supprime)                 AS passages,
    round(avg(f.retard_secondes) FILTER (WHERE NOT f.est_supprime), 1)
                                                               AS retard_moyen_sec,
    round(100.0 * sum(f.est_ponctuel::int)
          / nullif(sum(f.nb_passages) FILTER (WHERE NOT f.est_supprime), 0), 2)
                                                               AS taux_ponctualite_pct
FROM entrepot.fait_passage f
JOIN entrepot.dim_creneau c ON c.sk_creneau = f.sk_creneau
WHERE f.sk_creneau <> -1
GROUP BY c.sk_creneau, c.libelle_creneau, c.est_heure_pointe;


-- -----------------------------------------------------------------------------
--  Ponctualité par ligne (version courante de la ligne)
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW restitution.v_ponctualite_ligne AS
SELECT
    l.code_ligne,
    l.nom_ligne,
    l.mode_transport,
    l.est_ligne_structurante,
    count(*) FILTER (WHERE NOT f.est_supprime)                 AS passages,
    count(*) FILTER (WHERE f.est_supprime)                     AS courses_supprimees,
    round(avg(f.retard_secondes) FILTER (WHERE NOT f.est_supprime), 1)
                                                               AS retard_moyen_sec,
    round(100.0 * sum(f.est_ponctuel::int)
          / nullif(sum(f.nb_passages) FILTER (WHERE NOT f.est_supprime), 0), 2)
                                                               AS taux_ponctualite_pct
FROM entrepot.fait_passage f
JOIN entrepot.dim_ligne l ON l.sk_ligne = f.sk_ligne
WHERE f.sk_ligne <> -1
GROUP BY l.code_ligne, l.nom_ligne, l.mode_transport, l.est_ligne_structurante;


-- -----------------------------------------------------------------------------
--  Série temporelle : ponctualité par jour
-- -----------------------------------------------------------------------------
--  Jointure à dim_date : on récupère au passage le type de jour (ouvrable,
--  week-end, férié…), un axe d'analyse que le fait seul ne porte pas.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW restitution.v_ponctualite_jour AS
SELECT
    d.jour,
    d.libelle_jour_semaine,
    d.est_weekend,
    d.type_jour_reseau,
    count(*) FILTER (WHERE NOT f.est_supprime)                 AS passages,
    round(avg(f.retard_secondes) FILTER (WHERE NOT f.est_supprime), 1)
                                                               AS retard_moyen_sec,
    round(100.0 * sum(f.est_ponctuel::int)
          / nullif(sum(f.nb_passages) FILTER (WHERE NOT f.est_supprime), 0), 2)
                                                               AS taux_ponctualite_pct
FROM entrepot.fait_passage f
JOIN entrepot.dim_date d ON d.sk_date = f.sk_date
WHERE f.sk_date <> -1
GROUP BY d.jour, d.libelle_jour_semaine, d.est_weekend, d.type_jour_reseau;


-- -----------------------------------------------------------------------------
--  Qualité : rejets par règle et par exécution
-- -----------------------------------------------------------------------------
--  Jointure au catalogue rejet.regle pour porter le libellé et la sévérité :
--  un rejet n'apparaît jamais sans son explication.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW restitution.v_qualite_rejets AS
SELECT
    r.id_execution,
    r.code_regle,
    g.libelle,
    g.severite,
    count(*)                                                   AS nombre
FROM rejet.rejet r
JOIN rejet.regle g ON g.code_regle = r.code_regle
GROUP BY r.id_execution, r.code_regle, g.libelle, g.severite;


-- -----------------------------------------------------------------------------
--  Journal des exécutions : durée et statut par étape
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW restitution.v_journal_executions AS
SELECT
    e.id_execution,
    e.horodate_debut,
    e.horodate_fin,
    e.statut,
    round(EXTRACT(EPOCH FROM (e.horodate_fin - e.horodate_debut))::numeric, 1)
                                                               AS duree_totale_sec,
    et.nom_etape,
    et.phase,
    et.statut                                                  AS statut_etape,
    et.lignes_inserees,
    et.lignes_rejetees,
    round(et.duree_ms / 1000.0, 1)                             AS duree_etape_sec
FROM meta.execution e
LEFT JOIN meta.etape et ON et.id_execution = e.id_execution;

COMMENT ON VIEW restitution.v_kpi_globaux IS
    'Indicateurs de synthèse. Tous les ratios y sont recalculés (jamais stockés).';
