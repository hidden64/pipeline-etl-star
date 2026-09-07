"""Journalisation du pipeline.

Un pipeline ETL tourne sans personne devant l'écran. Le journal est donc la
SEULE trace exploitable en cas d'incident. Trois règles suivies ici :

1. Toujours écrire sur deux canaux : la console (pour le développement) et un
   fichier horodaté (pour l'exploitation).
2. Toujours horodater en ISO 8601 avec le fuseau : `2026-09-05T14:32:11+02:00`.
   Un journal sans fuseau devient inexploitable au changement d'heure.
3. Ne jamais journaliser de secret. La fonction `masquer_secrets` filtre les
   mots de passe présents dans les chaînes de connexion.
"""

from __future__ import annotations

import logging
import re
import sys
from datetime import datetime
from pathlib import Path

_MOTIF_SECRET = re.compile(
    r"(password|pwd|mot_de_passe|token|api_key)\s*[=:]\s*([^\s,;'\"]+)",
    re.IGNORECASE,
)


def masquer_secrets(message: str) -> str:
    """Remplace la valeur de tout paramètre ressemblant à un secret par ``***``."""
    return _MOTIF_SECRET.sub(lambda m: f"{m.group(1)}=***", message)


class _FiltreSecrets(logging.Filter):
    """Applique le masquage à chaque enregistrement avant écriture."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = masquer_secrets(record.msg)
        return True


def configurer_journal(
    repertoire_logs: Path | str = "logs",
    niveau: int = logging.INFO,
) -> logging.Logger:
    """Configure et renvoie le journal racine du pipeline.

    Idempotente : plusieurs appels ne dupliquent pas les gestionnaires, ce qui
    arriverait sinon lors des tests et produirait chaque ligne en double.
    """
    logger = logging.getLogger("mobilite")
    if logger.handlers:
        return logger

    logger.setLevel(niveau)
    logger.propagate = False

    format_ligne = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)-22s | %(message)s",
        # %z fait apparaître le décalage : indispensable au changement d'heure.
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(format_ligne)
    console.addFilter(_FiltreSecrets())
    logger.addHandler(console)

    repertoire = Path(repertoire_logs)
    repertoire.mkdir(parents=True, exist_ok=True)
    fichier = repertoire / f"mobilite_{datetime.now():%Y%m%d}.log"
    flux_fichier = logging.FileHandler(fichier, encoding="utf-8")
    flux_fichier.setFormatter(format_ligne)
    flux_fichier.addFilter(_FiltreSecrets())
    logger.addHandler(flux_fichier)

    return logger


def obtenir_journal(nom: str) -> logging.Logger:
    """Journal enfant, nommé d'après le module appelant."""
    return logging.getLogger(f"mobilite.{nom}")
