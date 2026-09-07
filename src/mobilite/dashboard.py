"""Export des données d'analyse pour le tableau de bord.

Ce module interroge les vues de restitution et produit un unique document JSON,
prêt à être injecté dans une page de graphiques. Aucune mise en forme ici : on
sépare la donnée (ce module) de la visualisation (le gabarit du dashboard).

Le JSON est écrit dans `rapports/` ; il sert à la fois de source du tableau de
bord et d'artefact réutilisable (import dans un autre outil, archivage).
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import psycopg

from .config import Configuration
from .journal import obtenir_journal

LOG = obtenir_journal("dashboard")


def _lignes(conn: psycopg.Connection, requete: str, params: tuple = ()) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(requete, params)
        colonnes = [d[0] for d in cur.description]
        return [dict(zip(colonnes, ligne)) for ligne in cur.fetchall()]


def _un(conn: psycopg.Connection, requete: str, params: tuple = ()) -> dict[str, Any]:
    resultats = _lignes(conn, requete, params)
    return resultats[0] if resultats else {}


def collecter_donnees_dashboard(
    conn: psycopg.Connection, id_execution: int
) -> dict[str, Any]:
    """Rassemble toutes les séries du tableau de bord depuis les vues."""
    return {
        "meta": {
            "id_execution": id_execution,
            "genere_le": dt.datetime.now().isoformat(timespec="seconds"),
        },
        "kpi": _un(conn, "SELECT * FROM restitution.v_kpi_globaux"),
        # Météo agrégée par tranche de précipitation (pondérée par les passages).
        "meteo": _lignes(
            conn,
            """
            SELECT tranche_precipitation,
                   sum(passages)                                        AS passages,
                   round(sum(passages * retard_moyen_sec)
                         / nullif(sum(passages), 0), 1)                 AS retard_moyen_sec,
                   round(sum(passages * taux_ponctualite_pct)
                         / nullif(sum(passages), 0), 1)                 AS taux_ponctualite_pct
              FROM restitution.v_ponctualite_meteo
             GROUP BY tranche_precipitation
             ORDER BY CASE tranche_precipitation
                        WHEN 'AUCUNE' THEN 0 WHEN 'FAIBLE' THEN 1
                        WHEN 'MODEREE' THEN 2 WHEN 'FORTE' THEN 3 ELSE 4 END
            """,
        ),
        # Retard par heure : les 24 créneaux, dans l'ordre.
        "creneaux": _lignes(
            conn,
            "SELECT heure, libelle_creneau, est_heure_pointe, passages, "
            "       retard_moyen_sec, taux_ponctualite_pct "
            "  FROM restitution.v_ponctualite_creneau ORDER BY heure",
        ),
        # Lignes, triées par retard décroissant.
        "lignes": _lignes(
            conn,
            "SELECT code_ligne, nom_ligne, mode_transport, passages, "
            "       courses_supprimees, retard_moyen_sec, taux_ponctualite_pct "
            "  FROM restitution.v_ponctualite_ligne ORDER BY retard_moyen_sec DESC",
        ),
        # Série temporelle : un point par jour.
        "jours": _lignes(
            conn,
            "SELECT jour, libelle_jour_semaine, est_weekend, type_jour_reseau, "
            "       passages, retard_moyen_sec, taux_ponctualite_pct "
            "  FROM restitution.v_ponctualite_jour ORDER BY jour",
        ),
        # Qualité : rejets par règle (dernière exécution ayant des rejets).
        "qualite": _lignes(
            conn,
            """
            SELECT code_regle, libelle, severite, sum(nombre) AS nombre
              FROM restitution.v_qualite_rejets
             GROUP BY code_regle, libelle, severite
             ORDER BY severite, nombre DESC
            """,
        ),
    }


#: Colonnes du cube, dans l'ordre. Format compact « colonnes + lignes-tableaux »
#: plutôt qu'une liste d'objets : sur ~8 000 lignes, cela divise par ~3 le poids
#: du JSON embarqué. Le navigateur reconstruit les enregistrements par indice.
COLONNES_CUBE = [
    "lig", "mode", "jour", "we", "tj", "h", "cr", "pk", "pr", "fa",
    "nr", "np", "sr", "ns",
]


def collecter_cube(conn: psycopg.Connection) -> dict[str, Any]:
    """Exporte un cube d'agrégats au grain (ligne × jour × heure × météo).

    C'est ce qui rend le tableau de bord réellement interactif : au lieu de
    figer des résultats pré-calculés, on livre les briques élémentaires, et le
    navigateur recompose n'importe quel indicateur sous n'importe quel filtre —
    exactement comme un moteur OLAP. Les mesures sont des sommes et des
    comptages, donc **additives** : on peut les cumuler dans tous les sens sans
    fausser le résultat (les ratios, eux, se recalculent à la lecture).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT l.code_ligne, l.mode_transport,
                   to_char(d.jour, 'YYYY-MM-DD'), d.est_weekend, d.type_jour_reseau,
                   c.sk_creneau, c.libelle_creneau, c.est_heure_pointe,
                   m.tranche_precipitation, m.famille_condition,
                   count(*) FILTER (WHERE NOT f.est_supprime)                    AS n_real,
                   count(*) FILTER (WHERE f.est_ponctuel)                        AS n_ponct,
                   coalesce(sum(f.retard_secondes) FILTER (WHERE NOT f.est_supprime), 0) AS sum_retard,
                   count(*) FILTER (WHERE f.est_supprime)                        AS n_supp
              FROM entrepot.fait_passage f
              JOIN entrepot.dim_ligne   l ON l.sk_ligne = f.sk_ligne AND f.sk_ligne <> -1
              JOIN entrepot.dim_date    d ON d.sk_date = f.sk_date
              JOIN entrepot.dim_creneau c ON c.sk_creneau = f.sk_creneau
              JOIN entrepot.dim_meteo   m ON m.sk_meteo = f.sk_meteo
             GROUP BY 1,2,3,4,5,6,7,8,9,10
            """
        )
        lignes = cur.fetchall()
    LOG.info("Cube exporté : %d cellules", len(lignes))
    return {"cols": COLONNES_CUBE, "rows": [list(r) for r in lignes]}


def collecter_lignes_info(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Référentiel des lignes (code, nom, mode) pour les libellés et la recherche."""
    return _lignes(
        conn,
        "SELECT code_ligne, nom_ligne, mode_transport "
        "  FROM restitution.v_ponctualite_ligne ORDER BY code_ligne",
    )


