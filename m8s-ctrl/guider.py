"""
Autoguidage EQ / ALT-AZ (CONCEPTION.md §6).

Chaîne par trame : trame RAW d'allsky -> étoiles (stars.py) -> similitude
référence -> courante (translation + rotation) -> déplacement du *pivot*
(point du champ guide qui doit rester fixe : l'axe de l'instrument
principal vu dans la caméra guide, centre de l'image par défaut) ->
décomposition sur les axes RA/Dec calibrés -> impulsions `:MG`.

OnStepX applique les impulsions en RA/Dec, y compris en ALTAZM (Mount.cpp,
vérifié et mesuré le 24/09). Les directions RA/Dec tournent donc dans
l'image avec le champ en ALT-AZ : la calibration est tournée de la rotation
de champ *mesurée* par l'ajustement multi-étoiles depuis la calibration
(repli sur le modèle d'angle parallactique si la mesure manque).

Coordonnées internes : **pixels natifs du capteur, comptés depuis son
centre** (les modes recadrés de minicam sont centrés ; un pixel d'un mode
binné 2×2 vaut 2 pixels natifs). Pivot, étoiles de référence et
calibration sont donc indépendants du mode vidéo choisi sur la page
d'allsky : on peut guider en 480p avec une calibration faite en 1080p, ou
changer de mode en cours de guidage (latence de calcul proportionnelle au
nombre de pixels : 0,85 s en 2028×1080, 0,17 s en 480p sur le M8S).

Les sources de trames et la monture sont abstraites (FrameSource,
MountIO) pour pouvoir tout valider avec le simulateur `SimSky` sans ciel.
"""
from __future__ import annotations

import json
import math
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

import astro
import solver
import stars as st

CALIB_PATH = Path("/var/lib/m8s-ctrl/calibration.json")


# ------------------------------------------------------------- interfaces --

class FrameSource:
    def fetch(self):          # -> objet avec .data .bits .exposure_us .gain .sensor .raw_mode
        raise NotImplementedError


class MountIO:
    def pulse(self, direction: str, ms: int) -> None:
        raise NotImplementedError

    def state(self) -> dict:
        """dec_deg, ha_h, lat_deg, pier_side, mount_type (valeurs lentes)."""
        raise NotImplementedError


class AllskySource(FrameSource):
    def fetch(self):
        import guidecam
        return guidecam.fetch_frame()


class OnStepIO(MountIO):
    """Adaptateur vers la monture réelle ; l'état (lent) est mis en cache
    pour ne pas occuper la liaison série à 9600 bauds à chaque trame."""

    def __init__(self, mount, cache_s: float = 20.0) -> None:
        self.mount, self.cache_s = mount, cache_s
        self._state, self._t = None, 0.0
        self._lat = None

    def pulse(self, direction: str, ms: int) -> None:
        self.mount.pulse_guide(direction, ms)

    def state(self) -> dict:
        if self._state is None or time.monotonic() - self._t > self.cache_s:
            from onstep import parse_hours
            s = self.mount.status()
            if self._lat is None:
                self._lat = self.mount.latitude_deg()
            lst = parse_hours(self.mount.cmd(":GS#"))
            self._state = {"dec_deg": s["dec_deg"], "ha_h": (lst - s["ra_h"]) % 24,
                           "lat_deg": self._lat, "pier_side": s["pier_side"],
                           "mount_type": s["mount_type"], "tracking": s["tracking"]}
            self._t = time.monotonic()
        return self._state


# ---------------------------------------------------------------- réglages --

