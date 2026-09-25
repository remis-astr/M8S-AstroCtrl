"""
Plate solving ASTAP sur une trame de la caméra allsky.

Chaîne (tout sur le M8S) : trame RAW packée d'allsky (/guide/frame, avec la
pose et le gain réglés à la main par l'utilisateur) -> dépaquetage -> FITS
16 bits de la matrice de Bayer en pleine résolution, en RAM (/dev/shm) ->
astap_cli -check (filtre prévu par ASTAP pour les images couleur brutes ;
une réduction superpixel 2×2 déclenchait « small image dimensions ») avec
indices (position monture, hauteur de champ) -> J2000 -> JNow.
"""
from __future__ import annotations

import configparser
import datetime as dt
import json
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

import astro
import guidecam

ASTAP_CLI = "/opt/astap/astap_cli"
WORK = Path("/dev/shm/m8s_solve")
OPTICS_PATH = Path("/var/lib/m8s-ctrl/optics.json")
TIMEOUT_HINT_S = 60       # avec indice : ~1 à 2 s mesurés (champ 0,43°, indice à 4,6°)
TIMEOUT_BLIND_S = 300     # à l'aveugle : > 120 s mesurés sur un champ de 0,43°

# Focale de l'optique qui porte la caméra guide : réglage persistant, la
# caméra pouvant changer d'optique (lunette chercheuse 177 mm, 415 mm…).
# L'échelle d'une image résolue la confirme (206,265 × pixel µm / ″/px).
GUIDE_FOCAL_MM = 177.0


def load_optics() -> None:
    global GUIDE_FOCAL_MM
    try:
        GUIDE_FOCAL_MM = float(json.loads(OPTICS_PATH.read_text())["focal_mm"])
    except Exception:
        pass


def save_optics(focal_mm: float) -> None:
    global GUIDE_FOCAL_MM
    GUIDE_FOCAL_MM = float(focal_mm)
    OPTICS_PATH.parent.mkdir(parents=True, exist_ok=True)
    OPTICS_PATH.write_text(json.dumps({"focal_mm": GUIDE_FOCAL_MM}))


load_optics()
# Taille de pixel native des capteurs ; les modes "_bin" (binning 2×2
# matériel) la doublent.
SENSOR_PIXEL_UM = {"imx477": 1.55, "imx327": 2.9, "imx462": 2.9,
                   "imx585": 2.9, "imx662": 2.9, "imx678": 2.0}


@dataclass
class SolveResult:
    solved: bool
    ra_j2000_h: float | None = None
    dec_j2000_deg: float | None = None
    ra_jnow_h: float | None = None
    dec_jnow_deg: float | None = None
    rotation_deg: float | None = None      # CROTA2 : angle de position du champ
    scale_arcsec_px: float | None = None
    fov_height_deg: float | None = None
    capture_s: float = 0.0
    solve_s: float = 0.0
    utc: str = ""
    raw_mode: str = ""
    sensor: str = ""
    exposure_ms: float = 0.0     # pose réelle de la trame (lue dans ses en-têtes)
    gain: float = 0.0
    measured_focal_mm: float | None = None
    message: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def write_fits(path: Path, img: np.ndarray, cards: dict[str, object]) -> None:
    lines = ["SIMPLE  =                    T", "BITPIX  =                   16",
             "NAXIS   =                    2", f"NAXIS1  = {img.shape[1]:>20d}",
             f"NAXIS2  = {img.shape[0]:>20d}", "BZERO   =              32768.0",
             "BSCALE  =                  1.0"]
    for k, v in cards.items():
        val = f"'{v:<8}'" if isinstance(v, str) else f"{v:>20}"
        lines.append(f"{k:<8}= {val}")
    lines.append("END")
    hdr = b"".join(line.ljust(80).encode("ascii") for line in lines)
    hdr += b" " * ((2880 - len(hdr) % 2880) % 2880)
    body = (img.astype(np.int32) - 32768).astype(">i2").tobytes()
    body += b"\0" * ((2880 - len(body) % 2880) % 2880)
    path.write_bytes(hdr + body)


