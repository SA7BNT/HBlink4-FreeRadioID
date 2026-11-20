"""
Talkgroup statistics: every finished group call is stored in SQLite and
summarised per talkgroup and per hour/day for the dashboard charts.

One row per received call (RX streams only; the copies HBlink4 forwards to other
repeaters are not counted again). Private calls are not talkgroup traffic and
are skipped. Rows older than the retention period are deleted once a day.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tg_calls (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL    NOT NULL,     -- call end (unix time)
    tg        INTEGER NOT NULL,
    slot      INTEGER,
    src_id    INTEGER,
    duration  REAL    NOT NULL DEFAULT 0,
    is_data   INTEGER NOT NULL DEFAULT 0,
    source    TEXT
);
CREATE INDEX IF NOT EXISTS tg_calls_ts ON tg_calls(ts);
"""

RANGES = ("today", "24h", "7d", "30d")
TOP_SERIES = 5          # talkgroups with their own colour in the time chart; the rest is "other"
MAX_TABLE = 100
ORIGINS = ("repeater", "openbridge", "outbound")


def origin_of(origin: str, source: str) -> str:
    """Where a call came in. Older rows have no origin: a numeric source is a local repeater/hotspot."""
    if origin in ORIGINS:
        return origin
    return "repeater" if (source or "").isdigit() or not source else "outbound"


def parse_names(text: str) -> Dict[int, str]:
    """'262 = Deutschland' per line (also '262: …' or '262 …') -> {262: 'Deutschland'}."""
    names: Dict[int, str] = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        for sep in ("=", ":", ";", "\t", " "):
            if sep in line:
                k, v = line.split(sep, 1)
                break
        else:
            continue
        k, v = k.strip(), v.strip().lstrip("=:;").strip()
        if k.isdigit() and v:
            names[int(k)] = v
    return names


