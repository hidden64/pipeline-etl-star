"""Orchestrateur bout-en-bout du pipeline.

Enchaîne les étapes dans l'ordre, sous une seule exécution journalisée :

    extraction → chargement → validation → dimensions → faits

Chaque étape a déjà sa propre journalisation fine (``meta.etape``). Ce module
assure la cohérence d'ensemble : une exécution, un statut global, et une
politique claire en cas d'échec — l'exécution est marquée ``ECHEC`` et l'erreur
est journalisée, mais les étapes déjà validées le restent (autocommit), ce qui
permet une reprise sans tout recommencer.
"""

from __future__ import annotations

import argparse
import sys
import time

from .base import (
    appliquer_migrations,
    connexion,
    demarrer_execution,
    enregistrer_source_fichier,
    terminer_execution,
)
from .chargement import executer_chargement
from .config import Configuration, charger_configuration
from .dashboard import exporter_json, generer_dashboard
from .dimensions import charger_dimensions
from .extraction import extraire_gtfs, extraire_meteo
from .faits import charger_faits
from .journal import configurer_journal, obtenir_journal
from .rapport import generer_rapport
from .realisations import generer_realisations
from .validation import valider_realisations

LOG = obtenir_journal("pipeline")


def executer_pipeline(
    configuration: Configuration,
    *,
    extraire: bool = True,
    generer: bool = True,
    reinitialiser: bool = False,
) -> int:
    """Exécute le pipeline complet. Renvoie le code de sortie (0 = succès).

    Paramètres :
      extraire      télécharge GTFS et météo (désactivable pour rejouer hors ligne) ;
      generer       (re)génère l'export d'exploitation simulé ;
      reinitialiser vide faits et dimensions avant de recharger (rejeu propre).
    """
    debut = time.monotonic()
    appliquer_migrations(configuration)

    with connexion(configuration, autocommit=True) as conn:
        if reinitialiser:
            LOG.info("Réinitialisation de l'entrepôt demandée.")
            with conn.cursor() as cur:
                cur.execute("CALL staging.reinitialiser_entrepot()")

        id_execution = demarrer_execution(conn, {
            "periode_debut": configuration.periode_debut.isoformat(),
            "periode_fin": configuration.periode_fin.isoformat(),
            "lignes": configuration.lignes_retenues,
            "reinitialisation": reinitialiser,
        })

        try:
            # --- Extraction ---------------------------------------------------
            if extraire:
                descr_gtfs, repertoire_gtfs = extraire_gtfs(configuration)
                descr_meteo = extraire_meteo(configuration)
            else:
                repertoire_gtfs = configuration.repertoire_travail / "gtfs_en_cours"
                descr_gtfs = descr_meteo = None

            if generer:
                generer_realisations(configuration)

            # --- Chargement ---------------------------------------------------
            executer_chargement(conn, configuration, id_execution,
                                repertoire_gtfs=repertoire_gtfs)

            # --- Transformation ----------------------------------------------
            valider_realisations(conn, configuration, id_execution)
            charger_dimensions(conn, id_execution)
            charger_faits(conn, id_execution)

            terminer_execution(conn, id_execution, "SUCCES")

            # Le rapport se génère APRÈS la clôture de l'exécution, pour que sa
            # durée totale y figure. Un échec de génération ne doit pas faire
            # échouer un pipeline par ailleurs réussi : on le tolère et on avertit.
            try:
                chemin = generer_rapport(conn, configuration, id_execution)
                LOG.info("Rapport disponible : %s", chemin)
                exporter_json(conn, configuration, id_execution)
                chemin_db = generer_dashboard(conn, configuration, id_execution)
                LOG.info("Tableau de bord disponible : %s", chemin_db)
            except Exception as exc:  # noqa: BLE001
                LOG.warning("Pipeline réussi mais restitution non générée : %s", exc)

            LOG.info("Pipeline terminé en %.1f s.", time.monotonic() - debut)
            return 0

        except Exception as exc:  # noqa: BLE001 — on veut tout tracer
            terminer_execution(conn, id_execution, "ECHEC", str(exc)[:2000])
            LOG.exception("Pipeline interrompu : %s", exc)
            return 1


def _analyser_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    analyseur = argparse.ArgumentParser(
        prog="python -m mobilite",
        description="Pipeline ETL — ponctualité STAR croisée avec la météo.",
    )
    analyseur.add_argument("--sans-extraction", action="store_true",
                           help="ne pas retélécharger GTFS/météo (rejeu hors ligne)")
    analyseur.add_argument("--sans-generation", action="store_true",
                           help="ne pas régénérer l'export d'exploitation simulé")
    analyseur.add_argument("--reinitialiser", action="store_true",
                           help="vider faits et dimensions avant de recharger")
    return analyseur.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    configurer_journal()
    arguments = _analyser_arguments(argv)
    configuration = charger_configuration()
    return executer_pipeline(
        configuration,
        extraire=not arguments.sans_extraction,
        generer=not arguments.sans_generation,
        reinitialiser=arguments.reinitialiser,
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