def pixel_um(frame: guidecam.RawFrame) -> float:
    """Taille effective d'un pixel de la trame : pixel du capteur, doublée
    en binning 2×2. Le binning vient de l'en-tête X-Binned d'allsky ; à
    défaut (ancienne version), du suffixe « _bin » du nom de mode."""
    base = SENSOR_PIXEL_UM.get(frame.sensor, 1.55)
    binned = getattr(frame, "binned", None)
    if binned is None:
        binned = frame.raw_mode.endswith("_bin")
    return base * (2 if binned else 1)


def solve(hint: tuple[float, float] | None = None, radius_deg: float = 10.0) -> SolveResult:
    """`hint` = (ra_h, dec_deg) approximatifs (JNow de la monture : les 22′
    de précession sont négligeables devant le rayon de recherche)."""
    t0 = time.monotonic()
    frame = guidecam.fetch_frame()
    exposure_ms = frame.exposure_us / 1000
    utc = dt.datetime.now(dt.timezone.utc)
    img = frame.data
    capture_s = time.monotonic() - t0

    scale = 206.265 * pixel_um(frame) / GUIDE_FOCAL_MM   # ″/px
    fov_h = img.shape[0] * scale / 3600
    WORK.mkdir(parents=True, exist_ok=True)
    fits = WORK / "frame.fits"
    ini = fits.with_suffix(".ini")
    ini.unlink(missing_ok=True)
    write_fits(fits, img, {"EXPTIME": exposure_ms / 1000, "XPIXSZ": pixel_um(frame),
                           "YPIXSZ": pixel_um(frame), "FOCALLEN": GUIDE_FOCAL_MM,
                           "BAYERPAT": frame.bayer,
                           "DATE-OBS": utc.strftime("%Y-%m-%dT%H:%M:%S")})
    cmd = [ASTAP_CLI, "-f", str(fits), "-fov", f"{fov_h:.4f}", "-z", "0", "-check"]
    if hint is not None:
        cmd += ["-ra", f"{hint[0]:.5f}", "-spd", f"{hint[1] + 90:.5f}", "-r", f"{radius_deg:g}"]
    else:
        cmd += ["-r", "180"]
    t1 = time.monotonic()
    timeout = TIMEOUT_HINT_S if hint is not None else TIMEOUT_BLIND_S
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        proc = None
    solve_s = time.monotonic() - t1

    res = SolveResult(solved=False, capture_s=round(capture_s, 2), solve_s=round(solve_s, 2),
                      utc=utc.isoformat(timespec="seconds"), raw_mode=frame.raw_mode,
                      sensor=frame.sensor, exposure_ms=exposure_ms, gain=frame.gain, fov_height_deg=round(fov_h, 3),
                      scale_arcsec_px=round(scale, 3))
    if proc is None:
        res.message = (f"délai dépassé ({timeout} s) — "
                       + ("vérifier la focale réglée" if hint is None else "vérifier la focale ou l'indice de position"))
        return res
    if not ini.exists():
        res.message = f"astap_cli sans résultat (code {proc.returncode}): {proc.stdout[-300:]}"
        return res
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read_string("[astap]\n" + ini.read_text(errors="replace"))
    raw = {k.upper(): v for k, v in cfg["astap"].items()}
    if raw.get("PLTSOLVD", "F").strip().upper() != "T":
        res.message = raw.get("WARNING", "") or raw.get("ERROR", "") or "non résolu"
        return res
    ra_h = float(raw["CRVAL1"]) / 15
    dec = float(raw["CRVAL2"])
    ra_now, dec_now = astro.j2000_to_jnow(ra_h, dec, utc)
    res.solved = True
    res.ra_j2000_h, res.dec_j2000_deg = ra_h, dec
    res.ra_jnow_h, res.dec_jnow_deg = ra_now, dec_now
    if "CROTA2" in raw:
        res.rotation_deg = float(raw["CROTA2"])
    if "CDELT2" in raw:
        res.scale_arcsec_px = round(abs(float(raw["CDELT2"])) * 3600, 3)
    res.message = "résolu"
    if frame.sensor in SENSOR_PIXEL_UM and res.scale_arcsec_px:
        # focale déduite de l'échelle mesurée : signale une focale mal réglée
        res.measured_focal_mm = round(206.265 * pixel_um(frame) / res.scale_arcsec_px, 1)
    return res


def offset_arcsec(res: SolveResult, ra_h: float, dec_deg: float) -> float:
    return astro.separation_arcsec(res.ra_jnow_h, res.dec_jnow_deg, ra_h, dec_deg)

