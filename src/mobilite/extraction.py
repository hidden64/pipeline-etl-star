"""Étape E du pipeline : extraction des sources hétérogènes.

Trois sources, trois formats, trois protocoles différents — c'est tout l'intérêt
pédagogique du sujet :

  1. **GTFS statique** — archive ZIP contenant des CSV, servie en HTTP.
     Référentiel (lignes, arrêts) + horaires théoriques.
  2. **Météo horaire** — API REST JSON (Open-Meteo, sans clé d'API).
     Deux points d'entrée distincts selon l'ancienneté de la date demandée.
  3. **Réalisations d'exploitation** — CSV « sale » produit par un système tiers
     (cf. `realisations.py`). Encodage latin-1, dates au format français,
     doublons, valeurs aberrantes.

Règles appliquées ici, valables pour toute extraction :

* **On ne transforme rien.** L'extraction dépose l'octet reçu sur le disque, tel
  quel. Toute normalisation appartient à la phase de transformation. Sinon on
  perd la capacité de rejouer un traitement sans retélécharger.
* **On calcule une empreinte SHA-256 de chaque fichier.** Elle sert à
  l'idempotence (ne pas recharger deux fois le même contenu) et à l'audit
  (« d'où vient cette ligne ? »).
* **On réessaie avec temporisation exponentielle.** Un réseau échoue ; un
  pipeline qui abandonne à la première erreur réveille l'astreinte pour rien.
"""

from __future__ import annotations

import hashlib
import json
import time
import zipfile
from dataclasses import dataclass, asdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import requests

from .config import Configuration
from .journal import obtenir_journal

LOG = obtenir_journal("extraction")

# Nombre de jours de recul à partir duquel l'API d'archive Open-Meteo dispose
# des données consolidées. En deçà, il faut interroger le point d'entrée de
# prévision, qui expose aussi le passé récent via `past_days`.
DELAI_CONSOLIDATION_ARCHIVE_JOURS = 6

# Horizon maximal de prévision offert par Open-Meteo. Au-delà, AUCUN relevé
# n'existe : les faits de ces dates seront rattachés au membre inconnu de
# dim_meteo, et le rapport de qualité le signalera. Les exécutions quotidiennes
# successives comblent progressivement le trou.
HORIZON_PREVISION_JOURS = 16

# Variables horaires demandées à Open-Meteo. L'ordre n'a pas d'importance :
# la réponse est un objet nommé.
VARIABLES_METEO = ("temperature_2m", "precipitation", "wind_speed_10m", "weather_code")

FICHIERS_GTFS_ATTENDUS = (
    "agency.txt",
    "routes.txt",
    "trips.txt",
    "stops.txt",
    "stop_times.txt",
    "calendar.txt",
)


class ErreurExtraction(RuntimeError):
    """Échec d'extraction après épuisement des tentatives."""


# --------------------------------------------------------------------------- #
#  Descripteur de fichier source
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class SourceFichier:
    """Métadonnées d'un fichier ingéré. Alimente ``meta.source_fichier``."""

    code_source: str
    nom_fichier: str
    chemin_local: str
    url_origine: str | None
    taille_octets: int
    hash_sha256: str
    version_source: str | None = None

    def vers_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- #
#  Utilitaires
# --------------------------------------------------------------------------- #
def empreinte_sha256(chemin: Path, taille_bloc: int = 1 << 20) -> str:
    """Empreinte SHA-256 d'un fichier, lu par blocs de 1 Mio.

    La lecture par blocs est indispensable : ``stop_times.txt`` fait 112 Mo une
    fois décompressé, et un ``read()`` intégral chargerait tout en mémoire.
    """
    condensat = hashlib.sha256()
    with chemin.open("rb") as flux:
        while bloc := flux.read(taille_bloc):
            condensat.update(bloc)
    return condensat.hexdigest()


