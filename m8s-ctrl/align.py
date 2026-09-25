"""
Alignement OnStepX par plate solve, sans viser d'étoile (CONCEPTION.md §5).

Principe vérifié dans OnStepX (Goto.command.cpp) : pendant un alignement
(`:A<n>#`), un `:CM#` n'est pas un simple sync : il ajoute un point au
modèle avec la cible courante (`:Sr`/`:Sd`) comme position *vraie*.
Séquence : pour chaque point, goto vers une position répartie dans le ciel,
pose, solve ASTAP, cible := coordonnées solvées (JNow), `:CM#`.
À la fin, `:AW#` enregistre le modèle en mémoire non volatile.

Deux modes :
- automatique : points répartis dans une zone de ciel dégagée choisie par
  l'utilisateur (azimut central + étendue), hauteurs variées ;
- manuel : l'utilisateur vise où il veut à la raquette puis « ajoute ce
  point » (solve + `:CM#`) — aucun goto, OnStepX associe simplement la
  position mécanique courante aux coordonnées solvées.
Un bon modèle demande des points écartés (≥ 60° conseillés entre eux) et
des hauteurs variées (index en hauteur vs inclinaison de l'axe d'azimut).
"""
from __future__ import annotations

import random
import threading
import time

import astro
import solver
from onstep import OnStep, OnStepError, parse_hours

SETTLE_S = 2.0
MIN_SEPARATION_DEG = 30.0      # en dessous : alerte « points trop proches »


def sky_points(n: int, az_center: float = 180.0, az_span: float = 90.0,
               alt_min: float = 35.0, alt_max: float = 65.0) -> list[tuple[float, float]]:
    """n positions (hauteur, azimut) : azimuts répartis sur `az_span` autour
    de `az_center` (zone de ciel dégagée), hauteurs alternées entre
    alt_min et alt_max (loin de l'horizon et du zénith)."""
    if n == 1:
        return [((alt_min + alt_max) / 2, az_center % 360)]
    alts = [alt_min + (alt_max - alt_min) * k / (n - 1) for k in range(n)]
    alts = alts[0::2] + alts[1::2][::-1]          # alterne bas / haut le long de l'arc
    return [(alts[i], (az_center - az_span / 2 + i * az_span / (n - 1)) % 360) for i in range(n)]


def altaz_separation_deg(a1: float, z1: float, a2: float, z2: float) -> float:
    return astro.separation_arcsec(z1 / 15, a1, z2 / 15, a2) / 3600


