"""
Mesure des pas par degré d'un axe par plate solve.

Principe : suivi arrêté, on fait tourner **un seul axe** (déplacement
manuel `:Mw`/`:Mn`, sans modèle de pointage) par paliers, dans un seul
sens (le jeu est rattrapé avant le premier point). À chaque arrêt : angle
de l'axe compté par OnStepX (`:GX42#`/`:GX43#` = pas / pas par degré) et
solve ASTAP de la caméra guide.

Chaque position solvée est ramenée dans un repère lié à la Terre (angle
horaire, déclinaison) grâce à l'heure de la pose : le ciel qui tourne
pendant la mesure est ainsi retiré. Une erreur d'horloge *constante* ne
fait que tourner tous les points autour du pôle et ne change aucun angle
entre eux ; seule la dispersion des horodatages compte (d'où l'intérêt de
mesurer vers le nord, où le ciel bouge peu).

Tourner un axe fait décrire à l'axe optique un cercle autour de cet axe :
le plan ajusté sur les points donne l'axe de rotation réel, et les angles
autour de lui l'angle réellement parcouru. Pente angle vrai / angle compté
= pas configurés / pas réels. En hauteur, la réfraction (qui décroît quand
on monte) est corrigée ; en azimut elle n'a pas d'effet.

Bonus pour l'azimut : la direction de l'axe ajusté, comparée au zénith,
donne l'inclinaison de la base (valable seulement si l'horloge est juste).
"""
from __future__ import annotations

import datetime as dt
import math
import threading
import time

import numpy as np

import journal
import solver
from onstep import OnStep, OnStepError

SETTLE_S = 2.0          # amortissement des vibrations du Dobson après arrêt
STILL_TOL_DEG = 2e-5


def gmst_hours(ts: float) -> float:
    jd = ts / 86400.0 + 2440587.5
    T = (jd - 2451545.0) / 36525.0
    g = 280.46061837 + 360.98564736629 * (jd - 2451545.0) + 0.000387933 * T * T
    return (g % 360) / 15


def earth_vector(ra_h: float, dec_deg: float, lst_h: float) -> np.ndarray:
    """Vecteur unitaire dans un repère lié au lieu (x : méridien sur
    l'équateur, z : pôle nord céleste)."""
    H = math.radians((lst_h - ra_h) * 15)
    d = math.radians(dec_deg)
    return np.array([math.cos(d) * math.cos(H), -math.cos(d) * math.sin(H), math.sin(d)])


def refraction_deg(alt_deg: float) -> float:
    """Réfraction (Bennett), en degrés, pour une hauteur apparente."""
    h = max(alt_deg, 1.0)
    return 1.0 / math.tan(math.radians(h + 7.31 / (h + 4.4))) / 60


def fit_rotation(vectors: np.ndarray, counted_deg: np.ndarray) -> dict:
    """Axe de rotation (normale du plan des points) et angles réellement
    parcourus autour de lui, orientés comme les angles comptés."""
    c = vectors.mean(axis=0)
    _, _, vt = np.linalg.svd(vectors - c)
    n = vt[2]
    u = vectors - np.outer(vectors @ n, n)
    ref = u[0] / np.linalg.norm(u[0])
    ang = np.array([math.degrees(math.atan2(np.cross(ref, ui) @ n, ref @ ui)) for ui in u])
    if np.polyfit(counted_deg - counted_deg[0], ang, 1)[0] < 0:
        n, ang = -n, -ang
    return {"axis": n, "true_deg": ang}


def analyse(points: list[dict], axis: int, steps_active: float, lat_deg: float, lon_east_deg: float) -> dict:
    ok = [p for p in points if p.get("solved")]
    if len(ok) < 3:
        return {"error": f"{len(ok)} point(s) résolu(s) : il en faut au moins 3"}
    counted = np.array([p["counted_deg"] for p in ok])
    counted = np.degrees(np.unwrap(np.radians(counted)))
    vec = np.array([earth_vector(p["ra_jnow_h"], p["dec_jnow_deg"],
                                 gmst_hours(p["utc_ts"]) + lon_east_deg / 15) for p in ok])
    fit = fit_rotation(vec, counted)
    true = fit["true_deg"]
    if axis == 2:
        # la monture pointe la position apparente (vraie + réfraction)
        r = np.array([refraction_deg(p["alt_deg"]) for p in ok])
        true = true + (r - r[0])
    x = counted - counted[0]
    A = np.vstack([x, np.ones_like(x)]).T
    (slope, icpt), *_ = np.linalg.lstsq(A, true, rcond=None)
    resid = true - (slope * x + icpt)
    n = len(x)
    se = math.sqrt((resid ** 2).sum() / max(n - 2, 1) / ((x - x.mean()) ** 2).sum()) if n > 2 else float("nan")
    proposed = steps_active / slope
    out = {
        "axis": axis, "points": n, "span_counted_deg": round(float(np.ptp(x)), 4),
        "span_true_deg": round(float(np.ptp(true)), 4),
        "ratio_true_over_counted": round(float(slope), 6),
        "error_percent": round((float(slope) - 1) * 100, 4),
        "steps_per_degree_active": steps_active,
        "steps_per_degree_proposed": round(float(proposed), 1),
        "uncertainty_steps": round(float(steps_active * se / slope ** 2), 1) if se == se else None,
        "residuals_arcsec": [round(float(v) * 3600, 1) for v in resid],
        "rms_residual_arcsec": round(float(np.sqrt((resid ** 2).mean())) * 3600, 1),
        # angle entre l'axe optique et l'axe de rotation (90° pour la hauteur,
        # 90° - h pour l'azimut) : contrôle que c'est bien l'axe attendu
        "circle_radius_deg": round(math.degrees(math.acos(min(1.0, abs(float(vec[0] @ fit["axis"]))))), 3),
    }
    if axis == 1:
        # inclinaison de l'axe d'azimut par rapport au zénith du lieu
        phi = math.radians(lat_deg)
        zen = np.array([math.cos(phi), 0.0, math.sin(phi)])
        north = np.array([-math.sin(phi), 0.0, math.cos(phi)])   # horizon nord
        east = np.array([0.0, 1.0, 0.0])                          # H = -6 h
        a = fit["axis"] if fit["axis"] @ zen > 0 else -fit["axis"]
        tilt = math.degrees(math.acos(min(1.0, float(a @ zen))))
        out["azimuth_axis_tilt_arcmin"] = round(tilt * 60, 1)
        out["azimuth_axis_leans_toward_az_deg"] = round(math.degrees(math.atan2(a @ east, a @ north)) % 360)
    return out


