"""
Liaison série LX200 avec un contrôleur OnStep / OnStepX.

Deux contrôleurs possibles sur ce projet (voir CONCEPTION.md) :
  - ALT-AZ : OnStepX sur FYSETC E4 (CH340, 9600 bauds)
  - EQ     : kit OnStep Pro V5 Terrans sur Vixen GP (port/baud à découvrir)
Le port et le débit sont donc détectés, et le type de monture est lu sur
le contrôleur (lettre de `:GU#`), jamais supposé.

Le port reste ouvert en permanence : chaque ouverture fait basculer DTR/RTS,
ce qui redémarre l'ESP32 de la E4.

Format des réponses (OnStepX, libApp/commands/ProcessCmds.cpp) :
  - commande « numérique » : un seul caractère '1' (succès) ou '0'
    (échec), SANS '#' — y compris quand une commande de lecture échoue,
    d'où le « 0 » reçu autrefois sur `:GR#` ;
  - commande « texte » : chaîne terminée par '#' ;
  - certaines commandes (`:Mg…#`, `:Q#`) ne répondent rien.
"""
from __future__ import annotations

import datetime as dt
import glob
import logging
import os
import re
import threading
import time

import serial

log = logging.getLogger("onstep")

BAUDS = (9600, 115200, 57600, 19200)
BOOT_WAIT_S = 6.0      # l'ESP32 peut redémarrer à l'ouverture du port
REPLY_TIMEOUT_S = 1.0

NUM, STR, NONE = "num", "str", "none"

MOUNT_TYPES = {"E": "GEM", "K": "FORK", "A": "ALTAZM", "L": "ALTALT"}
PIER_SIDES = {"o": None, "T": "east", "W": "west"}

# Codes de retour de :MS# (Goto.command.cpp)
GOTO_ERRORS = {
    "1": "sous la limite d'horizon",
    "2": "au-dessus de la limite zénithale",
    "3": "contrôleur en veille",
    "4": "monture parquée",
    "5": "goto déjà en cours",
    "6": "hors limites",
    "7": "défaut matériel",
    "8": "déjà en mouvement",
    "9": "erreur non spécifiée",
}


class OnStepError(RuntimeError):
    pass


def candidate_ports() -> list[str]:
    """Ports série candidats, un seul chemin par périphérique réel (le
    lien /dev/serial/by-id/… et /dev/ttyUSB0 désignent souvent le même)."""
    ports: dict[str, str] = {}
    for p in (sorted(glob.glob("/dev/serial/by-id/*"))
              + sorted(glob.glob("/dev/ttyUSB*")) + sorted(glob.glob("/dev/ttyACM*"))):
        ports.setdefault(os.path.realpath(p), p)
    return list(ports.values())


# ------------------------------------------------------------ conversions --

def parse_hours(s: str) -> float:
    """'HH:MM.T', 'HH:MM:SS' ou 'HH:MM:SS.SSSS' -> heures décimales."""
    m = re.fullmatch(r"(\d+):(\d+)(?:\.(\d))?(?::(\d+(?:\.\d+)?))?", s.strip())
    if not m:
        raise OnStepError(f"format horaire illisible: {s!r}")
    h, mi = int(m[1]), int(m[2])
    sec = float(m[4]) if m[4] else (int(m[3]) * 6 if m[3] else 0)
    return h + mi / 60 + sec / 3600


def parse_degrees(s: str) -> float:
    """'sDD*MM', 'sDD*MM:SS', "sDD*MM'SS", 'DDD*MM'SS.SSS' -> degrés décimaux."""
    m = re.fullmatch(r"([+-]?)(\d+)\D(\d+)(?:\D(\d+(?:\.\d+)?))?", s.strip())
    if not m:
        raise OnStepError(f"format angulaire illisible: {s!r}")
    v = int(m[2]) + int(m[3]) / 60 + (float(m[4]) / 3600 if m[4] else 0)
    return -v if m[1] == "-" else v


def fmt_ra(ra_h: float) -> str:
    total = round((ra_h % 24) * 3600)
    h, rem = divmod(total, 3600)
    return f"{h % 24:02d}:{rem // 60:02d}:{rem % 60:02d}"


def fmt_dec(dec_deg: float) -> str:
    if not -90 <= dec_deg <= 90:
        raise OnStepError(f"déclinaison hors plage: {dec_deg}")
    sign = "-" if dec_deg < 0 else "+"
    total = round(abs(dec_deg) * 3600)
    d, rem = divmod(total, 3600)
    return f"{sign}{d:02d}*{rem // 60:02d}:{rem % 60:02d}"


# ---------------------------------------------------------------- liaison --

