"""
Station database of the APRS server (SQLite, dashboard/data/aprs.db).

* stations  – last state of every station, object and item (one row each)
* positions – track points (only when a station moved or a minute passed)
* packets   – raw packet log for the map and the admin area

The map reads from memory (dict of stations, ring buffer of packets); SQLite
is written in batches from a worker thread so the event loop never waits for
the disk. Old rows are removed by purge() once an hour.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

from .filters import distance_km
from .packet import Packet

_SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS stations (
    call      TEXT PRIMARY KEY,
    data      TEXT NOT NULL,
    ts        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS stations_ts ON stations(ts);
CREATE TABLE IF NOT EXISTS positions (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    call   TEXT NOT NULL,
    ts     REAL NOT NULL,
    lat    REAL NOT NULL,
    lon    REAL NOT NULL,
    speed  REAL,
    course INTEGER
);
CREATE INDEX IF NOT EXISTS positions_call_ts ON positions(call, ts);
CREATE INDEX IF NOT EXISTS positions_ts ON positions(ts);
CREATE TABLE IF NOT EXISTS packets (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     REAL NOT NULL,
    call   TEXT NOT NULL,
    src    TEXT NOT NULL,
    type   TEXT NOT NULL,
    origin TEXT NOT NULL,
    raw    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS packets_ts ON packets(ts);
CREATE INDEX IF NOT EXISTS packets_call_ts ON packets(call, ts);
"""

# origin of a packet → "source" shown on the map
SOURCE_DMR = "dmr"            # DAPRS: position from a DMR radio
SOURCE_DMR_OBJECT = "rpt"     # repeater/hotspot object from the DMR login data
SOURCE_LOCAL = "local"        # sent by a client of this APRS server
SOURCE_APRSIS = "aprsis"      # received from the APRS-IS uplink

TRACK_MIN_M = 25              # new track point when moved more than this …
TRACK_MIN_S = 120             # … or this much time passed while moving a little


