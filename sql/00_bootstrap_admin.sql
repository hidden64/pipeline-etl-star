-- =============================================================================
--  BOOTSTRAP : à exécuter UNE SEULE FOIS, en superutilisateur (postgres)
-- =============================================================================
--  C'est le seul script du projet qui exige des droits d'administration.
--  Tout le reste tourne sous le rôle applicatif `mobilite_etl`.
--
--  Principe du MOINDRE PRIVILÈGE : un pipeline ETL n'a aucune raison d'être
--  superutilisateur. S'il l'était :
--    - une erreur de script pourrait détruire d'autres bases de l'instance ;
--    - une injection SQL deviendrait une compromission totale du serveur ;
--    - aucune trace ne distinguerait une action du pipeline d'une action humaine.
--
--  Usage :
--    psql -U postgres -h localhost -d postgres -f sql/00_bootstrap_admin.sql
-- =============================================================================

-- -----------------------------------------------------------------------------
--  Rôle applicatif
-- -----------------------------------------------------------------------------
--  NOSUPERUSER / NOCREATEROLE / NOCREATEDB sont explicites plutôt qu'implicites :
--  ce sont les valeurs par défaut, mais les écrire documente l'intention et
--  résiste à un changement de défaut d'une version à l'autre.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mobilite_etl') THEN
        CREATE ROLE mobilite_etl
            LOGIN
            NOSUPERUSER
            NOCREATEDB
            NOCREATEROLE
            NOINHERIT
            PASSWORD 'etl_mobilite_2026';
        RAISE NOTICE 'Rôle mobilite_etl créé.';
    ELSE
        RAISE NOTICE 'Rôle mobilite_etl déjà présent.';
    END IF;
END
$$;

-- -----------------------------------------------------------------------------
--  Base de données
-- -----------------------------------------------------------------------------
--  CREATE DATABASE ne peut pas figurer dans un bloc DO : il n'est pas
--  transactionnel. On passe donc par \gexec, qui exécute le texte produit par
--  la requête précédente : uniquement si elle renvoie une ligne.
SELECT format(
    'CREATE DATABASE mobilite OWNER mobilite_etl ENCODING ''UTF8'' '
    'LC_COLLATE ''French_France.1252'' LC_CTYPE ''French_France.1252'' '
    'TEMPLATE template0'
)
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'mobilite')
\gexec

-- -----------------------------------------------------------------------------
--  Durcissement du schéma public
-- -----------------------------------------------------------------------------
--  Depuis PostgreSQL 15, `public` n'est plus ouvert en écriture à tous. On
--  révoque quand même explicitement : le projet n'y place rien, tout vit dans
--  ses propres schémas (staging, entrepot, rejet, meta, restitution).
\connect mobilite
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO mobilite_etl;

COMMENT ON DATABASE mobilite IS
    'Entrepôt décisionnel : ponctualité du réseau STAR (Rennes) croisée avec la météo.';
