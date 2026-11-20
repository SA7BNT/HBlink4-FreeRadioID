"""
OpenBridge statistics: every finished stream on an OBP trunk is stored in SQLite,
in both directions:

* ``rx`` – received from the far end (the call comes in over the trunk)
* ``tx`` – sent to the far end (a local call goes out over the trunk)

The summary feeds the "OpenBridge" tab: totals per direction, a time series
(RX above / TX below the baseline), the busiest talkgroups and the latest calls.
Rows older than the talkgroup-statistics retention are deleted once a day.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from .tgstats import TGStats, RANGES

_SCHEMA = """
CREATE TABLE IF NOT EXISTS obp_calls (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL    NOT NULL,     -- stream end (unix time)
    trunk     TEXT    NOT NULL,
    dir       TEXT    NOT NULL,     -- 'rx' | 'tx'
    tg        INTEGER NOT NULL,
    slot      INTEGER,
    src_id    INTEGER,
    duration  REAL    NOT NULL DEFAULT 0,
    is_data   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS obp_calls_ts ON obp_calls(ts);
CREATE INDEX IF NOT EXISTS obp_calls_trunk ON obp_calls(trunk, ts);
"""

TOP_TGS = 10
RECENT = 25


class OBPStats:
    def __init__(self, auth_store):
        """Shares the dashboard database with AuthStore."""
        self._a = auth_store
        with self._a._lock:
            self._a._db.executescript(_SCHEMA)

    # ------------------------------------------------------------ writing
    def record(self, trunk: str, direction: str, tg: int, duration: float, *, slot: Optional[int] = None,
               src_id: Optional[int] = None, is_data: bool = False, ts: Optional[float] = None) -> None:
        if not trunk or not tg or direction not in ("rx", "tx"):
            return
        self._a._x("INSERT INTO obp_calls (ts, trunk, dir, tg, slot, src_id, duration, is_data) "
                   "VALUES (?,?,?,?,?,?,?,?)",
                   (float(ts or time.time()), trunk, direction, int(tg), slot, src_id,
                    max(0.0, float(duration or 0)), 1 if is_data else 0))

    def purge(self, keep_days: int) -> int:
        cur = self._a._x("DELETE FROM obp_calls WHERE ts < ?", (time.time() - max(1, int(keep_days)) * 86400,))
        return cur.rowcount or 0

    def today(self, now: Optional[float] = None) -> Dict[str, Dict[str, Any]]:
        """Calls and airtime per trunk and direction since midnight (for the live cards)."""
        start, _, _ = TGStats.buckets("today", now)
        rows = self._a._q("SELECT trunk, dir, COUNT(*) AS n, SUM(duration) AS d FROM obp_calls "
                          "WHERE ts >= ? AND is_data = 0 GROUP BY trunk, dir", (start.timestamp(),))
        out: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            t = out.setdefault(r["trunk"], {"rx": 0, "tx": 0, "rx_duration": 0.0, "tx_duration": 0.0})
            t[r["dir"]] = int(r["n"])
            t[r["dir"] + "_duration"] = round(float(r["d"] or 0), 1)
        return out

    # ------------------------------------------------------------ reading
    def summary(self, trunk: str, rng: str = "today", names: Optional[Dict[int, str]] = None,
                callsign=lambda _id: "", now: Optional[float] = None) -> Dict[str, Any]:
        if rng not in RANGES:
            rng = "today"
        names = names or {}
        start, starts, kind = TGStats.buckets(rng, now)
        rows = self._a._q("SELECT ts, dir, tg, slot, src_id, duration, is_data FROM obp_calls "
                          "WHERE trunk = ? AND ts >= ? ORDER BY ts", (trunk, start.timestamp()))
        edges = [s.timestamp() for s in starts]
        n = len(edges)

        def bucket_of(ts: float) -> int:
            lo, hi = 0, n - 1
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if edges[mid] <= ts:
                    lo = mid
                else:
                    hi = mid - 1
            return lo

        tot = {d: {"calls": 0, "duration": 0.0, "users": set()} for d in ("rx", "tx")}
        series = {d: {"calls": [0] * n, "duration": [0.0] * n} for d in ("rx", "tx")}
        per_tg: Dict[int, Dict[str, Any]] = {}
        data_calls = 0
        for r in rows:
            d, tg, dur, src = r["dir"], int(r["tg"]), float(r["duration"] or 0), r["src_id"]
            if r["is_data"]:
                data_calls += 1
                continue
            t = tot[d]
            t["calls"] += 1
            t["duration"] += dur
            if src:
                t["users"].add(src)
            b = bucket_of(float(r["ts"]))
            series[d]["calls"][b] += 1
            series[d]["duration"][b] += dur
            p = per_tg.setdefault(tg, {"tg": tg, "name": names.get(tg, ""), "rx": 0, "tx": 0,
                                       "rx_duration": 0.0, "tx_duration": 0.0, "last": 0.0})
            p[d] += 1
            p[d + "_duration"] += dur
            p["last"] = max(p["last"], float(r["ts"]))

        top = sorted(per_tg.values(), key=lambda p: (-(p["rx_duration"] + p["tx_duration"]), -(p["rx"] + p["tx"]), p["tg"]))
        for p in top:
            p["rx_duration"] = round(p["rx_duration"], 1)
            p["tx_duration"] = round(p["tx_duration"], 1)

        recent = []
        for r in reversed(rows[-RECENT * 2:]):
            if r["is_data"]:
                continue
            src = r["src_id"]
            recent.append({"ts": r["ts"], "dir": r["dir"], "tg": r["tg"], "name": names.get(int(r["tg"]), ""),
                           "slot": r["slot"], "src_id": src, "callsign": callsign(src) if src else "",
                           "duration": round(float(r["duration"] or 0), 1)})
            if len(recent) >= RECENT:
                break

        return {
            "range": rng, "bucket": kind, "buckets": edges,
            "totals": {d: {"calls": tot[d]["calls"], "duration": round(tot[d]["duration"], 1),
                           "users": len(tot[d]["users"])} for d in ("rx", "tx")},
            "data_calls": data_calls,
            "active_talkgroups": len(per_tg),
            "series": {d: {"calls": series[d]["calls"], "duration": [round(x, 1) for x in series[d]["duration"]]}
                       for d in ("rx", "tx")},
            "top": top[:TOP_TGS],
            "recent": recent,
        }