def telecharger(
    url: str,
    cible: Path,
    *,
    tentatives: int = 3,
    delai_initial: float = 2.0,
    delai_expiration: int = 180,
) -> Path:
    """Télécharge ``url`` vers ``cible`` en flux, avec temporisation exponentielle.

    Le téléchargement se fait en flux (``stream=True``) et écrit par morceaux :
    une archive GTFS de 12 Mo tient en mémoire, mais la même logique doit
    fonctionner sur un fichier de plusieurs gigaoctets.

    L'écriture passe par un fichier temporaire ``.partiel`` renommé à la fin.
    C'est le motif « écriture atomique » : si le processus meurt en cours de
    route, on ne laisse jamais un fichier tronqué que la suite du pipeline
    prendrait pour un téléchargement réussi.
    """
    cible.parent.mkdir(parents=True, exist_ok=True)
    fichier_partiel = cible.with_suffix(cible.suffix + ".partiel")

    derniere_erreur: Exception | None = None
    for tentative in range(1, tentatives + 1):
        try:
            LOG.info("Téléchargement (%d/%d) : %s", tentative, tentatives, url)
            with requests.get(
                url,
                stream=True,
                timeout=delai_expiration,
                headers={"User-Agent": "mobilite-etl/0.1 (projet pédagogique)"},
            ) as reponse:
                reponse.raise_for_status()
                octets = 0
                with fichier_partiel.open("wb") as sortie:
                    for morceau in reponse.iter_content(chunk_size=1 << 16):
                        sortie.write(morceau)
                        octets += len(morceau)

            fichier_partiel.replace(cible)  # atomique sur le même volume
            LOG.info("Reçu %.2f Mo -> %s", octets / 1024 / 1024, cible.name)
            return cible

        except (requests.RequestException, OSError) as exc:
            derniere_erreur = exc
            fichier_partiel.unlink(missing_ok=True)
            if tentative < tentatives:
                # Temporisation exponentielle : 2 s, puis 4 s, puis 8 s.
                # Laisse le temps à un service momentanément saturé de repartir,
                # au lieu de le marteler.
                attente = delai_initial * (2 ** (tentative - 1))
                LOG.warning("Échec (%s). Nouvelle tentative dans %.0f s.", exc, attente)
                time.sleep(attente)

    raise ErreurExtraction(
        f"Téléchargement impossible après {tentatives} tentatives : {url}"
    ) from derniere_erreur


# --------------------------------------------------------------------------- #
#  Source 1 — GTFS statique (ZIP de CSV)
# --------------------------------------------------------------------------- #
def extraire_gtfs(
    configuration: Configuration,
    *,
    url: str | None = None,
    etiquette: str = "EN_COURS",
) -> tuple[SourceFichier, Path]:
    """Télécharge une archive GTFS, la vérifie et la décompresse.

    Renvoie le descripteur du ZIP et le répertoire de décompression.

    La version du jeu de données est lue dans ``feed_info.txt`` (champ
    ``feed_version``). C'est elle qui permettra, en phase 5, de savoir laquelle
    de deux versions concurrentes est la plus récente lors du dédoublonnage.
    """
    url = url or configuration.url_gtfs
    nom_archive = f"GTFS_{configuration.code_reseau}_{etiquette}.zip"
    chemin_archive = configuration.repertoire_brut / nom_archive

    telecharger(url, chemin_archive)

    # Contrôle d'intégrité AVANT de faire confiance au contenu. Une archive
    # tronquée ou une page d'erreur HTML renvoyée avec un code 200 échouent ici,
    # au lieu de produire un message obscur trois étapes plus loin.
    if not zipfile.is_zipfile(chemin_archive):
        raise ErreurExtraction(
            f"{nom_archive} n'est pas une archive ZIP valide. "
            f"La source a probablement renvoyé une page d'erreur."
        )

    repertoire_sortie = configuration.repertoire_travail / f"gtfs_{etiquette.lower()}"
    repertoire_sortie.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(chemin_archive) as archive:
        presents = set(archive.namelist())
        manquants = [f for f in FICHIERS_GTFS_ATTENDUS if f not in presents]
        if manquants:
            raise ErreurExtraction(
                f"Fichiers GTFS obligatoires absents de {nom_archive} : {manquants}"
            )

        for membre in archive.infolist():
            # Protection contre la traversée de répertoire (« Zip Slip ») : une
            # archive malveillante peut contenir « ../../autre.txt » et écrire
            # hors du répertoire cible. On refuse tout nom non plat.
            if "/" in membre.filename or "\\" in membre.filename or membre.filename.startswith(".."):
                LOG.warning("Membre d'archive au chemin suspect, ignoré : %s", membre.filename)
                continue
            archive.extract(membre, repertoire_sortie)

        version = _lire_version_gtfs(archive)

    descripteur = SourceFichier(
        code_source="GTFS",
        nom_fichier=nom_archive,
        chemin_local=str(chemin_archive),
        url_origine=url,
        taille_octets=chemin_archive.stat().st_size,
        hash_sha256=empreinte_sha256(chemin_archive),
        version_source=version,
    )
    LOG.info(
        "GTFS %s décompressé dans %s (version %s)",
        etiquette, repertoire_sortie, version,
    )
    return descripteur, repertoire_sortie


