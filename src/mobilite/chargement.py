"""Étape L du pipeline : chargement des sources brutes vers ``staging``.

Rien n'est transformé ici. On déplace des octets d'un fichier vers une table,
en conservant tout en ``text``. Toute la logique métier vient après.

------------------------------------------------------------------------------
POURQUOI `COPY` ET PAS `INSERT`
------------------------------------------------------------------------------
Un ``INSERT`` par ligne, c'est, pour chacune des 2,3 millions de lignes :
un aller-retour réseau, une analyse syntaxique, la construction d'un plan
d'exécution, et une transaction. ``COPY`` fait le tour en un seul ordre, avec un
analyseur spécialisé et sans replanifier.

L'écart mesuré sur ce projet est d'environ **un facteur 50** (voir
``comparer_copy_insert``). Sur 2,3 millions de lignes, cela fait la différence
entre une minute et une heure.

Trois précisions qui comptent en entretien :

* ``COPY ... FROM '/chemin'`` lit un fichier **côté serveur** et exige le droit
  ``pg_read_server_files``. Notre rôle ne l'a pas, et c'est voulu : le pipeline
  ne doit pas pouvoir lire n'importe quel fichier de la machine hôte. On utilise
  donc ``COPY ... FROM STDIN``, où le client pousse le flux — ce qui fonctionne
  aussi quand la base est sur une autre machine.
* ``COPY`` applique les **valeurs par défaut** des colonnes absentes de sa liste.
  C'est ce qui permet à ``id_execution`` de se remplir tout seul.
* ``COPY`` sait **transcoder** : l'option ``ENCODING 'LATIN1'`` convertit l'export
  d'exploitation en UTF-8 pendant le chargement. Inutile de relire le fichier en
  Python pour cela.
"""

from __future__ import annotations

import csv
import io
import json
import time
from pathlib import Path
from typing import Iterable

import psycopg
from psycopg import sql

from .base import enregistrer_source_fichier, etape
from .config import Configuration
from .extraction import SourceFichier, empreinte_sha256
from .journal import obtenir_journal

LOG = obtenir_journal("chargement")

TAILLE_BLOC_OCTETS = 1 << 20  # 1 Mio : compromis usuel entre appels et mémoire

# Correspondance entre encodages Python et noms d'encodage PostgreSQL.
ENCODAGES_PG = {
    "utf-8": "UTF8",
    "utf-8-sig": "UTF8",
    "latin-1": "LATIN1",
    "iso-8859-1": "LATIN1",
    "cp1252": "WIN1252",
}

# Quel fichier GTFS va dans quelle table de staging.
TABLES_GTFS = {
    "agency.txt": "gtfs_agency",
    "routes.txt": "gtfs_routes",
    "stops.txt": "gtfs_stops",
    "trips.txt": "gtfs_trips",
    "stop_times.txt": "gtfs_stop_times",
    "calendar.txt": "gtfs_calendar",
    "calendar_dates.txt": "gtfs_calendar_dates",
    "feed_info.txt": "gtfs_feed_info",
}


class ErreurChargement(RuntimeError):
    """Le fichier source ne correspond pas à la table cible."""


# --------------------------------------------------------------------------- #
#  Introspection
# --------------------------------------------------------------------------- #
def colonnes_de_table(
    conn: psycopg.Connection, schema: str, table: str
) -> list[str]:
    """Colonnes d'une table, dans l'ordre de définition."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name
              FROM information_schema.columns
             WHERE table_schema = %s AND table_name = %s
             ORDER BY ordinal_position
            """,
            (schema, table),
        )
        return [ligne[0] for ligne in cur.fetchall()]


def lire_entete(chemin: Path, encodage: str, delimiteur: str) -> list[str]:
    """Lit la première ligne du fichier et renvoie les noms de colonnes normalisés.

    On lit l'en-tête **du fichier** au lieu de coder l'ordre des colonnes en dur.
    Le GTFS est une spécification ouverte : un producteur peut ajouter une
    colonne facultative ou en changer l'ordre d'une version à l'autre. Un
    chargeur qui suppose un ordre fixe casse silencieusement — en décalant
    toutes les valeurs d'une colonne, ce qui est bien pire qu'une erreur franche.
    """
    with chemin.open(encoding=encodage, newline="") as flux:
        premiere_ligne = flux.readline()
    if not premiere_ligne:
        raise ErreurChargement(f"{chemin.name} est vide.")

    lecteur = csv.reader(io.StringIO(premiere_ligne), delimiter=delimiteur)
    return [colonne.strip().lower() for colonne in next(lecteur)]


