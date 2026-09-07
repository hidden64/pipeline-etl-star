"""Tests de la configuration et de la journalisation.

Deux modules peu spectaculaires mais critiques : une configuration qui échoue
tard, ou un journal qui recopie un mot de passe, coûtent bien plus cher qu'un
bug de transformation.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from mobilite.config import Configuration, ErreurConfiguration, charger_configuration
from mobilite.journal import masquer_secrets


def _configuration(**remplacements) -> Configuration:
    """Configuration minimale valide, que chaque test altère à sa guise."""
    defauts = dict(
        pg_hote="localhost", pg_port=5432, pg_base="test",
        pg_utilisateur="test", pg_mot_de_passe="secret",
        repertoire_brut=Path("data/brut"),
        repertoire_travail=Path("data/travail"),
        repertoire_rapports=Path("rapports"),
        url_gtfs="https://exemple.test/gtfs.zip",
        url_gtfs_a_venir="", url_gtfs_rt_trip_update="",
        code_reseau="STAR",
        url_meteo_archive="https://exemple.test/archive",
        url_meteo_prevision="https://exemple.test/forecast",
        meteo_latitude=48.1147, meteo_longitude=-1.6794,
        meteo_fuseau="Europe/Paris",
        periode_debut=dt.date(2026, 9, 4), periode_fin=dt.date(2026, 10, 12),
        lignes_retenues=["a", "b"], taille_lot=10_000,
        seuil_alerte_rejet_pct=5.0,
    )
    return Configuration(**(defauts | remplacements))


class TestValidationConfiguration:
    """La configuration doit échouer AU DÉMARRAGE, jamais en cours de traitement."""

    def test_configuration_valide_passe(self, tmp_path: Path) -> None:
        _configuration(
            repertoire_brut=tmp_path / "brut",
            repertoire_travail=tmp_path / "travail",
            repertoire_rapports=tmp_path / "rapports",
        ).valider()

    def test_periode_inversee_est_refusee(self) -> None:
        with pytest.raises(ErreurConfiguration, match="antérieure"):
            _configuration(
                periode_debut=dt.date(2026, 10, 12),
                periode_fin=dt.date(2026, 9, 4),
            ).valider()

    @pytest.mark.parametrize("latitude", [-91.0, 91.0])
    def test_latitude_hors_bornes_est_refusee(self, latitude: float) -> None:
        with pytest.raises(ErreurConfiguration, match="Latitude"):
            _configuration(meteo_latitude=latitude).valider()

    def test_seuil_de_rejet_hors_pourcentage_est_refuse(self) -> None:
        with pytest.raises(ErreurConfiguration):
            _configuration(seuil_alerte_rejet_pct=150.0).valider()

    def test_valider_cree_les_repertoires_manquants(self, tmp_path: Path) -> None:
        """La validation prépare le terrain : plus d'échec sur un dossier absent."""
        cible = tmp_path / "inexistant" / "brut"
        assert not cible.exists()
        _configuration(
            repertoire_brut=cible,
            repertoire_travail=tmp_path / "travail",
            repertoire_rapports=tmp_path / "rapports",
        ).valider()
        assert cible.is_dir()


class TestSecrets:
    """Un mot de passe ne doit jamais franchir la frontière du journal."""

    def test_le_mot_de_passe_est_absent_du_repr(self) -> None:
        """`field(repr=False)` : un `print(configuration)` accidentel ne fuite rien."""
        assert "secret" not in repr(_configuration())

    def test_la_chaine_de_connexion_masquee_ne_porte_pas_le_secret(self) -> None:
        configuration = _configuration()
        assert "secret" not in configuration.chaine_connexion_masquee
        assert "password=***" in configuration.chaine_connexion_masquee
        # …mais la vraie chaîne, elle, doit bien le contenir.
        assert "secret" in configuration.chaine_connexion

    @pytest.mark.parametrize(
        "message",
        [
            "host=x password=tresSecret dbname=y",
            "connexion avec PASSWORD: tresSecret",
            "token=tresSecret",
            "api_key = tresSecret",
        ],
    )
    def test_masquage_dans_le_journal(self, message: str) -> None:
        assert "tresSecret" not in masquer_secrets(message)

    def test_le_masquage_ne_detruit_pas_le_reste_du_message(self) -> None:
        resultat = masquer_secrets("host=localhost password=abc dbname=mobilite")
        assert "host=localhost" in resultat
        assert "dbname=mobilite" in resultat


class TestChargementDepuisEnv:
    """Lecture effective d'un fichier .env."""

    def test_mot_de_passe_absent_leve_une_erreur_explicite(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("PGPASSWORD", raising=False)
        fichier = tmp_path / ".env"
        fichier.write_text("PGHOST=localhost\nURL_GTFS=https://x.test/g.zip\n", encoding="utf-8")
        with pytest.raises(ErreurConfiguration, match="PGPASSWORD"):
            charger_configuration(fichier)

    def test_l_environnement_reel_prime_sur_le_fichier(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """En production, l'ordonnanceur injecte les secrets : il doit gagner."""
        monkeypatch.setenv("PGPASSWORD", "depuis_environnement")
        monkeypatch.setenv("PGDATABASE", "depuis_environnement")
        fichier = tmp_path / ".env"
        fichier.write_text(
            "PGPASSWORD=depuis_fichier\nPGDATABASE=depuis_fichier\n"
            "URL_GTFS=https://x.test/g.zip\n",
            encoding="utf-8",
        )
        configuration = charger_configuration(fichier)
        assert configuration.pg_base == "depuis_environnement"
