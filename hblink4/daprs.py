"""
DAPRS – GPS positions from DMR radios (DMR → APRS).

HBlink4 sees every burst a repeater or hotspot sends. Radios report their
position in two ways, both decoded here:

* Embedded GPS (ETSI TS 102 361-2, "GPS Info" LC, FLCO 0b001000): during a
  voice call the radio puts its position into the embedded signalling of the
  voice superframes instead of the normal voice LC. Bursts B–E carry four
  32-bit fragments; together they are a BPTC(128,72) block with the 9-byte LC.
* GPS data calls: a data header followed by rate-½ data blocks
  (BPTC(196,96)), sent to an APRS/data ID such as 900999 or a talkgroup.
  Understood payloads: NMEA sentences ($GPRMC / $GNRMC / $GPGGA / $GNGGA –
  Anytone, TYT/Retevis, MD-380 GPS firmware and others), also inside IP/UDP,
  and Motorola LRRP location reports (UDP port 4001).
  Rate-¾ and rate-1 data are not decoded (logged once at debug level).

Hot path: the per-burst cost is one integer comparison for frames that are
not involved; voice bursts B–E cost a 32-bit slice when DAPRS is enabled. The
BPTC decode runs once per superframe (every 360 ms of an active call).

The decoder only *finds* positions. Callsign lookup, SSID, symbol, rate limit
and gating to APRS-IS happen in the dashboard (dashboard/aprs/).
"""
from __future__ import annotations

import re
import struct
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

try:
    from .lc import _decode_bptc_96
except ImportError:  # pragma: no cover - direct execution
    from lc import _decode_bptc_96

# ----------------------------------------------------------------- constants
FLCO_GPS_INFO = 0x08

# Position error field of the GPS Info LC → metres (upper bound)
POSITION_ERROR_M = {0: 2, 1: 20, 2: 200, 3: 2000, 4: 20000, 5: 200000, 6: 999999, 7: None}

DTYPE_DATA_HEADER = 6
DTYPE_RATE_12 = 7
DTYPE_RATE_34 = 8
DTYPE_RATE_1 = 10

LRRP_PORT = 4001
TMS_PORT = 4007

# Motorola LRRP tokens that start with latitude/longitude (4 + 4 bytes)
_LRRP_POSITION_TOKENS = (0x51, 0x54, 0x55, 0x66, 0x69)

_NMEA_RE = re.compile(rb"\$(G[PNL])(RMC|GGA),([^\r\n*$]{10,90})")
_NMEA_TXT_RE = re.compile(r"\$(G[PNL])(RMC|GGA),([^\r\n*$]{10,90})")


@dataclass
class GpsFix:
    """One position found in the DMR traffic."""
    radio_id: int
    lat: float
    lon: float
    source: str                     # 'embedded' | 'nmea' | 'lrrp'
    slot: int = 0
    dst_id: int = 0
    group: bool = True
    speed_kmh: Optional[float] = None
    course: Optional[int] = None
    altitude_m: Optional[float] = None
    error_m: Optional[int] = None
    ts: float = field(default_factory=time.time)

    def as_event(self) -> Dict[str, Any]:
        d = {"radio_id": self.radio_id, "lat": round(self.lat, 6), "lon": round(self.lon, 6),
             "source": self.source, "slot": self.slot, "dst_id": self.dst_id, "group": self.group}
        for k in ("speed_kmh", "course", "altitude_m", "error_m"):
            v = getattr(self, k)
            if v is not None:
                d[k] = v
        return d


def _valid(lat: float, lon: float) -> bool:
    return -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0 and not (abs(lat) < 1e-6 and abs(lon) < 1e-6)


# ------------------------------------------------------------- embedded GPS

def emb_fragment(payload: bytes) -> int:
    """The 32-bit embedded-signalling fragment of a voice burst B–E (bits 116..147)."""
    return (int.from_bytes(payload[14:19], "big") >> 4) & 0xFFFFFFFF