# --------------------------------------------------------------------------- #
#  Chargement d'un CSV
# --------------------------------------------------------------------------- #
def charger_csv(
    conn: psycopg.Connection,
    chemin: Path,
    schema: str,
    table: str,
    *,
    encodage: str = "utf-8-sig",
    delimiteur: str = ",",
    vider_avant: bool = True,
) -> int:
    """Charge un CSV dans une table de staging par ``COPY``. Renvoie le nombre de lignes.

    Le fichier est poussé **par blocs de 1 Mio, sans jamais être lu en entier en
    mémoire**. ``stop_times.txt`` fait 112 Mo et l'export d'exploitation 188 Mo :
    un ``read()`` intégral fonctionnerait ici, mais la même fonction doit tenir
    sur un fichier de plusieurs gigaoctets.
    """
    colonnes_fichier = lire_entete(chemin, encodage, delimiteur)
    colonnes_table = set(colonnes_de_table(conn, schema, table))

    inconnues = [c for c in colonnes_fichier if c not in colonnes_table]
    if inconnues:
        # Erreur franche, avec le remède. Ignorer silencieusement une colonne
        # inconnue laisserait croire que tout va bien alors qu'on perd de la
        # donnée à chaque exécution.
        raise ErreurChargement(
            f"{chemin.name} contient des colonnes absentes de {schema}.{table} : "
            f"{inconnues}. La source a changé de format : ajoutez ces colonnes "
            f"en `text` dans sql/ddl/05_staging.sql."
        )

    encodage_pg = ENCODAGES_PG.get(encodage.lower())
    if encodage_pg is None:
        raise ErreurChargement(f"Encodage non pris en charge : {encodage}")

    with conn.cursor() as cur:
        if vider_avant:
            # RESTART IDENTITY remet le compteur à 1, de sorte que `numero_ligne`
            # corresponde au rang de la ligne dans le fichier. Précision utile :
            # c'est l'ordre d'INSERTION, qui n'égale l'ordre du fichier que
            # parce qu'un COPY est séquentiel — ce qui est le cas ici.
            cur.execute(
                sql.SQL("TRUNCATE TABLE {}.{} RESTART IDENTITY").format(
                    sql.Identifier(schema), sql.Identifier(table)
                )
            )

        requete = sql.SQL(
            "COPY {}.{} ({}) FROM STDIN "
            "WITH (FORMAT csv, DELIMITER {}, HEADER true, ENCODING {})"
        ).format(
            sql.Identifier(schema),
            sql.Identifier(table),
            sql.SQL(", ").join(sql.Identifier(c) for c in colonnes_fichier),
            sql.Literal(delimiteur),
            sql.Literal(encodage_pg),
        )

        debut = time.monotonic()
        with cur.copy(requete) as copie:
            with chemin.open("rb") as flux:
                while bloc := flux.read(TAILLE_BLOC_OCTETS):
                    copie.write(bloc)
        lignes = cur.rowcount

    duree = time.monotonic() - debut
    LOG.info(
        "  %-22s → %-22s %10d lignes en %6.1f s (%s lignes/s)",
        chemin.name, f"{schema}.{table}", lignes, duree,
        f"{lignes / duree:,.0f}".replace(",", " ") if duree > 0 else "—",
    )
    return lignes


# --------------------------------------------------------------------------- #
#  Chargement des trois sources
# --------------------------------------------------------------------------- #
def charger_gtfs(
    conn: psycopg.Connection, repertoire_gtfs: Path
) -> dict[str, int]:
    """Charge tous les fichiers GTFS présents dans le répertoire décompressé."""
    volumetries: dict[str, int] = {}
    for nom_fichier, table in TABLES_GTFS.items():
        chemin = repertoire_gtfs / nom_fichier
        if not chemin.exists():
            # feed_info.txt et calendar_dates.txt sont facultatifs dans la
            # spécification GTFS : leur absence n'est pas une erreur.
            LOG.info("  %-22s absent (facultatif), ignoré.", nom_fichier)
            continue
        volumetries[table] = charger_csv(
            conn, chemin, "staging", table, encodage="utf-8-sig", delimiteur=","
        )
    return volumetries