class TGStats:
    def __init__(self, auth_store):
        """Shares the dashboard database with AuthStore."""
        self._a = auth_store
        with self._a._lock:
            self._a._db.executescript(_SCHEMA)
            cols = {r[1] for r in self._a._db.execute("PRAGMA table_info(tg_calls)")}
            if "origin" not in cols:   # where the call came in: repeater | openbridge | outbound
                self._a._db.execute("ALTER TABLE tg_calls ADD COLUMN origin TEXT NOT NULL DEFAULT ''")
                self._a._db.commit()

    # ------------------------------------------------------------ writing
    def record(self, tg: int, duration: float, *, slot: Optional[int] = None, src_id: Optional[int] = None,
               is_data: bool = False, source: str = "", ts: Optional[float] = None, origin: str = "") -> None:
        if not tg:
            return
        self._a._x("INSERT INTO tg_calls (ts, tg, slot, src_id, duration, is_data, source, origin) VALUES (?,?,?,?,?,?,?,?)",
                   (float(ts or time.time()), int(tg), slot, src_id, max(0.0, float(duration or 0)),
                    1 if is_data else 0, source or "", origin or ""))

    def tag_origin(self, source: str, origin: str) -> None:
        """Older rows were stored without origin: mark those of a known trunk/link once it reports in."""
        if source and origin in ORIGINS:
            self._a._x("UPDATE tg_calls SET origin = ? WHERE origin = '' AND source = ?", (origin, source))

    def purge(self, keep_days: int) -> int:
        cur = self._a._x("DELETE FROM tg_calls WHERE ts < ?", (time.time() - max(1, int(keep_days)) * 86400,))
        return cur.rowcount or 0

    def count(self) -> int:
        return int(self._a._q("SELECT COUNT(*) AS n FROM tg_calls")[0]["n"])

    # ------------------------------------------------------------ reading
    @staticmethod
    def buckets(rng: str, now: Optional[float] = None):
        """(start, list of bucket start times, bucket kind) in server local time."""
        now_dt = datetime.fromtimestamp(now if now is not None else time.time())
        if rng == "today":
            start = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
            return start, [start + timedelta(hours=h) for h in range(24)], "hour"
        if rng == "24h":
            last = now_dt.replace(minute=0, second=0, microsecond=0)
            starts = [last - timedelta(hours=h) for h in range(23, -1, -1)]
            return starts[0], starts, "hour"
        days = 7 if rng == "7d" else 30
        today = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
        starts = [today - timedelta(days=d) for d in range(days - 1, -1, -1)]
        return starts[0], starts, "day"

    def summary(self, rng: str = "today", names: Optional[Dict[int, str]] = None,
                callsign=lambda _id: "", now: Optional[float] = None) -> Dict[str, Any]:
        if rng not in RANGES:
            rng = "today"
        names = names or {}
        start, starts, kind = self.buckets(rng, now)
        t0 = start.timestamp()
        rows = self._a._q("SELECT ts, tg, src_id, duration, source, origin FROM tg_calls WHERE ts >= ? AND is_data = 0 ORDER BY ts",
                          (t0,))
        edges = [s.timestamp() for s in starts]

        def bucket_of(ts: float) -> int:
            lo, hi = 0, len(edges) - 1
            while lo < hi:                      # last edge <= ts
                mid = (lo + hi + 1) // 2
                if edges[mid] <= ts:
                    lo = mid
                else:
                    hi = mid - 1
            return lo

        per_tg: Dict[int, Dict[str, Any]] = {}
        per_bucket: Dict[int, List[List[float]]] = {}   # tg -> [[calls, dur] per bucket]
        all_users = set()
        origins = {o: {"calls": 0, "duration": 0.0} for o in ORIGINS}
        n = len(starts)
        for r in rows:
            tg, dur, src, ts = int(r["tg"]), float(r["duration"] or 0), r["src_id"], float(r["ts"])
            t = per_tg.setdefault(tg, {"tg": tg, "calls": 0, "duration": 0.0, "users": set(), "last": 0.0,
                                       "talkers": {}, "obp": 0.0})
            org = origin_of(r["origin"], r["source"])
            origins[org]["calls"] += 1
            origins[org]["duration"] += dur
            if org == "openbridge":
                t["obp"] += dur
            t["calls"] += 1
            t["duration"] += dur
            t["last"] = max(t["last"], ts)
            if src:
                t["users"].add(src)
                all_users.add(src)
                t["talkers"][src] = t["talkers"].get(src, 0.0) + dur
            b = per_bucket.setdefault(tg, [[0, 0.0] for _ in range(n)])[bucket_of(ts)]
            b[0] += 1
            b[1] += dur

        total_dur = sum(t["duration"] for t in per_tg.values()) or 0.0
        tgs = sorted(per_tg.values(), key=lambda t: (-t["duration"], -t["calls"], t["tg"]))
        table = []
        for t in tgs[:MAX_TABLE]:
            top_id, top_dur = (max(t["talkers"].items(), key=lambda kv: kv[1]) if t["talkers"] else (None, 0))
            table.append({
                "tg": t["tg"], "name": names.get(t["tg"], ""),
                "calls": t["calls"], "duration": round(t["duration"], 1), "users": len(t["users"]),
                "last": t["last"], "share": round(t["duration"] / total_dur, 4) if total_dur else 0,
                "obp_share": round(t["obp"] / t["duration"], 3) if t["duration"] else 0,
                "top_talker": ({"id": top_id, "callsign": callsign(top_id) or "", "duration": round(top_dur, 1)}
                               if top_id else None),
            })

        series = []
        for t in tgs[:TOP_SERIES]:
            pb = per_bucket[t["tg"]]
            series.append({"tg": t["tg"], "name": names.get(t["tg"], ""),
                           "calls": [int(b[0]) for b in pb], "duration": [round(b[1], 1) for b in pb]})
        if len(tgs) > TOP_SERIES:
            other_c, other_d = [0] * n, [0.0] * n
            for t in tgs[TOP_SERIES:]:
                for i, b in enumerate(per_bucket[t["tg"]]):
                    other_c[i] += int(b[0])
                    other_d[i] += b[1]
            series.append({"tg": None, "name": f"{len(tgs) - TOP_SERIES} other",
                           "calls": other_c, "duration": [round(d, 1) for d in other_d]})

        return {
            "range": rng, "bucket": kind,
            "buckets": [s.timestamp() for s in starts],
            "totals": {"calls": sum(t["calls"] for t in tgs), "duration": round(total_dur, 1),
                       "talkgroups": len(tgs), "users": len(all_users)},
            "talkgroups": table,
            "series": series,
            "origins": {o: {"calls": v["calls"], "duration": round(v["duration"], 1)} for o, v in origins.items()},
        }