@dataclass
class Settings:
    pivot_dx: float = 0.0               # pivot, px natifs depuis le centre du capteur
    pivot_dy: float = 0.0
    ra_aggressiveness: float = 0.7
    ra_hysteresis: float = 0.1
    dec_aggressiveness: float = 0.7
    min_move_px: float = 0.15
    max_pulse_ms: int = 2500
    dec_mode: str = "auto"              # auto | north | south | off
    skip_frames_after_pulse: int = 1    # trame dont la pose a chevauché l'impulsion
    calib_step_ms: int = 1000
    calib_target_px: float = 25.0
    calib_max_steps: int = 30
    pier_flip_mode: str = "negate_both"  # GEM : à confirmer sur la monture EQ réelle
    dec_backlash_comp: bool = False      # ajoute une fraction du jeu mesuré à chaque inversion Dec
    dec_backlash_fraction: float = 0.6   # le jeu mesuré est surestimé (pas de calibration + seuil)
    arcsec_per_px: float = 1.81          # par px natif ; renseigné depuis la trame (capteur/focale)


@dataclass
class Calibration:
    v_ra: list[float]            # px natifs par ms d'impulsion 'w'
    v_dec: list[float]           # px natifs par ms d'impulsion 'n'
    dec_deg: float
    ha_h: float
    lat_deg: float
    pier_side: str | None
    mount_type: str | None
    q_deg: float                 # angle parallactique à la calibration
    time: float
    # Vecteurs v_ra/v_dec exprimés dans le repère de la 1re trame de
    # calibration ; ref_stars = étoiles de la *dernière* trame, qui avait
    # tourné de rot_end_deg par rapport à ce repère (ALT-AZ).
    ref_stars: list[list[float]] = field(default_factory=list)
    rot_end_deg: float = 0.0
    ortho_error_deg: float = 0.0
    backlash_ms: int = 0

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self)))

    @staticmethod
    def load(path: Path) -> "Calibration | None":
        try:
            return Calibration(**json.loads(path.read_text()))
        except Exception:
            return None


def rot(v: np.ndarray, deg: float) -> np.ndarray:
    a = math.radians(deg)
    return np.array([v[0] * math.cos(a) - v[1] * math.sin(a), v[0] * math.sin(a) + v[1] * math.cos(a)])


# ------------------------------------------------------------------ moteur --

