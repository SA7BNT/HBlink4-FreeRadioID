"""
APRS packets: parsing (TNC2 text format as used on APRS-IS) and building.

Understood formats: uncompressed and compressed positions (with and without
timestamp), Mic-E, objects, items, messages (incl. acks), status, positionless
weather, raw NMEA ($GPRMC/$GPGGA), third-party packets and the common comment
extensions (course/speed, altitude, weather in the comment of '_' symbols).

Everything else is kept as an "other" packet: it is still forwarded to
clients, it just has no position for the map.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from hblink4.daprs import parse_nmea  # noqa: E402

TOCALL = "APZN8D"          # experimental tocall (APZ = experimental software)
SOFTWARE = "N8-APRS"

CALL_RE = re.compile(r"^[A-Z0-9]{1,9}(-[A-Z0-9]{1,2})?$")
_PATH_ELEM_RE = re.compile(r"^[A-Za-z0-9-]{1,9}\*?$")
_SRC_RE = re.compile(r"^[A-Za-z0-9]{1,9}(-[A-Za-z0-9]{1,2})?$")
_Q_RE = re.compile(r"^qA[CXUoOSrRRZI]$")


class APRSError(ValueError):
    """Not a valid APRS-IS packet (reason in the message)."""


def passcode(callsign: str) -> int:
    """The APRS-IS passcode of a callsign (the SSID does not count)."""
    base = callsign.upper().split("-")[0][:10]
    h = 0x73E2
    for i in range(0, len(base), 2):
        h ^= ord(base[i]) << 8
        if i + 1 < len(base):
            h ^= ord(base[i + 1])
    return h & 0x7FFF


def valid_call(call: str) -> bool:
    return bool(CALL_RE.match(call or ""))


def base_call(call: str) -> str:
    return (call or "").upper().split("-")[0]


@dataclass
class Packet:
    raw: str                       # SRC>DST,PATH:body
    src: str
    dst: str
    path: List[str]
    body: str
    type: str = "other"            # position, object, item, message, ack, status, weather, mice, telemetry, nmea, query, other
    lat: Optional[float] = None
    lon: Optional[float] = None
    symbol: str = ""               # table + code, e.g. "/>"
    comment: str = ""
    course: Optional[int] = None
    speed_kmh: Optional[float] = None
    altitude_m: Optional[float] = None
    name: str = ""                 # object / item name
    alive: bool = True             # object/item: False = killed
    msg_to: str = ""
    msg_text: str = ""
    msg_id: str = ""
    status: str = ""
    weather: Dict[str, Any] = field(default_factory=dict)
    messaging: bool = False
    third_party: bool = False

    @property
    def header(self) -> str:
        return f"{self.src}>{','.join([self.dst] + self.path)}"

    @property
    def key(self) -> str:
        """Duplicate check: same source, destination and body (path ignored)."""
        return f"{self.src}>{self.dst}:{self.body}"

    @property
    def station(self) -> str:
        """Who this packet puts on the map: the object/item name or the source."""
        return self.name if self.type in ("object", "item") else self.src

    def q_construct(self) -> Tuple[Optional[str], Optional[str]]:
        for i, p in enumerate(self.path):
            if _Q_RE.match(p):
                return p, (self.path[i + 1] if i + 1 < len(self.path) else None)
        return None, None

    def with_path(self, path: List[str]) -> "Packet":
        self.path = path
        self.raw = f"{self.header}:{self.body}"
        return self

    def has_position(self) -> bool:
        return self.lat is not None and self.lon is not None


# ------------------------------------------------------------------ parsing

def _uncompressed(s: str) -> Optional[Tuple[float, float, str, str]]:
    """'DDMM.mmN/DDDMM.mmE>' (19 chars) → lat, lon, symbol, rest."""
    if len(s) < 19:
        return None
    lat_s, table, lon_s, code = s[0:8], s[8], s[9:18], s[18]
    lat_s = lat_s.replace(" ", "0") if lat_s[2:4].strip() else lat_s.replace(" ", "0")
    lon_s = lon_s.replace(" ", "0")
    m1 = re.match(r"^(\d{2})(\d{2}\.\d{2})([NS])$", lat_s)
    m2 = re.match(r"^(\d{3})(\d{2}\.\d{2})([EW])$", lon_s)
    if not m1 or not m2:
        return None
    lat = int(m1.group(1)) + float(m1.group(2)) / 60.0
    lon = int(m2.group(1)) + float(m2.group(2)) / 60.0
    if m1.group(3) == "S":
        lat = -lat
    if m2.group(3) == "W":
        lon = -lon
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    if not (table in "/\\" or table.isalnum()):
        return None
    return lat, lon, table + code, s[19:]


def _b91(s: str) -> int:
    v = 0
    for c in s:
        n = ord(c) - 33
        if not 0 <= n <= 90:
            raise ValueError("base91")
        v = v * 91 + n
    return v


def _compressed(s: str) -> Optional[Tuple[float, float, str, str, Dict[str, Any]]]:
    """Compressed position (13 chars) → lat, lon, symbol, rest, extras."""
    if len(s) < 13:
        return None
    table = s[0]
    if not (table in "/\\" or table.isalnum()):
        return None
    try:
        lat = 90.0 - _b91(s[1:5]) / 380926.0
        lon = -180.0 + _b91(s[5:9]) / 190463.0
    except ValueError:
        return None
    if table.isdigit():                       # a-j encode overlay digits
        table = chr(ord(table) - ord("a") + ord("0")) if table in "abcdefghij" else table
    code = s[9]
    extras: Dict[str, Any] = {}
    c, sp, t = s[10], s[11], s[12]
    if c != " ":
        ct = ord(t) - 33
        if (ct >> 3) & 3 == 2:                # GGA: altitude
            try:
                extras["altitude_m"] = round(1.002 ** _b91(c + sp) * 0.3048, 1)
            except ValueError:
                pass
        elif "!" <= c <= "z":
            extras["course"] = ((ord(c) - 33) * 4) % 360 or 360
            extras["speed_kmh"] = round((1.08 ** (ord(sp) - 33) - 1) * 1.852, 1)
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon, table + code, s[13:], extras


def _comment_extras(p: Packet, comment: str) -> str:
    """Course/speed (CSE/SPD), altitude (/A=), weather data – the rest stays the comment."""
    m = re.match(r"^(\d{3})/(\d{3})", comment)
    if m and p.symbol[1:] != "_":
        crs, spd = int(m.group(1)), int(m.group(2))
        if 0 < crs <= 360:
            p.course = crs % 360
        p.speed_kmh = round(spd * 1.852, 1)
        comment = comment[7:]
    m = re.search(r"/A=(-?\d{5,6})", comment)
    if m:
        p.altitude_m = round(int(m.group(1)) * 0.3048, 1)
        comment = comment[:m.start()] + comment[m.end():]
    if p.symbol[1:] == "_":
        comment = _weather(p, comment, with_wind=True)
    return comment.strip()


_WX_FIELDS = {"g": ("gust_kmh", 3, 1.609344), "t": ("temp_c", 3, None), "r": ("rain_1h_mm", 3, 0.254),
              "p": ("rain_24h_mm", 3, 0.254), "P": ("rain_midnight_mm", 3, 0.254), "h": ("humidity", 2, 1),
              "b": ("pressure_hpa", 5, 0.1), "L": ("luminosity", 3, 1), "l": ("luminosity", 3, 1)}


def _weather(p: Packet, s: str, with_wind: bool) -> str:
    wx: Dict[str, Any] = {}
    m = re.match(r"^(\d{3}|\.{3}|\s{3})/(\d{3}|\.{3}|\s{3})", s) if with_wind else None
    if m:
        if m.group(1).isdigit():
            wx["wind_dir"] = int(m.group(1))
        if m.group(2).isdigit():
            wx["wind_kmh"] = round(int(m.group(2)) * 1.609344, 1)
        s = s[7:]
    elif not with_wind:
        m = re.match(r"^c(\d{3}|\.{3})s(\d{3}|\.{3})", s)
        if m:
            if m.group(1).isdigit():
                wx["wind_dir"] = int(m.group(1))
            if m.group(2).isdigit():
                wx["wind_kmh"] = round(int(m.group(2)) * 1.609344, 1)
            s = s[8:]
    while s:
        k = s[0]
        if k not in _WX_FIELDS:
            break
        name, n, f = _WX_FIELDS[k]
        m = re.match(r"^(-?[\d.]{%d}|-\d{%d})" % (n, n - 1), s[1:])
        if not m:
            if re.match(r"^[ .]{%d}" % n, s[1:]):     # field present without value
                s = s[1 + n:]
                continue
            break
        raw = m.group(1)
        s = s[1 + len(raw):]
        try:
            v = float(raw)
        except ValueError:
            continue
        if name == "temp_c":
            wx[name] = round((v - 32) * 5 / 9, 1)
        elif name == "humidity":
            wx[name] = 100 if v == 0 else int(v)
        else:
            wx[name] = round(v * f, 1)
    if wx:
        p.weather = wx
        if p.type in ("position", "other"):
            p.type = "weather" if p.type == "other" else p.type
    return s


def _mice(p: Packet, info: str) -> bool:
    """Mic-E: latitude in the destination, longitude/speed/course in the info field."""
    dst = p.dst.split("-")[0]
    if len(dst) != 6 or len(info) < 9:
        return False
    digits = []
    for ch in dst:
        if ch.isdigit():
            digits.append(int(ch))
        elif "A" <= ch <= "J":
            digits.append(ord(ch) - ord("A"))
        elif "P" <= ch <= "Y":
            digits.append(ord(ch) - ord("P"))
        elif ch in "KLZ":
            digits.append(0)
        else:
            return False
    lat = digits[0] * 10 + digits[1] + (digits[2] * 10 + digits[3] + (digits[4] * 10 + digits[5]) / 100.0) / 60.0
    north = "P" <= dst[3] <= "Z"
    lon_off = 100 if "P" <= dst[4] <= "Z" else 0
    west = "P" <= dst[5] <= "Z"
    if not north:
        lat = -lat
    d = ord(info[1]) - 28 + lon_off
    if 180 <= d <= 189:
        d -= 80
    elif 190 <= d <= 199:
        d -= 190
    m = ord(info[2]) - 28
    if m >= 60:
        m -= 60
    h = ord(info[3]) - 28
    lon = d + (m + h / 100.0) / 60.0
    if west:
        lon = -lon
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return False
    sp = ord(info[4]) - 28
    dc = ord(info[5]) - 28
    se = ord(info[6]) - 28
    speed = sp * 10 + dc // 10
    if speed >= 800:
        speed -= 800
    course = (dc % 10) * 100 + se
    if course >= 400:
        course -= 400
    p.lat, p.lon = lat, lon
    p.symbol = info[8] + info[7]
    if 0 < course <= 360:
        p.course = course % 360
    p.speed_kmh = round(speed * 1.852, 1)
    rest = info[9:]
    # altitude: "xxx}" base91 metres + 10000
    m_alt = re.search(r"(.{3})\}", rest)
    if m_alt:
        try:
            p.altitude_m = float(_b91(m_alt.group(1)) - 10000)
            rest = rest[:m_alt.start()] + rest[m_alt.end():]
        except ValueError:
            pass
    rest = re.sub(r"^[`'\x1c\x1d>\]]", "", rest)
    p.comment = rest.strip()
    p.type = "position"
    return True


def _position_body(p: Packet, s: str) -> bool:
    """Position at the start of s (uncompressed or compressed); sets fields, returns success."""
    if s[:1].isdigit() or s[:1] == " ":
        r = _uncompressed(s)
        if not r:
            return False
        p.lat, p.lon, p.symbol, rest = r
        p.comment = _comment_extras(p, rest)
        return True
    r2 = _compressed(s)
    if not r2:
        return False
    p.lat, p.lon, p.symbol, rest, extras = r2
    for k, v in extras.items():
        setattr(p, k, v)
    if p.symbol[1:] == "_":
        rest = _weather(p, rest, with_wind=False)
    p.comment = rest.strip()
    return True


def _nmea(p: Packet, body: str) -> bool:
    pos = parse_nmea(body)
    if not pos:
        return False
    p.lat, p.lon = pos["lat"], pos["lon"]
    p.speed_kmh = pos.get("speed_kmh")
    p.course = pos.get("course")
    p.altitude_m = pos.get("altitude_m")
    p.symbol = "/>"
    p.type = "position"
    return True


def parse(line: str) -> Packet:
    """One TNC2 line → Packet. Raises APRSError for anything that is not a packet."""
    line = line.rstrip("\r\n")
    if not line or line.startswith("#"):
        raise APRSError("comment line")
    if len(line) > 512:
        raise APRSError("too long")
    if ">" not in line or ":" not in line:
        raise APRSError("no header")
    header, body = line.split(":", 1)
    src, rest = header.split(">", 1)
    parts = rest.split(",")
    dst, path = parts[0], parts[1:]
    if not _SRC_RE.match(src):
        raise APRSError("invalid source callsign")
    if not re.match(r"^[A-Za-z0-9-]{1,9}$", dst):
        raise APRSError("invalid destination")
    for el in path:
        if not _PATH_ELEM_RE.match(el):
            raise APRSError("invalid path")
    if len(path) > 12:
        raise APRSError("path too long")
    if not body:
        raise APRSError("empty body")
    p = Packet(raw=line, src=src, dst=dst, path=path, body=body)
    _parse_body(p, body)
    return p


def _parse_body(p: Packet, body: str) -> None:
    dti = body[0]
    try:
        if dti in "!=":
            p.messaging = dti == "="
            if _position_body(p, body[1:]):
                p.type = "weather" if p.symbol[1:] == "_" and p.weather else "position"
            elif "!" in body[1:41]:          # some trackers put text before '!'
                i = body.index("!", 1)
                if _position_body(p, body[i + 1:]):
                    p.type = "position"
        elif dti in "/@":
            p.messaging = dti == "@"
            if len(body) >= 8 and _position_body(p, body[8:]):
                p.type = "weather" if p.symbol[1:] == "_" and p.weather else "position"
        elif dti == ";":
            if len(body) >= 18:
                p.name = body[1:10].rstrip()
                p.alive = body[10] == "*"
                if _position_body(p, body[18:]):
                    p.type = "object"
        elif dti == ")":
            m = re.match(r"^\)([^!_]{3,9})([!_])", body)
            if m:
                p.name = m.group(1).rstrip()
                p.alive = m.group(2) == "!"
                if _position_body(p, body[m.end():]):
                    p.type = "item"
        elif dti == ":":
            if len(body) >= 11 and body[10] == ":":
                p.msg_to = body[1:10].strip()
                text = body[11:]
                m = re.match(r"^(ack|rej)([A-Za-z0-9}]{1,10})\s*$", text)
                if m:
                    p.type = "ack"
                    p.msg_id = m.group(2)
                    p.msg_text = m.group(1)
                else:
                    m = re.search(r"\{([A-Za-z0-9}]{1,10})\s*$", text)
                    if m:
                        p.msg_id = m.group(1)
                        text = text[:m.start()]
                    p.msg_text = text
                    p.type = "telemetry" if p.msg_to == p.src and re.match(r"^(PARM|UNIT|EQNS|BITS)\.", text) \
                        else "message"
        elif dti == ">":
            p.type = "status"
            p.status = body[1:].strip()
        elif dti in "`'\x1c\x1d":
            _mice(p, body)
        elif dti == "_":
            p.type = "weather"
            _weather(p, body[9:], with_wind=False)
        elif dti == "T":
            p.type = "telemetry"
        elif dti == "$":
            if not _nmea(p, body):
                p.type = "other"
        elif dti == "?":
            p.type = "query"
        elif dti == "}":
            inner = parse(body[1:])
            inner.third_party = True
            for k in ("type", "lat", "lon", "symbol", "comment", "course", "speed_kmh", "altitude_m", "name",
                      "alive", "msg_to", "msg_text", "msg_id", "status", "weather"):
                setattr(p, k, getattr(inner, k))
            p.third_party = True
    except (ValueError, IndexError, APRSError):
        p.type = "other"
    if p.lat is not None and p.type == "other":
        p.type = "position"


# ----------------------------------------------------------------- building

def _lat(v: float) -> str:
    h = "N" if v >= 0 else "S"
    v = abs(v)
    d = int(v)
    m = (v - d) * 60
    if round(m, 2) >= 60:
        d, m = d + 1, 0.0
    return f"{d:02d}{m:05.2f}{h}"


def _lon(v: float) -> str:
    h = "E" if v >= 0 else "W"
    v = abs(v)
    d = int(v)
    m = (v - d) * 60
    if round(m, 2) >= 60:
        d, m = d + 1, 0.0
    return f"{d:03d}{m:05.2f}{h}"


def position_body(lat: float, lon: float, symbol: str = "/[", comment: str = "", *, course: Optional[int] = None,
                  speed_kmh: Optional[float] = None, altitude_m: Optional[float] = None,
                  messaging: bool = False) -> str:
    sym = (symbol or "/[") + "  "
    s = ("=" if messaging else "!") + _lat(lat) + sym[0] + _lon(lon) + sym[1]
    if course is not None or speed_kmh is not None:
        s += f"{int(course or 0) % 360 or 360:03d}/{min(999, int(round((speed_kmh or 0) / 1.852))):03d}"
    if altitude_m is not None:
        s += f"/A={int(round(altitude_m / 0.3048)):06d}"
    if comment:
        s += (" " if course is not None or altitude_m is not None else "") + comment
    return s


def object_body(name: str, lat: float, lon: float, symbol: str = "/r", comment: str = "",
                alive: bool = True, ts: Optional[float] = None) -> str:
    t = time.gmtime(ts or time.time())
    sym = (symbol or "/r") + "  "
    return (";" + name[:9].ljust(9) + ("*" if alive else "_") + time.strftime("%d%H%Mz", t)
            + _lat(lat) + sym[0] + _lon(lon) + sym[1] + (comment or ""))


def build(src: str, body: str, path: List[str], dst: str = TOCALL) -> Packet:
    line = f"{src}>{','.join([dst] + path)}:{body}"
    return parse(line)


def sanitize_comment(text: str, limit: int = 43) -> str:
    """Printable ASCII only, '|' and '~' removed (they have a meaning in APRS)."""
    import unicodedata
    t = unicodedata.normalize("NFKD", text or "")
    t = t.replace("ß", "ss")
    out = "".join(c for c in t if 32 <= ord(c) < 127 and c not in "|~")
    return out[:limit].strip()
