"""Étape T (transformation, partie qualité) : validation, nettoyage, rejets.

Toute la logique lourde vit en SQL (procédure ``staging.router_realisations``),
parce qu'elle est ensembliste : appliquer une règle à 2,3 millions de lignes se
fait en un ordre SQL, jamais en une boucle Python. Ce module se contente
d'orchestrer l'appel, de récupérer le bilan, et d'appliquer la politique de
seuil.

Répartition des responsabilités, à retenir :
  * **le SQL DÉCIDE** quelles lignes sont propres, rejetées, dédoublonnées ;
  * **Python ARBITRE** ce qu'on fait du bilan — continuer ou s'arrêter.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import psycopg

from .base import etape
from .config import Configuration
from .journal import obtenir_journal

LOG = obtenir_journal("validation")


class ErreurQualite(RuntimeError):
    """Le taux de rejet dépasse le seuil : on refuse de charger un entrepôt faux."""


@dataclass(slots=True)
class BilanQualite:
    """Résultat chiffré du routage, tel qu'il alimentera le rapport."""

    lignes_lues: int = 0
    lignes_propres: int = 0
    doublons_absorbes: int = 0
    rejets_erreur: int = 0
    # {code_regle: (libelle, severite, nombre)}
    detail_par_regle: dict[str, tuple[str, str, int]] = field(default_factory=dict)

    @property
    def taux_rejet_pct(self) -> float:
        """Part des lignes ÉCARTÉES (erreurs uniquement), en pourcentage.

        Les doublons ne comptent pas comme des rejets : la donnée n'est pas
        perdue, seulement dédupliquée. Les mélanger gonflerait artificiellement
        le taux et déclencherait de fausses alertes.
        """
        if self.lignes_lues == 0:
            return 0.0
        return 100.0 * self.rejets_erreur / self.lignes_lues


def valider_realisations(
    conn: psycopg.Connection,
    configuration: Configuration,
    id_execution: int,
) -> BilanQualite:
    """Exécute le routage qualité et renvoie le bilan.

    Lève :class:`ErreurQualite` si le taux de rejet dépasse le seuil configuré.
    C'est un **garde-fou** : au-delà d'un certain volume d'erreurs, ce n'est plus
    une donnée à nettoyer, c'est une source cassée ou un mauvais fichier — et il
    vaut mieux arrêter que remplir l'entrepôt de bruit.
    """
    with etape(conn, id_execution, "Validation et routage", "VALIDATION") as suivi:
        with conn.cursor() as cur:
            cur.execute("CALL staging.router_realisations(%s)", (id_execution,))

        bilan = _collecter_bilan(conn, id_execution)
        suivi.lignes_lues = bilan.lignes_lues
        suivi.lignes_inserees = bilan.lignes_propres
        suivi.lignes_rejetees = bilan.rejets_erreur + bilan.doublons_absorbes
        suivi.message = (
            f"{bilan.lignes_propres} propres, {bilan.rejets_erreur} rejets, "
            f"{bilan.doublons_absorbes} doublons ({bilan.taux_rejet_pct:.2f} % de rejet)"
        )

    _journaliser_bilan(bilan)

    if bilan.taux_rejet_pct > configuration.seuil_alerte_rejet_pct:
        raise ErreurQualite(
            f"Taux de rejet {bilan.taux_rejet_pct:.2f} % au-dessus du seuil "
            f"{configuration.seuil_alerte_rejet_pct:.2f} %. Chargement interrompu : "
            f"la source est probablement défectueuse."
        )

    return bilan


def _collecter_bilan(conn: psycopg.Connection, id_execution: int) -> BilanQualite:
    """Lit les compteurs du routage depuis la base."""
    bilan = BilanQualite()

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM staging.realisation")
        bilan.lignes_lues = cur.fetchone()[0]

        cur.execute("SELECT count(*) FROM staging.realisation_valide")
        bilan.lignes_propres = cur.fetchone()[0]

        # Détail par règle, joint au catalogue pour le libellé et la sévérité.
        cur.execute(
            """
            SELECT rr.code_regle, g.libelle, g.severite, count(*) AS nb
              FROM rejet.rejet rr
              JOIN rejet.regle g ON g.code_regle = rr.code_regle
             WHERE rr.id_execution = %s
             GROUP BY rr.code_regle, g.libelle, g.severite
             ORDER BY g.severite, nb DESC
            """,
            (id_execution,),
        )
        for code, libelle, severite, nb in cur.fetchall():
            bilan.detail_par_regle[code] = (libelle, severite, nb)
            if severite == "ERREUR":
                bilan.rejets_erreur += nb
            # Les avertissements A103/A104 comptent des GROUPES de doublons, pas
            # des lignes absorbées. Le nombre de lignes réellement retirées est
            # déduit ci-dessous par différence, ce qui est exact quel que soit le
            # nombre de copies par groupe.

    # Doublons absorbés = propres théoriques (hors erreurs) − propres réels.
    bilan.doublons_absorbes = max(
        0,
        (bilan.lignes_lues - bilan.rejets_erreur) - bilan.lignes_propres,
    )
    return bilan


def _journaliser_bilan(bilan: BilanQualite) -> None:
    """Écrit le bilan dans le journal, lisible d'un coup d'œil."""
    LOG.info("─" * 60)
    LOG.info("BILAN QUALITÉ")
    LOG.info("  lignes lues .......... %10d", bilan.lignes_lues)
    LOG.info("  lignes propres ....... %10d", bilan.lignes_propres)
    LOG.info("  doublons absorbés .... %10d", bilan.doublons_absorbes)
    LOG.info("  rejets (erreurs) ..... %10d  (%.2f %%)",
             bilan.rejets_erreur, bilan.taux_rejet_pct)
    LOG.info("  détail par règle :")
    for code, (libelle, severite, nb) in sorted(
        bilan.detail_par_regle.items(), key=lambda x: (x[1][1], -x[1][2])
    ):
        marque = "✗" if severite == "ERREUR" else "⚠"
        LOG.info("    %s %-32s %8d  %s", marque, code, nb, libelle[:50])
    LOG.info("─" * 60)
