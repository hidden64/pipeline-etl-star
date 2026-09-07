"""Tests des fonctions de normalisation SQL, exécutées dans PostgreSQL.

Ces fonctions sont le premier rempart de qualité : une erreur ici propage des
valeurs fausses (ou des rejets injustifiés) dans tout l'entrepôt. On les teste
donc directement contre le moteur, avec les cas limites réels des sources.

Ignorés proprement si la base est injoignable (voir conftest.py).
"""

from __future__ import annotations

import pytest


def _scalaire(conn, sql: str, *params):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()[0]


class TestParseDateFr:
    @pytest.mark.parametrize(
        ("entree", "attendu"),
        [
            ("04/09/2026", "2026-09-04"),
            ("31/12/2026", "2026-12-31"),
            ("01/01/2020", "2020-01-01"),
        ],
    )
    def test_dates_valides(self, connexion_base, entree, attendu):
        resultat = _scalaire(connexion_base, "SELECT staging.parse_date_fr(%s)", entree)
        assert str(resultat) == attendu

    @pytest.mark.parametrize(
        "entree",
        [
            "2026-09-04",   # format ISO, pas français
            "31/13/2026",   # mois inexistant
            "30/02/2026",   # 30 février : le contrôle aller-retour doit l'attraper
            "",             # vide
            "n'importe quoi",
        ],
    )
    def test_dates_invalides_donnent_null(self, connexion_base, entree):
        """Une date invalide renvoie NULL — jamais une exception, jamais une date fausse."""
        assert _scalaire(connexion_base, "SELECT staging.parse_date_fr(%s)", entree) is None


class TestParseHeureGtfs:
    def test_heure_normale(self, connexion_base):
        assert str(_scalaire(connexion_base, "SELECT staging.parse_heure_gtfs('08:30:00')")) == "8:30:00"

    def test_heure_au_dela_de_24h(self, connexion_base):
        """« 25:14:00 » doit devenir 1 jour + 1 h 14 — le cœur du piège GTFS."""
        resultat = _scalaire(connexion_base, "SELECT staging.parse_heure_gtfs('25:14:00')")
        # psycopg renvoie un timedelta : 1 jour + 4440 s.
        assert resultat.days == 1
        assert resultat.seconds == 1 * 3600 + 14 * 60

    def test_date_plus_heure_franchit_minuit(self, connexion_base):
        """La composition date + heure GTFS doit basculer au lendemain."""
        resultat = _scalaire(
            connexion_base,
            "SELECT (DATE '2026-09-04' + staging.parse_heure_gtfs('25:14:00'))"
            "        AT TIME ZONE 'Europe/Paris'",
        )
        assert str(resultat).startswith("2026-09-05 01:14:00")

    @pytest.mark.parametrize("entree", ["8h30", "", "25:14"])
    def test_invalides_donnent_null(self, connexion_base, entree):
        assert _scalaire(connexion_base, "SELECT staging.parse_heure_gtfs(%s)", entree) is None


class TestParseEntierEtDecimal:
    @pytest.mark.parametrize(("entree", "attendu"), [("42", 42), ("-90", -90), ("0", 0)])
    def test_entiers_valides(self, connexion_base, entree, attendu):
        assert _scalaire(connexion_base, "SELECT staging.parse_entier(%s)", entree) == attendu

    @pytest.mark.parametrize("entree", ["12,5", "abc", "", "1e9"])
    def test_entiers_invalides_donnent_null(self, connexion_base, entree):
        assert _scalaire(connexion_base, "SELECT staging.parse_entier(%s)", entree) is None

    def test_decimale_a_virgule(self, connexion_base):
        assert float(_scalaire(connexion_base, "SELECT staging.parse_decimal_fr('18,4')")) == 18.4

    def test_decimale_a_point_aussi_acceptee(self, connexion_base):
        assert float(_scalaire(connexion_base, "SELECT staging.parse_decimal_fr('18.4')")) == 18.4


class TestRejetDesRetardsAberrants:
    """La règle R003 doit attraper exactement les valeurs hors bornes physiques."""

    @pytest.mark.parametrize("retard", [-1800, 0, 180, 7200])
    def test_retards_dans_les_bornes_sont_acceptes(self, connexion_base, retard):
        viole = _scalaire(
            connexion_base,
            "SELECT %s < staging.retard_min_sec() OR %s > staging.retard_max_sec()",
            retard, retard,
        )
        assert viole is False

    @pytest.mark.parametrize("retard", [99999, -5000, 86400, -99999])
    def test_retards_aberrants_sont_rejetes(self, connexion_base, retard):
        """Ce sont précisément les valeurs que le simulateur injecte comme aberrantes."""
        viole = _scalaire(
            connexion_base,
            "SELECT %s < staging.retard_min_sec() OR %s > staging.retard_max_sec()",
            retard, retard,
        )
        assert viole is True