class OnStep:
    def __init__(self) -> None:
        self._ser: serial.Serial | None = None
        self._lock = threading.Lock()
        self.port: str | None = None
        self.baud: int | None = None
        self.product: str | None = None
        self.version: str | None = None
        self._high_precision = True   # :GRH#/:GDH# (OnStepX) sinon repli
        self._acked_guide = True      # :MG (OnStepX) sinon :Mg

    # -- connexion ---------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._ser is not None and self._ser.is_open

    def connect(self) -> None:
        """Trouve le contrôleur : chaque port n'est ouvert qu'une fois, le
        débit est changé à chaud (pas de réouverture, donc pas de reset)."""
        with self._lock:
            if self.connected:
                return
            tried = []
            for port in candidate_ports():
                # DTR/RTS laissés à leur valeur par défaut : c'est la
                # configuration vérifiée sans reset intempestif de la E4.
                ser = serial.Serial()
                ser.port = port
                ser.baudrate = BAUDS[0]
                ser.timeout = 0.2
                try:
                    ser.open()
                except serial.SerialException as e:
                    tried.append(f"{port}: {e}")
                    continue
                product = self._probe(ser)
                if product:
                    self._ser, self.port, self.baud = ser, port, ser.baudrate
                    self.product = product
                    self.version = self._cmd(":GVN#", STR)
                    log.info("OnStep trouvé: %s @%d (%s %s)", port, ser.baudrate,
                             self.product, self.version)
                    return
                ser.close()
                tried.append(f"{port}: pas de réponse OnStep")
            raise OnStepError("aucun contrôleur OnStep trouvé — " + "; ".join(tried or ["aucun port série"]))

    def _probe(self, ser: serial.Serial) -> str | None:
        deadline = time.monotonic() + BOOT_WAIT_S
        while True:
            for baud in BAUDS:
                ser.baudrate = baud
                # '#' termine toute commande partielle laissée dans le
                # tampon d'OnStep (octets reçus à un mauvais débit, etc.)
                ser.write(b"#")
                time.sleep(0.1)
                ser.reset_input_buffer()
                ser.write(b":GVP#")
                reply = self._read_until_hash(ser, 0.4)
                if reply and "On-Step" in reply:
                    return reply
            if time.monotonic() > deadline:
                return None
            time.sleep(0.5)   # ESP32 encore en démarrage

    def _drop(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
        self._ser = None

    # -- échanges bas niveau ----------------------------------------------

    @staticmethod
    def _read_until_hash(ser: serial.Serial, timeout: float) -> str | None:
        buf = bytearray()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            b = ser.read(1)
            if not b:
                continue
            if b == b"#":
                return buf.decode("ascii", "replace")
            buf += b
        return None if not buf else buf.decode("ascii", "replace")

    def _cmd(self, cmd: str, kind: str, timeout: float = REPLY_TIMEOUT_S) -> str | None:
        ser = self._ser
        assert ser is not None
        ser.reset_input_buffer()
        ser.write(cmd.encode("ascii"))
        if kind == NONE:
            ser.flush()
            return None
        if kind == NUM:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                b = ser.read(1)
                if b:
                    return b.decode("ascii", "replace")
            raise OnStepError(f"pas de réponse à {cmd}")
        reply = self._read_until_hash(ser, timeout)
        if reply is None:
            raise OnStepError(f"pas de réponse à {cmd}")
        return reply

    def cmd(self, cmd: str, kind: str = STR, timeout: float = REPLY_TIMEOUT_S) -> str | None:
        """Envoie une commande, connecte au besoin ; en cas de perte du port
        (câble débranché), la connexion est abandonnée pour être refaite
        au prochain appel."""
        if not self.connected:
            self.connect()
        with self._lock:
            try:
                return self._cmd(cmd, kind, timeout)
            except serial.SerialException as e:
                self._drop()
                raise OnStepError(f"liaison série perdue: {e}") from e

    def cmd_ok(self, cmd: str) -> None:
        r = self.cmd(cmd, NUM)
        if r != "1":
            raise OnStepError(f"{cmd} refusé par le contrôleur (réponse {r!r})")

    # -- lectures ----------------------------------------------------------

    def _read_coord(self, hp_cmd: str, std_cmd: str, parse) -> float:
        """Réponse d'erreur = '0' sans '#' : lu ici comme une chaîne '0'
        après timeout de _read_until_hash, d'où le test explicite."""
        if self._high_precision:
            r = self.cmd(hp_cmd)
            if r not in (None, "0", "1"):
                return parse(r)
            self._high_precision = False
        r = self.cmd(std_cmd)
        if r in (None, "0"):
            raise OnStepError(f"{std_cmd} a échoué (réponse {r!r})")
        return parse(r)

    def status(self) -> dict:
        gu = self.cmd(":GU#") or ""
        mount_letter = next((c for c in gu if c in MOUNT_TYPES), None)
        pier = next((PIER_SIDES[c] for c in gu if c in PIER_SIDES), None)
        st = {
            "raw_gu": gu,
            "mount_type": MOUNT_TYPES.get(mount_letter),
            "tracking": "n" not in gu,
            "slewing": "N" not in gu,
            "parked": "P" in gu,
            "at_home": "H" in gu,
            "pulse_guiding": "G" in gu,
            "pier_side": pier,
            "error_code": int(gu[-1]) if gu and gu[-1].isdigit() else None,
        }
        st["ra_h"] = self._read_coord(":GRH#", ":GR#", parse_hours)
        st["dec_deg"] = self._read_coord(":GDH#", ":GD#", parse_degrees)
        st["alt_deg"] = self._read_coord(":GAH#", ":GA#", parse_degrees)
        st["az_deg"] = self._read_coord(":GZH#", ":GZ#", parse_degrees)
        return st

    def site_time(self) -> dict:
        return {
            "date": self.cmd(":GC#"),          # MM/DD/YY (heure locale)
            "local_time": self.cmd(":GL#"),    # HH:MM:SS
            "utc_offset": self.cmd(":GG#"),    # heures à AJOUTER au local pour UTC
            "latitude": self.cmd(":Gt#"),
            "longitude": self.cmd(":Gg#"),     # est négatif (convention LX200)
        }

    def utc_time(self) -> tuple[dt.datetime, dt.datetime]:
        """Heure UTC de la monture, avec l'instant système de la lecture.

        UTC = heure locale (`:GC#` + `:GLH#`) + `:GG#` (heures à ajouter
        au local pour obtenir l'UTC). C'est l'UTC, pas l'heure locale
        affichée, qui sert au temps sidéral et donc au pointage.
        """
        date = self.cmd(":GC#")                      # MM/DD/YY
        try:
            local = self.cmd(":GLH#")                # HH:MM:SS.SSSS
            if local in (None, "0"):
                raise OnStepError(":GLH# non supporté")
        except OnStepError:
            local = self.cmd(":GL#")
        read_at = dt.datetime.now(dt.timezone.utc)
        offset = self.cmd(":GG#")                    # sHH:MM
        m = re.fullmatch(r"(\d+)/(\d+)/(\d+)", date or "")
        t = re.fullmatch(r"(\d+):(\d+):(\d+(?:\.\d+)?)", local or "")
        o = re.fullmatch(r"([+-]?)(\d+):(\d+)", offset or "")
        if not (m and t and o):
            raise OnStepError(f"date/heure illisibles: {date!r} {local!r} {offset!r}")
        naive = dt.datetime(2000 + int(m[3]), int(m[1]), int(m[2]), int(t[1]), int(t[2]),
                            tzinfo=dt.timezone.utc) + dt.timedelta(seconds=float(t[3]))
        sign = -1 if o[1] == "-" else 1
        utc = naive + sign * dt.timedelta(hours=int(o[2]), minutes=int(o[3]))
        return utc, read_at

    def sidereal_hours(self) -> float:
        r = self.cmd(":GSH#")
        if r in (None, "0"):
            r = self.cmd(":GS#")
        return parse_hours(r)

    def latitude_deg(self) -> float:
        r = self.cmd(":GtH#")
        if r in (None, "0"):
            r = self.cmd(":Gt#")
        return parse_degrees(r)

    def wait_goto_done(self, timeout_s: float = 240.0, abort=None) -> None:
        """Attend la fin du goto (lettre 'N' = pas de goto dans :GU#)."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if abort is not None and abort():
                raise OnStepError("interrompu")
            if "N" in (self.cmd(":GU#") or ""):
                return
            time.sleep(0.5)
        raise OnStepError("goto trop long (délai dépassé)")

    # -- déplacements manuels (raquette) ------------------------------------

    # :Rn# (Guide.command.cpp) : 0=0,25× 1=0,5× 2=1× 3=2× 4=4× 5=8× 6=20×
    # 7=48× 8=½ max 9=max. Une vitesse ≤ 1× remplacerait AUSSI la vitesse
    # des impulsions de guidage (et fausserait la calibration) : la raquette
    # n'accepte donc que 3..9.
    MOVE_RATES = {3: "2×", 4: "4×", 5: "8×", 6: "20×", 7: "48×", 8: "½ max", 9: "max"}

    def guide_rate_index(self) -> int | None:
        """Vitesse de déplacement manuel courante (avant-dernier chiffre de :GU#)."""
        gu = self.cmd(":GU#") or ""
        return int(gu[-2]) if len(gu) >= 2 and gu[-2].isdigit() else None

    def move_start(self, direction: str, rate_index: int) -> None:
        """Déplacement continu. En ALTAZM, n/s agissent sur la hauteur et
        e/w sur l'azimut (mesuré le 24/09 : n = hauteur +, w = azimut +) ;
        en EQ, sur Déc et AD."""
        if len(direction) != 1 or direction not in "nsew":
            raise OnStepError("direction doit être n, s, e ou w")
        if rate_index not in self.MOVE_RATES:
            raise OnStepError("vitesse de raquette hors plage (3..9)")
        self.cmd(f":R{rate_index}#", NONE)
        self.cmd(f":M{direction}#", NONE)

    def move_stop(self, direction: str) -> None:
        self.cmd(f":Q{direction}#", NONE)

    def set_guide_rate_index(self, rate_index: int) -> None:
        self.cmd(f":R{rate_index}#", NONE)

    # -- alignement (Goto.command.cpp) --------------------------------------

    def align_start(self, n_points: int) -> None:
        """Démarre un alignement à n points. OnStepX remet alors la monture
        à sa position « home » (sans bouger) et active le suivi : la
        monture doit donc réellement être en position home."""
        if not 1 <= n_points <= 9:
            raise OnStepError("nombre de points d'alignement hors plage (1..9)")
        self.cmd_ok(f":A{n_points}#")

    def align_status(self) -> dict:
        r = self.cmd(":A?#") or "000"
        return {"max": ord(r[0]) - 48, "current": ord(r[1]) - 48, "last": ord(r[2]) - 48}

    def align_write(self) -> None:
        self.cmd_ok(":AW#")

    def align_cancel(self) -> None:
        """Sort du mode alignement sans rien enregistrer (`:SX09,0#` ->
        Goto::alignReset). Sans ça, après une interruption, un `:CM#` de
        recalage ajouterait encore un point au modèle."""
        self.cmd(":SX09,0#", NUM)
        st = self.align_status()
        if st["last"] != 0:
            raise OnStepError(f"OnStepX est resté en mode alignement: {st}")

    # -- actions -----------------------------------------------------------

    def tracking(self, on: bool) -> bool:
        self.cmd_ok(":Te#" if on else ":Td#")
        time.sleep(0.2)
        actual = "n" not in (self.cmd(":GU#") or "")
        if actual != on:
            raise OnStepError(f"suivi demandé {'ON' if on else 'OFF'}, "
                              f"mais :GU# indique {'ON' if actual else 'OFF'}")
        return actual

    def set_target(self, ra_h: float, dec_deg: float) -> None:
        self.cmd_ok(f":Sr{fmt_ra(ra_h)}#")
        self.cmd_ok(f":Sd{fmt_dec(dec_deg)}#")

    def goto(self, ra_h: float, dec_deg: float) -> None:
        self.set_target(ra_h, dec_deg)
        r = self.cmd(":MS#", NUM)
        if r != "0":
            raise OnStepError(f"goto refusé: {GOTO_ERRORS.get(r, r)}")

    def sync(self, ra_h: float, dec_deg: float) -> None:
        """Sync sur (ra, dec). En mode alignement (`:A<n>#` actif), OnStepX
        ajoute ce point au modèle d'alignement avec ces coordonnées comme
        position vraie (Goto.command.cpp, alignAddStar(true)) — c'est ce qui
        permet l'alignement par plate solve sans viser d'étoile."""
        self.set_target(ra_h, dec_deg)
        r = self.cmd(":CM#")
        if r is None or r.startswith("E") or r == "0":
            raise OnStepError(f"sync refusé (réponse {r!r})")

    def stop(self) -> None:
        self.cmd(":Q#", NONE)

    def pulse_guide(self, direction: str, ms: int) -> None:
        """Impulsion de guidage. En OnStepX, w/e agissent sur Axis1 et n/s
        sur Axis2 (Guide.command.cpp) : en ALTAZM ce sont donc les axes
        Az/Alt, pas RA/Dec. `:MG` (acquitté) est préféré à `:Mg` (muet)."""
        if len(direction) != 1 or direction not in "nsew":
            raise OnStepError("direction doit être n, s, e ou w")
        if not 1 <= ms <= 16399:
            raise OnStepError("durée d'impulsion hors plage (1..16399 ms)")
        if self._acked_guide:
            try:
                r = self.cmd(f":MG{direction}{ms}#", NUM, timeout=0.5)
            except OnStepError:
                self._acked_guide = False   # firmware sans :MG — repli muet
            else:
                if r != "1":
                    raise OnStepError(f"impulsion {direction}{ms} refusée")
                return
        self.cmd(f":Mg{direction}{ms}#", NONE)