def charger_meteo(conn: psycopg.Connection, chemin_json: Path) -> int:
    """Charge le document météo en ``jsonb``.

    Un seul enregistrement : le document complet. Le dépliage des relevés se
    fera en SQL avec ``jsonb_to_recordset`` en phase 4. Charger le JSON tel quel
    a un avantage concret — si l'API ajoute demain un champ, le chargement
    continue de fonctionner sans modification du schéma.
    """
    contenu = chemin_json.read_text(encoding="utf-8")
    document = json.loads(contenu)  # valide le JSON avant de l'envoyer

    with conn.cursor() as cur:
        cur.execute("TRUNCATE TABLE staging.meteo_brut")
        cur.execute(
            "INSERT INTO staging.meteo_brut (nom_fichier, charge) VALUES (%s, %s)",
            (chemin_json.name, contenu),
        )
    nb_releves = len(document.get("releves", []))
    LOG.info(
        "  %-22s → %-22s %10d relevés (document jsonb)",
        chemin_json.name, "staging.meteo_brut", nb_releves,
    )
    return nb_releves


def charger_realisations(conn: psycopg.Connection, chemin_csv: Path) -> int:
    """Charge l'export d'exploitation : latin-1, point-virgule, CRLF.

    Le transcodage latin-1 → UTF-8 est fait par PostgreSQL pendant le ``COPY``,
    via l'option ``ENCODING``. C'est plus rapide que de relire 188 Mo en Python
    pour les réencoder, et cela garde le chemin de données le plus court possible.
    """
    return charger_csv(
        conn, chemin_csv, "staging", "realisation",
        encodage="latin-1", delimiteur=";",
    )


# --------------------------------------------------------------------------- #
#  Orchestration
# --------------------------------------------------------------------------- #
def executer_chargement(
    conn: psycopg.Connection,
    configuration: Configuration,
    id_execution: int,
    *,
    repertoire_gtfs: Path | None = None,
    chemin_meteo: Path | None = None,
    chemin_realisations: Path | None = None,
) -> dict[str, int]:
    """Charge les trois sources en staging, chaque source étant une étape journalisée."""
    repertoire_gtfs = repertoire_gtfs or (configuration.repertoire_travail / "gtfs_en_cours")
    chemin_meteo = chemin_meteo or _dernier(configuration.repertoire_brut, "meteo_rennes_*.json")
    chemin_realisations = chemin_realisations or _dernier(
        configuration.repertoire_brut, f"realisations_{configuration.code_reseau}_2*.csv"
    )

    volumetries: dict[str, int] = {}

    with etape(conn, id_execution, "Chargement GTFS", "CHARGEMENT") as suivi:
        volumetries |= charger_gtfs(conn, repertoire_gtfs)
        suivi.lignes_lues = suivi.lignes_inserees = sum(volumetries.values())
        # La version du feed n'est connue qu'APRÈS chargement : elle est dans
        # feed_info.txt, donc dans staging. On l'attache au fichier ingéré pour
        # que le dédoublonnage inter-versions (phase 5) sache l'exploiter.
        archive = configuration.repertoire_brut / f"GTFS_{configuration.code_reseau}_EN_COURS.zip"
        if archive.exists():
            enregistrer_source_fichier(
                conn, id_execution,
                _descripteur_local(archive, "GTFS", configuration.url_gtfs,
                                   _version_feed_chargee(conn)),
            )

    with etape(conn, id_execution, "Chargement météo", "CHARGEMENT") as suivi:
        nb = charger_meteo(conn, chemin_meteo)
        volumetries["meteo_brut"] = nb
        suivi.lignes_lues = suivi.lignes_inserees = nb
        enregistrer_source_fichier(
            conn, id_execution,
            _descripteur_local(chemin_meteo, "METEO", configuration.url_meteo_archive),
        )

    with etape(conn, id_execution, "Chargement réalisations", "CHARGEMENT") as suivi:
        nb = charger_realisations(conn, chemin_realisations)
        volumetries["realisation"] = nb
        suivi.lignes_lues = suivi.lignes_inserees = nb
        enregistrer_source_fichier(
            conn, id_execution,
            _descripteur_local(chemin_realisations, "EXPLOITATION", None),
        )

    return volumetries


