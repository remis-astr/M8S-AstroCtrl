"""
Journal des sessions : un fichier JSON Lines par nuit dans
/var/lib/m8s-ctrl/journal/ (calibrations, trames de guidage, mesures des
pas/degré, solves). Sert à analyser une session après coup — l'historique
en mémoire du guideur est perdu au redémarrage du service.

La « nuit » va de midi à midi (heure UTC du M8S) : une session qui passe
minuit reste dans un seul fichier.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import threading
import time
from pathlib import Path

LOG_DIR = Path("/var/lib/m8s-ctrl/journal")
_lock = threading.Lock()
log = logging.getLogger("journal")


def _night_file() -> Path:
    night = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=12)).date()
    return LOG_DIR / f"{night.isoformat()}.jsonl"


def event(kind: str, **data) -> None:
    rec = {"utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds"),
           "t": round(time.time(), 3), "kind": kind, **data}
    try:
        line = json.dumps(rec, default=float)
        with _lock:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            with _night_file().open("a") as f:
                f.write(line + "\n")
    except Exception as e:           # le journal ne doit jamais bloquer le guidage
        log.warning("journal: %s", e)