class AprsStore:
    RECENT_PACKETS = 1500

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(_SCHEMA)
        self.stations: Dict[str, Dict[str, Any]] = {}
        self.recent: Deque[Dict[str, Any]] = deque(maxlen=self.RECENT_PACKETS)
        self._dirty: Dict[str, Dict[str, Any]] = {}
        self._pos_q: List[Tuple] = []
        self._pkt_q: List[Tuple] = []
        self._load()

    # ------------------------------------------------------------- loading
    def _load(self, max_age_s: float = 7 * 86400) -> None:
        cutoff = time.time() - max_age_s
        with self._lock:
            rows = self._db.execute("SELECT call, data FROM stations WHERE ts >= ?", (cutoff,)).fetchall()
            pk = self._db.execute("SELECT ts, call, src, type, origin, raw FROM packets ORDER BY id DESC LIMIT ?",
                                  (self.RECENT_PACKETS,)).fetchall()
        for r in rows:
            try:
                self.stations[r["call"]] = json.loads(r["data"])
            except ValueError:
                pass
        for r in reversed(pk):
            self.recent.append({"ts": r["ts"], "call": r["call"], "src": r["src"], "type": r["type"],
                                "origin": r["origin"], "raw": r["raw"]})

    # ------------------------------------------------------------- writing
    def add(self, p: Packet, origin: str, extra: Optional[Dict[str, Any]] = None,
            log_packet: bool = True) -> Optional[Dict[str, Any]]:
        """Record a packet. Returns the updated station (for the map) or None."""
        now = time.time()
        call = p.station
        if log_packet:
            self.recent.append({"ts": now, "call": call, "src": p.src, "type": p.type, "origin": origin, "raw": p.raw})
            self._pkt_q.append((now, call, p.src, p.type, origin, p.raw))
        if p.type in ("message", "ack", "query", "telemetry") and not p.has_position():
            st = self.stations.get(p.src)
            if st:
                st["ts"] = now
                st["packets"] = st.get("packets", 0) + 1
                self._dirty[p.src] = st
            return None
        if not p.has_position() and p.type not in ("status", "weather"):
            return None
        st = self.stations.get(call)
        if st is None:
            if not p.has_position():
                return None              # status/weather before we know where the station is
            st = {"call": call, "first": now, "packets": 0}
            self.stations[call] = st
        st["ts"] = now
        st["packets"] = st.get("packets", 0) + 1
        st["source"] = _source(origin)
        st["origin"] = origin
        st["path"] = ",".join(p.path)
        if p.type == "status":
            st["status"] = p.status[:120]
        if p.weather:
            st["wx"] = p.weather
            st["wx_ts"] = now
        if p.has_position():
            moved = ("lat" not in st or distance_km(st["lat"], st["lon"], p.lat, p.lon) * 1000 > TRACK_MIN_M)
            if moved or now - st.get("track_ts", 0) > TRACK_MIN_S and (p.speed_kmh or 0) > 2:
                self._pos_q.append((call, now, p.lat, p.lon, p.speed_kmh, p.course))
                st["track_ts"] = now
            st.update({"lat": round(p.lat, 6), "lon": round(p.lon, 6), "pos_ts": now,
                       "symbol": p.symbol or "//", "comment": p.comment[:120]})
            for k, v in (("course", p.course), ("speed", p.speed_kmh), ("alt", p.altitude_m)):
                if v is None:
                    st.pop(k, None)
                else:
                    st[k] = v
        st["kind"] = ("object" if p.type == "object" else "item" if p.type == "item"
                      else "weather" if p.weather and p.symbol[1:] == "_" else "station")
        if p.type in ("object", "item"):
            st["owner"] = p.src
            st["alive"] = p.alive
        if extra:
            st.update(extra)
        self._dirty[call] = st
        return st

    def note(self, call: str, **fields) -> None:
        st = self.stations.get(call)
        if st:
            st.update(fields)
            self._dirty[call] = st

    def flush(self) -> int:
        """Write pending rows (run in a worker thread). Returns the number of rows."""
        dirty, self._dirty = self._dirty, {}
        pos, self._pos_q = self._pos_q, []
        pkt, self._pkt_q = self._pkt_q, []
        if not (dirty or pos or pkt):
            return 0
        rows = [(c, json.dumps(s, separators=(",", ":")), s.get("ts", time.time())) for c, s in list(dirty.items())]
        with self._lock:
            self._db.execute("BEGIN")
            try:
                self._db.executemany("INSERT OR REPLACE INTO stations (call, data, ts) VALUES (?,?,?)", rows)
                self._db.executemany("INSERT INTO positions (call, ts, lat, lon, speed, course) VALUES (?,?,?,?,?,?)",
                                     pos)
                self._db.executemany("INSERT INTO packets (ts, call, src, type, origin, raw) VALUES (?,?,?,?,?,?)", pkt)
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
        return len(rows) + len(pos) + len(pkt)

    def purge(self, station_days: float, track_days: float, packet_hours: float) -> None:
        now = time.time()
        cut_st = now - station_days * 86400
        for c in [c for c, s in self.stations.items() if s.get("ts", 0) < cut_st]:
            del self.stations[c]
        with self._lock:
            self._db.execute("DELETE FROM stations WHERE ts < ?", (cut_st,))
            self._db.execute("DELETE FROM positions WHERE ts < ?", (now - track_days * 86400,))
            self._db.execute("DELETE FROM packets WHERE ts < ?", (now - packet_hours * 3600,))

    # ------------------------------------------------------------- reading
    def position_of(self, call: str) -> Optional[Tuple[float, float]]:
        st = self.stations.get(call.upper())
        if st and "lat" in st:
            return st["lat"], st["lon"]
        return None

    def list(self, max_age_s: float, sources: Optional[set] = None) -> List[Dict[str, Any]]:
        cutoff = time.time() - max_age_s
        out = []
        for s in self.stations.values():
            if s.get("ts", 0) < cutoff or "lat" not in s:
                continue
            if sources is not None and s.get("source") not in sources:
                continue
            out.append(public(s))
        return out

    def track(self, call: str, hours: float) -> List[List[float]]:
        with self._lock:
            rows = self._db.execute("SELECT ts, lat, lon FROM positions WHERE call = ? AND ts >= ? ORDER BY ts",
                                    (call, time.time() - hours * 3600)).fetchall()
        pts = [[round(r["ts"]), r["lat"], r["lon"]] for r in rows]
        # points still waiting for the writer
        pts += [[round(t[1]), t[2], t[3]] for t in self._pos_q if t[0] == call]
        return pts

    def packets_of(self, call: str, limit: int = 30) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT ts, type, origin, raw FROM packets WHERE call = ? ORDER BY id DESC LIMIT ?",
                                    (call, limit)).fetchall()
        out = [dict(r) for r in rows]
        pending = [{"ts": t[0], "type": t[3], "origin": t[4], "raw": t[5]} for t in reversed(self._pkt_q) if t[1] == call]
        return (pending + out)[:limit]

    def counts(self) -> Dict[str, int]:
        now = time.time()
        by: Dict[str, int] = {}
        active = 0
        for s in self.stations.values():
            if now - s.get("ts", 0) < 3600:
                active += 1
                by[s.get("source", "?")] = by.get(s.get("source", "?"), 0) + 1
        return {"stations": len(self.stations), "active_1h": active, **{f"active_{k}": v for k, v in by.items()}}

    def close(self) -> None:
        try:
            self.flush()
        finally:
            with self._lock:
                self._db.close()


def _source(origin: str) -> str:
    return {"daprs": SOURCE_DMR, "server": SOURCE_DMR_OBJECT, "uplink": SOURCE_APRSIS}.get(origin, SOURCE_LOCAL)


_PUBLIC_KEYS = ("call", "lat", "lon", "symbol", "comment", "course", "speed", "alt", "ts", "pos_ts", "source", "kind",
                "owner", "alive", "status", "wx", "dmr_id", "dmr_name", "tg", "via", "gps", "rpt_type", "freq", "packets",
                "first", "path")


def public(s: Dict[str, Any]) -> Dict[str, Any]:
    return {k: s[k] for k in _PUBLIC_KEYS if k in s}
