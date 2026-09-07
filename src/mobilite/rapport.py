"""Étape finale : génération du rapport d'exécution automatisé.

À chaque exécution du pipeline, on produit un rapport HTML autonome (aucune
dépendance externe, tout est embarqué) qui présente d'un coup d'œil :
  * le bilan de l'exécution (durées, statuts, volumétries par étape) ;
  * la qualité des données (rejets par règle, taux) ;
  * les indicateurs métier (ponctualité globale, effet météo, heures de pointe).

Pourquoi un rapport automatisé ? Parce qu'un pipeline qui tourne sans personne
devant l'écran a besoin d'un artefact qu'on puisse consulter APRÈS coup, archiver,
et comparer d'une exécution à l'autre. Le journal technique répond au « que s'est-il
passé en détail » ; le rapport répond au « tout va-t-il bien, en un coup d'œil ».

Le rendu est confié à Jinja2 : on sépare la LOGIQUE (ce module, qui interroge la
base) de la PRÉSENTATION (le gabarit HTML). Le même jeu de données pourrait être
rendu en Markdown ou en courriel sans toucher aux requêtes.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Any

import psycopg
from jinja2 import Environment, FileSystemLoader

from .config import Configuration
from .journal import obtenir_journal

LOG = obtenir_journal("rapport")

REPERTOIRE_GABARITS = Path(__file__).parent / "gabarits"


def _lignes(conn: psycopg.Connection, requete: str, params: tuple = ()) -> list[dict[str, Any]]:
    """Exécute une requête et renvoie une liste de dictionnaires (colonne → valeur)."""
    with conn.cursor() as cur:
        cur.execute(requete, params)
        colonnes = [d[0] for d in cur.description]
        return [dict(zip(colonnes, ligne)) for ligne in cur.fetchall()]


def _un(conn: psycopg.Connection, requete: str, params: tuple = ()) -> dict[str, Any]:
    """Première ligne d'une requête, ou dictionnaire vide."""
    resultats = _lignes(conn, requete, params)
    return resultats[0] if resultats else {}


def collecter_donnees_rapport(
    conn: psycopg.Connection, id_execution: int
) -> dict[str, Any]:
    """Rassemble toutes les données du rapport en interrogeant les vues de restitution.

    Une seule fonction de collecte, qui délègue tout le calcul aux vues SQL. Le
    Python ne fait que transporter : aucune agrégation ici, elle serait redondante
    avec la base et moins performante.
    """
    execution = _un(
        conn,
        """
        SELECT id_execution, horodate_debut, horodate_fin, statut, parametres,
               round(EXTRACT(EPOCH FROM (horodate_fin - horodate_debut))::numeric, 1) AS duree_sec
          FROM meta.execution WHERE id_execution = %s
        """,
        (id_execution,),
    )

    etapes = _lignes(
        conn,
        """
        SELECT nom_etape, phase, statut, lignes_lues, lignes_inserees,
               lignes_rejetees, round(duree_ms / 1000.0, 1) AS duree_sec
          FROM meta.etape WHERE id_execution = %s ORDER BY id_etape
        """,
        (id_execution,),
    )

    return {
        "genere_le": dt.datetime.now(),
        "execution": execution,
        "etapes": etapes,
        "kpi": _un(conn, "SELECT * FROM restitution.v_kpi_globaux"),
        "qualite": _lignes(
            conn,
            """
            SELECT code_regle, libelle, severite, nombre
              FROM restitution.v_qualite_rejets WHERE id_execution = %s
             ORDER BY severite, nombre DESC
            """,
            (id_execution,),
        ),
        "meteo": _lignes(
            conn,
            """
            -- Moyennes PONDÉRÉES par le nombre de passages : on ne fait pas la
            -- moyenne de moyennes (fausse), on repondère par l'effectif.
            -- taux_ponctualite_pct est DEJA un pourcentage : ne jamais le
            -- remultiplier par cent, sinon le resultat serait cent fois trop grand.
            SELECT tranche_precipitation, sum(passages) AS passages,
                   round(sum(passages * retard_moyen_sec) / nullif(sum(passages), 0), 1)
                       AS retard_moyen_sec,
                   round(sum(passages * taux_ponctualite_pct)
                         / nullif(sum(passages), 0), 1) AS taux_ponctualite_pct
              FROM restitution.v_ponctualite_meteo
             GROUP BY tranche_precipitation
             ORDER BY CASE tranche_precipitation
                        WHEN 'AUCUNE' THEN 0 WHEN 'FAIBLE' THEN 1
                        WHEN 'MODEREE' THEN 2 WHEN 'FORTE' THEN 3 ELSE 4 END
            """,
        ),
        "creneaux": _lignes(
            conn,
            "SELECT heure, libelle_creneau, est_heure_pointe, passages, "
            "       retard_moyen_sec, taux_ponctualite_pct "
            "  FROM restitution.v_ponctualite_creneau ORDER BY heure",
        ),
        "lignes_top": _lignes(
            conn,
            "SELECT code_ligne, nom_ligne, mode_transport, passages, "
            "       courses_supprimees, retard_moyen_sec, taux_ponctualite_pct "
            "  FROM restitution.v_ponctualite_ligne ORDER BY retard_moyen_sec DESC",
        ),
    }