def _hamming_16114_ok(row: List[int]) -> bool:
    d = row
    p = (d[0] ^ d[1] ^ d[2] ^ d[3] ^ d[5] ^ d[7] ^ d[8],
         d[1] ^ d[2] ^ d[3] ^ d[4] ^ d[6] ^ d[8] ^ d[9],
         d[2] ^ d[3] ^ d[4] ^ d[5] ^ d[7] ^ d[9] ^ d[10],
         d[0] ^ d[1] ^ d[2] ^ d[4] ^ d[6] ^ d[7] ^ d[10],
         d[0] ^ d[2] ^ d[5] ^ d[6] ^ d[8] ^ d[9] ^ d[10])
    return tuple(d[11:16]) == p


def decode_embedded_lc(fragments: Tuple[int, int, int, int]) -> Optional[bytes]:
    """
    Four 32-bit fragments (bursts B, C, D, E) → 9-byte LC, or None.

    The 128 bits are the 8×16 BPTC matrix sent column by column (bit k is row
    k % 8, column k // 8). Rows 0–6 are Hamming(16,11,4) protected, row 7 is
    column parity; LC bits sit in columns 0–10 (rows 2–6: 0–9, column 10 holds
    the 5-bit checksum). Any error rejects the block – a wrong position is
    worse than none.
    """
    word = 0
    for f in fragments:
        word = (word << 32) | (f & 0xFFFFFFFF)
    bits = [(word >> (127 - k)) & 1 for k in range(128)]
    rows = [[bits[c * 8 + r] for c in range(16)] for r in range(8)]
    for r in range(7):
        if not _hamming_16114_ok(rows[r]):
            return None
    for c in range(16):
        if (rows[0][c] ^ rows[1][c] ^ rows[2][c] ^ rows[3][c] ^ rows[4][c] ^ rows[5][c] ^ rows[6][c]) != rows[7][c]:
            return None
    data: List[int] = rows[0][0:11] + rows[1][0:11]
    for r in range(2, 7):
        data += rows[r][0:10]
    lc = int("".join(map(str, data)), 2).to_bytes(9, "big")
    csum = (rows[2][10] << 4) | (rows[3][10] << 3) | (rows[4][10] << 2) | (rows[5][10] << 1) | rows[6][10]
    if sum(lc) % 31 != csum:
        return None
    return lc


