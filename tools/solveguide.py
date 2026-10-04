"""Guidage par plate solve (test) : maintient Saturne (+ décalage) au centre
de la caméra guide en agissant directement sur les axes ALTAZM
(n/s = hauteur, w/e = azimut, impulsions à 0,5× sidéral = 7,5″/s d'axe).
Usage : solveguide.py DUREE_S [AGRESSIVITE] [DALT_ARCMIN] [DAZ_ARCMIN]"""
import json, math, os, sys, time, urllib.request
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "m8s-ctrl"))
import axismeas
from astropy.time import Time
from astropy.coordinates import get_body, EarthLocation, solar_system_ephemeris, FK5
import astropy.units as u

B = "http://192.168.1.56:8080"
LAT, LON = 47.3, 0.4833
PULSE_ARCSEC_S = 0.5 * 15.041
PADDLE_ARCSEC_S = 8 * 15.041          # vitesse 5 = 8×
LOC = EarthLocation(lat=LAT * u.deg, lon=LON * u.deg, height=100 * u.m)
OFFSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "offsets.json")
LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "solveguide.jsonl")


def call(path, body=None, timeout=60):
    for k in range(3):
        try:
            data = None if body is None else json.dumps(body).encode()
            req = urllib.request.Request(B + path, data=data, method="GET" if body is None else "POST",
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except Exception as e:     # 502 pendant une reconnexion USB
            if k == 2:
                raise
            time.sleep(1.5)


def altaz(ra, dec, ts):
    lst = (axismeas.gmst_hours(ts) + LON / 15) % 24
    H = math.radians((lst - ra) * 15); d = math.radians(dec); p = math.radians(LAT)
    alt = math.asin(math.sin(p) * math.sin(d) + math.cos(p) * math.cos(d) * math.cos(H))
    az = math.atan2(-math.cos(d) * math.sin(H), math.sin(d) * math.cos(p) - math.cos(d) * math.cos(H) * math.sin(p))
    return math.degrees(alt), math.degrees(az) % 360


def saturn(ts):
    t = Time(ts, format="unix")
    with solar_system_ephemeris.set("builtin"):
        s = get_body("saturn", t, LOC).transform_to(FK5(equinox=t))
    return s.ra.hour, s.dec.deg


def paddle(direction, secs):
    t0 = time.time()
    while time.time() - t0 < secs:
        call("/paddle/press", {"direction": direction, "rate": 5})
        time.sleep(0.25)
    call("/paddle/release", {})


def main():
    dur = float(sys.argv[1]); aggr = float(sys.argv[2]) if len(sys.argv) > 2 else 0.6
    off_alt = float(sys.argv[3]) * 60 if len(sys.argv) > 3 else 0.0
    off_az = float(sys.argv[4]) * 60 if len(sys.argv) > 4 else 0.0
    t0 = time.time(); fails = 0; log = open(LOG, "a")
    while time.time() - t0 < dur:
        r = call("/solve", {"blind": False, "sync": False})
        if not r["solved"]:
            fails += 1
            print(f"{time.time()-t0:6.0f}s solve échoué ({fails})", flush=True)
            if fails >= 5:
                print("ARRÊT : 5 échecs de solve consécutifs"); break
            continue
        fails = 0
        try:
            o = json.load(open(OFFSETS))
            off_alt, off_az = o["alt_arcmin"] * 60, o["az_arcmin"] * 60
        except Exception:
            pass
        ts = r["utc_ts"]
        ca, cz = altaz(r["ra_jnow_h"], r["dec_jnow_deg"], ts)
        sa, sz = altaz(*saturn(ts), ts)
        e_alt = (sa - ca) * 3600 + off_alt                                  # ″ à corriger en hauteur
        e_az_axis = ((sz - cz + 180) % 360 - 180) * 3600 + off_az / math.cos(math.radians(ca))
        e_az_sky = e_az_axis * math.cos(math.radians(ca))
        err = math.hypot(e_alt, e_az_sky)
        rec = {"t": round(time.time() - t0, 1), "utc_ts": ts, "e_alt": round(e_alt, 1),
               "e_az_sky": round(e_az_sky, 1), "err": round(err, 1), "alt": round(ca, 3), "az": round(cz, 3)}
        if err > 2400:
            print(f"ARRÊT : écart {err/60:.1f}′ > 40′"); log.write(json.dumps(rec) + "\n"); break
        acts = []
        if err > 60:
            for d, e in (("n" if e_alt > 0 else "s", e_alt), ("w" if e_az_axis > 0 else "e", e_az_axis)):
                if abs(e) > 20:
                    secs = abs(e) * 0.9 / PADDLE_ARCSEC_S
                    paddle(d, secs); acts.append(f"raquette {d} {secs:.1f}s")
            time.sleep(2)
        else:
            longest = 0
            for d, e in (("n" if e_alt > 0 else "s", e_alt), ("w" if e_az_axis > 0 else "e", e_az_axis)):
                # « s » s'oppose au suivi qui monte : le tube s'arrête dans la
                # zone morte puis le suivi doit reprendre le jeu, l'effet réel
                # vaut ~2× l'impulsion -> gain moitié et seuil plus haut.
                down = d == "s"
                ms = int(min(abs(e) * aggr * (0.5 if down else 1.0) / PULSE_ARCSEC_S * 1000, 4000))
                if abs(e) >= (5 if down else 3) and ms >= 50:
                    call("/mount/guide", {"direction": d, "ms": ms}); acts.append(f"{d}{ms}")
                    longest = max(longest, ms)
            time.sleep(longest / 1000 + 0.5)
        rec["act"] = acts
        log.write(json.dumps(rec) + "\n"); log.flush()
        print(f"{rec['t']:6.0f}s  écart {err:6.1f}″  (haut {e_alt:+6.1f}″, az {e_az_sky:+6.1f}″)  {' '.join(acts)}", flush=True)


if __name__ == "__main__":
    main()
