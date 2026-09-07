"""Collecteur GTFS-RT — le producteur *réel* de l'export d'exploitation.

Il interroge le flux temps réel du réseau STAR et écrit dans **exactement le
même format** que le simulateur (:mod:`mobilite.realisations`). Le pipeline en
aval ne sait pas — et n'a pas à savoir — lequel des deux a produit le fichier.

C'est le motif **producteur / consommateur découplé** : le contrat entre les deux
est le format de fichier, pas le code. Conséquences concrètes :

  * on démontre tout le pipeline aujourd'hui avec des données simulées ;
  * on bascule sur du réel en changeant l'ordonnancement, sans toucher à l'ETL ;
  * on peut faire tourner les deux et comparer.

------------------------------------------------------------------------------
CE QU'EST GTFS-RT
------------------------------------------------------------------------------
GTFS-RT (*realtime*) est le pendant temps réel du GTFS statique. Ce n'est ni du
JSON ni du CSV : c'est du **Protocol Buffers**, un format binaire de Google.
Plus compact et plus rapide à décoder que du texte, mais illisible sans le
schéma — d'où la dépendance ``gtfs-realtime-bindings``, qui embarque le schéma
officiel compilé.

Trois types de messages existent ; on n'utilise ici que le premier :
  * ``TripUpdate``  — retards prévus/observés par course et par arrêt ;
  * ``VehiclePosition`` — position GPS des véhicules ;
  * ``Alert``       — perturbations déclarées (travaux, manifestation).

------------------------------------------------------------------------------
LIMITE STRUCTURELLE, ET POURQUOI LE SIMULATEUR EXISTE
------------------------------------------------------------------------------
Un flux temps réel est un **instantané** : il décrit l'état du réseau à
l'instant T, et rien d'autre. Il n'existe aucun moyen de lui demander l'état
d'hier. Constituer un historique impose donc d'interroger le flux en continu et
d'accumuler — plusieurs semaines avant d'avoir de quoi remplir un entrepôt.

Ce module s'exécute donc en boucle, typiquement toutes les 60 secondes, sous un
ordonnanceur (tâche planifiée Windows, cron, ou un simple service).
"""

from __future__ import annotations

import csv
import datetime as dt
import time
from pathlib import Path
from typing import Any

import requests

from .config import Configuration
from .journal import obtenir_journal
from .realisations import COLONNES_EXPORT, formater_heure

LOG = obtenir_journal("collecteur_rt")

# Un flux GTFS-RT est republié toutes les 30 à 60 s. Interroger plus vite ne
# rapporte rien et fait peser une charge inutile sur le fournisseur.
INTERVALLE_INTERROGATION_S = 60


class ErreurCollecte(RuntimeError):
    """Le flux temps réel est injoignable ou illisible."""


def collecter_une_fois(
    configuration: Configuration,
    referentiel_arrets: dict[str, str] | None = None,
    referentiel_courses: dict[str, str] | None = None,
) -> list[list[str]]:
    """Interroge le flux une fois et renvoie les lignes au format d'export.

    Les deux référentiels (``stop_id -> nom``, ``trip_id -> code ligne``) viennent
    du GTFS statique : le flux temps réel ne transporte que des identifiants,
    jamais de libellés. C'est un choix de conception du format — il est fait pour
    être diffusé toutes les 30 secondes, donc il ne répète pas ce qui est déjà
    connu par ailleurs.
    """
    try:
        from google.transit import gtfs_realtime_pb2
    except ImportError as exc:  # pragma: no cover
        raise ErreurCollecte(
            "Le paquet gtfs-realtime-bindings est absent. "
            "Installez-le : pip install gtfs-realtime-bindings"
        ) from exc

    if not configuration.url_gtfs_rt_trip_update:
        raise ErreurCollecte("URL_GTFS_RT_TRIP_UPDATE n'est pas renseignée dans .env")

    referentiel_arrets = referentiel_arrets or {}
    referentiel_courses = referentiel_courses or {}

    try:
        reponse = requests.get(
            configuration.url_gtfs_rt_trip_update,
            timeout=45,
            headers={"User-Agent": "mobilite-etl/0.1 (projet pédagogique)"},
        )
        reponse.raise_for_status()
    except requests.RequestException as exc:
        raise ErreurCollecte(f"Flux GTFS-RT injoignable : {exc}") from exc

    flux = gtfs_realtime_pb2.FeedMessage()
    try:
        flux.ParseFromString(reponse.content)
    except Exception as exc:  # protobuf lève des exceptions non typées
        raise ErreurCollecte(f"Charge GTFS-RT indécodable : {exc}") from exc

    lignes: list[list[str]] = []
    for entite in flux.entity:
        if not entite.HasField("trip_update"):
            continue
        mise_a_jour = entite.trip_update
        id_course = mise_a_jour.trip.trip_id

        code_ligne = referentiel_courses.get(id_course, mise_a_jour.trip.route_id or "")
        # Le filtrage sur les lignes retenues se fait ici, au plus tôt : inutile
        # de porter dans tout le pipeline des données qu'on écartera à la fin.
        if configuration.lignes_retenues and code_ligne not in configuration.lignes_retenues:
            continue

        # start_date est au format AAAAMMJJ. Absent, on retombe sur aujourd'hui —
        # approximation acceptable puisqu'on interrogera de nouveau dans 60 s.
        date_service = _lire_date_service(mise_a_jour.trip.start_date)

        for etape in mise_a_jour.stop_time_update:
            evenement = etape.departure if etape.HasField("departure") else etape.arrival
            if evenement is None or not (evenement.delay or evenement.time):
                continue

            retard = int(evenement.delay or 0)
            # `time` est un horodatage POSIX absolu ; `delay` un écart en secondes.
            # Les deux sont optionnels et l'un peut manquer : on reconstruit.
            if evenement.time:
                horodate_reelle = dt.datetime.fromtimestamp(evenement.time)
                seconde_reelle = (
                    horodate_reelle.hour * 3600
                    + horodate_reelle.minute * 60
                    + horodate_reelle.second
                )
                seconde_theorique = seconde_reelle - retard
            else:
                continue  # sans horodatage absolu, la ligne n'est pas exploitable

            annulee = (
                etape.schedule_relationship
                == gtfs_realtime_pb2.TripUpdate.StopTimeUpdate.SKIPPED
            )

            lignes.append([
                date_service.strftime("%d/%m/%Y"),
                code_ligne,
                id_course,
                str(etape.stop_sequence),
                etape.stop_id,
                referentiel_arrets.get(etape.stop_id, ""),
                formater_heure(seconde_theorique),
                "" if annulee else formater_heure(seconde_reelle),
                "SUPPRIME" if annulee else "REALISE",
                "" if annulee else str(retard),
                "",  # vitesse : non fournie par le flux TripUpdate
            ])

    LOG.info("Collecte GTFS-RT : %d passages observés", len(lignes))
    return lignes


