"""Tests du générateur de rapport.

On teste ici ce qui ne dépend pas de la base : le rendu du gabarit et les
filtres de présentation. Le rendu est vérifié avec des données factices, pour
garantir que le gabarit reste valide même quand les requêtes évoluent.
"""

from __future__ import annotations

import datetime as dt

from jinja2 import Environment, FileSystemLoader

from mobilite.rapport import (
    REPERTOIRE_GABARITS,
    _filtre_duree,
    _filtre_libelle,
    _filtre_nombre,
)


class TestFiltres:
    def test_nombre_separe_les_milliers(self):
        assert _filtre_nombre(2342951) == "2 342 951"

    def test_nombre_gere_le_none(self):
        assert _filtre_nombre(None) == "—"

    def test_libelle_rend_lisible(self):
        assert _filtre_libelle("POINTE_MATIN") == "Pointe matin"

    def test_duree_courte_en_secondes(self):
        assert _filtre_duree(45.2) == "45.2 s"

    def test_duree_longue_en_minutes(self):
        assert _filtre_duree(756.9) == "12 min 36 s"


def _environnement():
    env = Environment(
        loader=FileSystemLoader(REPERTOIRE_GABARITS),
        autoescape=True,  # comme en production : échappement inconditionnel
    )
    env.filters["nombre"] = _filtre_nombre
    env.filters["duree"] = _filtre_duree
    env.filters["libelle"] = _filtre_libelle
    return env


def _donnees_factices():
    return {
        "genere_le": dt.datetime(2026, 9, 6, 10, 30),
        "execution": {"id_execution": 42, "statut": "SUCCES", "duree_sec": 120.0,
                      "parametres": {"periode_debut": "2026-09-04", "periode_fin": "2026-10-12"}},
        "etapes": [{"nom_etape": "Table de faits", "phase": "FAIT", "statut": "SUCCES",
                    "lignes_lues": 0, "lignes_inserees": 2342951, "lignes_rejetees": 0,
                    "duree_sec": 60.0}],
        "kpi": {"total_passages": 2342951, "taux_ponctualite_pct": 90.7,
                "retard_moyen_sec": 67.5, "retard_median_sec": 40, "retard_p90_sec": 190,
                "passages_supprimes": 19188, "passages_degrades": 1345414},
        "qualite": [{"code_regle": "R003_RETARD_ABERRANT", "libelle": "Retard aberrant",
                     "severite": "ERREUR", "nombre": 3588}],
        "meteo": [{"tranche_precipitation": "AUCUNE", "passages": 824241,
                   "retard_moyen_sec": 66.0, "taux_ponctualite_pct": 91.0}],
        "creneaux": [{"heure": 8, "libelle_creneau": "POINTE_MATIN", "est_heure_pointe": True,
                      "passages": 100000, "retard_moyen_sec": 106.7, "taux_ponctualite_pct": 80.0}],
        "lignes_top": [{"code_ligne": "C1", "nom_ligne": "Ligne C1", "mode_transport": "BUS",
                        "passages": 200000, "courses_supprimees": 500,
                        "retard_moyen_sec": 100.1, "taux_ponctualite_pct": 82.0}],
    }


class TestRenduGabarit:
    def test_le_gabarit_se_rend_sans_erreur(self):
        html = _environnement().get_template("rapport.html.j2").render(**_donnees_factices())
        assert html.startswith("<!doctype html>")
        assert "{{" not in html and "{%" not in html, "aucun reste de balise Jinja"

    def test_les_valeurs_cles_apparaissent(self):
        html = _environnement().get_template("rapport.html.j2").render(**_donnees_factices())
        assert "2 342 951" in html          # nombre formaté
        assert "90.7 %" in html             # ponctualité
        assert "Pointe matin" in html       # libellé embelli
        assert "R003_RETARD_ABERRANT" in html

    def test_echappement_html_actif(self):
        """Un libellé hostile ne doit jamais produire de HTML exécutable."""
        donnees = _donnees_factices()
        donnees["lignes_top"][0]["nom_ligne"] = "<script>alert(1)</script>"
        html = _environnement().get_template("rapport.html.j2").render(**donnees)
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html
