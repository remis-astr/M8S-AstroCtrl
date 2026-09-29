"""
API de commande M8S — pilote OnStep/OnStepX en LX200 série direct (module
`onstep`, sans INDI) et déclenche le plate solving ASTAP sur les frames
captées par la caméra allsky (RPi0, réseau gadget USB 192.168.7.3).

Appelée par la page HTML d'allsky (voir CONCEPTION.md), utilisable aussi
directement (curl, tests).
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

import astro
import guider as gd
import journal
import solver
import urllib.request
from align import AlignJob
from axismeas import StepsJob
from onstep import OnStep, OnStepError, parse_degrees

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("m8s-ctrl")

STATE_PATH = Path("/var/lib/m8s-ctrl/state.json")
# Référence de temps = allsky (29/09) : sa page règle son horloge sur celle
# du téléphone à chaque connexion. Le NTP du M8S est désactivé (pas
# d'Internet au jardin) et son horloge est recalée sur celle d'allsky.
# La E4 n'est jamais écrite : son écart est seulement contrôlé
# (/mount/site), l'utilisateur la met à l'heure via SWS.
ALLSKY_TIME_URL = "http://192.168.7.3:8000/system/time"
CLOCK_TOLERANCE_S = 0.5
CLOCK_RESYNC_PERIOD_S = 900
MOUNT_CLOCK_TOLERANCE_S = 5.0

mount = OnStep()
align_job = AlignJob(mount)
steps_job = StepsJob(mount)
real_guider = gd.Guider(gd.AllskySource(), gd.OnStepIO(mount), settings=gd.load_settings())
guider = real_guider           # remplacé par un guideur simulé en mode simulation
clock_state: dict = {}


def sync_clock_from_allsky() -> dict:
    """Recale l'horloge système du M8S sur celle d'allsky (aller-retour
    HTTP compensé de moitié)."""
    t0 = time.time()
    with urllib.request.urlopen(ALLSKY_TIME_URL, timeout=5) as r:
        remote = json.loads(r.read())["epoch_ms"] / 1000
    t1 = time.time()
    delta = remote - (t0 + t1) / 2
    applied = abs(delta) > CLOCK_TOLERANCE_S
    if applied:
        time.clock_settime(time.CLOCK_REALTIME, time.time() + delta)
        log.info("horloge système recalée sur allsky: %+.2f s", delta)
    clock_state.update(source="allsky", last_sync=time.time(), delta_s=round(delta, 3),
                       applied=applied, rtt_s=round(t1 - t0, 3))
    return dict(clock_state)


def clock_loop() -> None:
    while True:
        time.sleep(CLOCK_RESYNC_PERIOD_S)
        try:
            sync_clock_from_allsky()
        except Exception as e:
            log.warning("recalage horaire sur allsky impossible: %s", e)


app = FastAPI(title="m8s-ctrl")


# FastAPI 0.92 (Debian bookworm) : pas de paramètre `lifespan`, qui serait
# ignoré sans erreur — on utilise donc l'ancien crochet on_event.
@app.on_event("startup")
def startup() -> None:
    # Connexion dès le démarrage pour que l'éventuel reset de l'ESP32 à
    # l'ouverture du port ait lieu maintenant, pas pendant une commande.
    try:
        sync_clock_from_allsky()
    except Exception as e:
        log.warning("heure d'allsky illisible au démarrage: %s", e)
    try:
        mount.connect()
    except OnStepError as e:
        log.warning("monture non connectée au démarrage: %s", e)
    threading.Thread(target=clock_loop, daemon=True).start()


# La page HTML est servie par allsky et appelle cette API directement.
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def mount_call(fn, *args):
    try:
        return fn(*args)
    except OnStepError as e:
        raise HTTPException(502, str(e)) from e


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
    result = {"connected": False, "last_solve": read_state().get("last_solve")}
    try:
        st = mount.status()
    except OnStepError as e:
        result["error"] = str(e)
        return result
    result.update(connected=True, port=mount.port, baud=mount.baud,
                  product=mount.product, version=mount.version, **st)
    return result


def local_sidereal_hours(utc: dt.datetime, lon_east_deg: float) -> float:
    d = utc.timestamp() / 86400 + 2440587.5 - 2451545.0
    gmst = (18.697374558 + 24.06570982441908 * d) % 24
    return (gmst + lon_east_deg / 15) % 24


@app.get("/mount/site")
def mount_site():
    """Site et heure de la monture, avec deux contrôles de cohérence :
    horloge du M8S vs UTC monture, et temps sidéral de la monture vs celui
    recalculé depuis son UTC et sa longitude (détecte un fuseau ou une
    longitude incohérents)."""
    info = mount_call(mount.site_time)
    utc, read_at = mount_call(mount.utc_time)
    lst_mount = mount_call(mount.sidereal_hours)
    lon_east = -parse_degrees(info["longitude"])   # LX200 : est négatif
    lst_calc = local_sidereal_hours(utc, lon_east)
    mount_error = round((utc - read_at).total_seconds(), 3)
    info.update(
        utc=utc.isoformat(timespec="milliseconds"),
        system_minus_mount_s=-mount_error,
        # horloge de la E4 comparée à celle du M8S (= allsky = téléphone)
        mount_clock_error_s=mount_error,
        mount_clock_ok=abs(mount_error) <= MOUNT_CLOCK_TOLERANCE_S,
        sidereal_mount_h=round(lst_mount, 6),
        sidereal_error_s=round(((lst_mount - lst_calc + 12) % 24 - 12) * 3600, 2),
        clock=dict(clock_state),
    )
    return info


@app.post("/time/sync")
def time_sync():
    try:
        return sync_clock_from_allsky()
    except Exception as e:
        raise HTTPException(502, f"heure d'allsky illisible: {e}") from e


@app.get("/time/check")
def time_check():
    """Contrôle en lecture seule contre un serveur NTP (si le M8S a accès
    à Internet, ex. par le CPL) : écart de l'horloge du M8S et de la E4.
    Ne règle rien."""
    import re
    import subprocess
    out = subprocess.run(["chronyd", "-Q", "-t", "8", "server pool.ntp.org iburst"],
                         capture_output=True, text=True, timeout=15)
    m = re.search(r"System clock wrong by (-?[\d.]+) seconds", out.stdout + out.stderr)
    if not m:
        raise HTTPException(502, f"NTP injoignable: {(out.stdout + out.stderr)[-200:]}")
    ntp_minus_system = float(m[1])
    res = {"system_error_s": round(-ntp_minus_system, 3)}
    try:
        utc, read_at = mount.utc_time()
        res["mount_error_s"] = round((utc - read_at).total_seconds() - ntp_minus_system, 3)
    except OnStepError as e:
        res["mount_error"] = str(e)
    return res


class TrackBody(BaseModel):
    action: str  # "start" ou "stop"


@app.post("/mount/track")
def mount_track(body: TrackBody):
    if body.action not in ("start", "stop"):
        raise HTTPException(400, "action doit être 'start' ou 'stop'")
    tracking = mount_call(mount.tracking, body.action == "start")
    return {"ok": True, "tracking": tracking}


class CoordBody(BaseModel):
    ra_h: float = Field(ge=0, lt=24)       # ascension droite, heures décimales
    dec_deg: float = Field(ge=-90, le=90)  # déclinaison, degrés décimaux


@app.post("/mount/slew")
def mount_slew(body: CoordBody):
    mount_call(mount.goto, body.ra_h, body.dec_deg)
    return {"ok": True, "ra_h": body.ra_h, "dec_deg": body.dec_deg}


@app.post("/mount/sync")
def mount_sync(body: CoordBody):
    mount_call(mount.sync, body.ra_h, body.dec_deg)
    return {"ok": True, "ra_h": body.ra_h, "dec_deg": body.dec_deg}


@app.post("/mount/stop")
def mount_stop():
    mount_call(mount.stop)
    return {"ok": True}


class GuideBody(BaseModel):
    direction: str               # n, s, e, w
    ms: int = Field(ge=1, le=16399)


@app.post("/mount/guide")
def mount_guide(body: GuideBody):
    mount_call(mount.pulse_guide, body.direction, body.ms)
    return {"ok": True}


# Pose et gain : ceux réglés à la main sur la page d'allsky, jamais imposés
# par le M8S (décision utilisateur du 24/09) ; ils sont relus dans chaque
# trame et renvoyés avec le résultat.
class SolveBody(BaseModel):
    sync: bool = False           # True = recalage de la monture sur le solve
    blind: bool = False          # True = sans indice de position (lent)


def _do_solve(blind: bool) -> solver.SolveResult:
    hint = None
    if not blind:
        st = mount_call(mount.status)
        hint = (st["ra_h"], st["dec_deg"])
    res = solver.solve(hint=hint)
    journal.event("solve", **res.as_dict())
    state = read_state()
    state["last_solve"] = {"ts": time.time(), **res.as_dict()}
    write_state(state)
    return res


@app.post("/solve")
def solve(body: SolveBody):
    """Capture sur allsky + plate solve ASTAP ; avec `sync`, recale la
    monture (hors alignement : sync simple ; pendant un alignement : ajoute
    un point, voir align.py)."""
    if align_job.active:
        raise HTTPException(409, "alignement en cours (en mode manuel : utiliser « ajouter ce point »)")
    res = _do_solve(body.blind)
    out = res.as_dict()
    if res.solved and body.sync:
        mount_call(mount.sync, res.ra_jnow_h, res.dec_jnow_deg)
        out["synced"] = True
    return out


class AlignBody(BaseModel):
    points: int = Field(3, ge=1, le=9)
    az_center: float = Field(180, ge=0, lt=360)   # direction du ciel dégagé
    az_span: float = Field(90, ge=0, le=300)      # étendue en azimut des points
    alt_min: float = Field(35, ge=20, le=75)
    alt_max: float = Field(65, ge=20, le=75)
    simulate: bool = False   # essai sans ciel : solve simulé


@app.post("/align/start")
def align_start(body: AlignBody):
    """Alignement automatique : la monture doit être en position home."""
    mount_call(align_job.start, body.points, body.az_center, body.az_span,
               min(body.alt_min, body.alt_max), max(body.alt_min, body.alt_max), body.simulate)
    return {"ok": True}


class ManualAlignBody(BaseModel):
    points: int = Field(3, ge=1, le=9)


@app.post("/align/manual/start")
def align_manual_start(body: ManualAlignBody):
    """Alignement manuel : la monture doit être en position home ; ensuite
    viser à la raquette et appeler /align/manual/add pour chaque point."""
    if guider.busy:
        raise HTTPException(409, "guideur occupé")
    mount_call(align_job.start_manual, body.points)
    return {"ok": True}


@app.post("/align/manual/add")
def align_manual_add():
    return mount_call(align_job.add_manual_point)


@app.get("/align/status")
def align_status():
    out = dict(align_job.state)
    if align_job.manual:
        try:
            st = mount.status()
            out["current_separation"] = align_job.separation_report(st["alt_deg"], st["az_deg"])
        except OnStepError:
            pass
    try:
        out["onstep"] = mount.align_status()
    except OnStepError as e:
        out["onstep_error"] = str(e)
    return out


@app.post("/align/abort")
def align_abort():
    align_job.abort()
    return {"ok": True}


class CenterBody(CoordBody):
    tolerance_arcsec: float = Field(60, gt=0)
    max_iterations: int = Field(4, ge=1, le=10)


@app.post("/center")
def center(body: CenterBody):
    """GoTo de centrage : goto, solve, sync, re-goto jusqu'à ce que la
    lunette guide pointe la cible à `tolerance_arcsec` près."""
    if align_job.active:
        raise HTTPException(409, "alignement en cours")
    steps = []
    for i in range(body.max_iterations):
        mount_call(mount.goto, body.ra_h, body.dec_deg)
        mount_call(mount.wait_goto_done)
        time.sleep(2)
        res = solver.solve(hint=(body.ra_h, body.dec_deg))
        if not res.solved:
            return {"ok": False, "steps": steps, "error": f"solve échoué: {res.message}"}
        err = astro.separation_arcsec(res.ra_jnow_h, res.dec_jnow_deg, body.ra_h, body.dec_deg)
        steps.append({"iteration": i + 1, "error_arcsec": round(err, 1)})
        if err <= body.tolerance_arcsec:
            return {"ok": True, "steps": steps}
        mount_call(mount.sync, res.ra_jnow_h, res.dec_jnow_deg)
    return {"ok": False, "steps": steps, "error": "tolérance non atteinte"}


# ------------------------------------------------------------ guidage -----

def _guider_call(fn, *args):
    try:
        return fn(*args)
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e


@app.post("/guide/calibrate")
def guide_calibrate():
    if align_job.active or steps_job.running:
        raise HTTPException(409, "alignement ou mesure des axes en cours")
    _guider_call(guider.start_calibration)
    return {"ok": True}


class GuideStartBody(BaseModel):
    dry_run: bool = False     # mesure l'erreur sans envoyer d'impulsion (dérive du suivi)


@app.post("/guide/start")
def guide_start(body: GuideStartBody | None = None):
    if align_job.active or steps_job.running:
        raise HTTPException(409, "alignement ou mesure des axes en cours")
    _guider_call(guider.start_guiding, bool(body and body.dry_run))
    return {"ok": True}


@app.post("/guide/stop")
def guide_stop():
    guider.stop()
    return {"ok": True}


@app.get("/guide/status")
def guide_status(points: int = 120):
    out = guider.status(points)
    out["simulated"] = guider is not real_guider
    return out


@app.get("/guide/settings")
def guide_settings_get():
    return gd.asdict(guider.settings)


@app.post("/guide/settings")
def guide_settings_set(changes: dict):
    """Mise à jour partielle des réglages (clés de guider.Settings)."""
    known = gd.asdict(guider.settings)
    bad = [k for k in changes if k not in known]
    if bad:
        raise HTTPException(400, f"réglages inconnus: {bad}")
    try:
        for k, v in changes.items():
            setattr(guider.settings, k, gd.coerce(known[k], v))
    except (TypeError, ValueError) as e:
        raise HTTPException(400, f"valeur invalide: {e}") from e
    if guider is real_guider:
        gd.save_settings(guider.settings)      # persistants (redémarrage du service)
        journal.event("guide_settings", changes=changes)
    return gd.asdict(guider.settings)


@app.get("/guide/preview.jpg")
def guide_preview():
    jpg = gd.preview_jpeg(guider)
    if jpg is None:
        raise HTTPException(404, "aucune trame")
    return Response(content=jpg, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store", "Cross-Origin-Resource-Policy": "cross-origin"})


class SimBody(BaseModel):
    enabled: bool
    mount_type: str = "ALTAZM"         # ALTAZM ou GEM
    field_rot_deg_s: float = 0.0042    # 15°/h ≈ ciel réel ; plus pour accélérer


@app.post("/guide/simulation")
def guide_simulation(body: SimBody):
    """Bascule le guideur sur un ciel et une monture simulés (essais et
    mise au point de l'interface sans ciel). Calibration séparée : la
    calibration réelle n'est jamais écrasée."""
    global guider
    if guider.busy:
        raise HTTPException(409, "guideur occupé : l'arrêter d'abord")
    if body.enabled:
        sky = gd.SimSky(mount_type=body.mount_type, field_rot_deg_s=body.field_rot_deg_s)
        guider = gd.Guider(sky, sky, calib_path=Path("/var/lib/m8s-ctrl/calibration_sim.json"),
                           log_events=False)
    else:
        guider = real_guider
    return {"ok": True, "simulated": guider is not real_guider}


# ------------------------------------------------------------- raquette ---

class Paddle:
    """Raquette à « homme mort » : un mouvement ne dure que tant que la page
    renouvelle l'appui (toutes les 250 ms) ; sans nouvelle pendant HOLD_S
    (doigt relâché, WiFi coupé, écran verrouillé), l'axe est arrêté. La
    vitesse manuelle d'origine d'OnStepX est restaurée à la fin."""

    HOLD_S = 0.8
    OPPOSITE = {"n": "s", "s": "n", "e": "w", "w": "e"}

    def __init__(self) -> None:
        self.active: dict[str, float] = {}
        self.saved_rate: int | None = None
        self.lock = threading.Lock()
        threading.Thread(target=self._watchdog, daemon=True).start()

    def press(self, direction: str, rate: int) -> None:
        with self.lock:
            if direction not in self.active:
                if not self.active and self.saved_rate is None:
                    self.saved_rate = mount.guide_rate_index()
                opp = self.OPPOSITE.get(direction)
                if opp in self.active:
                    mount.move_stop(opp)
                    del self.active[opp]
                mount.move_start(direction, rate)
            self.active[direction] = time.monotonic() + self.HOLD_S

    def release(self, direction: str | None = None) -> None:
        with self.lock:
            for d in ([direction] if direction else list(self.active)):
                if d in self.active:
                    mount.move_stop(d)
                    del self.active[d]
            self._restore()

    def _restore(self) -> None:
        if not self.active and self.saved_rate is not None:
            try:
                mount.set_guide_rate_index(self.saved_rate)
            finally:
                self.saved_rate = None

    def _watchdog(self) -> None:
        while True:
            time.sleep(0.1)
            now = time.monotonic()
            with self.lock:
                for d, deadline in list(self.active.items()):
                    if now > deadline:
                        try:
                            mount.move_stop(d)
                        except OnStepError as e:
                            log.warning("raquette : arrêt %s impossible: %s", d, e)
                        del self.active[d]
                        log.info("raquette : arrêt %s (plus d'appui)", d)
                try:
                    self._restore()
                except OnStepError:
                    pass


paddle = Paddle()


class PaddleBody(BaseModel):
    direction: str
    rate: int = Field(5, ge=3, le=9)


@app.post("/paddle/press")
def paddle_press(body: PaddleBody):
    if guider.busy or align_job.running or steps_job.running:
        raise HTTPException(409, "raquette indisponible pendant le guidage, la calibration, l'alignement ou la mesure des axes")
    mount_call(paddle.press, body.direction, body.rate)
    return {"ok": True, "active": list(paddle.active)}


@app.post("/paddle/release")
def paddle_release(body: dict | None = None):
    mount_call(paddle.release, (body or {}).get("direction"))
    return {"ok": True}


@app.get("/paddle/rates")
def paddle_rates():
    return OnStep.MOVE_RATES


# -------------------------------------------------------------- optique ---

class OpticsBody(BaseModel):
    focal_mm: float = Field(gt=20, lt=5000)


@app.get("/optics")
def optics_get():
    return {"focal_mm": solver.GUIDE_FOCAL_MM}


@app.post("/optics")
def optics_set(body: OpticsBody):
    """Focale de l'optique portant la caméra guide (plate solve, échelle du
    guidage). La calibration de guidage reste valable (px natifs), seul le
    passage en ″ en dépend."""
    solver.save_optics(body.focal_mm)
    return {"focal_mm": solver.GUIDE_FOCAL_MM}


# ------------------------------------------------- réglages des axes ----

@app.get("/mount/axes")
def mount_axes():
    """Réglages des axes lus dans la E4 (lecture seule) : pas par degré en
    service et en NV, micro-pas, courants, jeu, vitesse des impulsions."""
    return mount_call(mount.axis_setup)


class StepsBody(BaseModel):
    axis: int = Field(ge=1, le=2)             # 1 = azimut (AD), 2 = hauteur (Déc)
    points: int = Field(5, ge=3, le=12)
    move_s: float = Field(8.0, gt=0.5, le=60)  # durée de chaque déplacement
    rate: int = Field(9, ge=5, le=9)          # vitesse de raquette (9 = max)
    direction: str | None = None              # w/e (axe 1), n/s (axe 2) ; défaut w / n


@app.post("/mount/steps/start")
def steps_start(body: StepsBody):
    """Mesure des pas/degré d'un axe par plate solve (voir axismeas.py).
    Arrête le suivi ; aucun réglage n'est écrit dans la E4."""
    if guider.busy or align_job.active:
        raise HTTPException(409, "guideur ou alignement en cours")
    mount_call(steps_job.start, body.axis, body.points, body.move_s, body.rate, body.direction)
    return {"ok": True}


@app.get("/mount/steps/status")
def steps_status():
    return steps_job.state


@app.post("/mount/steps/abort")
def steps_abort():
    steps_job.abort()
    return {"ok": True}
