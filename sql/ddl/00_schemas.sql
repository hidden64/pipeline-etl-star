-- =============================================================================
--  00 : Schémas de l'entrepôt
-- =============================================================================
--  Chaque schéma matérialise une ZONE du pipeline. La séparation physique est
--  ce qui rend le traitement rejouable : on peut vider et recharger `staging`
--  sans jamais risquer d'abîmer `entrepot`.
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS staging;
CREATE SCHEMA IF NOT EXISTS rejet;
CREATE SCHEMA IF NOT EXISTS entrepot;
CREATE SCHEMA IF NOT EXISTS restitution;
CREATE SCHEMA IF NOT EXISTS meta;

COMMENT ON SCHEMA staging     IS 'Zone brute. Copie fidèle des sources, toutes colonnes en text. Vidée par lot.';
COMMENT ON SCHEMA rejet       IS 'Lignes refusées par les règles de qualité, avec leur motif. Jamais purgée automatiquement.';
COMMENT ON SCHEMA entrepot    IS 'Schéma en étoile : dimensions conformes + tables de faits.';
COMMENT ON SCHEMA restitution IS 'Vues et agrégats destinés à l''analyse. Aucune donnée propre, uniquement des vues.';
COMMENT ON SCHEMA meta        IS 'Journal technique : exécutions, étapes, volumétries, versions de sources.';

-- Fuseau horaire de session : tout le réseau STAR est en Europe/Paris.
-- On le fixe au niveau de la base pour que `timestamptz` s'affiche et se calcule
-- correctement, y compris lors des changements d'heure (cf. piège n°8 du doc).
ALTER DATABASE mobilite SET timezone TO 'Europe/Paris';
