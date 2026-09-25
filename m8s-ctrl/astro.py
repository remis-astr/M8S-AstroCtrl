"""
Petits calculs astronomiques (sans dépendance) pour le M8S.

ASTAP rend des coordonnées J2000 ; OnStepX (MOUNT_COORDS TOPOCENTRIC)
échange des coordonnées *de la date* (JNow) sur sa liaison LX200. En 2026 la
précession seule représente ~22′ — indispensable pour un alignement.
Précession : Meeus, Astronomical Algorithms, ch. 21 (rigoureuse).
Nutation : Meeus ch. 22, version basse précision (~0,5″).
Aberration annuelle (≤ 20,5″) volontairement ignorée : négligeable devant
la précision de pointage d'une lunette chercheuse.
"""
from __future__ import annotations

import datetime as dt
import math

ARCSEC = math.pi / (180 * 3600)


def julian_date(t: dt.datetime) -> float:
    return t.timestamp() / 86400.0 + 2440587.5


def _nutation(T: float) -> tuple[float, float, float]:
    """(Δψ, Δε, ε) en radians, T en siècles juliens depuis J2000."""
    omega = math.radians(125.04452 - 1934.136261 * T)
    L = math.radians(280.4665 + 36000.7698 * T)
    Lm = math.radians(218.3165 + 481267.8813 * T)
    dpsi = (-17.20 * math.sin(omega) - 1.32 * math.sin(2 * L)
            - 0.23 * math.sin(2 * Lm) + 0.21 * math.sin(2 * omega)) * ARCSEC
    deps = (9.20 * math.cos(omega) + 0.57 * math.cos(2 * L)
            + 0.10 * math.cos(2 * Lm) - 0.09 * math.cos(2 * omega)) * ARCSEC
    eps0 = (84381.448 - 46.8150 * T - 0.00059 * T**2 + 0.001813 * T**3) * ARCSEC
    return dpsi, deps, eps0 + deps


def j2000_to_jnow(ra_h: float, dec_deg: float, when: dt.datetime) -> tuple[float, float]:
    """Coordonnées moyennes J2000 -> apparentes de la date (sans aberration)."""
    t = (julian_date(when) - 2451545.0) / 36525.0
    zeta = (2306.2181 * t + 0.30188 * t**2 + 0.017998 * t**3) * ARCSEC
    z = (2306.2181 * t + 1.09468 * t**2 + 0.018203 * t**3) * ARCSEC
    theta = (2004.3109 * t - 0.42665 * t**2 - 0.041833 * t**3) * ARCSEC
    a0, d0 = math.radians(ra_h * 15), math.radians(dec_deg)
    A = math.cos(d0) * math.sin(a0 + zeta)
    B = math.cos(theta) * math.cos(d0) * math.cos(a0 + zeta) - math.sin(theta) * math.sin(d0)
    C = math.sin(theta) * math.cos(d0) * math.cos(a0 + zeta) + math.cos(theta) * math.sin(d0)
    a = math.atan2(A, B) + z
    d = math.asin(max(-1.0, min(1.0, C)))
    dpsi, deps, eps = _nutation(t)
    if abs(math.cos(d)) > 1e-9:   # nutation indéfinie exactement au pôle
        da = ((math.cos(eps) + math.sin(eps) * math.sin(a) * math.tan(d)) * dpsi
              - math.cos(a) * math.tan(d) * deps)
        dd = math.sin(eps) * math.cos(a) * dpsi + math.sin(a) * deps
        a, d = a + da, d + dd
    return (math.degrees(a) / 15) % 24, math.degrees(d)


def altaz_to_hadec(alt_deg: float, az_deg: float, lat_deg: float) -> tuple[float, float]:
    """(hauteur, azimut compté depuis le nord vers l'est) -> (angle horaire h, déc °)."""
    h, A, phi = map(math.radians, (alt_deg, az_deg, lat_deg))
    sin_d = math.sin(h) * math.sin(phi) + math.cos(h) * math.cos(phi) * math.cos(A)
    d = math.asin(max(-1.0, min(1.0, sin_d)))
    H = math.atan2(-math.sin(A) * math.cos(h),
                   math.sin(h) * math.cos(phi) - math.cos(h) * math.sin(phi) * math.cos(A))
    return (math.degrees(H) / 15) % 24, math.degrees(d)


def altaz_to_radec(alt_deg: float, az_deg: float, lat_deg: float, lst_h: float) -> tuple[float, float]:
    ha, dec = altaz_to_hadec(alt_deg, az_deg, lat_deg)
    return (lst_h - ha) % 24, dec


def parallactic_angle_deg(ha_h: float, dec_deg: float, lat_deg: float) -> float:
    """Angle parallactique q (utile au guidage ALT-AZ, voir CONCEPTION.md §6)."""
    H, d, phi = math.radians(ha_h * 15), math.radians(dec_deg), math.radians(lat_deg)
    return math.degrees(math.atan2(math.sin(H), math.tan(phi) * math.cos(d) - math.sin(d) * math.cos(H)))


def separation_arcsec(ra1_h: float, dec1: float, ra2_h: float, dec2: float) -> float:
    a1, d1, a2, d2 = math.radians(ra1_h * 15), math.radians(dec1), math.radians(ra2_h * 15), math.radians(dec2)
    c = math.sin(d1) * math.sin(d2) + math.cos(d1) * math.cos(d2) * math.cos(a1 - a2)
    return math.degrees(math.acos(max(-1.0, min(1.0, c)))) * 3600
