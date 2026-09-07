"""Fixtures partagées par les tests qui ont besoin de la base.

Un test « base » n'est pertinent que si PostgreSQL est joignable. Plutôt que de
les faire échouer sur une machine sans base (une CI minimale, par exemple), on
les IGNORE proprement avec ``pytest.skip``. La distinction est importante : un
test ignoré signale « non exécuté ici », un test en échec signale « le code est
cassé ». Confondre les deux érode la confiance dans la suite.

Les tests unitaires purs (conversion d'heures, configuration, masquage) ne
dépendent pas de ces fixtures et tournent partout, sans base.
"""

from __future__ import annotations

import pytest


@pytest.fixture(scope="session")
def configuration():
    """Configuration réelle, lue depuis .env. Ignore le test si elle est absente."""
    from mobilite.config import ErreurConfiguration, charger_configuration

    try:
        return charger_configuration()
    except ErreurConfiguration as exc:
        pytest.skip(f"Configuration indisponible : {exc}")


@pytest.fixture()
def connexion_base(configuration):
    """Connexion PostgreSQL. Ignore le test si le serveur est injoignable."""
    import psycopg

    from mobilite.base import connexion as ouvrir

    try:
        with ouvrir(configuration, autocommit=True) as conn:
            yield conn
    except psycopg.OperationalError as exc:
        pytest.skip(f"Base injoignable : {exc}")
