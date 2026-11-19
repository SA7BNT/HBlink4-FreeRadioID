"""Talkgroup statistics: storing group calls and summarising them for the charts."""

import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

from dashboard.auth import AuthStore
from dashboard.tgstats import TGStats, parse_names


def make():
    return TGStats(AuthStore(Path(tempfile.mkdtemp()) / "t.db"))


class TestTGStats(unittest.TestCase):
    def setUp(self):
        self.st = make()
        # fixed "now": today 15:30 local time
        self.now = datetime.now().replace(hour=15, minute=30, second=0, microsecond=0).timestamp()

    def at(self, hours_ago):
        return self.now - hours_ago * 3600

    def test_summary_per_tg_and_hour(self):
        self.st.record(262, 30, src_id=1, ts=self.at(0.2))     # 15:18
        self.st.record(262, 10, src_id=2, ts=self.at(0.3))
        self.st.record(91, 5, src_id=1, ts=self.at(2))          # 13:30
        self.st.record(262, 99, src_id=3, ts=self.at(1), is_data=True)   # data: not counted
        d = self.st.summary("today", {262: "Deutschland"}, lambda i: {1: "DL1A"}.get(i, ""), now=self.now)
        self.assertEqual(d["bucket"], "hour")
        self.assertEqual(len(d["buckets"]), 24)
        self.assertEqual(d["totals"], {"calls": 3, "duration": 45.0, "talkgroups": 2, "users": 2})
        top = d["talkgroups"][0]
        self.assertEqual((top["tg"], top["name"], top["calls"], top["duration"], top["users"]),
                         (262, "Deutschland", 2, 40.0, 2))
        self.assertEqual(top["top_talker"]["callsign"], "DL1A")
        s262 = next(s for s in d["series"] if s["tg"] == 262)
        self.assertEqual(s262["calls"][15], 2)
        self.assertEqual(s262["duration"][15], 40.0)
        s91 = next(s for s in d["series"] if s["tg"] == 91)
        self.assertEqual(s91["calls"][13], 1)

    def test_other_bucket_and_ranges(self):
        for tg in range(1, 9):
            self.st.record(tg, tg * 10, src_id=tg, ts=self.at(1))
        self.st.record(5, 10, ts=self.at(24 * 5))       # 5 days ago: only in 7d / 30d
        d = self.st.summary("today", now=self.now)
        self.assertEqual(len(d["series"]), 6)
        self.assertIsNone(d["series"][-1]["tg"])
        self.assertEqual(sum(d["series"][-1]["calls"]), 3)
        self.assertEqual(self.st.summary("7d", now=self.now)["totals"]["calls"], 9)
        self.assertEqual(len(self.st.summary("30d", now=self.now)["buckets"]), 30)
        self.assertEqual(self.st.summary("24h", now=self.now)["totals"]["calls"], 8)

    def test_purge(self):
        self.st.record(262, 5, ts=time.time() - 40 * 86400)
        self.st.record(262, 5)
        self.assertEqual(self.st.purge(30), 1)
        self.assertEqual(self.st.count(), 1)

    def test_names(self):
        self.assertEqual(parse_names("262 = Deutschland\n# x\n91: Worldwide\n9 Lokal\nfoo=bar"),
                         {262: "Deutschland", 91: "Worldwide", 9: "Lokal"})


if __name__ == "__main__":
    unittest.main()