def _lire_date_service(brut: str) -> dt.date:
    """Lit ``start_date`` (AAAAMMJJ). Retombe sur aujourd'hui si absent ou illisible."""
    if brut:
        try:
            return dt.datetime.strptime(brut, "%Y%m%d").date()
        except ValueError:
            LOG.warning("start_date illisible dans le flux : %r", brut)
    return dt.date.today()


def collecter_en_continu(
    configuration: Configuration,
    duree_max_s: int | None = None,
    intervalle_s: int = INTERVALLE_INTERROGATION_S,
) -> Path:
    """Interroge le flux en boucle et accumule dans l'export du jour.

    Le fichier est ouvert en **ajout** et vidé (``flush``) après chaque collecte :
    si le processus est tué, tout ce qui a été collecté avant reste sur le
    disque. Un collecteur qui garderait tout en mémoire jusqu'à la fin perdrait
    la journée entière au premier incident.

    Le dédoublonnage n'est **pas** fait ici, volontairement : le même passage
    sera collecté plusieurs fois, avec une estimation qui s'affine à mesure que
    le véhicule approche. C'est le pipeline qui tranchera en phase 5, avec une
    règle explicite (garder la dernière observation). Un collecteur doit
    collecter, pas décider.
    """
    referentiel_arrets, referentiel_courses = _charger_referentiels(configuration)

    jour = dt.date.today()
    chemin = configuration.repertoire_brut / (
        f"realisations_{configuration.code_reseau}_rt_{jour:%Y%m%d}.csv"
    )
    nouveau = not chemin.exists()

    debut = time.monotonic()
    total = 0
    with chemin.open("a", encoding="latin-1", errors="replace", newline="") as flux:
        graveur = csv.writer(flux, delimiter=";", lineterminator="\r\n")
        if nouveau:
            graveur.writerow(COLONNES_EXPORT)

        while True:
            try:
                lignes = collecter_une_fois(
                    configuration, referentiel_arrets, referentiel_courses
                )
                graveur.writerows(lignes)
                flux.flush()
                total += len(lignes)
            except ErreurCollecte as exc:
                # Une collecte ratée n'arrête pas le collecteur : le flux sera
                # de nouveau disponible dans 60 s. On journalise et on continue.
                LOG.warning("Collecte ignorée : %s", exc)

            if duree_max_s is not None and time.monotonic() - debut >= duree_max_s:
                break
            time.sleep(intervalle_s)

    LOG.info("Collecte terminée : %d lignes cumulées dans %s", total, chemin.name)
    return chemin


def _charger_referentiels(
    configuration: Configuration,
) -> tuple[dict[str, str], dict[str, str]]:
    """Charge ``stop_id -> nom`` et ``trip_id -> code ligne`` depuis le GTFS statique."""
    repertoire = configuration.repertoire_travail / "gtfs_en_cours"
    if not repertoire.exists():
        LOG.warning("GTFS statique absent : les libellés d'arrêt seront vides.")
        return {}, {}

    def lire(nom: str):
        with (repertoire / nom).open(encoding="utf-8-sig", newline="") as flux:
            yield from csv.DictReader(flux)

    arrets = {s["stop_id"]: s.get("stop_name", "") for s in lire("stops.txt")}
    routes = {r["route_id"]: r["route_short_name"] for r in lire("routes.txt")}
    courses = {c["trip_id"]: routes.get(c["route_id"], "") for c in lire("trips.txt")}
    return arrets, courses


def _point_entree() -> None:  # pragma: no cover
    """Point d'entrée pour l'ordonnanceur : ``python -m mobilite.collecteur_rt``."""
    from .config import charger_configuration
    from .journal import configurer_journal

    configurer_journal()
    collecter_en_continu(charger_configuration())


if __name__ == "__main__":  # pragma: no cover
    _point_entree()
