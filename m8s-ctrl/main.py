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
import solver
from align import AlignJob
from onstep import OnStep, OnStepError, parse_degrees

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("m8s-ctrl")

STATE_PATH = Path("/var/lib/m8s-ctrl/state.json")
# La monture est la référence de temps (décision utilisateur du 24/09) :
# le NTP du M8S est désactivé et l'horloge système est recalée sur la E4.
CLOCK_TOLERANCE_S = 0.5
CLOCK_RESYNC_PERIOD_S = 900

mount = OnStep()
align_job = AlignJob(mount)
real_guider = gd.Guider(gd.AllskySource(), gd.OnStepIO(mount))
guider = real_guider           # remplacé par un guideur simulé en mode simulation
clock_state: dict = {}


def sync_clock_from_mount() -> dict:
    """Recale l'horloge système du M8S sur l'UTC de la monture."""
    utc, read_at = mount.utc_time()
    delta = (utc - read_at).total_seconds()
    applied = abs(delta) > CLOCK_TOLERANCE_S
    if applied:
        time.clock_settime(time.CLOCK_REALTIME, time.time() + delta)
        log.info("horloge système recalée sur la monture: %+.2f s", delta)
    clock_state.update(last_sync=time.time(), delta_s=round(delta, 3), applied=applied)
    return dict(clock_state)


def clock_loop() -> None:
    while True:
        time.sleep(CLOCK_RESYNC_PERIOD_S)
        try:
            sync_clock_from_mount()
        except OnStepError as e:
            log.warning("recalage horaire impossible: %s", e)


app = FastAPI(title="m8s-ctrl")


# FastAPI 0.92 (Debian bookworm) : pas de paramètre `lifespan`, qui serait
# ignoré sans erreur — on utilise donc l'ancien crochet on_event.
@app.on_event("startup")
def startup() -> None:
    # Connexion dès le démarrage pour que l'éventuel reset de l'ESP32 à
    # l'ouverture du port ait lieu maintenant, pas pendant une commande.
    try:
        mount.connect()
        sync_clock_from_mount()
    except OnStepError as e:
        log.warning("monture non connectée ou heure illisible au démarrage: %s", e)
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
    info.update(
        utc=utc.isoformat(timespec="milliseconds"),
        system_minus_mount_s=round((read_at - utc).total_seconds(), 3),
        sidereal_mount_h=round(lst_mount, 6),
        sidereal_error_s=round(((lst_mount - lst_calc + 12) % 24 - 12) * 3600, 2),
        clock=dict(clock_state),
    )
    return info


@app.post("/time/sync")
def time_sync():
    return mount_call(sync_clock_from_mount)


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
    if align_job.active:
        raise HTTPException(409, "alignement en cours")
    _guider_call(guider.start_calibration)
    return {"ok": True}


@app.post("/guide/start")
def guide_start():
    if align_job.active:
        raise HTTPException(409, "alignement en cours")
    _guider_call(guider.start_guiding)
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
    for k, v in changes.items():
        setattr(guider.settings, k, v)
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
        guider = gd.Guider(sky, sky, calib_path=Path("/var/lib/m8s-ctrl/calibration_sim.json"))
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
    if guider.busy or align_job.running:
        raise HTTPException(409, "raquette indisponible pendant le guidage, la calibration ou l'alignement")
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
