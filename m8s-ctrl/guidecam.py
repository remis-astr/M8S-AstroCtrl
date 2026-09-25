"""
Récupération des trames de guidage depuis la caméra allsky (RPi0 2W).

allsky livre le RAW *packé* de picamera2 sur GET /guide/frame (voir
~/minicam_program/.../api/routes_guide.py) : tout le travail de
dépaquetage se fait ici, sur le M8S, pour épargner le CPU du Pi.
"""
from __future__ import annotations

import time
import urllib.request
from dataclasses import dataclass

import numpy as np

ALLSKY_URL = "http://192.168.7.3:8000"


@dataclass
class RawFrame:
    data: np.ndarray        # uint16 (H, W), valeurs brutes sur `bits` bits
    bits: int
    bayer: str              # motif de Bayer réel (ex. "BGGR")
    raw_mode: str
    sensor: str
    exposure_us: int
    gain: float
    sensor_ts_ns: int
    wall_time: float        # horloge d'allsky au moment de l'envoi
    fetch_s: float          # durée totale de la requête vue du M8S
    binned: bool | None = None   # binning 2×2 matériel annoncé par allsky (X-Binned)


def unpack(buf: bytes, width: int, height: int, stride: int, bits: int) -> np.ndarray:
    rows = np.frombuffer(buf, dtype=np.uint8).reshape(height, stride)
    if bits == 12:
        tight = rows[:, : width // 2 * 3].astype(np.uint16)
        out = np.empty((height, width), dtype=np.uint16)
        out[:, 0::2] = (tight[:, 0::3] << 4) | (tight[:, 2::3] & 0x0F)
        out[:, 1::2] = (tight[:, 1::3] << 4) | (tight[:, 2::3] >> 4)
        return out
    if bits == 10:
        tight = rows[:, : width // 4 * 5].astype(np.uint16)
        lsb = tight[:, 4::5]
        out = np.empty((height, width), dtype=np.uint16)
        for k in range(4):
            out[:, k::4] = (tight[:, k::5] << 2) | ((lsb >> (2 * k)) & 0x03)
        return out
    raise ValueError(f"profondeur RAW non supportée: {bits} bits")


def fetch_frame(base_url: str = ALLSKY_URL, timeout: float = 30.0) -> RawFrame:
    """Trame courante, avec la pose et le gain réglés par l'utilisateur sur
    la page d'allsky : le M8S ne modifie jamais ces réglages."""
    t0 = time.monotonic()
    with urllib.request.urlopen(f"{base_url}/guide/frame", timeout=timeout) as r:
        h = r.headers
        buf = r.read()
    fetch_s = time.monotonic() - t0
    width, height = int(h["X-Width"]), int(h["X-Height"])
    stride, bits = int(h["X-Stride"]), int(h["X-Bits"])
    if len(buf) != stride * height:
        raise ValueError(f"trame tronquée: {len(buf)} octets, attendu {stride * height}")
    return RawFrame(
        data=unpack(buf, width, height, stride, bits),
        bits=bits,
        bayer=h["X-Bayer"],
        raw_mode=h["X-Raw-Mode"],
        sensor=h["X-Sensor"],
        exposure_us=int(h["X-Exposure-Us"]),
        gain=float(h["X-Gain"]),
        sensor_ts_ns=int(h["X-Sensor-Timestamp-Ns"]),
        wall_time=float(h["X-Wall-Time"]),
        fetch_s=fetch_s,
        binned=(h["X-Binned"] == "1") if "X-Binned" in h else None,
    )