def _descripteur_local(
    chemin: Path, code_source: str, url: str | None = None, version: str | None = None
) -> SourceFichier:
    """Construit le descripteur d'un fichier présent sur le disque.

    L'empreinte est calculée **au moment du chargement**, pas de l'extraction :
    c'est le contenu réellement chargé qui doit être tracé. Un fichier modifié
    entre les deux étapes produirait sinon une empreinte mensongère.
    """
    return SourceFichier(
        code_source=code_source,
        nom_fichier=chemin.name,
        chemin_local=str(chemin),
        url_origine=url,
        taille_octets=chemin.stat().st_size,
        hash_sha256=empreinte_sha256(chemin),
        version_source=version,
    )


def _version_feed_chargee(conn: psycopg.Connection) -> str | None:
    """Lit ``feed_version`` dans le staging fraîchement chargé."""
    with conn.cursor() as cur:
        cur.execute("SELECT feed_version FROM staging.gtfs_feed_info LIMIT 1")
        ligne = cur.fetchone()
    return ligne[0] if ligne else None


def _dernier(repertoire: Path, motif: str) -> Path:
    """Fichier le plus récent correspondant au motif."""
    candidats = sorted(repertoire.glob(motif), key=lambda p: p.stat().st_mtime)
    if not candidats:
        raise ErreurChargement(f"Aucun fichier ne correspond à {motif} dans {repertoire}")
    return candidats[-1]


# --------------------------------------------------------------------------- #
#  Démonstration : COPY contre INSERT
# --------------------------------------------------------------------------- #
def comparer_copy_insert(
    conn: psycopg.Connection, chemin_csv: Path, nb_lignes: int = 50_000
) -> dict[str, float]:
    """Mesure l'écart entre ``COPY``, ``executemany`` et ``INSERT`` un par un.

    Sur une table temporaire, donc sans effet de bord. Trois stratégies :

    * ``INSERT`` ligne à ligne  — un aller-retour réseau par ligne ;
    * ``executemany``           — psycopg3 regroupe les ordres en lots ;
    * ``COPY``                  — un seul ordre, analyseur spécialisé.

    Le rapport obtenu est l'argument à donner quand on demande pourquoi on ne
    fait pas « simplement des INSERT ».
    """
    with chemin_csv.open(encoding="latin-1", newline="") as flux:
        lecteur = csv.reader(flux, delimiter=";")
        next(lecteur)  # en-tête
        echantillon = [ligne for _, ligne in zip(range(nb_lignes), lecteur)]

    colonnes = (
        "date_exploitation, ligne, course, sequence, code_arret, libelle_arret, "
        "h_theorique, h_reelle, etat_course, retard_sec, vitesse_moy_kmh"
    )
    resultats: dict[str, float] = {}

    with conn.cursor() as cur:
        # Pas de `ON COMMIT DROP` : en mode autocommit, le CREATE valide
        # immédiatement, ce qui déclenche la suppression aussitôt et rend la
        # table introuvable à l'ordre suivant. On la supprime explicitement à la
        # fin. Une table temporaire disparaît de toute façon à la déconnexion.
        cur.execute("DROP TABLE IF EXISTS bench")
        cur.execute(
            "CREATE TEMP TABLE bench (date_exploitation text, ligne text, course text, "
            "sequence text, code_arret text, libelle_arret text, h_theorique text, "
            "h_reelle text, etat_course text, retard_sec text, vitesse_moy_kmh text)"
        )

        # 1. INSERT un par un — sur un échantillon réduit, sinon c'est interminable.
        echantillon_reduit = echantillon[: min(5_000, len(echantillon))]
        requete = f"INSERT INTO bench ({colonnes}) VALUES ({', '.join(['%s'] * 11)})"
        debut = time.monotonic()
        for ligne in echantillon_reduit:
            cur.execute(requete, ligne)
        duree = time.monotonic() - debut
        # Ramené au débit, pour être comparable aux deux autres.
        resultats["insert_un_par_un"] = len(echantillon_reduit) / duree
        cur.execute("TRUNCATE bench")

        # 2. executemany
        debut = time.monotonic()
        cur.executemany(requete, echantillon)
        resultats["executemany"] = len(echantillon) / (time.monotonic() - debut)
        cur.execute("TRUNCATE bench")

        # 3. COPY
        debut = time.monotonic()
        with cur.copy(f"COPY bench ({colonnes}) FROM STDIN") as copie:
            for ligne in echantillon:
                copie.write_row(ligne)
        resultats["copy"] = len(echantillon) / (time.monotonic() - debut)

        cur.execute("DROP TABLE bench")

    return resultats