class AlignJob:
    def __init__(self, mount: OnStep) -> None:
        self.mount = mount
        self._thread: threading.Thread | None = None
        self._abort = False
        self.state: dict = {"state": "idle"}

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def manual(self) -> bool:
        return self.state.get("state") == "manual"

    @property
    def active(self) -> bool:
        return self.running or self.manual

    def start(self, n_points: int, az_center: float, az_span: float,
              alt_min: float, alt_max: float, simulate: bool) -> None:
        if self.active:
            raise OnStepError("un alignement est déjà en cours")
        self._abort = False
        self.state = {"state": "running", "mode": "auto", "n_points": n_points, "simulate": simulate,
                      "started": time.time(), "points": [], "log": []}
        self._thread = threading.Thread(
            target=self._run, args=(n_points, sky_points(n_points, az_center, az_span, alt_min, alt_max),
                                    simulate), daemon=True)
        self._thread.start()

    # -- mode manuel -------------------------------------------------------

    def start_manual(self, n_points: int) -> None:
        if self.active:
            raise OnStepError("un alignement est déjà en cours")
        self.mount.align_start(n_points)
        self.state = {"state": "manual", "mode": "manuel", "n_points": n_points, "simulate": False,
                      "started": time.time(), "points": [], "log": []}
        self._log(f"alignement manuel {n_points} point(s) démarré : viser à la raquette puis « ajouter ce point »")

    def add_manual_point(self) -> dict:
        """Solve à la position courante puis ajout du point (`:CM#`)."""
        if not self.manual:
            raise OnStepError("aucun alignement manuel en cours")
        m = self.mount
        st = m.status()
        i = len(self.state["points"]) + 1
        res = solver.solve(hint=(st["ra_h"], st["dec_deg"]))
        point = {"index": i, "alt": round(st["alt_deg"], 2), "az": round(st["az_deg"], 2), "solve": res.as_dict()}
        if not res.solved:
            self._log(f"point {i} : solve échoué ({res.message}) — rien n'est ajouté, viser ailleurs ou allonger la pose")
            return {"added": False, "point": point}
        point["solved_ra_h"], point["solved_dec_deg"] = res.ra_jnow_h, res.dec_jnow_deg
        point["pointing_error_arcmin"] = round(
            astro.separation_arcsec(st["ra_h"], st["dec_deg"], res.ra_jnow_h, res.dec_jnow_deg) / 60, 2)
        m.sync(res.ra_jnow_h, res.dec_jnow_deg)
        self.state["points"].append(point)
        self._log(f"point {i} ajouté : Alt {point['alt']:.0f}° Az {point['az']:.0f}° "
                  f"(écart monture/ciel {point['pointing_error_arcmin']}′)")
        al = m.align_status()
        if al["current"] > al["last"]:
            m.align_write()
            self._log("alignement terminé, modèle enregistré (:AW#)")
            self.state["state"] = "done"
            self.state["finished"] = time.time()
        return {"added": True, "point": point}

    def separation_report(self, alt: float, az: float) -> dict:
        """Écart angulaire entre une visée (alt, az) et les points déjà pris."""
        seps = [round(altaz_separation_deg(alt, az, p["alt"], p["az"]), 1) for p in self.state.get("points", [])]
        return {"separations_deg": seps, "too_close": any(s < MIN_SEPARATION_DEG for s in seps),
                "min_recommended_deg": MIN_SEPARATION_DEG}

    def abort(self) -> None:
        self._abort = True
        if self.manual:
            self.mount.align_cancel()
            self.state["state"] = "error"
            self.state["error"] = "alignement manuel interrompu (modèle non enregistré)"
            self._log(self.state["error"])
        try:
            self.mount.stop()
        except OnStepError:
            pass

    def _log(self, msg: str) -> None:
        self.state["log"].append(f"{time.strftime('%H:%M:%S')} {msg}")

    def _run(self, n: int, targets: list[tuple[float, float]], simulate: bool) -> None:
        m = self.mount
        try:
            lat = m.latitude_deg()
            m.align_start(n)
            self._log(f"alignement {n} point(s) démarré (latitude {lat:.3f}°)")
            for i, (alt, az) in enumerate(targets, start=1):
                if self._abort:
                    raise OnStepError("interrompu")
                lst = parse_hours(m.cmd(":GS#"))
                ra, dec = astro.altaz_to_radec(alt, az, lat, lst)
                point = {"index": i, "alt": alt, "az": az, "target_ra_h": ra, "target_dec_deg": dec}
                self.state["points"].append(point)
                self._log(f"point {i}/{n} : goto Alt {alt:.0f}° Az {az:.0f}°")
                m.goto(ra, dec)
                m.wait_goto_done(abort=lambda: self._abort)
                time.sleep(SETTLE_S)
                if simulate:
                    # Essai sans ciel : on fait comme si le solve avait
                    # trouvé la cible décalée de quelques minutes d'arc.
                    s_ra = ra + random.uniform(-5, 5) / 60 / 15
                    s_dec = dec + random.uniform(-5, 5) / 60
                    point["solve"] = {"simulated": True}
                else:
                    res = solver.solve(hint=(ra, dec))
                    point["solve"] = res.as_dict()
                    if not res.solved:
                        raise OnStepError(f"point {i} : solve échoué ({res.message})")
                    s_ra, s_dec = res.ra_jnow_h, res.dec_jnow_deg
                point["solved_ra_h"], point["solved_dec_deg"] = s_ra, s_dec
                point["pointing_error_arcmin"] = round(
                    astro.separation_arcsec(ra, dec, s_ra, s_dec) / 60, 2)
                m.sync(s_ra, s_dec)   # en mode alignement : ajoute le point
                st = m.align_status()
                self._log(f"point {i} accepté (erreur de pointage "
                          f"{point['pointing_error_arcmin']}′) — OnStepX {st}")
            st = m.align_status()
            if st["current"] <= st["last"]:
                raise OnStepError(f"OnStepX n'a pas terminé l'alignement: {st}")
            if simulate:
                # Ne jamais écrire un modèle fictif en mémoire non volatile.
                self._log("simulation : modèle NON enregistré (pas de :AW#)")
            else:
                m.align_write()
                self._log("modèle enregistré (:AW#)")
            self.state["state"] = "done"
        except Exception as e:   # l'état doit toujours refléter l'échec
            try:
                m.align_cancel()     # ne pas laisser OnStepX en mode alignement
            except OnStepError:
                pass
            self.state["state"] = "error"
            self.state["error"] = str(e)
            self._log(f"ERREUR : {e}")
        finally:
            self.state["finished"] = time.time()
