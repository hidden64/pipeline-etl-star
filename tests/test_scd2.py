"""Test d'intégration du mécanisme SCD2 sur dim_ligne.

On vérifie le comportement complet d'une dimension historisée :
  1. première apparition d'une clé → une version courante ;
  2. rechargement sans changement → aucune nouvelle version (idempotence) ;
  3. changement d'attribut → ancienne version fermée, nouvelle version ouverte,
     les deux coexistant, une seule courante.

Le test travaille sur une clé de ligne DÉDIÉE (``@TEST-SCD2``) qu'il injecte
dans le staging puis nettoie, pour ne jamais perturber les vraies données.
Ignoré proprement si la base est injoignable (voir conftest.py).
"""

from __future__ import annotations

import datetime as dt

import pytest


CLE_TEST = "@TEST-SCD2"


@pytest.fixture()
def ligne_de_test(connexion_base):
    """Injecte une route de test dans le staging, la retire à la fin."""
    _nettoyer(connexion_base)
    yield connexion_base
    _nettoyer(connexion_base)


def _nettoyer(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM entrepot.dim_ligne WHERE id_ligne_source = %s", (CLE_TEST,))
        cur.execute("DELETE FROM staging.gtfs_routes WHERE route_id = %s", (CLE_TEST,))


def _injecter_route(conn, nom_long: str) -> None:
    """Écrit (ou réécrit) la route de test dans le staging avec un nom donné."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM staging.gtfs_routes WHERE route_id = %s", (CLE_TEST,))
        cur.execute(
            """
            INSERT INTO staging.gtfs_routes
                (route_id, agency_id, route_short_name, route_long_name, route_type)
            VALUES (%s, '1', 'TST', %s, '3')
            """,
            (CLE_TEST, nom_long),
        )


def _versions(conn) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT nom_ligne, date_debut_validite, date_fin_validite, est_courant
              FROM entrepot.dim_ligne
             WHERE id_ligne_source = %s
             ORDER BY date_debut_validite
            """,
            (CLE_TEST,),
        )
        return cur.fetchall()


def test_cycle_de_vie_scd2(ligne_de_test):
    conn = ligne_de_test

    # --- 1. Première apparition ---------------------------------------------
    _injecter_route(conn, "Ligne de test — version A")
    with conn.cursor() as cur:
        cur.execute("CALL staging.charger_dim_ligne(%s, %s)", (0, dt.date(2026, 1, 1)))

    versions = _versions(conn)
    assert len(versions) == 1, "une première apparition doit créer exactement une version"
    nom, debut, fin, courant = versions[0]
    assert nom == "Ligne de test — version A"
    assert courant is True
    assert fin == dt.date(9999, 12, 31), "la version courante ne se ferme pas"

    # --- 2. Rechargement à l'identique : rien ne doit bouger ----------------
    with conn.cursor() as cur:
        cur.execute("CALL staging.charger_dim_ligne(%s, %s)", (0, dt.date(2026, 2, 1)))
    assert len(_versions(conn)) == 1, "sans changement d'attribut, aucune version créée"

    # --- 3. Changement d'attribut : versionnement ---------------------------
    _injecter_route(conn, "Ligne de test — version B")
    with conn.cursor() as cur:
        cur.execute("CALL staging.charger_dim_ligne(%s, %s)", (0, dt.date(2026, 3, 15)))

    versions = _versions(conn)
    assert len(versions) == 2, "un changement doit produire une seconde version"

    ancienne, nouvelle = versions
    # L'ancienne est fermée la veille de la date d'effet du changement.
    assert ancienne[0] == "Ligne de test — version A"
    assert ancienne[2] == dt.date(2026, 3, 14), "l'ancienne se ferme à la date d'effet - 1"
    assert ancienne[3] is False
    # La nouvelle est courante, ouverte à la date d'effet.
    assert nouvelle[0] == "Ligne de test — version B"
    assert nouvelle[1] == dt.date(2026, 3, 15)
    assert nouvelle[3] is True

    # --- 4. Invariant : une seule version courante par clé ------------------
    courantes = [v for v in versions if v[3] is True]
    assert len(courantes) == 1, "il ne peut exister qu'une version courante par clé"

    # --- 5. Continuité temporelle : aucun trou entre les versions -----------
    assert ancienne[2] + dt.timedelta(days=1) == nouvelle[1], (
        "la nouvelle version doit prendre le relais dès le lendemain de la fermeture"
    )
