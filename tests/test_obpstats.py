"""OpenBridge statistics: both directions per trunk, and the origin breakdown in the TG statistics."""

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from dashboard.auth import AuthStore
from dashboard.obpstats import OBPStats
from dashboard.tgstats import TGStats


class TestOBPStats(unittest.TestCase):
    def setUp(self):
        a = AuthStore(Path(tempfile.mkdtemp()) / "t.db")
        self.obp, self.tg = OBPStats(a), TGStats(a)
        self.now = datetime.now().replace(hour=15, minute=30, second=0, microsecond=0).timestamp()

    def test_directions_series_and_recent(self):
        self.obp.record("OBP-A", "rx", 262, 30, slot=2, src_id=1, ts=self.now - 600)      # 15:20
        self.obp.record("OBP-A", "tx", 262, 10, slot=2, src_id=2, ts=self.now - 3600)     # 14:30
        self.obp.record("OBP-A", "rx", 91, 5, slot=2, src_id=1, ts=self.now - 300)
        self.obp.record("OBP-A", "rx", 91, 9, is_data=True, ts=self.now - 200)            # data: separate
        self.obp.record("OBP-B", "rx", 262, 99, ts=self.now - 100)                        # other trunk
        d = self.obp.summary("OBP-A", "today", {262: "Deutschland"}, lambda i: {1: "DL1A"}.get(i, ""), now=self.now)
        self.assertEqual(d["totals"]["rx"], {"calls": 2, "duration": 35.0, "users": 1})
        self.assertEqual(d["totals"]["tx"], {"calls": 1, "duration": 10.0, "users": 1})
        self.assertEqual(d["data_calls"], 1)
        self.assertEqual(d["series"]["rx"]["calls"][15], 2)
        self.assertEqual(d["series"]["tx"]["duration"][14], 10.0)
        self.assertEqual(d["top"][0]["tg"], 262)
        self.assertEqual((d["top"][0]["rx"], d["top"][0]["tx"], d["top"][0]["name"]), (1, 1, "Deutschland"))
        self.assertEqual(d["recent"][0]["tg"], 91)
        self.assertEqual(d["recent"][1]["callsign"], "DL1A")
        today = self.obp.today(now=self.now)
        self.assertEqual(today["OBP-A"]["rx"], 2)
        self.assertEqual(today["OBP-B"]["rx_duration"], 99.0)

    def test_invalid_rows_are_ignored(self):
        self.obp.record("", "rx", 262, 5)
        self.obp.record("X", "up", 262, 5)
        self.obp.record("X", "rx", 0, 5)
        self.assertEqual(self.obp.today(), {})

    def test_origins_in_tg_stats(self):
        self.tg.record(262, 20, source="265800", origin="repeater", ts=self.now - 60)
        self.tg.record(262, 30, source="OBP-A", origin="openbridge", ts=self.now - 50)
        self.tg.record(91, 10, source="OBP-A", ts=self.now - 40)          # old row without origin
        self.tg.tag_origin("OBP-A", "openbridge")
        d = self.tg.summary("today", now=self.now)
        self.assertEqual(d["origins"]["openbridge"], {"calls": 2, "duration": 40.0})
        self.assertEqual(d["origins"]["repeater"], {"calls": 1, "duration": 20.0})
        row = next(r for r in d["talkgroups"] if r["tg"] == 262)
        self.assertEqual(row["obp_share"], 0.6)


if __name__ == "__main__":
    unittest.main()
