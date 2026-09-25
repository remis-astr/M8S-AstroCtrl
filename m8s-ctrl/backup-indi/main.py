"""
API de commande M8S — pilote OnStepX via INDI et déclenche le plate
solving ASTAP sur les frames captées par la caméra allsky (RPi0, réseau
gadget USB 192.168.7.3).

Conçu pour être appelé par le backend FastAPI de allsky (relais depuis sa
page HTML), mais utilisable directement (curl, tests) sans dépendance.
"""
from __future__ import annotations

import configparser
import json
import subprocess
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="m8s-ctrl")

INDI_DEVICE = "LX200 OnStep"
ASTAP_CLI = "/opt/astap/astap_cli"
ALLSKY_CAPTURE_URL = "http://192.168.7.3:8000/capture.fits"
# FOV calculé pour la lunette chercheuse (177 mm) + capteur IMX327 —
# a ajuster si la focale ou le capteur change (voir ROADMAP.md).
SOLVE_FOV_DEG = 2.09
SOLVE_RADIUS_DEG = 30
STATE_PATH = Path("/var/lib/m8s-ctrl/state.json")


# ---------------------------------------------------------------- INDI ----

def indi_get(prop_spec: str) -> dict[str, str]:
    """Interroge indiserver via indi_getprop, renvoie {device.prop.elem: valeur}."""
    r = subprocess.run(
        ["indi_getprop", prop_spec], capture_output=True, text=True, timeout=5
    )
    out: dict[str, str] = {}
    for line in r.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
    return out


def indi_set(spec: str) -> None:
    r = subprocess.run(
        ["indi_setprop", spec], capture_output=True, text=True, timeout=5
    )
    if r.returncode != 0:
        detail = r.stderr.strip() or r.stdout.strip() or "erreur inconnue"
        raise HTTPException(500, f"indi_setprop a échoué ({spec}): {detail}")


def ensure_connected() -> None:
    """Connecte le driver OnStep si ce n'est pas déjà fait (idempotent)."""
    conn = indi_get(f"{INDI_DEVICE}.CONNECTION.CONNECT")
    if conn.get(f"{INDI_DEVICE}.CONNECTION.CONNECT") != "On":
        indi_set(f"{INDI_DEVICE}.CONNECTION.CONNECT=On")
        time.sleep(2)


# --------------------------------------------------------------- state ----

def read_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except Exception:
            pass
    return {}


def write_state(data: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(STATE_PATH)


# ------------------------------------------------------------- routes -----

@app.get("/status")
def status():
    coords = indi_get(f"{INDI_DEVICE}.EQUATORIAL_EOD_COORD.*")
    conn = indi_get(f"{INDI_DEVICE}.CONNECTION.*")
    track = indi_get(f"{INDI_DEVICE}.TELESCOPE_TRACK_STATE.*")
    state = read_state()
    return {
        "connected": conn.get(f"{INDI_DEVICE}.CONNECTION.CONNECT") == "On",
        "tracking": track.get(f"{INDI_DEVICE}.TELESCOPE_TRACK_STATE.TRACK_ON") == "On",
        "ra_h": coords.get(f"{INDI_DEVICE}.EQUATORIAL_EOD_COORD.RA"),
        "dec_deg": coords.get(f"{INDI_DEVICE}.EQUATORIAL_EOD_COORD.DEC"),
        "last_solve": state.get("last_solve"),
    }


class TrackBody(BaseModel):
    action: str  # "start" ou "stop"


@app.post("/mount/track")
def mount_track(body: TrackBody):
    ensure_connected()
    if body.action == "start":
        indi_set(f"{INDI_DEVICE}.TELESCOPE_TRACK_STATE.TRACK_ON=On")
    elif body.action == "stop":
        indi_set(f"{INDI_DEVICE}.TELESCOPE_TRACK_STATE.TRACK_OFF=On")
    else:
        raise HTTPException(400, "action doit être 'start' ou 'stop'")
    return {"ok": True, "action": body.action}


class SlewBody(BaseModel):
    ra_h: float     # ascension droite, heures décimales (0-24)
    dec_deg: float  # déclinaison, degrés décimaux (-90..90)


@app.post("/mount/slew")
def mount_slew(body: SlewBody):
    ensure_connected()
    indi_set(f"{INDI_DEVICE}.ON_COORD_SET.SLEW=On")
    indi_set(f"{INDI_DEVICE}.EQUATORIAL_EOD_COORD.RA={body.ra_h};DEC={body.dec_deg}")
    return {"ok": True, "ra_h": body.ra_h, "dec_deg": body.dec_deg}


@app.post("/solve")
def solve():
    """Capture une frame sur allsky, la résout avec ASTAP, sync la monture
    si le solve réussit."""
    ensure_connected()

    fits_path = Path("/tmp/m8s_solve.fits")
    ini_path = fits_path.with_suffix(".ini")
    ini_path.unlink(missing_ok=True)
    fits_path.unlink(missing_ok=True)

    r = subprocess.run(
        ["curl", "-s", "-m", "15", "-o", str(fits_path), ALLSKY_CAPTURE_URL],
        capture_output=True, timeout=20,
    )
    if r.returncode != 0 or not fits_path.exists() or fits_path.stat().st_size == 0:
        raise HTTPException(502, "capture depuis allsky échouée (fichier vide/absent)")

    subprocess.run(
        [ASTAP_CLI, "-f", str(fits_path), "-fov", str(SOLVE_FOV_DEG),
         "-r", str(SOLVE_RADIUS_DEG)],
        capture_output=True, text=True, timeout=30,
    )

    if not ini_path.exists():
        raise HTTPException(500, "astap_cli n'a produit aucun résultat (.ini absent)")

    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read_string("[astap]\n" + ini_path.read_text())
    raw = dict(cfg["astap"])
    solved = raw.get("pltsolvd", "f").lower() == "t"

    result = {"ok": solved, "raw": raw}

    state = read_state()
    state["last_solve"] = {"ok": solved, "ts": time.time(), "raw": raw}
    write_state(state)

    if not solved:
        return result

    # CRVAL1/CRVAL2 : RA/DEC résolus, en degrés (convention FITS/WCS).
    # À confirmer sur un vrai solve nocturne (voir ROADMAP.md Phase 6) —
    # non testé avec de vraies étoiles pour l'instant.
    try:
        ra_deg = float(raw["crval1"])
        dec_deg = float(raw["crval2"])
    except (KeyError, ValueError) as e:
        result["warning"] = f"solve réussi mais coordonnées illisibles: {e}"
        return result

    ra_h = ra_deg / 15.0
    result["ra_h"] = ra_h
    result["dec_deg"] = dec_deg

    indi_set(f"{INDI_DEVICE}.ON_COORD_SET.SYNC=On")
    indi_set(f"{INDI_DEVICE}.EQUATORIAL_EOD_COORD.RA={ra_h};DEC={dec_deg}")

    return result
