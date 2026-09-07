"""Configuration du pipeline, lue depuis l'environnement (fichier ``.env``).

Principe : **aucune valeur d'environnement n'est lue ailleurs que dans ce
module.** Le reste du code reçoit un objet ``Configuration`` déjà validé.

Trois bénéfices concrets :
  - les tests injectent une configuration factice sans toucher à l'environnement ;
  - une variable manquante échoue au démarrage avec un message clair, pas trois
    étapes plus loin avec un ``NoneType`` incompréhensible ;
  - aucun secret ne se retrouve en dur dans le code.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

RACINE_PROJET = Path(__file__).resolve().parents[2]


class ErreurConfiguration(RuntimeError):
    """Configuration absente ou invalide. Levée au démarrage, jamais en cours de route."""


# --------------------------------------------------------------------------- #
#  Lecteurs typés
# --------------------------------------------------------------------------- #
def _texte(cle: str, defaut: str | None = None, *, obligatoire: bool = False) -> str:
    valeur = os.getenv(cle, defaut)
    if obligatoire and not valeur:
        raise ErreurConfiguration(
            f"Variable d'environnement obligatoire absente : {cle}. "
            f"Copiez .env.example en .env et renseignez-la."
        )
    return valeur or ""


def _entier(cle: str, defaut: int) -> int:
    brut = os.getenv(cle)
    if not brut:
        return defaut
    try:
        return int(brut)
    except ValueError as exc:
        raise ErreurConfiguration(f"{cle} doit être un entier, reçu : {brut!r}") from exc


def _decimal(cle: str, defaut: float) -> float:
    brut = os.getenv(cle)
    if not brut:
        return defaut
    try:
        return float(brut)
    except ValueError as exc:
        raise ErreurConfiguration(f"{cle} doit être un nombre, reçu : {brut!r}") from exc


def _date(cle: str, defaut: str) -> date:
    brut = os.getenv(cle, defaut)
    try:
        return date.fromisoformat(brut)
    except ValueError as exc:
        raise ErreurConfiguration(
            f"{cle} doit être une date ISO (AAAA-MM-JJ), reçu : {brut!r}"
        ) from exc


def _liste(cle: str, defaut: str = "") -> list[str]:
    brut = os.getenv(cle, defaut)
    return [x.strip() for x in brut.split(",") if x.strip()]


def _chemin(cle: str, defaut: str) -> Path:
    """Résout un chemin relatif par rapport à la racine du projet, pas au cwd.

    Sans cela, lancer le pipeline depuis un autre répertoire écrirait les
    fichiers au mauvais endroit — bug silencieux et pénible à diagnostiquer.
    """
    brut = Path(os.getenv(cle, defaut))
    return brut if brut.is_absolute() else (RACINE_PROJET / brut).resolve()


# --------------------------------------------------------------------------- #
#  Objet de configuration
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class Configuration:
    """Configuration immuable du pipeline.

    ``frozen=True`` : une fois construite, elle ne peut plus être modifiée. Cela
    élimine toute une classe de bugs où une étape du pipeline altère un paramètre
    et change le comportement des suivantes.
    """

    # --- Base de données ---------------------------------------------------
    pg_hote: str
    pg_port: int
    pg_base: str
    pg_utilisateur: str
    pg_mot_de_passe: str = field(repr=False)  # jamais affiché dans un repr()

    # --- Chemins -----------------------------------------------------------
    repertoire_brut: Path
    repertoire_travail: Path
    repertoire_rapports: Path

    # --- Sources -----------------------------------------------------------
    url_gtfs: str
    url_gtfs_a_venir: str
    url_gtfs_rt_trip_update: str
    code_reseau: str
    url_meteo_archive: str
    url_meteo_prevision: str
    meteo_latitude: float
    meteo_longitude: float
    meteo_fuseau: str

    # --- Périmètre ---------------------------------------------------------
    periode_debut: date
    periode_fin: date
    lignes_retenues: list[str]

    # --- Comportement ------------------------------------------------------
    taille_lot: int
    seuil_alerte_rejet_pct: float

    # ----------------------------------------------------------------------- #
    @property
    def chaine_connexion(self) -> str:
        """Chaîne libpq. Ne jamais la journaliser telle quelle : elle porte le mot de passe."""
        return (
            f"host={self.pg_hote} port={self.pg_port} dbname={self.pg_base} "
            f"user={self.pg_utilisateur} password={self.pg_mot_de_passe} "
            f"client_encoding=UTF8 options='-c timezone=Europe/Paris'"
        )

    @property
    def chaine_connexion_masquee(self) -> str:
        """Version journalisable de la chaîne de connexion."""
        return (
            f"host={self.pg_hote} port={self.pg_port} dbname={self.pg_base} "
            f"user={self.pg_utilisateur} password=***"
        )

    def valider(self) -> None:
        """Contrôles de cohérence effectués une fois, au démarrage."""
        if self.periode_fin < self.periode_debut:
            raise ErreurConfiguration(
                f"PERIODE_FIN ({self.periode_fin}) est antérieure à "
                f"PERIODE_DEBUT ({self.periode_debut})."
            )
        if not (-90 <= self.meteo_latitude <= 90):
            raise ErreurConfiguration(f"Latitude hors bornes : {self.meteo_latitude}")
        if not (-180 <= self.meteo_longitude <= 180):
            raise ErreurConfiguration(f"Longitude hors bornes : {self.meteo_longitude}")
        if self.taille_lot < 1:
            raise ErreurConfiguration("TAILLE_LOT doit valoir au moins 1.")
        if not 0 <= self.seuil_alerte_rejet_pct <= 100:
            raise ErreurConfiguration("SEUIL_ALERTE_REJET_PCT doit être compris entre 0 et 100.")

        for repertoire in (self.repertoire_brut, self.repertoire_travail, self.repertoire_rapports):
            repertoire.mkdir(parents=True, exist_ok=True)


def charger_configuration(fichier_env: Path | str | None = None) -> Configuration:
    """Charge, valide et renvoie la configuration du pipeline."""
    chemin_env = Path(fichier_env) if fichier_env else RACINE_PROJET / ".env"
    # override=False : une variable déjà présente dans l'environnement réel
    # (conteneur, ordonnanceur, CI) prime toujours sur le fichier .env local.
    load_dotenv(chemin_env, override=False)

    configuration = Configuration(
        pg_hote=_texte("PGHOST", "localhost"),
        pg_port=_entier("PGPORT", 5432),
        pg_base=_texte("PGDATABASE", "mobilite"),
        pg_utilisateur=_texte("PGUSER", "mobilite_etl"),
        pg_mot_de_passe=_texte("PGPASSWORD", obligatoire=True),
        repertoire_brut=_chemin("REPERTOIRE_DONNEES_BRUTES", "data/brut"),
        repertoire_travail=_chemin("REPERTOIRE_TRAVAIL", "data/travail"),
        repertoire_rapports=_chemin("REPERTOIRE_RAPPORTS", "rapports"),
        url_gtfs=_texte("URL_GTFS", obligatoire=True),
        url_gtfs_a_venir=_texte("URL_GTFS_A_VENIR", ""),
        url_gtfs_rt_trip_update=_texte("URL_GTFS_RT_TRIP_UPDATE", ""),
        code_reseau=_texte("CODE_RESEAU", "STAR"),
        url_meteo_archive=_texte("URL_METEO_ARCHIVE", "https://archive-api.open-meteo.com/v1/archive"),
        url_meteo_prevision=_texte("URL_METEO_PREVISION", "https://api.open-meteo.com/v1/forecast"),
        meteo_latitude=_decimal("METEO_LATITUDE", 48.1147),
        meteo_longitude=_decimal("METEO_LONGITUDE", -1.6794),
        meteo_fuseau=_texte("METEO_FUSEAU", "Europe/Paris"),
        periode_debut=_date("PERIODE_DEBUT", "2026-06-01"),
        periode_fin=_date("PERIODE_FIN", "2026-08-31"),
        lignes_retenues=_liste("LIGNES_RETENUES"),
        taille_lot=_entier("TAILLE_LOT", 10_000),
        seuil_alerte_rejet_pct=_decimal("SEUIL_ALERTE_REJET_PCT", 5.0),
    )
    configuration.valider()
    return configuration