class StepsJob:
    """Mesure en tâche de fond ; état consultable par l'API."""

    def __init__(self, mount: OnStep) -> None:
        self.mount = mount
        self._thread: threading.Thread | None = None
        self._abort = False
        self.state: dict = {"state": "idle"}

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, axis: int, points: int, move_s: float, rate: int, direction: str | None) -> None:
        if self.running:
            raise OnStepError("une mesure est déjà en cours")
        direction = direction or {1: "w", 2: "n"}[axis]
        if direction not in ({1: "we", 2: "ns"}[axis]):
            raise OnStepError(f"direction {direction!r} invalide pour l'axe {axis}")
        self._abort = False
        self.state = {"state": "running", "axis": axis, "direction": direction, "points_wanted": points,
                      "move_s": move_s, "rate": rate, "started": time.time(), "points": [], "message": ""}
        self._thread = threading.Thread(target=self._guard, args=(axis, points, move_s, rate, direction),
                                        daemon=True)
        self._thread.start()

    def abort(self) -> None:
        self._abort = True

    def _guard(self, *args) -> None:
        saved_rate = None
        try:
            saved_rate = self.mount.guide_rate_index()
            self._run(*args)
        except Exception as e:
            self.state.update(state="error", message=str(e))
            journal.event("steps_error", **{k: v for k, v in self.state.items() if k != "points"})
        finally:
            try:
                self.mount.stop()
                if saved_rate is not None:
                    self.mount.set_guide_rate_index(saved_rate)
            except OnStepError:
                pass

    def _check_abort(self) -> None:
        if self._abort:
            raise RuntimeError("mesure interrompue")

    def _move(self, direction: str, rate: int, seconds: float) -> None:
        self.mount.move_start(direction, rate)
        try:
            t_end = time.monotonic() + seconds
            while time.monotonic() < t_end:
                if self._abort:
                    break
                time.sleep(0.05)
        finally:
            self.mount.move_stop(direction)
        self._check_abort()

    def _wait_still(self, axis: int, timeout_s: float = 40.0) -> float:
        last = self.mount.axis_angles()[axis - 1]
        t_end = time.monotonic() + timeout_s
        while time.monotonic() < t_end:
            self._check_abort()
            time.sleep(0.4)
            cur = self.mount.axis_angles()[axis - 1]
            if abs(cur - last) < STILL_TOL_DEG:
                return cur
            last = cur
        raise RuntimeError("l'axe ne s'arrête pas")

    def _run(self, axis: int, n_points: int, move_s: float, rate: int, direction: str) -> None:
        st = self.mount.status()
        if st["parked"]:
            raise RuntimeError("monture parquée : la déparquer d'abord")
        if st["tracking"]:
            self.mount.tracking(False)
        spd = self.mount.steps_per_degree(axis)
        lat = self.mount.latitude_deg()
        lon = self.mount.longitude_east_deg()
        self.state.update(steps_per_degree=spd, lat_deg=lat, lon_east_deg=lon)
        journal.event("steps_start", axis=axis, direction=direction, points=n_points, move_s=move_s,
                      rate=rate, steps_per_degree=spd, lat_deg=lat, lon_east_deg=lon)
        self.state["message"] = "rattrapage du jeu"
        self._move(direction, rate, min(2.0, move_s))
        for i in range(n_points):
            if i:
                self.state["message"] = f"déplacement vers le point {i + 1}"
                self._move(direction, rate, move_s)
            self.state["message"] = f"point {i + 1}/{n_points} : stabilisation"
            self._wait_still(axis)
            time.sleep(SETTLE_S)
            a1, a2 = self.mount.axis_angles()
            s = self.mount.status()
            self.state["message"] = f"point {i + 1}/{n_points} : solve"
            res = solver.solve(hint=(s["ra_h"], s["dec_deg"]))
            if not res.solved:     # second essai (nuage, vibration)
                res = solver.solve(hint=(s["ra_h"], s["dec_deg"]))
            a1b, a2b = self.mount.axis_angles()
            pt = {"i": i + 1, "counted_deg": (a1, a2)[axis - 1], "axis1_deg": a1, "axis2_deg": a2,
                  "moved_during_solve_arcsec": round(max(abs(a1b - a1), abs(a2b - a2)) * 3600, 1),
                  "alt_deg": s["alt_deg"], "az_deg": s["az_deg"], "solved": res.solved,
                  "ra_jnow_h": res.ra_jnow_h, "dec_jnow_deg": res.dec_jnow_deg, "utc_ts": res.utc_ts,
                  "solve_s": res.solve_s, "message": res.message}
            self.state["points"].append(pt)
            journal.event("steps_point", axis=axis, **pt)
        self.state["message"] = "calcul"
        result = analyse(self.state["points"], axis, spd["active"], lat, lon)
        self.state.update(state="done" if "error" not in result else "error", result=result,
                          message=result.get("error", "terminé"))
        journal.event("steps_result", **result)
