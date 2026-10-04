"""Test du jeu par plate solve : petits déplacements alternés, solve après
chaque arrêt, comparaison angle vrai / angle compté (corrigé du rapport de
pas mesuré). Usage : backlash.py AXE(1|2) RATIO SEQUENCE [rate] [press_s]"""
import json, math, os, sys, time, urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "m8s-ctrl"))
import axismeas

B = "http://192.168.1.56:8080"
LAT, LON = 47.3, -0.4833 + 0.0   # remplacé par /mount/site


def call(path, body=None, timeout=60):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(B + path, data=data, method="GET" if body is None else "POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def true_altaz(ra_h, dec_deg, ts, lat, lon):
    lst = (axismeas.gmst_hours(ts) + lon / 15) % 24
    H = math.radians((lst - ra_h) * 15); d = math.radians(dec_deg); p = math.radians(lat)
    alt = math.asin(math.sin(p) * math.sin(d) + math.cos(p) * math.cos(d) * math.cos(H))
    az = math.atan2(-math.cos(d) * math.sin(H), math.sin(d) * math.cos(p) - math.cos(d) * math.cos(H) * math.sin(p))
    return math.degrees(alt), math.degrees(az) % 360


def still():
    prev = None
    for _ in range(40):
        s = call("/status")
        cur = (s["alt_deg"], s["az_deg"])
        if prev and abs(cur[0] - prev[0]) < 2e-4 and abs(cur[1] - prev[1]) < 2e-4:
            return s
        prev = cur
        time.sleep(1)
    return s


def press(d, rate, secs):
    t0 = time.time()
    while time.time() - t0 < secs:
        call("/paddle/press", {"direction": d, "rate": rate})
        time.sleep(0.3)
    call("/paddle/release", {})


def measure():
    s = still(); time.sleep(2)
    for _ in range(2):
        r = call("/solve", {"blind": False, "sync": False})
        if r["solved"]:
            break
    return s, r


def main():
    axis, ratio, seq = int(sys.argv[1]), float(sys.argv[2]), sys.argv[3]
    rate = int(sys.argv[4]) if len(sys.argv) > 4 else 6
    secs = float(sys.argv[5]) if len(sys.argv) > 5 else 3.0
    site = call("/mount/site")
    lat = LAT
    lon = -axismeas_lon(site["longitude"])
    if call("/status")["tracking"]:
        call("/mount/track", {"action": "stop"})
    pts = []
    s, r = measure(); pts.append((None, s, r))
    for d in seq:
        press(d, rate, secs)
        s, r = measure(); pts.append((d, s, r))
    out = []
    prev = None
    for d, s, r in pts:
        if not r["solved"]:
            print(d, "solve échoué"); prev = None; continue
        ta, tz = true_altaz(r["ra_jnow_h"], r["dec_jnow_deg"], r["utc_ts"], lat, lon)
        cur = (s["alt_deg"], s["az_deg"], ta, tz)
        if prev and d:
            if axis == 2:
                dc, dt_ = cur[0] - prev[0], cur[2] - prev[2]
            else:
                dc = (cur[1] - prev[1] + 180) % 360 - 180
                dt_ = (cur[3] - prev[3] + 180) % 360 - 180
            err = (dt_ - dc * ratio) * 3600
            out.append({"dir": d, "counted_arcsec": round(dc * 3600, 1), "true_arcsec": round(dt_ * 3600, 1),
                        "err_arcsec": round(err, 1)})
            print(f"{d}  compté {dc*3600:8.1f}″  vrai {dt_*3600:8.1f}″  écart {err:7.1f}″")
        prev = cur
    print(json.dumps(out))


def axismeas_lon(s):
    # "-000*29" (OnStep : ouest positif) -> degrés ouest
    sign = -1 if s.strip().startswith("-") else 1
    deg, minute = s.strip().lstrip("+-").split("*")
    return sign * (int(deg) + int(minute) / 60)


if __name__ == "__main__":
    main()
