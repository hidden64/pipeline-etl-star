"""Étape T (transformation, partie dimensions) : alimentation SCD2.

Peuple les dimensions du schéma en étoile depuis le staging :
  * ``dim_ligne`` et ``dim_arret`` en SCD2 (historisées) ;
  * ``dim_meteo`` en junk dimension (combinaisons distinctes).

Comme pour la validation, la logique vit en SQL (procédures ``charger_dim_*``) ;
Python orchestre et journalise.

La **date d'effet** mérite une explication. En SCD2, une nouvelle version prend
effet à une date précise. On la prend égale à la date de début de validité du
feed GTFS (``feed_info.feed_start_date``) : c'est la date à laquelle la nouvelle
description du réseau devient officielle. À défaut, on retombe sur la date du
jour.
"""

from __future__ import annotations

import datetime as dt

import psycopg

from .base import etape
from .journal import obtenir_journal

LOG = obtenir_journal("dimensions")


def date_effet_du_feed(conn: psycopg.Connection) -> dt.date:
    """Date de début de validité du feed GTFS, ou aujourd'hui à défaut.

    ``feed_start_date`` est au format AAAAMMJJ. On la lit dans le staging plutôt
    que de la coder en dur : elle change à chaque nouveau feed.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT feed_start_date FROM staging.gtfs_feed_info LIMIT 1")
        ligne = cur.fetchone()

    if ligne and ligne[0]:
        try:
            return dt.datetime.strptime(ligne[0].strip(), "%Y%m%d").date()
        except ValueError:
            LOG.warning("feed_start_date illisible : %r, repli sur aujourd'hui.", ligne[0])
    return dt.date.today()


def charger_dimensions(
    conn: psycopg.Connection, id_execution: int
) -> dict[str, int]:
    """Alimente les trois dimensions issues du GTFS et de la météo.

    Renvoie un décompte des versions/combinaisons créées, pour le rapport.
    L'ordre n'a pas d'importance entre elles : les dimensions sont indépendantes.
    Elles doivent seulement être toutes prêtes AVANT la construction des faits
    (phase 6), qui a besoin de leurs clés de substitution.
    """
    date_effet = date_effet_du_feed(conn)
    LOG.info("Date d'effet des versions de dimension : %s", date_effet)

    volumetries: dict[str, int] = {}

    with etape(conn, id_execution, "Dimension ligne (SCD2)", "DIMENSION") as suivi:
        avant = _compter(conn, "entrepot.dim_ligne")
        with conn.cursor() as cur:
            cur.execute("CALL staging.charger_dim_ligne(%s, %s)", (id_execution, date_effet))
        volumetries["dim_ligne"] = _compter(conn, "entrepot.dim_ligne") - avant
        suivi.lignes_inserees = volumetries["dim_ligne"]

    with etape(conn, id_execution, "Dimension arrêt (SCD2)", "DIMENSION") as suivi:
        avant = _compter(conn, "entrepot.dim_arret")
        with conn.cursor() as cur:
            cur.execute("CALL staging.charger_dim_arret(%s, %s)", (id_execution, date_effet))
        volumetries["dim_arret"] = _compter(conn, "entrepot.dim_arret") - avant
        suivi.lignes_inserees = volumetries["dim_arret"]

    with etape(conn, id_execution, "Dimension météo", "DIMENSION") as suivi:
        avant = _compter(conn, "entrepot.dim_meteo")
        with conn.cursor() as cur:
            cur.execute("CALL staging.charger_dim_meteo(%s)", (id_execution,))
        volumetries["dim_meteo"] = _compter(conn, "entrepot.dim_meteo") - avant
        suivi.lignes_inserees = volumetries["dim_meteo"]

    LOG.info("Dimensions chargées : %s", volumetries)
    return volumetries


def _compter(conn: psycopg.Connection, table: str) -> int:
    """Nombre de lignes d'une table (pour mesurer les créations d'une étape)."""
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {table}")
        return cur.fetchone()[0]