def _lire_version_gtfs(archive: zipfile.ZipFile) -> str | None:
    """Extrait ``feed_version`` de ``feed_info.txt``, si le fichier est présent.

    ``feed_info.txt`` est optionnel dans la spécification GTFS : son absence ne
    doit pas faire échouer l'extraction.
    """
    if "feed_info.txt" not in archive.namelist():
        return None
    import csv
    import io

    with archive.open("feed_info.txt") as flux:
        # `utf-8-sig` : les exports GTFS français portent souvent une marque
        # d'ordre d'octets (BOM). Sans `-sig`, la première colonne s'appellerait
        # "﻿feed_publisher_name" et toute lecture par nom échouerait.
        lecteur = csv.DictReader(io.TextIOWrapper(flux, encoding="utf-8-sig", newline=""))
        for ligne in lecteur:
            return ligne.get("feed_version") or None
    return None


# --------------------------------------------------------------------------- #
#  Source 2 — Météo horaire (API REST JSON)
# --------------------------------------------------------------------------- #
def extraire_meteo(
    configuration: Configuration,
    debut: date | None = None,
    fin: date | None = None,
) -> SourceFichier:
    """Récupère la météo horaire de Rennes et la dépose en JSON normalisé.

    Open-Meteo expose deux points d'entrée qu'il faut savoir arbitrer :

    * ``archive-api``  — données réanalysées, consolidées, mais avec ~5 jours de
      retard. C'est la source de référence pour un historique.
    * ``api/forecast`` — avec ``past_days``, expose le passé récent et le futur,
      mais ces valeurs sont des prévisions, moins fiables.

    Quand la période demandée chevauche la frontière, on interroge les deux et
    on **réconcilie**, en donnant systématiquement la priorité à l'archive. Ce
    genre d'arbitrage entre une source lente et fiable et une source rapide et
    approximative est un cas très courant en entrepôt.
    """
    debut = debut or configuration.periode_debut
    fin = fin or configuration.periode_fin
    limite_archive = date.today() - timedelta(days=DELAI_CONSOLIDATION_ARCHIVE_JOURS)

    releves: dict[str, dict[str, Any]] = {}

    # 1. La partie ancienne, via l'archive consolidée.
    if debut <= limite_archive:
        fin_archive = min(fin, limite_archive)
        LOG.info("Météo (archive consolidée) : %s -> %s", debut, fin_archive)
        releves.update(
            _appeler_open_meteo(
                configuration.url_meteo_archive,
                configuration,
                {"start_date": debut.isoformat(), "end_date": fin_archive.isoformat()},
            )
        )

    # 2. La partie récente, via la prévision — sans jamais écraser l'archive.
    if fin > limite_archive:
        aujourdhui = date.today()
        # `past_days` est plafonné à 92 par l'API ; `forecast_days` à 16.
        recul = max(1, min(92, (aujourdhui - min(debut, limite_archive)).days + 1))
        horizon = max(1, min(HORIZON_PREVISION_JOURS, (fin - aujourdhui).days + 1))
        LOG.info(
            "Météo (passé récent / prévision) : past_days=%d, forecast_days=%d",
            recul, horizon,
        )
        recents = _appeler_open_meteo(
            configuration.url_meteo_prevision,
            configuration,
            {"past_days": str(recul), "forecast_days": str(horizon)},
        )
        for horodate, releve in recents.items():
            releves.setdefault(horodate, releve)  # l'archive reste prioritaire

        # Au-delà de l'horizon de prévision, il n'existe simplement aucune
        # donnée. On l'annonce explicitement plutôt que de laisser découvrir le
        # trou à l'étape de chargement.
        derniere_couverte = aujourdhui + timedelta(days=horizon - 1)
        if fin > derniere_couverte:
            LOG.warning(
                "Aucune météo disponible du %s au %s (%d jours au-delà de "
                "l'horizon de prévision). Les faits concernés seront rattachés "
                "au membre inconnu de dim_meteo.",
                derniere_couverte + timedelta(days=1), fin,
                (fin - derniere_couverte).days,
            )

    if not releves:
        raise ErreurExtraction(f"Aucun relevé météo obtenu pour {debut} -> {fin}.")

    # On ne conserve que la période demandée : la prévision renvoie souvent plus
    # large que ce qu'on a demandé.
    bornes = {h: r for h, r in releves.items() if debut.isoformat() <= h[:10] <= fin.isoformat()}

    nom_fichier = f"meteo_rennes_{debut:%Y%m%d}_{fin:%Y%m%d}.json"
    chemin = configuration.repertoire_brut / nom_fichier
    contenu = {
        "source": "open-meteo",
        "latitude": configuration.meteo_latitude,
        "longitude": configuration.meteo_longitude,
        "fuseau": configuration.meteo_fuseau,
        "periode_debut": debut.isoformat(),
        "periode_fin": fin.isoformat(),
        "nb_releves": len(bornes),
        # Trié : rend le fichier reproductible, donc son SHA-256 stable pour une
        # même donnée. Sans tri, deux exécutions identiques produiraient deux
        # empreintes différentes et l'idempotence tomberait à l'eau.
        "releves": [bornes[h] for h in sorted(bornes)],
    }
    chemin.write_text(json.dumps(contenu, ensure_ascii=False, indent=1), encoding="utf-8")

    LOG.info("Météo : %d relevés horaires -> %s", len(bornes), nom_fichier)
    return SourceFichier(
        code_source="METEO",
        nom_fichier=nom_fichier,
        chemin_local=str(chemin),
        url_origine=configuration.url_meteo_archive,
        taille_octets=chemin.stat().st_size,
        hash_sha256=empreinte_sha256(chemin),
        version_source=None,
    )


