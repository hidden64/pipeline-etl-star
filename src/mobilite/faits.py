"""Étape T (transformation, partie faits) : construction de fait_passage.

La logique de résolution des clés vit en SQL (procédure
``staging.charger_fait_passage``). Python orchestre, journalise, et vérifie
l'intégrité une fois le chargement fait.
"""

from __future__ import annotations

from dataclasses import dataclass

import psycopg

from .base import etape
from .journal import obtenir_journal

LOG = obtenir_journal("faits")


@dataclass(slots=True)
class BilanFaits:
    """Décompte des faits chargés, pour le rapport."""

    faits_charges: int = 0
    faits_degrades: int = 0
    faits_supprimes: int = 0
    faits_ponctuels: int = 0
    part_inconnu_arret: int = 0
    part_inconnu_meteo: int = 0

    @property
    def taux_ponctualite_pct(self) -> float:
        """Ponctualité globale — recalculée, jamais stockée (mesure non additive)."""
        realises = self.faits_charges - self.faits_supprimes
        return 100.0 * self.faits_ponctuels / realises if realises else 0.0

    @property
    def taux_degrade_pct(self) -> float:
        if self.faits_charges == 0:
            return 0.0
        return 100.0 * self.faits_degrades / self.faits_charges


def charger_faits(conn: psycopg.Connection, id_execution: int) -> BilanFaits:
    """Construit fait_passage et renvoie son bilan."""
    with etape(conn, id_execution, "Table de faits", "FAIT") as suivi:
        with conn.cursor() as cur:
            cur.execute("CALL staging.charger_fait_passage(%s)", (id_execution,))
        bilan = _collecter_bilan(conn, id_execution)
        suivi.lignes_inserees = bilan.faits_charges
        suivi.message = (
            f"{bilan.faits_charges} faits, {bilan.faits_degrades} dégradés, "
            f"ponctualité {bilan.taux_ponctualite_pct:.1f} %"
        )

    _verifier_integrite(conn, id_execution)
    _journaliser(bilan)
    return bilan


def _collecter_bilan(conn: psycopg.Connection, id_execution: int) -> BilanFaits:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*)                                          AS total,
                   count(*) FILTER (WHERE est_degrade)               AS degrades,
                   count(*) FILTER (WHERE est_supprime)              AS supprimes,
                   count(*) FILTER (WHERE est_ponctuel)              AS ponctuels,
                   count(*) FILTER (WHERE sk_arret = -1)             AS arret_inconnu,
                   count(*) FILTER (WHERE sk_meteo = -1)             AS meteo_inconnue
              FROM entrepot.fait_passage
             WHERE id_execution = %s
            """,
            (id_execution,),
        )
        total, degrades, supprimes, ponctuels, arret_inc, meteo_inc = cur.fetchone()
    return BilanFaits(total, degrades, supprimes, ponctuels, arret_inc, meteo_inc)


def _verifier_integrite(conn: psycopg.Connection, id_execution: int) -> None:
    """Contrôles d'intégrité APRÈS chargement.

    Un pipeline sérieux ne se contente pas de charger : il vérifie que ce qu'il a
    chargé respecte les invariants du modèle. Ces contrôles doivent TOUS renvoyer
    zéro ; un résultat non nul signale une régression dans la résolution des clés.
    """
    controles = {
        "faits sans partition (tombés dans la partition par défaut)":
            "SELECT count(*) FROM entrepot.fait_passage_defaut",
        "clés étrangères nulles (impossible par construction, on vérifie)":
            "SELECT count(*) FROM entrepot.fait_passage "
            "WHERE sk_date IS NULL OR sk_ligne IS NULL OR sk_arret IS NULL "
            "   OR sk_creneau IS NULL OR sk_meteo IS NULL",
        "retards hors bornes ayant franchi la validation":
            "SELECT count(*) FROM entrepot.fait_passage "
            "WHERE retard_secondes IS NOT NULL "
            "  AND (retard_secondes < -1800 OR retard_secondes > 7200)",
    }
    for libelle, requete in controles.items():
        with conn.cursor() as cur:
            cur.execute(requete)
            n = cur.fetchone()[0]
        if n:
            LOG.warning("CONTRÔLE ÉCHOUÉ — %s : %d", libelle, n)
        else:
            LOG.debug("Contrôle OK — %s", libelle)


def _journaliser(bilan: BilanFaits) -> None:
    LOG.info("─" * 60)
    LOG.info("BILAN FAITS")
    LOG.info("  faits chargés ........ %10d", bilan.faits_charges)
    LOG.info("  dont supprimés ....... %10d", bilan.faits_supprimes)
    LOG.info("  dont dégradés ........ %10d  (%.2f %%)",
             bilan.faits_degrades, bilan.taux_degrade_pct)
    LOG.info("    · arrêt inconnu .... %10d", bilan.part_inconnu_arret)
    LOG.info("    · météo inconnue ... %10d", bilan.part_inconnu_meteo)
    LOG.info("  ponctualité globale .. %9.1f %%", bilan.taux_ponctualite_pct)
    LOG.info("─" * 60)