def _json_defaut(valeur: Any) -> Any:
    """Sérialise les types PostgreSQL que json ne connaît pas (date, Decimal)."""
    from decimal import Decimal

    if isinstance(valeur, (dt.date, dt.datetime)):
        return valeur.isoformat()
    if isinstance(valeur, Decimal):
        return float(valeur)
    raise TypeError(f"Type non sérialisable : {type(valeur)}")


def exporter_json(
    conn: psycopg.Connection, configuration: Configuration, id_execution: int
) -> Path:
    """Écrit les données du tableau de bord en JSON et renvoie le chemin."""
    donnees = collecter_donnees_dashboard(conn, id_execution)
    chemin = configuration.repertoire_rapports / f"dashboard_donnees_{id_execution:04d}.json"
    chemin.write_text(
        json.dumps(donnees, ensure_ascii=False, indent=2, default=_json_defaut),
        encoding="utf-8",
    )
    LOG.info("Données du tableau de bord exportées : %s", chemin)
    return chemin


def generer_dashboard(
    conn: psycopg.Connection, configuration: Configuration, id_execution: int
) -> Path:
    """Génère le tableau de bord HTML autonome (graphes Chart.js) et le renvoie.

    Les données sont injectées dans la page en JSON : le fichier est **autonome**,
    ouvrable hors ligne (Chart.js est chargé depuis un CDN). C'est le pendant
    visuel du rapport tabulaire de ``rapport.py``.
    """
    from jinja2 import Environment, FileSystemLoader

    # Charge utile INTERACTIVE : le cube (briques recomposables), le référentiel
    # des lignes, la qualité (propre à l'exécution) et la métadonnée. Le tableau
    # de bord recalcule tous les indicateurs à partir de ce cube, selon les
    # filtres choisis par l'utilisateur — aucun résultat n'est figé.
    donnees = collecter_donnees_dashboard(conn, id_execution)
    charge = {
        "meta": donnees["meta"],
        "qualite": donnees["qualite"],
        "lignes": collecter_lignes_info(conn),
        "cube": collecter_cube(conn),
    }
    # `default=_json_defaut` convertit dates et Decimal ; le résultat est du JSON
    # que l'on injecte tel quel dans un <script> (marqué safe côté gabarit).
    donnees_json = json.dumps(charge, ensure_ascii=False, default=_json_defaut).replace(
        "</", "<\\/"  # neutralise un éventuel </script> présent dans une donnée
    )

    environnement = Environment(
        loader=FileSystemLoader(Path(__file__).parent / "gabarits"),
        # Le gabarit est un document HTML complet, pas un fragment à échapper :
        # les seules valeurs dynamiques sont du JSON déjà assaini et un entier.
        autoescape=False,
    )
    gabarit = environnement.get_template("dashboard.html.j2")
    html = gabarit.render(donnees_json=donnees_json, id_execution=id_execution)

    chemin = configuration.repertoire_rapports / f"dashboard_{id_execution:04d}.html"
    chemin.write_text(html, encoding="utf-8")
    LOG.info("Tableau de bord généré : %s", chemin)
    return chemin