def _appeler_open_meteo(
    url: str,
    configuration: Configuration,
    parametres_specifiques: dict[str, str],
    *,
    tentatives: int = 3,
) -> dict[str, dict[str, Any]]:
    """Interroge Open-Meteo et renvoie ``{horodate_iso: relevé}``.

    L'API renvoie des **tableaux parallèles** (``time``, ``temperature_2m``, …)
    plutôt qu'une liste d'objets. C'est compact mais fragile : si un tableau est
    plus court que les autres, un ``zip`` silencieux tronquerait les données.
    On pivote donc explicitement, en contrôlant les longueurs.
    """
    parametres = {
        "latitude": configuration.meteo_latitude,
        "longitude": configuration.meteo_longitude,
        "hourly": ",".join(VARIABLES_METEO),
        "timezone": configuration.meteo_fuseau,
        **parametres_specifiques,
    }

    derniere_erreur: Exception | None = None
    for tentative in range(1, tentatives + 1):
        try:
            reponse = requests.get(url, params=parametres, timeout=60)
            reponse.raise_for_status()
            charge = reponse.json()
            break
        except (requests.RequestException, ValueError) as exc:
            derniere_erreur = exc
            if tentative < tentatives:
                time.sleep(2.0 * (2 ** (tentative - 1)))
    else:
        raise ErreurExtraction(f"Appel Open-Meteo impossible : {url}") from derniere_erreur

    horaire = charge.get("hourly") or {}
    horodates = horaire.get("time") or []
    if not horodates:
        return {}

    colonnes = {v: horaire.get(v) or [] for v in VARIABLES_METEO}
    for nom, valeurs in colonnes.items():
        if len(valeurs) != len(horodates):
            raise ErreurExtraction(
                f"Réponse Open-Meteo incohérente : {len(horodates)} horodatages "
                f"mais {len(valeurs)} valeurs pour {nom!r}."
            )

    return {
        horodate: {
            "horodate": horodate,                              # ISO local, ex. 2026-06-01T08:00
            "temperature_c": colonnes["temperature_2m"][i],
            "precipitation_mm": colonnes["precipitation"][i],
            "vent_kmh": colonnes["wind_speed_10m"][i],
            "code_wmo": colonnes["weather_code"][i],
        }
        for i, horodate in enumerate(horodates)
    }