class Guider:
    def __init__(self, source: FrameSource, mount: MountIO, settings: Settings | None = None,
                 calib_path: Path = CALIB_PATH) -> None:
        self.src, self.mount = source, mount
        self.settings = settings or Settings()
        self.calib_path = calib_path
        self.calibration = Calibration.load(calib_path)
        self.state = "idle"             # idle | calibrating | guiding | lost | error
        self.message = ""
        self.history: deque = deque(maxlen=600)
        self.calib_log: list[dict] = []
        self.last_frame = None
        self.last_stars: list = []
        self.last_fit = None
        self._stop = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.rotation_parity = 1.0      # signe mesure/modèle en ALT-AZ, estimé en guidage
        self.geom = (1.0, 0.0, 0.0)     # (px natifs par px du mode, centre x, centre y) de la dernière trame

    # -- utilitaires -------------------------------------------------------

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _grab(self, skip: int = 0):
        for _ in range(skip):
            self.src.fetch()
        f = self.src.fetch()
        found = st.detect(f.data, f.bits)
        base = solver.SENSOR_PIXEL_UM.get(f.sensor)
        k = solver.pixel_um(f) / base if base else 1.0
        cx, cy = f.data.shape[1] / 2, f.data.shape[0] / 2
        self.geom = (k, cx, cy)
        if base:                                     # échelle réelle de la caméra guide
            self.settings.arcsec_per_px = round(206.265 * base / solver.GUIDE_FOCAL_MM, 3)
        native = [st.Star((x.x - cx) * k, (x.y - cy) * k, x.flux, x.snr, x.peak, x.saturated) for x in found]
        self.last_frame, self.last_stars = f, native
        return f, native

    def _pivot(self, shape=None) -> np.ndarray:
        return np.array([self.settings.pivot_dx, self.settings.pivot_dy])

    def _measure(self, ref: np.ndarray, found, predicted_t: np.ndarray, fixed_angle=None, prev=None):
        """Similitude référence -> trame courante, ou None si perdue.
        La prédiction utilise la dernière similitude complète (`prev`),
        rotation comprise : en ALT-AZ la rotation cumulée déplace fortement
        les étoiles du bord."""
        cur = np.array([[s.x, s.y] for s in found]) if found else np.zeros((0, 2))
        if len(cur) == 0:
            return None
        k = self.geom[0]
        predicted = prev.apply(ref) if prev is not None else ref + predicted_t
        pairs = st.match(ref, cur, predicted, radius=6.0 * k)
        if len(pairs) < max(1, min(3, len(ref)) // 2):
            # raccrochage : décalage global voté à partir des positions
            # *prédites* (rotation comprise), pas des positions de référence
            shift = st.estimate_shift(predicted, cur, tol=3.0 * k)
            if shift is None:
                return None
            pairs = st.match(ref, cur, predicted + shift, radius=6.0 * k)
            if not pairs:
                return None
        P = ref[[i for i, _ in pairs]]
        Q = cur[[j for _, j in pairs]]
        spread = float(np.ptp(P, axis=0).max()) if len(P) > 1 else 0.0
        angle = fixed_angle if (len(P) < 3 or spread < 200) else None   # bras de levier (px natifs)
        fit = st.fit_similarity(P, Q, fixed_angle=angle if angle is not None else (0.0 if len(P) < 2 else None))
        fit.unmatched = cur[[j for j in range(len(cur)) if j not in {q for _, q in pairs}]]
        return fit

    @staticmethod
    def _refresh_ref(ref: np.ndarray, fit, min_keep: int = 6) -> np.ndarray:
        """Ajoute à la référence les étoiles entrées dans le champ, ramenées
        dans le repère de référence par la similitude inverse — le pivot et
        les mesures restent continus quand le champ tourne ou dérive."""
        new = getattr(fit, "unmatched", None)
        if new is None or len(new) == 0 or fit.n >= max(min_keep, len(ref) // 2):
            return ref
        c, s = math.cos(fit.angle), math.sin(fit.angle)
        R = np.array([[c, s], [-s, c]])
        back = (new - fit.t) @ R.T
        return np.vstack([ref, back])[:80]

    def _start(self, target, *args) -> None:
        if self.busy:
            raise RuntimeError(f"guideur occupé ({self.state})")
        self._stop = False
        self._thread = threading.Thread(target=self._guard, args=(target, *args), daemon=True)
        self._thread.start()

    def _guard(self, target, *args) -> None:
        try:
            target(*args)
        except Exception as e:
            self.state, self.message = "error", str(e)
        finally:
            if self.state in ("calibrating", "guiding", "lost"):
                self.state = "idle"

    def stop(self) -> None:
        self._stop = True

    # -- calibration -------------------------------------------------------

    def start_calibration(self) -> None:
        self._start(self._calibrate)

    def _step_until(self, direction: str, ref, origin_t, target_px: float, max_steps: int,
                    record: bool, prev=None):
        """Impulsions répétées ; renvoie ([(ms cumulées, déplacement px)], dernière similitude)."""
        s = self.settings
        pts, cum, t_pred = [], 0, origin_t.copy()
        for _ in range(max_steps):
            if self._stop:
                raise RuntimeError("calibration interrompue")
            self.mount.pulse(direction, s.calib_step_ms)
            time.sleep(s.calib_step_ms / 1000)
            cum += s.calib_step_ms
            f, found = self._grab(skip=s.skip_frames_after_pulse)
            fit = self._measure(ref, found, t_pred, prev=prev)
            if fit is None:
                raise RuntimeError(f"étoiles perdues pendant la calibration ({direction})")
            piv = self._pivot(f.data.shape)
            d = fit.apply(piv[None])[0] - piv
            t_pred, prev = fit.t, fit
            # déplacement exprimé dans le repère de la référence (dérotation
            # de la rotation de champ accumulée : ALT-AZ)
            rel = rot(d - origin_t, -math.degrees(fit.angle)) if record else d
            pts.append((cum, rel))
            if record:
                self.calib_log.append({"dir": direction, "ms": cum, "dx": float(d[0]), "dy": float(d[1])})
            if np.hypot(*(d - origin_t)) >= target_px:
                break
        return pts, prev

    @staticmethod
    def _rotation_only(fit, pivot: np.ndarray):
        """Prédiction après un retour d'impulsions : le pivot est revenu à
        son point de départ, mais le champ garde la rotation de `fit`."""
        c, s = math.cos(fit.angle), math.sin(fit.angle)
        return st.Similarity(fit.angle, pivot - pivot @ np.array([[c, s], [-s, c]]), fit.n, fit.rms, True)

    @staticmethod
    def _slope(pts) -> np.ndarray:
        ms = np.array([p[0] for p in pts], dtype=float)
        d = np.array([p[1] for p in pts])
        return (ms[:, None] * d).sum(axis=0) / (ms ** 2).sum()

    def _return(self, direction: str, total_ms: int) -> None:
        left = total_ms
        while left > 0 and not self._stop:
            step = min(left, self.settings.max_pulse_ms)
            self.mount.pulse(direction, step)
            time.sleep(step / 1000 + 0.1)
            left -= step

    def _calibrate(self) -> None:
        s = self.settings
        self.state, self.message, self.calib_log = "calibrating", "calibration RA", []
        mstate = self.mount.state()
        if mstate.get("tracking") is False:
            raise RuntimeError("le suivi doit être actif (OnStepX n'applique les impulsions qu'en suivi)")
        f, found = self._grab()
        if len(found) < 1:
            raise RuntimeError("aucune étoile détectée")
        ref = np.array([[x.x, x.y] for x in found])
        zero = np.zeros(2)
        # RA
        pts_ra, last = self._step_until("w", ref, zero, s.calib_target_px, s.calib_max_steps, True)
        v_ra = self._slope(pts_ra)
        self.message = "retour RA"
        self._return("e", pts_ra[-1][0])
        # Dec : rattrapage du jeu (on pousse vers le nord jusqu'à un vrai mouvement)
        self.message = "rattrapage du jeu Dec"
        f, found = self._grab(skip=s.skip_frames_after_pulse)
        # Après le retour, le pivot est revenu près de l'origine mais le champ
        # a pu tourner (ALT-AZ) : prédiction = rotation de la dernière mesure
        # autour du pivot, sans translation au pivot.
        fit0 = self._measure(ref, found, zero, prev=self._rotation_only(last, self._pivot(f.data.shape)))
        if fit0 is None:
            raise RuntimeError("étoiles perdues après le retour RA")
        piv = self._pivot(f.data.shape)
        base = fit0.apply(piv[None])[0] - piv
        pts_bl, fit_bl = self._step_until("n", ref, base, 3.0, 15, False, prev=fit0)
        backlash_ms = pts_bl[-1][0]
        origin = pts_bl[-1][1]
        self.message = "calibration Dec"
        pts_dec, fit_dec = self._step_until("n", ref, origin, s.calib_target_px, s.calib_max_steps, True, prev=fit_bl)
        v_dec = self._slope(pts_dec)
        self.message = "retour Dec"
        self._return("s", pts_dec[-1][0] + backlash_ms)
        if np.hypot(*v_ra) == 0 or np.hypot(*v_dec) == 0:
            raise RuntimeError("aucun mouvement mesuré pendant la calibration")
        ang = math.degrees(math.atan2(v_dec[1], v_dec[0]) - math.atan2(v_ra[1], v_ra[0]))
        ortho = abs((abs((ang + 180) % 360 - 180)) - 90)
        # État du champ en fin de calibration : rotation depuis la référence
        # et étoiles visibles, pour chaîner la rotation jusqu'au guidage.
        f, found = self._grab(skip=s.skip_frames_after_pulse)
        fit_end = self._measure(ref, found, zero, prev=self._rotation_only(fit_dec, self._pivot(f.data.shape)))
        rot_end = math.degrees((fit_end or fit_dec).angle)
        mstate = self.mount.state()
        self.calibration = Calibration(
            v_ra=[float(v_ra[0]), float(v_ra[1])], v_dec=[float(v_dec[0]), float(v_dec[1])],
            dec_deg=mstate["dec_deg"], ha_h=mstate["ha_h"], lat_deg=mstate["lat_deg"],
            pier_side=mstate["pier_side"], mount_type=mstate["mount_type"],
            q_deg=astro.parallactic_angle_deg(mstate["ha_h"], mstate["dec_deg"], mstate["lat_deg"]),
            time=time.time(), ref_stars=[[x.x, x.y] for x in found],
            rot_end_deg=round(rot_end, 4), ortho_error_deg=round(ortho, 1),
            backlash_ms=int(backlash_ms))
        self.calibration.save(self.calib_path)
        self.message = (f"calibration OK — RA {np.hypot(*v_ra) * 1000:.2f} px/s, "
                        f"Dec {np.hypot(*v_dec) * 1000:.2f} px/s, orthogonalité {ortho:.1f}°"
                        + (" (> 10° : à vérifier)" if ortho > 10 else ""))
        self.state = "idle"

    # -- guidage -----------------------------------------------------------

    def start_guiding(self) -> None:
        if self.calibration is None:
            raise RuntimeError("pas de calibration")
        self._start(self._guide)

    def _q_since_calibration(self, mstate) -> float:
        """Variation de l'angle parallactique depuis la calibration (modèle,
        sans la parité de l'image)."""
        q = astro.parallactic_angle_deg(mstate["ha_h"], mstate["dec_deg"], mstate["lat_deg"])
        return (q - self.calibration.q_deg + 180) % 360 - 180

    def _axes_now(self, mstate, rot_deg: float) -> np.ndarray:
        """Matrice 2×2 [v_ra v_dec] (px/ms) valable maintenant."""
        c = self.calibration
        v_ra, v_dec = np.array(c.v_ra), np.array(c.v_dec)
        cos_cal = max(math.cos(math.radians(c.dec_deg)), 0.05)
        v_ra = v_ra * max(math.cos(math.radians(mstate["dec_deg"])), 0.05) / cos_cal
        if (mstate["mount_type"] == "GEM" and c.pier_side and mstate["pier_side"]
                and mstate["pier_side"] != c.pier_side and self.settings.pier_flip_mode == "negate_both"):
            v_ra, v_dec = -v_ra, -v_dec
        v_ra, v_dec = rot(v_ra, rot_deg), rot(v_dec, rot_deg)
        return np.column_stack([v_ra, v_dec])

    def _guide(self) -> None:
        s = self.settings
        self.state, self.message = "guiding", "acquisition"
        self.history.clear()
        self.last_fit = None
        mstate = self.mount.state()
        altaz = mstate["mount_type"] in ("ALTAZM", "ALTALT")
        f, found = self._grab()
        if not found:
            raise RuntimeError("aucune étoile détectée")
        ref = np.array([[x.x, x.y] for x in found])
        pivot = self._pivot(f.data.shape)
        # rotation calibration -> début du guidage : mesurée si le champ de
        # calibration est encore visible, sinon modèle parallactique.
        rot0, rot0_src = 0.0, "aucune"
        if altaz:
            cal_ref = np.array(self.calibration.ref_stars) if self.calibration.ref_stars else None
            fit_c = self._measure(cal_ref, found, np.zeros(2)) if cal_ref is not None and len(cal_ref) else None
            if fit_c is not None and fit_c.rotation_fitted:
                rot0, rot0_src = self.calibration.rot_end_deg + math.degrees(fit_c.angle), "mesurée"
            else:
                rot0, rot0_src = self.rotation_parity * self._q_since_calibration(mstate), "modèle"
        t_pred, prev_ra_ms, lost, t0 = np.zeros(2), 0.0, 0, time.time()
        last_dec_sign = 0
        q_start = self._q_since_calibration(mstate) if altaz else 0.0
        skip = 0
        while not self._stop:
            f, found = self._grab(skip=skip)
            mstate = self.mount.state()
            q_raw = (self._q_since_calibration(mstate) - q_start) if altaz else 0.0
            model_rot = self.rotation_parity * q_raw
            fit = self._measure(ref, found, t_pred, fixed_angle=math.radians(model_rot), prev=self.last_fit)
            if fit is None:
                lost += 1
                self.state, self.message = "lost", f"étoiles perdues ({lost})"
                skip = 0
                continue
            self.state, lost = "guiding", 0
            t_pred = fit.t
            self.last_fit = fit
            ref = self._refresh_ref(ref, fit)
            d = fit.apply(pivot[None])[0] - pivot                   # px, image
            meas_rot = math.degrees(fit.angle)
            if altaz and fit.rotation_fitted and abs(q_raw) > 0.3 and abs(meas_rot) > 0.1:
                # Parité de l'image (miroir ou non) : le sens de rotation
                # mesuré comparé au modèle. Ne sert qu'au repli sur le modèle.
                self.rotation_parity = 1.0 if meas_rot * q_raw > 0 else -1.0
            rot_total = rot0 + meas_rot if altaz else 0.0
            M = self._axes_now(mstate, rot_total)
            ra_ms, dec_ms = np.linalg.solve(M, -d)                   # ms 'w' / 'n' pour annuler d
            # erreurs le long des axes, en px puis en ″
            ra_px, dec_px = -ra_ms * np.hypot(*M[:, 0]), -dec_ms * np.hypot(*M[:, 1])
            # correcteurs (à la PHD2)
            ra_cmd = (1 - s.ra_hysteresis) * ra_ms + s.ra_hysteresis * prev_ra_ms
            ra_cmd *= s.ra_aggressiveness
            dec_cmd = dec_ms * s.dec_aggressiveness
            if abs(ra_px) < s.min_move_px:
                ra_cmd = 0.0
            if abs(dec_px) < s.min_move_px or s.dec_mode == "off" \
                    or (s.dec_mode == "north" and dec_cmd < 0) or (s.dec_mode == "south" and dec_cmd > 0):
                dec_cmd = 0.0
            prev_ra_ms = ra_cmd
            ra_cmd = float(np.clip(ra_cmd, -s.max_pulse_ms, s.max_pulse_ms))
            dec_cmd = float(np.clip(dec_cmd, -s.max_pulse_ms, s.max_pulse_ms))
            if abs(ra_cmd) >= 1:
                self.mount.pulse("w" if ra_cmd > 0 else "e", int(abs(ra_cmd)))
            if abs(dec_cmd) >= 1:
                sign = 1 if dec_cmd > 0 else -1
                extra = 0
                if s.dec_backlash_comp and last_dec_sign and sign != last_dec_sign:
                    extra = int(self.calibration.backlash_ms * s.dec_backlash_fraction)
                last_dec_sign = sign
                dec_cmd = sign * min(abs(dec_cmd) + extra, s.max_pulse_ms + extra)
                self.mount.pulse("n" if sign > 0 else "s", int(abs(dec_cmd)))
            longest = max(abs(ra_cmd), abs(dec_cmd))
            best_snr = max((x.snr for x in found), default=0.0)
            self.history.append({
                "t": round(time.time() - t0, 2),
                "dx": round(float(d[0]), 3), "dy": round(float(d[1]), 3),
                "ra_arcsec": round(float(ra_px) * s.arcsec_per_px, 2),
                "dec_arcsec": round(float(dec_px) * s.arcsec_per_px, 2),
                "ra_pulse_ms": int(ra_cmd), "dec_pulse_ms": int(dec_cmd),
                "stars": fit.n, "snr": round(best_snr, 1),
                "rot_deg": round(rot_total, 3), "rot_model_deg": round(model_rot, 3),
            })
            self.message = f"guidage — {fit.n} étoile(s), rotation {rot_total:+.2f}° ({rot0_src} au départ)"
            if longest >= 1:
                time.sleep(longest / 1000)
                skip = s.skip_frames_after_pulse
            else:
                skip = 0

    # -- état --------------------------------------------------------------

    def rms(self, n: int = 50) -> dict:
        h = list(self.history)[-n:]
        if not h:
            return {}
        ra = np.array([p["ra_arcsec"] for p in h])
        de = np.array([p["dec_arcsec"] for p in h])
        return {"ra": round(float(np.sqrt((ra ** 2).mean())), 2),
                "dec": round(float(np.sqrt((de ** 2).mean())), 2),
                "total": round(float(np.sqrt((ra ** 2).mean() + (de ** 2).mean())), 2), "n": len(h)}

    def status(self, history_points: int = 120) -> dict:
        f = self.last_frame
        return {
            "state": self.state, "message": self.message,
            "calibration": asdict(self.calibration) | {"ref_stars": len(self.calibration.ref_stars)}
            if self.calibration else None,
            "settings": asdict(self.settings), "rms": self.rms(),
            "frame": {"sensor": f.sensor, "raw_mode": f.raw_mode, "exposure_ms": f.exposure_us / 1000,
                      "gain": f.gain, "stars": len(self.last_stars),
                      # pour convertir un clic sur la vignette en px natifs (pivot)
                      "width": f.data.shape[1], "height": f.data.shape[0],
                      "native_per_px": self.geom[0]} if f is not None else None,
            "history": list(self.history)[-history_points:],
            "calibration_log": self.calib_log[-80:],
        }


# -------------------------------------------------------------- simulateur --

class SimSky(FrameSource, MountIO):
    """Ciel et monture simulés : champ d'étoiles, bruit, seeing, dérive
    lente (erreur périodique), rotation de champ (ALT-AZ), et impulsions qui
    déplacent le champ selon des axes RA/Dec cachés qui tournent avec lui.
    Sert à valider calibration et guidage de bout en bout sans ciel."""

    def __init__(self, shape=(1080, 2028), n_stars=25, seed=3, mount_type="ALTAZM",
                 ra_angle_deg=35.0, rate_px_s=2.1, dec_backlash_ms=1500,
                 drift_px_s=(0.05, -0.03), pe_amp_px=2.0, pe_period_s=120.0,
                 seeing_px=0.15, field_rot_deg_s=0.0, exposure_s=0.5, dec_deg=40.0) -> None:
        self.rng = np.random.default_rng(seed)
        self.h, self.w = shape
        self.x = self.rng.uniform(30, self.w - 30, n_stars)
        self.y = self.rng.uniform(30, self.h - 30, n_stars)
        self.flux = self.rng.uniform(400, 6000, n_stars)
        self.mount_type, self.dec_deg = mount_type, dec_deg
        a = math.radians(ra_angle_deg)
        r = rate_px_s / 1000
        self.v_ra0 = np.array([math.cos(a), math.sin(a)]) * r
        self.v_dec0 = np.array([-math.sin(a), math.cos(a)]) * r     # orthogonal
        self.backlash_ms, self.dec_dir, self.dec_slack = dec_backlash_ms, 0, 0.0
        self.offset = np.zeros(2)
        self.t0 = time.monotonic()
        self.drift, self.pe_amp, self.pe_period = np.array(drift_px_s), pe_amp_px, pe_period_s
        self.seeing, self.rot_rate, self.exposure_s = seeing_px, field_rot_deg_s, exposure_s
        self.crop: tuple[int, int] | None = None   # (w, h) : simule un mode vidéo recadré
        self.lock = threading.Lock()

    def _angle(self) -> float:
        return math.radians(self.rot_rate * (time.monotonic() - self.t0))

    def pulse(self, direction: str, ms: int) -> None:
        with self.lock:
            ang = math.degrees(self._angle())
            if direction in "we":
                v = rot(self.v_ra0, ang)
                self.offset += v * ms * (1 if direction == "w" else -1)
            else:
                sign = 1 if direction == "n" else -1
                if sign != self.dec_dir:            # jeu à l'inversion
                    self.dec_dir, self.dec_slack = sign, float(self.backlash_ms)
                eff = max(0.0, ms - self.dec_slack)
                self.dec_slack = max(0.0, self.dec_slack - ms)
                self.offset += rot(self.v_dec0, ang) * eff * sign

    def state(self) -> dict:
        return {"dec_deg": self.dec_deg, "ha_h": 1.0, "lat_deg": 47.3, "pier_side": None,
                "mount_type": self.mount_type, "tracking": True}

    def true_pivot_offset(self, pivot) -> np.ndarray:
        """Déplacement réel (px) du pivot depuis le début : vérité terrain."""
        t = time.monotonic() - self.t0
        pe = np.array([self.pe_amp * math.sin(2 * math.pi * t / self.pe_period), 0.0])
        return self.offset + self.drift * t + pe

    def fetch(self):
        time.sleep(self.exposure_s)
        with self.lock:
            t = time.monotonic() - self.t0
            ang = self._angle()
            shift = self.true_pivot_offset(None) + self.rng.normal(0, self.seeing, 2)
        cx, cy = self.w / 2, self.h / 2
        c, s = math.cos(ang), math.sin(ang)
        xs = cx + (self.x - cx) * c - (self.y - cy) * s + shift[0]
        ys = cy + (self.x - cx) * s + (self.y - cy) * c + shift[1]
        img = self.rng.normal(256, 10, (self.h, self.w)).astype(np.float32)
        for xi, yi, fl in zip(xs, ys, self.flux):
            x0, y0 = int(xi), int(yi)
            if not (8 <= x0 < self.w - 8 and 8 <= y0 < self.h - 8):
                continue
            yy, xx = np.mgrid[y0 - 6: y0 + 7, x0 - 6: x0 + 7]
            img[y0 - 6: y0 + 7, x0 - 6: x0 + 7] += fl / (2 * math.pi * 1.6 ** 2) * np.exp(
                -((xx - xi) ** 2 + (yy - yi) ** 2) / (2 * 1.6 ** 2))
        data = np.clip(img, 0, 4095).astype(np.uint16)
        if self.crop is not None:           # recadrage centré, comme les modes minicam
            cw, ch = self.crop
            y0, x0 = (self.h - ch) // 2 & ~1, (self.w - cw) // 2 & ~1
            data = data[y0: y0 + ch, x0: x0 + cw]

        class F:
            pass
        f = F()
        f.data, f.bits, f.exposure_us, f.gain = data, 12, int(self.exposure_s * 1e6), 8.0
        f.sensor, f.raw_mode, f.bayer = "sim", "sim", "RGGB"
        return f


def preview_jpeg(g: Guider, width: int = 640) -> bytes | None:
    """Vignette étirée et annotée de la dernière trame (calculée ici, sur le
    M8S : le RPi0 n'encode rien). Canal vert de la matrice de Bayer."""
    import io
    from PIL import Image, ImageDraw
    f = g.last_frame
    if f is None:
        return None
    green = f.data[0::2, 1::2].astype(np.float32)
    step = max(1, int(round(green.shape[1] / width)))
    small = green[::step, ::step]
    lo, hi = np.percentile(small, (5, 99.7))
    x = np.clip((small - lo) / max(hi - lo, 1.0), 0, 1)
    x = np.arcsinh(10 * x) / np.arcsinh(10)
    img = Image.fromarray((x * 255).astype(np.uint8)).convert("RGB")
    d = ImageDraw.Draw(img)
    kn, ox, oy = g.geom                      # natif -> pixels du mode
    k = 1 / (2 * step)                       # pixels du mode -> vignette
    for s in g.last_stars:
        cx, cy = (s.x / kn + ox) * k, (s.y / kn + oy) * k
        d.ellipse([cx - 6, cy - 6, cx + 6, cy + 6], outline=(0, 220, 0))
    pv = g._pivot()
    px, py = (pv[0] / kn + ox) * k, (pv[1] / kn + oy) * k
    d.line([px - 12, py, px + 12, py], fill=(255, 60, 60))
    d.line([px, py - 12, px, py + 12], fill=(255, 60, 60))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return buf.getvalue()