def encode_embedded_lc(lc: bytes) -> Tuple[int, int, int, int]:
    """Inverse of decode_embedded_lc (used by the tests and the demo generator)."""
    assert len(lc) == 9
    data = [int(b) for b in format(int.from_bytes(lc, "big"), "072b")]
    csum = sum(lc) % 31
    cbits = [(csum >> (4 - i)) & 1 for i in range(5)]
    rows: List[List[int]] = []
    pos = 0
    for r in range(7):
        n = 11 if r < 2 else 10
        row = data[pos:pos + n]
        pos += n
        if r >= 2:
            row = row + [cbits[r - 2]]
        d = row
        row = row + [d[0] ^ d[1] ^ d[2] ^ d[3] ^ d[5] ^ d[7] ^ d[8],
                     d[1] ^ d[2] ^ d[3] ^ d[4] ^ d[6] ^ d[8] ^ d[9],
                     d[2] ^ d[3] ^ d[4] ^ d[5] ^ d[7] ^ d[9] ^ d[10],
                     d[0] ^ d[1] ^ d[2] ^ d[4] ^ d[6] ^ d[7] ^ d[10],
                     d[0] ^ d[2] ^ d[5] ^ d[6] ^ d[8] ^ d[9] ^ d[10]]
        rows.append(row)
    rows.append([rows[0][c] ^ rows[1][c] ^ rows[2][c] ^ rows[3][c] ^ rows[4][c] ^ rows[5][c] ^ rows[6][c]
                 for c in range(16)])
    word = 0
    for k in range(128):
        word = (word << 1) | rows[k % 8][k // 8]
    return tuple((word >> (96 - 32 * i)) & 0xFFFFFFFF for i in range(4))  # type: ignore[return-value]


def parse_gps_info_lc(lc: bytes) -> Optional[Tuple[float, float, Optional[int]]]:
    """GPS Info LC → (lat, lon, error_m) or None. Layout after FLCO/FID:
    4 bits reserved, 3 bits position error, 25 bits longitude, 24 bits latitude
    (two's complement; 360/2^25 and 180/2^24 degrees per step)."""
    if len(lc) < 9 or (lc[0] & 0x3F) != FLCO_GPS_INFO:
        return None
    v = int.from_bytes(lc[2:9], "big")          # 56 bits
    err = (v >> 49) & 0x7
    lon_raw = (v >> 24) & 0x1FFFFFF
    lat_raw = v & 0xFFFFFF
    if lon_raw & 0x1000000:
        lon_raw -= 1 << 25
    if lat_raw & 0x800000:
        lat_raw -= 1 << 24
    lon = lon_raw * 360.0 / (1 << 25)
    lat = lat_raw * 180.0 / (1 << 24)
    if not _valid(lat, lon):
        return None
    return lat, lon, POSITION_ERROR_M.get(err)


def build_gps_info_lc(lat: float, lon: float, error: int = 1) -> bytes:
    """GPS Info LC for a position (tests / demo)."""
    lon_raw = int(round(lon * (1 << 25) / 360.0)) & 0x1FFFFFF
    lat_raw = int(round(lat * (1 << 24) / 180.0)) & 0xFFFFFF
    v = ((error & 7) << 49) | (lon_raw << 24) | lat_raw
    return bytes([FLCO_GPS_INFO, 0x00]) + v.to_bytes(7, "big")


# ------------------------------------------------------------------- NMEA

def _nmea_coord(value: str, hemi: str, deg_digits: int) -> Optional[float]:
    try:
        deg = int(value[:deg_digits])
        minutes = float(value[deg_digits:])
    except (ValueError, IndexError):
        return None
    if minutes >= 60:
        return None
    out = deg + minutes / 60.0
    return -out if hemi in ("S", "W") else out


def parse_nmea(text: str) -> Optional[Dict[str, Any]]:
    """First usable RMC/GGA sentence in text → {lat, lon, speed_kmh?, course?, altitude_m?}."""
    for m in _NMEA_TXT_RE.finditer(text):
        kind, rest = m.group(2), m.group(3).split(",")
        try:
            if kind == "RMC":
                # time, status, lat, N/S, lon, E/W, speed(kn), course, date …
                if len(rest) < 6 or rest[1] != "A":
                    continue
                lat = _nmea_coord(rest[2], rest[3], 2)
                lon = _nmea_coord(rest[4], rest[5], 3)
                if lat is None or lon is None or not _valid(lat, lon):
                    continue
                out: Dict[str, Any] = {"lat": lat, "lon": lon}
                if len(rest) > 6 and rest[6]:
                    out["speed_kmh"] = round(float(rest[6]) * 1.852, 1)
                if len(rest) > 7 and rest[7]:
                    out["course"] = int(float(rest[7])) % 360
                return out
            # GGA: time, lat, N/S, lon, E/W, quality, sats, hdop, alt, M …
            if len(rest) < 6 or rest[5] in ("", "0"):
                continue
            lat = _nmea_coord(rest[1], rest[2], 2)
            lon = _nmea_coord(rest[3], rest[4], 3)
            if lat is None or lon is None or not _valid(lat, lon):
                continue
            out = {"lat": lat, "lon": lon}
            if len(rest) > 8 and rest[8]:
                out["altitude_m"] = round(float(rest[8]), 1)
            return out
        except ValueError:
            continue
    return None


def _texts(data: bytes) -> List[str]:
    """The ways a payload can carry text: 8-bit, UTF-16 LE/BE (DMR SMS / TMS)."""
    out = [data.decode("latin-1")]
    if len(data) >= 4:
        for enc, start in (("utf-16-le", 0), ("utf-16-le", 1), ("utf-16-be", 0), ("utf-16-be", 1)):
            chunk = data[start:]
            chunk = chunk[:len(chunk) - len(chunk) % 2]
            try:
                out.append(chunk.decode(enc, errors="ignore"))
            except Exception:
                pass
    return out


# ------------------------------------------------------------------- LRRP

def parse_lrrp(data: bytes) -> Optional[Dict[str, Any]]:
    """
    Motorola LRRP location report (binary token format, UDP port 4001).
    Looks for the first point/circle token and reads 32-bit latitude
    (90/2^31 degrees per step) and longitude (180/2^31); speed (token 0x6C,
    ufloatvar km/h) and direction (0x56, 2° steps) when present.
    """
    if len(data) < 12:
        return None
    for i in range(2, len(data) - 8):
        if data[i] not in _LRRP_POSITION_TOKENS:
            continue
        lat_raw, lon_raw = struct.unpack(">ii", data[i + 1:i + 9])
        lat = lat_raw * 90.0 / 2147483648.0
        lon = lon_raw * 180.0 / 2147483648.0
        if not _valid(lat, lon):
            continue
        out: Dict[str, Any] = {"lat": lat, "lon": lon}
        tail = data[i + 9:]
        j = tail.find(b"\x56")
        if 0 <= j < len(tail) - 1:
            out["course"] = (tail[j + 1] * 2) % 360
        return out
    return None


# ------------------------------------------------------------ data blocks

def parse_data_header(raw: bytes) -> Optional[Dict[str, int]]:
    """12-byte data header (after BPTC) → dpf, sap, blocks, pad, dst, src, group."""
    if raw is None or len(raw) < 10:
        return None
    dpf = raw[0] & 0x0F
    h = {"dpf": dpf, "sap": raw[1] >> 4, "group": (raw[0] >> 7) & 1,
         "dst": int.from_bytes(raw[2:5], "big"), "src": int.from_bytes(raw[5:8], "big")}
    if dpf in (2, 3):                      # unconfirmed / confirmed packet data
        h["blocks"] = raw[8] & 0x7F
        h["pad"] = (((raw[0] >> 4) & 1) << 4) | (raw[1] & 0x0F)
    elif dpf == 0:                         # UDT: appended blocks + 1
        h["blocks"] = (raw[8] & 0x03) + 1
        h["pad"] = 0
        h["udt_format"] = raw[1] & 0x0F
    elif dpf in (0xD, 0xE):                # short data defined / raw: appended blocks
        h["blocks"] = (((raw[0] >> 4) & 3) << 4) | (raw[1] & 0x0F)
        h["pad"] = 0
    else:
        return None
    if not 1 <= h["blocks"] <= 64:
        return None
    return h


def user_data(header: Dict[str, int], blocks: List[bytes]) -> bytes:
    """Strip block serials, CRC and padding from rate-½ blocks."""
    dpf = header["dpf"]
    if dpf == 3:   # confirmed: every block starts with 7-bit serial + CRC-9
        data = b"".join(b[2:] for b in blocks)
    else:
        data = b"".join(blocks)
    if dpf in (2, 3) and len(data) >= 4:
        data = data[:-4]                        # CRC-32 of the message
        pad = header.get("pad", 0)
        if 0 < pad <= len(data):
            data = data[:-pad]
    elif dpf == 0 and len(data) >= 2:
        data = data[:-2]                        # CRC-16 of UDT
    return data


def find_position(header: Dict[str, int], data: bytes) -> Optional[Tuple[str, Dict[str, Any]]]:
    """('nmea'|'lrrp', fields) for the user data of a data call, or None."""
    payload = data
    port = None
    if header.get("sap") == 4 and len(data) >= 28 and data[0] >> 4 == 4:
        ihl = (data[0] & 0x0F) * 4
        if data[9] == 17 and len(data) >= ihl + 8:     # UDP
            port = int.from_bytes(data[ihl + 2:ihl + 4], "big")
            payload = data[ihl + 8:]
    if port == LRRP_PORT:
        pos = parse_lrrp(payload)
        if pos:
            return "lrrp", pos
    for text in _texts(payload):
        pos = parse_nmea(text)
        if pos:
            return "nmea", pos
    if port is None and header.get("sap") in (2, 3, 9):
        # compressed UDP/IP or proprietary transport: LRRP without visible port
        pos = parse_lrrp(payload)
        if pos:
            return "lrrp", pos
    return None


# --------------------------------------------------------------- decoder

class DaprsDecoder:
    """
    Per-slot state for embedded GPS and data calls. One instance lives in
    HBProtocol; feed() is called for every DMRD burst of a connected repeater.
    """

    DATA_TIMEOUT = 4.0      # seconds between header and last block
    REPEAT_S = 10.0         # same radio, same source: at most one fix per 10 s

    def __init__(self, cfg: Optional[Dict[str, Any]] = None):
        self._emb: Dict[Tuple[bytes, int], List[int]] = {}
        self._data: Dict[Tuple[bytes, int], Dict[str, Any]] = {}
        self._last: Dict[Tuple[int, str], float] = {}
        self.stats = {"embedded": 0, "nmea": 0, "lrrp": 0, "data_calls": 0, "undecoded": 0}
        self.configure(cfg or {})

    def configure(self, cfg: Dict[str, Any]) -> None:
        cfg = cfg or {}
        self.enabled = bool(cfg.get("enabled", False))
        self.embedded = bool(cfg.get("embedded_gps", True))
        self.data = bool(cfg.get("data_gps", True))
        ids = cfg.get("data_ids", None)
        self.data_ids = None if ids is None else {int(x) for x in ids}
        if not self.enabled:
            self._emb.clear()
            self._data.clear()

    # -- the per-burst entry point -------------------------------------
    def feed(self, repeater_id: bytes, slot: int, rf_src: bytes, dst_id: bytes, frame_type: int,
             dtype_vseq: int, payload: bytes, call_type_bit: int = 0) -> Optional[GpsFix]:
        if frame_type == 0:
            if self.embedded and 1 <= dtype_vseq <= 4 and len(payload) >= 19:
                return self._feed_embedded(repeater_id, slot, rf_src, dst_id, dtype_vseq, payload, call_type_bit)
            return None
        if frame_type == 2 and self.data and dtype_vseq in (DTYPE_DATA_HEADER, DTYPE_RATE_12, DTYPE_RATE_34,
                                                             DTYPE_RATE_1):
            return self._feed_data(repeater_id, slot, rf_src, dst_id, dtype_vseq, payload, call_type_bit)
        return None

    # -- embedded ------------------------------------------------------
    def _feed_embedded(self, rid, slot, rf_src, dst_id, vseq, payload, ct) -> Optional[GpsFix]:
        key = (rid, slot)
        frag = emb_fragment(payload)
        if vseq == 1:
            self._emb[key] = [frag]
            return None
        buf = self._emb.get(key)
        if buf is None or len(buf) != vseq - 1:
            self._emb.pop(key, None)
            return None
        buf.append(frag)
        if vseq < 4:
            return None
        del self._emb[key]
        # cheap pre-check: FLCO lives in the first 8 bits of row 0 (bits 0,8,…,56 of fragment B/C)
        word = (buf[0] << 32) | buf[1]
        flco_bits = [(word >> (63 - c * 8)) & 1 for c in range(2, 8)]
        if int("".join(map(str, flco_bits)), 2) != FLCO_GPS_INFO:
            return None
        lc = decode_embedded_lc(tuple(buf))          # type: ignore[arg-type]
        pos = parse_gps_info_lc(lc) if lc else None
        if not pos:
            return None
        radio = int.from_bytes(rf_src, "big")
        if not self._fresh(radio, "embedded"):
            return None
        self.stats["embedded"] += 1
        return GpsFix(radio, pos[0], pos[1], "embedded", slot=slot, dst_id=int.from_bytes(dst_id, "big"),
                      group=(ct == 0), error_m=pos[2])

    # -- data calls ----------------------------------------------------
    def _feed_data(self, rid, slot, rf_src, dst_id, dtype, payload, ct) -> Optional[GpsFix]:
        key = (rid, slot)
        now = time.time()
        if dtype == DTYPE_DATA_HEADER:
            dst = int.from_bytes(dst_id, "big")
            if self.data_ids is not None and dst not in self.data_ids:
                self._data.pop(key, None)
                return None
            hdr = parse_data_header(_decode_bptc_96(payload))
            if not hdr:
                self._data.pop(key, None)
                return None
            self.stats["data_calls"] += 1
            self._data[key] = {"hdr": hdr, "blocks": [], "t": now, "src": rf_src, "dst": dst_id, "ct": ct,
                               "bad": False}
            return None
        st = self._data.get(key)
        if st is None:
            return None
        if now - st["t"] > self.DATA_TIMEOUT:
            del self._data[key]
            return None
        if dtype != DTYPE_RATE_12:
            st["bad"] = True                 # rate ¾ / rate 1: not decoded
        else:
            blk = _decode_bptc_96(payload)
            if blk is None:
                st["bad"] = True
            else:
                st["blocks"].append(blk)
        if len(st["blocks"]) + (1 if st["bad"] else 0) < st["hdr"]["blocks"] and not st["bad"]:
            return None
        del self._data[key]
        if st["bad"]:
            self.stats["undecoded"] += 1
            return None
        found = find_position(st["hdr"], user_data(st["hdr"], st["blocks"]))
        if not found:
            self.stats["undecoded"] += 1
            return None
        source, pos = found
        radio = int.from_bytes(st["src"], "big")
        if not self._fresh(radio, source):
            return None
        self.stats[source] += 1
        return GpsFix(radio, pos["lat"], pos["lon"], source, slot=slot, dst_id=int.from_bytes(st["dst"], "big"),
                      group=(st["ct"] == 0), speed_kmh=pos.get("speed_kmh"), course=pos.get("course"),
                      altitude_m=pos.get("altitude_m"))

    def _fresh(self, radio: int, source: str) -> bool:
        now = time.time()
        k = (radio, source)
        if now - self._last.get(k, 0.0) < self.REPEAT_S:
            return False
        self._last[k] = now
        if len(self._last) > 5000:
            cutoff = now - 3600
            self._last = {kk: v for kk, v in self._last.items() if v > cutoff}
        return True


# ------------------------------------------------- helpers for tests / demo

def _crc_ccitt_dmr(data: bytes, mask: int) -> int:
    crc = 0
    for b in data:
        for i in range(8):
            bit = ((b >> (7 - i)) & 1) ^ ((crc >> 15) & 1)
            crc = (crc << 1) & 0xFFFF
            if bit:
                crc ^= 0x1021
    return (crc ^ 0xFFFF) ^ mask


def build_data_burst(raw12: bytes, dtype: int, color_code: int = 1) -> bytes:
    """A 33-byte data burst (BPTC(196,96)) carrying 12 bytes – for tests and the demo generator."""
    from bitarray import bitarray
    from dmr_utils3 import bptc
    code = bptc.interleave_19696(bptc.encode_19696(bytes(raw12)))
    slot_type = bitarray(endian="big")
    slot_type.frombytes(bytes([((color_code & 0xF) << 4) | (dtype & 0xF)]))
    # slot type is Golay(20,8); 12 parity bits – zeros are fine for our decoder (it ignores them)
    st20 = slot_type + bitarray("0" * 12)
    sync = bitarray(endian="big")
    sync.frombytes(bytes.fromhex("DFF57D75DF5D"))
    burst = code[0:98] + st20[0:10] + sync + st20[10:20] + code[98:196]
    return burst.tobytes()


def build_data_call(user: bytes, *, dst: int, src: int, sap: int = 0, group: bool = False) -> List[bytes]:
    """Unconfirmed rate-½ data call (header + blocks) as raw 12-byte units, CRCs included."""
    import zlib
    total = len(user) + 4
    nblocks = (total + 11) // 12
    pad = nblocks * 12 - total
    body = user + b"\x00" * pad
    crc32 = zlib.crc32(body) & 0xFFFFFFFF
    body += crc32.to_bytes(4, "little")
    hdr = bytearray(10)
    hdr[0] = (0x80 if group else 0) | (((pad >> 4) & 1) << 4) | 0x02
    hdr[1] = ((sap & 0xF) << 4) | (pad & 0x0F)
    hdr[2:5] = dst.to_bytes(3, "big")
    hdr[5:8] = src.to_bytes(3, "big")
    hdr[8] = 0x80 | nblocks
    hdr[9] = 0
    crc = _crc_ccitt_dmr(bytes(hdr), 0xCCCC)
    out = [bytes(hdr) + crc.to_bytes(2, "big")]
    out += [body[i:i + 12] for i in range(0, len(body), 12)]
    return out
