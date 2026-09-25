"""
Détection d'étoiles, appariement et ajustement de similitude (numpy seul).

Travaille directement sur la matrice de Bayer, sans binning (décision
utilisateur : pas de perte de résolution) : chaque canal R/G/G/B est
ramené à la même échelle de bruit, puis l'image est traitée comme mono.
Voir CONCEPTION.md §6.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

BG_BLOCK = 64          # taille des blocs pour le fond de ciel (px)
DETECT_SNR = 6.0       # seuil de détection sur l'image lissée 3×3
CENTROID_R = 4         # demi-fenêtre de centroïde (px)
MAX_STARS = 40


@dataclass
class Star:
    x: float
    y: float
    flux: float
    snr: float
    peak: float
    saturated: bool


def normalize(raw: np.ndarray) -> np.ndarray:
    """Bayer brut -> carte en unités de bruit (fond retiré), float32.
    Chaque canal CFA a son propre niveau de fond et son propre bruit."""
    img = raw.astype(np.float32)
    out = np.empty_like(img)
    for dy in (0, 1):
        for dx in (0, 1):
            ch = img[dy::2, dx::2]
            med = float(np.median(ch[::3, ::3]))
            mad = float(np.median(np.abs(ch[::3, ::3] - med)))
            sigma = max(1.4826 * mad, 0.5)
            out[dy::2, dx::2] = (ch - med) / sigma
    return out - background(out)


def background(img: np.ndarray) -> np.ndarray:
    """Fond lent (gradients, lune…) : médiane par blocs, étirée au plus
    proche voisin — suffisant devant des étoiles de quelques pixels."""
    h, w = img.shape
    bh, bw = h // BG_BLOCK, w // BG_BLOCK
    if bh == 0 or bw == 0:
        return np.zeros_like(img)
    core = img[: bh * BG_BLOCK, : bw * BG_BLOCK].reshape(bh, BG_BLOCK, bw, BG_BLOCK)
    med = np.median(core[:, ::4, :, ::4], axis=(1, 3))
    full = np.repeat(np.repeat(med, BG_BLOCK, axis=0), BG_BLOCK, axis=1)
    out = np.empty_like(img)
    out[: full.shape[0], : full.shape[1]] = full
    out[full.shape[0]:, :] = out[full.shape[0] - 1: full.shape[0], :]
    out[:, full.shape[1]:] = out[:, full.shape[1] - 1: full.shape[1]]
    return out


def box3(img: np.ndarray) -> np.ndarray:
    p = np.pad(img, 1, mode="edge")
    s = np.zeros_like(img)
    for dy in range(3):
        for dx in range(3):
            s += p[dy: dy + img.shape[0], dx: dx + img.shape[1]]
    return s / 9.0


def detect(raw: np.ndarray, bits: int, max_stars: int = MAX_STARS,
           snr_min: float = DETECT_SNR) -> list[Star]:
    norm = normalize(raw)
    sm = box3(norm)
    h, w = sm.shape
    m = CENTROID_R + 2
    core = sm[1:-1, 1:-1]
    peak = core > snr_min
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dy or dx:
                peak &= core >= sm[1 + dy: h - 1 + dy, 1 + dx: w - 1 + dx]
    ys, xs = np.nonzero(peak)
    ys, xs = ys + 1, xs + 1
    keep = (ys >= m) & (ys < h - m) & (xs >= m) & (xs < w - m)
    ys, xs = ys[keep], xs[keep]
    order = np.argsort(-sm[ys, xs])
    sat_level = 0.95 * ((1 << bits) - 1)
    stars: list[Star] = []
    for i in order:
        if len(stars) >= max_stars * 2:
            break
        y0, x0 = int(ys[i]), int(xs[i])
        # une seule détection par étoile (plateaux, étoiles brillantes)
        if any(abs(s.x - x0) <= CENTROID_R and abs(s.y - y0) <= CENTROID_R for s in stars):
            continue
        st = centroid(norm, raw, x0, y0, sat_level)
        if st is not None:
            stars.append(st)
    stars = [s for s in stars if not s.saturated][:max_stars] or stars[:max_stars]
    return stars


def centroid(norm: np.ndarray, raw: np.ndarray, x0: int, y0: int, sat_level: float) -> Star | None:
    r = CENTROID_R
    for _ in range(2):   # un recentrage
        win = norm[y0 - r: y0 + r + 1, x0 - r: x0 + r + 1]
        if win.shape != (2 * r + 1, 2 * r + 1):
            return None
        wgt = np.clip(win, 0, None)
        tot = float(wgt.sum())
        if tot <= 0:
            return None
        yy, xx = np.mgrid[-r: r + 1, -r: r + 1]
        cx = float((wgt * xx).sum() / tot)
        cy = float((wgt * yy).sum() / tot)
        nx, ny = int(round(x0 + cx)), int(round(y0 + cy))
        if (nx, ny) == (x0, y0):
            break
        x0, y0 = nx, ny
    peak_raw = float(raw[y0 - r: y0 + r + 1, x0 - r: x0 + r + 1].max())
    snr = tot / math.sqrt(win.size)
    return Star(x0 + cx, y0 + cy, tot, snr, peak_raw, peak_raw >= sat_level)


# ------------------------------------------------------------ appariement --

def estimate_shift(ref: np.ndarray, cur: np.ndarray, tol: float = 3.0) -> np.ndarray | None:
    """Décalage global par vote sur toutes les différences de paires (étoiles
    les plus brillantes). Sert à raccrocher après un grand déplacement
    (calibration) sans connaître le mouvement à l'avance. Chaque différence
    vote pour elle-même : il faut donc au moins 3 voix concordantes (2 s'il
    n'y a que 2 étoiles) pour éviter qu'une coïncidence l'emporte."""
    if len(ref) == 0 or len(cur) == 0:
        return None
    a, b = ref[:15], cur[:15]
    if len(a) == 1 and len(b) == 1:
        return b[0] - a[0]
    diffs = (b[None, :, :] - a[:, None, :]).reshape(-1, 2)
    votes = np.array([(np.hypot(*(diffs - d).T) <= tol).sum() for d in diffs])
    k = int(np.argmax(votes))
    if votes[k] < min(3, len(a), len(b)):
        return None
    close = diffs[np.hypot(*(diffs - diffs[k]).T) <= tol]
    return close.mean(axis=0)


def match(ref: np.ndarray, cur: np.ndarray, predicted: np.ndarray, radius: float = 6.0) -> list[tuple[int, int]]:
    """Paires (i_ref, j_cur) : chaque étoile de référence, déplacée selon
    la prédiction, est associée à l'étoile courante la plus proche."""
    pairs, used = [], set()
    for i, p in enumerate(predicted):
        if len(cur) == 0:
            break
        d = np.hypot(*(cur - p).T)
        j = int(np.argmin(d))
        if d[j] <= radius and j not in used:
            pairs.append((i, j))
            used.add(j)
    return pairs


@dataclass
class Similarity:
    angle: float          # radians, rotation image ref -> courante
    t: np.ndarray         # translation (px)
    n: int                # étoiles utilisées
    rms: float            # résidu (px)
    rotation_fitted: bool

    def apply(self, p: np.ndarray) -> np.ndarray:
        c, s = math.cos(self.angle), math.sin(self.angle)
        return p @ np.array([[c, s], [-s, c]]) + self.t


def fit_similarity(P: np.ndarray, Q: np.ndarray, fixed_angle: float | None = None) -> Similarity:
    """Moindres carrés rotation + translation (échelle fixe = 1) de P vers
    Q, avec rejet itératif des appariements aberrants. Si `fixed_angle`
    est donné (trop peu d'étoiles ou bras de levier trop court), seule la
    translation est ajustée."""
    idx = np.arange(len(P))
    for _ in range(3):
        p, q = P[idx], Q[idx]
        pc, qc = p.mean(axis=0), q.mean(axis=0)
        if fixed_angle is None:
            H = (p - pc).T @ (q - qc)
            angle = math.atan2(H[0, 1] - H[1, 0], H[0, 0] + H[1, 1])
        else:
            angle = fixed_angle
        c, s = math.cos(angle), math.sin(angle)
        R = np.array([[c, s], [-s, c]])
        t = qc - pc @ R
        res = np.hypot(*((P @ R + t) - Q).T)
        good = np.nonzero(res <= max(1.5, 3 * np.median(res[idx])))[0]
        if len(good) == len(idx) or len(good) < 2:
            break
        idx = good
    rms = float(np.sqrt(np.mean(res[idx] ** 2))) if len(idx) else 0.0
    return Similarity(angle, t, len(idx), rms, fixed_angle is None)
