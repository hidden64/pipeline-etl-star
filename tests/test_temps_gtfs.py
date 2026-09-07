"""Tests de la conversion des heures GTFS.

C'est le piège n°1 du projet, et il mérite ses propres tests : le GTFS autorise
explicitement les heures supérieures à 24 h. ``25:14:00`` signifie 1 h 14 le
lendemain, mais rattaché au service de la veille. Sur le feed STAR, 40 131 des
1 857 606 lignes de ``stop_times.txt`` sont concernées, jusqu'à ``31:xx``.
"""

from __future__ import annotations

import pytest

from mobilite.realisations import formater_heure, secondes_depuis_minuit


class TestSecondesDepuisMinuit:
    """Conversion « HH:MM:SS » → secondes depuis minuit du jour de service."""

    @pytest.mark.parametrize(
        ("heure", "attendu"),
        [
            ("00:00:00", 0),
            ("05:17:30", 5 * 3600 + 17 * 60 + 30),
            ("23:59:59", 86_399),
        ],
    )
    def test_heures_normales(self, heure: str, attendu: int) -> None:
        assert secondes_depuis_minuit(heure) == attendu

    @pytest.mark.parametrize(
        ("heure", "attendu"),
        [
            ("24:00:00", 86_400),        # minuit pile, service de la veille
            ("25:14:00", 90_840),        # 1 h 14 le lendemain
            ("31:45:00", 114_300),       # valeur maximale observée sur le feed STAR
        ],
    )
    def test_heures_au_dela_de_minuit(self, heure: str, attendu: int) -> None:
        """Ces valeurs sont LÉGALES en GTFS. Les rejeter perdrait le service de nuit."""
        assert secondes_depuis_minuit(heure) == attendu

    def test_une_heure_au_dela_de_24h_depasse_bien_une_journee(self) -> None:
        """Contrôle de sens : 25:14 doit tomber au-delà de 86 400 s."""
        assert secondes_depuis_minuit("25:14:00") > 86_400

    def test_format_invalide_leve_une_erreur(self) -> None:
        """Mieux vaut une erreur franche qu'une valeur silencieusement fausse."""
        with pytest.raises(ValueError):
            secondes_depuis_minuit("pas une heure")


class TestFormaterHeure:
    """Fonction inverse, qui doit conserver la convention GTFS."""

    @pytest.mark.parametrize("heure", ["00:00:00", "05:17:30", "23:59:59",
                                       "24:00:00", "25:14:00", "31:45:00"])
    def test_aller_retour(self, heure: str) -> None:
        """La conversion doit être réversible, y compris au-delà de 24 h.

        C'est la propriété qui garantit qu'un passage du service de nuit
        ressort de l'export tel qu'il y est entré.
        """
        assert formater_heure(secondes_depuis_minuit(heure)) == heure

    def test_retard_negatif_avant_minuit_reste_lisible(self) -> None:
        """Un véhicule en avance sur le premier passage de la journée.

        Le cas se produit vraiment : premier arrêt à 00:00:30, deux minutes
        d'avance. On replie sur la veille plutôt que de produire « -1:58:30 »,
        que rien en aval ne saurait relire.
        """
        assert formater_heure(-90) == "23:58:30"