def generer_rapport(
    conn: psycopg.Connection,
    configuration: Configuration,
    id_execution: int,
) -> Path:
    """Génère le rapport HTML de l'exécution et renvoie son chemin."""
    donnees = collecter_donnees_rapport(conn, id_execution)

    environnement = Environment(
        loader=FileSystemLoader(REPERTOIRE_GABARITS),
        # Échappement automatique : les libellés d'arrêts viennent d'une source
        # externe ; sans échappement, un nom contenant « <script> » deviendrait
        # une faille XSS. On ne fait jamais confiance à la donnée pour du HTML.
        #
        # `autoescape=True` (et non select_autoescape(["html"])) : ce dernier
        # regarde l'extension FINALE du gabarit, qui est « .j2 » et non « .html »
        # — l'échappement n'aurait donc PAS été activé. Un test injectant
        # « <script> » dans un nom de ligne a révélé la faille. Comme ce module
        # ne produit que du HTML, on active l'échappement inconditionnellement.
        autoescape=True,
    )
    environnement.filters["nombre"] = _filtre_nombre
    environnement.filters["duree"] = _filtre_duree
    environnement.filters["libelle"] = _filtre_libelle

    gabarit = environnement.get_template("rapport.html.j2")
    html = gabarit.render(**donnees)

    nom = f"rapport_execution_{id_execution:04d}_{dt.date.today():%Y%m%d}.html"
    chemin = configuration.repertoire_rapports / nom
    chemin.write_text(html, encoding="utf-8")

    LOG.info("Rapport généré : %s", chemin)
    return chemin


# --------------------------------------------------------------------------- #
#  Filtres de présentation
# --------------------------------------------------------------------------- #
def _filtre_nombre(valeur: Any) -> str:
    """Sépare les milliers par une espace insécable étroite : 2 342 951."""
    if valeur is None:
        return "—"
    try:
        return re.sub(r"[^\d-]", " ", f"{int(valeur):,}")
    except (ValueError, TypeError):
        return str(valeur)


def _filtre_libelle(code: Any) -> str:
    """Rend lisible une énumération technique : POINTE_MATIN → « Pointe matin »."""
    if not code:
        return "—"
    return str(code).replace("_", " ").capitalize()


def _filtre_duree(secondes: Any) -> str:
    """Formate une durée en secondes de façon lisible."""
    if secondes is None:
        return "—"
    secondes = float(secondes)
    if secondes < 60:
        return f"{secondes:.1f} s"
    minutes, reste = divmod(secondes, 60)
    return f"{int(minutes)} min {int(reste):02d} s"
