"""Source 3 — les passages réellement observés.

Le pipeline consomme un **export d'exploitation** : un CSV plat, tel qu'en
produisent les systèmes d'aide à l'exploitation (SAE) des réseaux de transport.
Deux producteurs écrivent dans ce même format, et le pipeline ne fait pas la
différence entre eux :

  * ``generer_realisations`` (ce module) — un **simulateur** qui fabrique un
    historique complet, calibré, et volontairement SALE ;
  * ``collecteur_rt.py`` — un **collecteur** qui interroge le vrai flux GTFS-RT
    du réseau STAR et écrit du réel, au fil de l'eau.

Pourquoi les deux ? Parce que le temps réel ne donne qu'un instantané : on ne
peut pas remonter dans le passé. Sans simulateur, il faudrait attendre des
semaines de collecte avant d'avoir de quoi remplir un entrepôt. Le découplage
producteur / consommateur est ce qui permet de démontrer tout le pipeline
aujourd'hui, puis de brancher le réel sans changer une ligne d'ETL.

------------------------------------------------------------------------------
POURQUOI SALIR LES DONNÉES VOLONTAIREMENT
------------------------------------------------------------------------------
Un pipeline qui n'a jamais vu de données sales n'a rien prouvé. Les anomalies
injectées ici ne sont pas aléatoires : ce sont celles qu'on rencontre vraiment
dans les exports de systèmes métier, listées dans docs/01-modele-dimensionnel.md.
Chacune a une contre-mesure identifiée en phase 4.

Le fichier produit est donc :
  * encodé en **latin-1** (et pas UTF-8), comme la plupart des exports Windows ;
  * séparé par des **points-virgules**, avec des fins de ligne **CRLF** ;
  * daté au **format français** ``JJ/MM/AAAA`` ;
  * porteur de **décimales à virgule** (``12,4``) ;
  * pollué de doublons, d'arrêts orphelins, de valeurs aberrantes et manquantes.

Le tout de façon **déterministe** : la graine du générateur pseudo-aléatoire est
fixée, donc deux exécutions produisent le même fichier. C'est indispensable pour
qu'un test puisse affirmer « ce lot doit produire exactement 1 843 rejets ».
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import random
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .config import Configuration
from .journal import obtenir_journal

LOG = obtenir_journal("realisations")

# En-tête de l'export d'exploitation. Colonnes en majuscules sans accent :
# c'est la convention des exports d'ERP et de SAE.
COLONNES_EXPORT = (
    "DATE_EXPLOITATION",
    "LIGNE",
    "COURSE",
    "SEQUENCE",
    "CODE_ARRET",
    "LIBELLE_ARRET",
    "H_THEORIQUE",
    "H_REELLE",
    "ETAT_COURSE",
    "RETARD_SEC",
    "VITESSE_MOY_KMH",
)

JOURS_GTFS = ("monday", "tuesday", "wednesday", "thursday",
              "friday", "saturday", "sunday")


# --------------------------------------------------------------------------- #
#  Paramétrage des anomalies
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class ProfilAnomalies:
    """Taux d'injection de chaque anomalie, entre 0 et 1.

    Les externaliser plutôt que de les coder en dur permet à un test de monter
    un taux à 1.0 pour vérifier qu'une règle de validation se déclenche bien.
    """

    # --- Anomalies de qualité (le pipeline doit les détecter) ---------------
    doublon_exact: float = 0.0030          # même ligne livrée deux fois
    doublon_divergent: float = 0.0010      # même clé, mesures différentes
    arret_orphelin: float = 0.0020         # code d'arrêt absent du référentiel
    retard_aberrant: float = 0.0015        # capteur en vrac : 99999 s, -5000 s
    valeur_manquante: float = 0.0030       # champ vide
    etat_incoherent: float = 0.0010        # SUPPRIME mais retard renseigné
    espaces_parasites: float = 0.0100      # "  C1 " au lieu de "C1"
    casse_incoherente: float = 0.0080      # "realise" / "Realise" / "REALISE"

    # --- Réalité métier (ce ne sont PAS des anomalies) ----------------------
    course_supprimee: float = 0.0080       # course réellement annulée
    incident_sur_course: float = 0.0150    # incident ponctuel : +5 à 15 min


@dataclass(frozen=True, slots=True)
class _Passage:
    """Un passage théorique, tel que décrit par le GTFS."""

    id_course: str
    code_ligne: str
    rang: int
    id_arret: str
    libelle_arret: str
    seconde_theorique: int  # secondes depuis minuit du jour de service (peut dépasser 86400)


# --------------------------------------------------------------------------- #
#  Lecture du référentiel GTFS
# --------------------------------------------------------------------------- #
def _lire_csv(chemin: Path) -> Iterator[dict[str, str]]:
    """Lit un fichier GTFS en flux.

    ``utf-8-sig`` retire la marque d'ordre d'octets que portent les exports
    français : sans elle, la première colonne s'appellerait ``\\ufeffroute_id``
    et toute lecture par nom échouerait silencieusement.
    """
    with chemin.open(encoding="utf-8-sig", newline="") as flux:
        yield from csv.DictReader(flux)


def secondes_depuis_minuit(heure_gtfs: str) -> int:
    """Convertit une heure GTFS en secondes depuis minuit du jour de service.

    Le GTFS autorise explicitement les heures **supérieures à 24** :
    ``25:14:00`` signifie 1 h 14 le lendemain, mais rattaché au service de la
    veille. C'est le piège n°1 du document de conception, et il est bien réel
    ici : 40 131 des 1 857 606 lignes de ``stop_times.txt`` sont concernées,
    avec des valeurs allant jusqu'à ``31:xx``.

    On ne « corrige » donc PAS l'heure : on la conserve telle quelle en secondes,
    et c'est la conversion en horodatage absolu (phase 4) qui ajoutera le bon
    nombre de jours.
    """
    heures, minutes, secondes = heure_gtfs.split(":")
    return int(heures) * 3600 + int(minutes) * 60 + int(secondes)


def formater_heure(secondes: int) -> str:
    """Inverse de :func:`secondes_depuis_minuit`, en conservant la convention GTFS."""
    if secondes < 0:  # un passage en avance sur le premier arrêt de la journée
        secondes += 86_400
    return f"{secondes // 3600:02d}:{(secondes % 3600) // 60:02d}:{secondes % 60:02d}"


def calendrier_actif(
    repertoire_gtfs: Path, debut: dt.date, fin: dt.date
) -> dict[dt.date, set[str]]:
    """Construit ``{date: {service_id actifs}}`` à partir de calendar + calendar_dates.

    Deux fichiers se combinent, et l'ordre compte :

    1. ``calendar.txt`` donne un motif hebdomadaire sur une plage de dates ;
    2. ``calendar_dates.txt`` apporte des **exceptions** :
       ``exception_type = 1`` ajoute un service ce jour-là (jour férié traité en
       dimanche), ``exception_type = 2`` le retire.

    Les exceptions sont appliquées **après** le motif hebdomadaire : c'est la
    règle de la spécification GTFS. L'inverse produirait des jours de service
    faux, typiquement autour des fêtes.
    """
    actifs: dict[dt.date, set[str]] = defaultdict(set)

    for service in _lire_csv(repertoire_gtfs / "calendar.txt"):
        depart = dt.datetime.strptime(service["start_date"], "%Y%m%d").date()
        arrivee = dt.datetime.strptime(service["end_date"], "%Y%m%d").date()
        jour = max(depart, debut)
        borne = min(arrivee, fin)
        while jour <= borne:
            if service[JOURS_GTFS[jour.weekday()]] == "1":
                actifs[jour].add(service["service_id"])
            jour += dt.timedelta(days=1)

    fichier_exceptions = repertoire_gtfs / "calendar_dates.txt"
    if fichier_exceptions.exists():
        for exception in _lire_csv(fichier_exceptions):
            jour = dt.datetime.strptime(exception["date"], "%Y%m%d").date()
            if not (debut <= jour <= fin):
                continue
            if exception["exception_type"] == "1":
                actifs[jour].add(exception["service_id"])
            else:
                actifs[jour].discard(exception["service_id"])

    return dict(actifs)


def charger_referentiel(
    repertoire_gtfs: Path, lignes_retenues: list[str]
) -> tuple[dict[str, list[_Passage]], dict[str, list[str]]]:
    """Charge les passages théoriques des lignes retenues.

    Renvoie ``({service_id: [courses]}, {id_course: [passages ordonnés]})``
    — en fait l'inverse, voir le type de retour : d'abord les passages par
    course, puis les courses par service.
    """
    codes_lignes = set(lignes_retenues)

    routes = {
        r["route_id"]: r["route_short_name"]
        for r in _lire_csv(repertoire_gtfs / "routes.txt")
        if not codes_lignes or r["route_short_name"] in codes_lignes
    }

    arrets = {
        s["stop_id"]: s.get("stop_name", "")
        for s in _lire_csv(repertoire_gtfs / "stops.txt")
    }

    courses: dict[str, tuple[str, str]] = {}      # id_course -> (service_id, code_ligne)
    for course in _lire_csv(repertoire_gtfs / "trips.txt"):
        if course["route_id"] in routes:
            courses[course["trip_id"]] = (course["service_id"], routes[course["route_id"]])

    # stop_times.txt fait 112 Mo : on le lit une seule fois, en flux, en ne
    # retenant que les courses qui nous intéressent. Le relire une fois par
    # journée de service coûterait 39 lectures de 112 Mo.
    passages: dict[str, list[_Passage]] = defaultdict(list)
    for horaire in _lire_csv(repertoire_gtfs / "stop_times.txt"):
        reference = courses.get(horaire["trip_id"])
        if reference is None:
            continue
        _, code_ligne = reference
        passages[horaire["trip_id"]].append(
            _Passage(
                id_course=horaire["trip_id"],
                code_ligne=code_ligne,
                rang=int(horaire["stop_sequence"]),
                id_arret=horaire["stop_id"],
                libelle_arret=arrets.get(horaire["stop_id"], ""),
                seconde_theorique=secondes_depuis_minuit(
                    horaire["departure_time"] or horaire["arrival_time"]
                ),
            )
        )

    # Le GTFS ne garantit pas l'ordre des lignes de stop_times.txt : on trie.
    for liste in passages.values():
        liste.sort(key=lambda p: p.rang)

    courses_par_service: dict[str, list[str]] = defaultdict(list)
    for id_course, (service_id, _) in courses.items():
        if id_course in passages:
            courses_par_service[service_id].append(id_course)

    LOG.info(
        "Référentiel : %d lignes, %d courses, %d passages théoriques",
        len(routes), len(passages), sum(len(v) for v in passages.values()),
    )
    return passages, dict(courses_par_service)


def charger_meteo(chemin_json: Path) -> dict[tuple[dt.date, int], dict]:
    """Indexe les relevés météo par ``(date, heure)``.

    Le simulateur s'en sert pour que la corrélation météo / retard **existe
    réellement** dans les données produites. Sans cela, l'analyse finale de
    l'entrepôt ne trouverait rien, et on ne saurait pas distinguer « le pipeline
    est faux » de « il n'y a pas de corrélation ».
    """
    if not chemin_json.exists():
        LOG.warning("Aucun fichier météo : les retards seront générés sans effet météo.")
        return {}

    contenu = json.loads(chemin_json.read_text(encoding="utf-8"))
    index: dict[tuple[dt.date, int], dict] = {}
    for releve in contenu["releves"]:
        horodate = dt.datetime.fromisoformat(releve["horodate"])
        index[(horodate.date(), horodate.hour)] = releve
    LOG.info("Météo : %d relevés horaires indexés", len(index))
    return index


# --------------------------------------------------------------------------- #
#  Modèle de retard
# --------------------------------------------------------------------------- #
def _increment_retard(
    alea: random.Random,
    heure: int,
    est_weekend: bool,
    meteo: dict | None,
) -> float:
    """Retard supplémentaire accumulé entre deux arrêts consécutifs, en secondes.

    Le modèle est **cumulatif le long de la course** : un bus pris dans le
    trafic ne rattrape pas son retard, il l'aggrave d'arrêt en arrêt. C'est le
    comportement réel, et c'est ce qui rend l'analyse « par rang d'arrêt »
    intéressante.

    Trois facteurs, dans l'ordre d'importance observé sur les réseaux urbains :
    l'heure de pointe, la pluie, le vent.
    """
    increment = alea.gauss(2.0, 7.0)

    if 7 <= heure <= 9 or 16 <= heure <= 19:
        increment += alea.gauss(6.0, 6.0)
    elif heure <= 5 or heure >= 22:
        increment *= 0.4                       # réseau fluide la nuit

    if est_weekend:
        increment *= 0.6

    if meteo:
        pluie = meteo.get("precipitation_mm") or 0.0
        vent = meteo.get("vent_kmh") or 0.0
        if pluie >= 2.0:
            increment += alea.gauss(11.0, 7.0)
        elif pluie >= 0.3:
            increment += alea.gauss(4.0, 4.0)
        if vent >= 45.0:
            increment += alea.gauss(6.0, 5.0)

    return increment


# --------------------------------------------------------------------------- #
#  Génération
# --------------------------------------------------------------------------- #
def generer_realisations(
    configuration: Configuration,
    *,
    repertoire_gtfs: Path | None = None,
    chemin_meteo: Path | None = None,
    profil: ProfilAnomalies | None = None,
    graine: int = 20260904,
) -> Path:
    """Produit l'export d'exploitation sale et renvoie son chemin.

    Écriture en flux : le fichier fait plusieurs centaines de mégaoctets, il
    n'est jamais construit en mémoire.
    """
    profil = profil or ProfilAnomalies()
    repertoire_gtfs = repertoire_gtfs or (configuration.repertoire_travail / "gtfs_en_cours")
    chemin_meteo = chemin_meteo or next(
        iter(sorted(configuration.repertoire_brut.glob("meteo_rennes_*.json"))), None
    )

    alea = random.Random(graine)  # déterminisme : même graine => même fichier
    passages_par_course, courses_par_service = charger_referentiel(
        repertoire_gtfs, configuration.lignes_retenues
    )
    calendrier = calendrier_actif(
        repertoire_gtfs, configuration.periode_debut, configuration.periode_fin
    )
    meteo = charger_meteo(chemin_meteo) if chemin_meteo else {}

    nom_sortie = (
        f"realisations_{configuration.code_reseau}_"
        f"{configuration.periode_debut:%Y%m%d}_{configuration.periode_fin:%Y%m%d}.csv"
    )
    chemin_sortie = configuration.repertoire_brut / nom_sortie

    compteurs: dict[str, int] = defaultdict(int)

    # errors="replace" : un caractère absent du jeu latin-1 ne doit pas faire
    # échouer l'écriture. C'est exactement ce que font les exports réels — et
    # c'est pour cela qu'on retrouve des « ? » dans les libellés en production.
    with chemin_sortie.open(
        "w", encoding="latin-1", errors="replace", newline=""
    ) as flux:
        graveur = csv.writer(flux, delimiter=";", lineterminator="\r\n")
        graveur.writerow(COLONNES_EXPORT)

        for jour in sorted(calendrier):
            est_weekend = jour.weekday() >= 5
            services = calendrier[jour]
            for service in services:
                for id_course in courses_par_service.get(service, ()):
                    lignes = _generer_course(
                        alea, profil, jour, est_weekend,
                        passages_par_course[id_course], meteo, compteurs,
                    )
                    graveur.writerows(lignes)
                    compteurs["lignes_ecrites"] += len(lignes)
            LOG.info("  %s : %d lignes cumulées", jour, compteurs["lignes_ecrites"])

    taille_mo = chemin_sortie.stat().st_size / 1024 / 1024
    LOG.info("Export généré : %s (%.1f Mo)", nom_sortie, taille_mo)
    LOG.info("Anomalies injectées : %s", dict(sorted(compteurs.items())))
    return chemin_sortie


def _generer_course(
    alea: random.Random,
    profil: ProfilAnomalies,
    jour: dt.date,
    est_weekend: bool,
    passages: list[_Passage],
    meteo: dict[tuple[dt.date, int], dict],
    compteurs: dict[str, int],
) -> list[list[str]]:
    """Génère les lignes d'export d'une course, pour une date de service."""
    date_fr = jour.strftime("%d/%m/%Y")  # format français : anomalie n°7

    # Une course entièrement supprimée : réalité métier, pas une anomalie.
    course_supprimee = alea.random() < profil.course_supprimee
    if course_supprimee:
        compteurs["courses_supprimees"] += 1

    # Incident ponctuel : à partir d'un arrêt donné, la course prend 5 à 15 min.
    incident_a_partir_du_rang = None
    if not course_supprimee and alea.random() < profil.incident_sur_course:
        incident_a_partir_du_rang = alea.randrange(len(passages))
        compteurs["incidents"] += 1

    lignes: list[list[str]] = []
    retard_cumule = alea.gauss(0.0, 20.0)  # écart au départ du dépôt

    for index, passage in enumerate(passages):
        heure_du_jour = (passage.seconde_theorique // 3600) % 24
        conditions = meteo.get((jour, heure_du_jour))

        retard_cumule += _increment_retard(alea, heure_du_jour, est_weekend, conditions)
        if incident_a_partir_du_rang is not None and index == incident_a_partir_du_rang:
            retard_cumule += alea.uniform(300, 900)
        # Un véhicule ne prend jamais plus de 90 s d'avance : il attend à l'arrêt.
        retard_cumule = max(retard_cumule, -90.0)

        retard = int(round(retard_cumule))
        etat = "SUPPRIME" if course_supprimee else "REALISE"
        heure_reelle = "" if course_supprimee else formater_heure(
            passage.seconde_theorique + retard
        )
        retard_texte = "" if course_supprimee else str(retard)

        code_arret = passage.id_arret
        libelle = passage.libelle_arret
        ligne_code = passage.code_ligne
        # Décimale à virgule : anomalie n°7 bis, très fréquente en export FR.
        vitesse = f"{alea.uniform(14.0, 32.0):.1f}".replace(".", ",")

        # ---- Injection des anomalies ---------------------------------------
        if alea.random() < profil.arret_orphelin:
            code_arret = f"7-9{alea.randrange(1000, 9999)}"  # absent du référentiel
            compteurs["arrets_orphelins"] += 1

        if not course_supprimee and alea.random() < profil.retard_aberrant:
            retard_texte = alea.choice(["99999", "-5000", "86400", "-99999"])
            compteurs["retards_aberrants"] += 1

        if alea.random() < profil.valeur_manquante:
            champ = alea.choice(["libelle", "vitesse", "heure_reelle"])
            if champ == "libelle":
                libelle = ""
            elif champ == "vitesse":
                vitesse = ""
            elif not course_supprimee:
                heure_reelle = ""
            compteurs["valeurs_manquantes"] += 1

        if course_supprimee and alea.random() < profil.etat_incoherent:
            # Contradiction : la course est annulée mais un retard est renseigné.
            retard_texte = str(alea.randrange(30, 600))
            compteurs["etats_incoherents"] += 1

        if alea.random() < profil.espaces_parasites:
            ligne_code = f"  {ligne_code} "
            compteurs["espaces_parasites"] += 1

        if alea.random() < profil.casse_incoherente:
            etat = alea.choice([etat.lower(), etat.capitalize()])
            compteurs["casses_incoherentes"] += 1

        enregistrement = [
            date_fr, ligne_code, passage.id_course, str(passage.rang),
            code_arret, libelle,
            formater_heure(passage.seconde_theorique), heure_reelle,
            etat, retard_texte, vitesse,
        ]
        lignes.append(enregistrement)

        # Doublon exact : la même ligne livrée deux fois (rejeu partiel du flux).
        if alea.random() < profil.doublon_exact:
            lignes.append(list(enregistrement))
            compteurs["doublons_exacts"] += 1
        # Doublon divergent : même clé naturelle, mesures différentes. C'est le
        # cas le plus pénible — il faut une règle explicite pour trancher.
        elif alea.random() < profil.doublon_divergent:
            variante = list(enregistrement)
            if variante[9]:
                variante[9] = str(int(variante[9]) + alea.randrange(-60, 60))
            lignes.append(variante)
            compteurs["doublons_divergents"] += 1

    return lignes
