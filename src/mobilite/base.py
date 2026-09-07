"""Accès à la base : connexion, migrations, journal d'exécution.

Trois responsabilités, volontairement regroupées parce qu'elles partagent le
même besoin : parler à PostgreSQL de façon fiable et traçable.

1. **Connexion** — un point d'entrée unique, avec le fuseau et l'encodage fixés.
2. **Migrations** — appliquer les scripts de ``sql/`` dans l'ordre, une seule
   fois, en gardant trace de ce qui a été appliqué.
3. **Journal d'exécution** — alimenter ``meta.execution`` et ``meta.etape``, qui
   servent au rapport automatisé de la phase 7 et à l'audit.
"""

from __future__ import annotations

import hashlib
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import psycopg
from psycopg import sql

from .config import Configuration, RACINE_PROJET
from .journal import obtenir_journal

LOG = obtenir_journal("base")

# Ordre d'application : structure, puis données de référence, puis objets de
# transformation (fonctions, vues, procédures). Cet ordre est impératif — les
# procédures de sql/transform référencent les tables de sql/ddl.
REPERTOIRES_MIGRATION = ("sql/ddl", "sql/seed", "sql/transform", "sql/restitution")


class ErreurBase(RuntimeError):
    """Échec d'une opération de base de données."""


# --------------------------------------------------------------------------- #
#  Connexion
# --------------------------------------------------------------------------- #
@contextmanager
def connexion(
    configuration: Configuration, *, autocommit: bool = False
) -> Iterator[psycopg.Connection]:
    """Ouvre une connexion et la referme quoi qu'il arrive.

    ``autocommit=True`` est nécessaire pour les ordres non transactionnels
    (``CREATE DATABASE``, ``VACUUM``) et pratique pour les migrations, où l'on
    veut que chaque script validé le reste même si le suivant échoue.

    Par défaut on reste en **transaction explicite** : le bloc ``with`` valide à
    la sortie normale et annule sur exception. C'est ce qui garantit qu'un
    chargement à moitié fait ne laisse jamais l'entrepôt dans un état
    incohérent.
    """
    conn = psycopg.connect(configuration.chaine_connexion, autocommit=autocommit)
    LOG.debug("Connexion ouverte : %s", configuration.chaine_connexion_masquee)
    try:
        yield conn
    except Exception:
        if not autocommit:
            conn.rollback()
        raise
    else:
        if not autocommit:
            conn.commit()
    finally:
        conn.close()


def tester_connexion(configuration: Configuration) -> str:
    """Vérifie l'accès à la base et renvoie la version du serveur."""
    with connexion(configuration, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT version(), current_user, current_setting('TimeZone')")
            version, utilisateur, fuseau = cur.fetchone()
    LOG.info("Connecté en tant que %s (fuseau %s)", utilisateur, fuseau)
    return version


# --------------------------------------------------------------------------- #
#  Migrations
# --------------------------------------------------------------------------- #
_DDL_TABLE_MIGRATION = """
CREATE SCHEMA IF NOT EXISTS meta;
CREATE TABLE IF NOT EXISTS meta.migration (
    nom_fichier          text PRIMARY KEY,
    hash_sha256          text        NOT NULL,
    horodate_application timestamptz NOT NULL DEFAULT now(),
    duree_ms             bigint      NOT NULL
);
"""


def appliquer_migrations(
    configuration: Configuration, racine: Path | None = None
) -> list[str]:
    """Applique les scripts SQL non encore appliqués. Renvoie la liste traitée.

    Le suivi se fait dans ``meta.migration``, par **nom de fichier et empreinte**.
    Trois cas :

    * fichier inconnu           → on l'applique et on l'enregistre ;
    * fichier connu, même hash  → on le saute ;
    * fichier connu, hash changé → on le réapplique et on avertit.

    Ce troisième cas mérite d'être compris. Dans un projet en production, on ne
    modifie **jamais** une migration déjà passée : on en ajoute une nouvelle,
    parce que les autres environnements ont déjà appliqué l'ancienne version.
    Ici, tous les scripts sont écrits pour être rejouables
    (``CREATE ... IF NOT EXISTS``, ``ON CONFLICT DO NOTHING``), donc réappliquer
    est sans danger et pratique en développement. L'avertissement est là pour
    rappeler que ce confort n'est pas transposable tel quel.
    """
    racine = racine or RACINE_PROJET
    fichiers: list[Path] = []
    for repertoire in REPERTOIRES_MIGRATION:
        chemin = racine / repertoire
        if chemin.is_dir():
            fichiers.extend(sorted(chemin.glob("*.sql")))

    if not fichiers:
        LOG.warning("Aucun script de migration trouvé sous %s", racine)
        return []

    appliques: list[str] = []
    # autocommit : chaque script est validé indépendamment. Si le cinquième
    # échoue, les quatre premiers restent en place et la reprise est immédiate.
    with connexion(configuration, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(_DDL_TABLE_MIGRATION)
            cur.execute("SELECT nom_fichier, hash_sha256 FROM meta.migration")
            deja = dict(cur.fetchall())

        for fichier in fichiers:
            nom = f"{fichier.parent.name}/{fichier.name}"
            contenu = fichier.read_text(encoding="utf-8")
            empreinte = hashlib.sha256(contenu.encode("utf-8")).hexdigest()

            if deja.get(nom) == empreinte:
                LOG.debug("Migration déjà appliquée, ignorée : %s", nom)
                continue
            if nom in deja:
                LOG.warning(
                    "Migration %s modifiée depuis son application. Réapplication. "
                    "En production, on ajouterait un nouveau script au lieu de "
                    "modifier celui-ci.", nom,
                )

            debut = time.monotonic()
            try:
                with conn.cursor() as cur:
                    cur.execute(contenu)
            except psycopg.Error as exc:
                raise ErreurBase(f"Échec de la migration {nom} : {exc}") from exc
            duree_ms = int((time.monotonic() - debut) * 1000)

            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO meta.migration (nom_fichier, hash_sha256, duree_ms)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (nom_fichier) DO UPDATE
                       SET hash_sha256 = EXCLUDED.hash_sha256,
                           horodate_application = now(),
                           duree_ms = EXCLUDED.duree_ms
                    """,
                    (nom, empreinte, duree_ms),
                )
            LOG.info("Migration appliquée : %-34s (%d ms)", nom, duree_ms)
            appliques.append(nom)

    if not appliques:
        LOG.info("Schéma déjà à jour : aucune migration à appliquer.")
    return appliques


# --------------------------------------------------------------------------- #
#  Journal d'exécution
# --------------------------------------------------------------------------- #
def demarrer_execution(
    conn: psycopg.Connection, parametres: dict[str, Any] | None = None
) -> int:
    """Ouvre une exécution dans ``meta.execution`` et renvoie son identifiant.

    Positionne aussi le paramètre de session ``mobilite.id_execution``, que les
    DEFAULT des tables de staging viennent lire (cf. ``staging.execution_courante``).
    C'est ce mécanisme qui permet à ``COPY`` de tracer la provenance de chaque
    ligne sans repasser derrière avec un UPDATE de masse.
    """
    from psycopg.types.json import Jsonb

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO meta.execution (parametres) VALUES (%s) RETURNING id_execution",
            (Jsonb(parametres or {}),),
        )
        id_execution = cur.fetchone()[0]
        # SET LOCAL serait annulé au COMMIT ; on veut que le paramètre survive
        # à toute la session, donc SET simple.
        cur.execute(
            sql.SQL("SET mobilite.id_execution = {}").format(sql.Literal(str(id_execution)))
        )
    LOG.info("Exécution #%d démarrée", id_execution)
    return id_execution


def terminer_execution(
    conn: psycopg.Connection,
    id_execution: int,
    statut: str = "SUCCES",
    message_erreur: str | None = None,
) -> None:
    """Clôture une exécution."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE meta.execution
               SET horodate_fin = now(), statut = %s, message_erreur = %s
             WHERE id_execution = %s
            """,
            (statut, message_erreur, id_execution),
        )
    LOG.info("Exécution #%d terminée : %s", id_execution, statut)


class _Etape:
    """Compteurs d'une étape, alimentés par le code appelant."""

    __slots__ = ("id_etape", "lignes_lues", "lignes_inserees", "lignes_rejetees", "message")

    def __init__(self, id_etape: int) -> None:
        self.id_etape = id_etape
        self.lignes_lues = 0
        self.lignes_inserees = 0
        self.lignes_rejetees = 0
        self.message: str | None = None


@contextmanager
def etape(
    conn: psycopg.Connection, id_execution: int, nom: str, phase: str
) -> Iterator[_Etape]:
    """Encadre une étape du pipeline et enregistre son bilan dans ``meta.etape``.

    Le bilan est écrit **dans tous les cas**, succès comme échec — c'est tout
    l'intérêt d'un gestionnaire de contexte ici. Une étape qui plante sans
    laisser de trace est le pire scénario pour un diagnostic.

    Usage ::

        with etape(conn, id_exec, "chargement realisations", "CHARGEMENT") as e:
            e.lignes_lues = 2_355_913
            e.lignes_inserees = 2_355_913
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO meta.etape (id_execution, nom_etape, phase)
            VALUES (%s, %s, %s) RETURNING id_etape
            """,
            (id_execution, nom, phase),
        )
        suivi = _Etape(cur.fetchone()[0])

    debut = time.monotonic()
    LOG.info("→ %s", nom)
    try:
        yield suivi
    except Exception as exc:
        _cloturer_etape(conn, suivi, "ECHEC", str(exc)[:2000])
        LOG.error("✗ %s : %s", nom, exc)
        raise
    else:
        _cloturer_etape(conn, suivi, "SUCCES", suivi.message)
        LOG.info(
            "✓ %s — %d lues, %d insérées, %d rejetées (%.1f s)",
            nom, suivi.lignes_lues, suivi.lignes_inserees, suivi.lignes_rejetees,
            time.monotonic() - debut,
        )


def _cloturer_etape(
    conn: psycopg.Connection, suivi: _Etape, statut: str, message: str | None
) -> None:
    """Écrit le bilan d'une étape.

    Une connexion en erreur ne peut plus exécuter de requête tant que la
    transaction n'est pas annulée. On protège donc l'écriture : ne pas réussir à
    journaliser un échec ne doit pas masquer l'échec lui-même.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE meta.etape
                   SET horodate_fin = now(), statut = %s, message = %s,
                       lignes_lues = %s, lignes_inserees = %s, lignes_rejetees = %s
                 WHERE id_etape = %s
                """,
                (statut, message, suivi.lignes_lues, suivi.lignes_inserees,
                 suivi.lignes_rejetees, suivi.id_etape),
            )
    except psycopg.Error as exc:
        LOG.warning("Impossible d'enregistrer le bilan de l'étape : %s", exc)


def enregistrer_source_fichier(
    conn: psycopg.Connection, id_execution: int, descripteur: Any
) -> bool:
    """Enregistre un fichier ingéré. Renvoie ``False`` s'il l'avait déjà été.

    L'unicité porte sur ``(code_source, hash_sha256)`` : c'est le cœur de
    l'**idempotence**. Retélécharger le même GTFS et relancer le pipeline ne
    doit pas dupliquer les faits. Le contenu, pas le nom, fait foi — un fichier
    renommé reste le même fichier.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO meta.source_fichier
                (id_execution, code_source, nom_fichier, chemin_local,
                 url_origine, taille_octets, hash_sha256, version_source)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (code_source, hash_sha256) DO NOTHING
            RETURNING id_source_fichier
            """,
            (
                id_execution, descripteur.code_source, descripteur.nom_fichier,
                descripteur.chemin_local, descripteur.url_origine,
                descripteur.taille_octets, descripteur.hash_sha256,
                descripteur.version_source,
            ),
        )
        nouveau = cur.fetchone() is not None

    if not nouveau:
        LOG.info(
            "%s : contenu déjà ingéré (empreinte %s…), rechargement inutile.",
            descripteur.nom_fichier, descripteur.hash_sha256[:12],
        )
    return nouveau
