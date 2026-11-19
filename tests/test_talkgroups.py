"""Talkgroup names from the N8schicht list (talkgroup_ids.json) plus own names."""
import tempfile
import unittest
from pathlib import Path

from dashboard.auth import AuthStore
from dashboard.talkgroups import BUNDLED, Talkgroups, parse


class TestTalkgroups(unittest.TestCase):
    def test_parse_bundled_list(self):
        names = parse(BUNDLED.read_bytes())
        self.assertGreater(len(names), 1000)
        self.assertEqual(names[262], "DEUTSCHLAND NATIONAL")
        self.assertEqual(names[2627], "BADEN-WÜRTTEMBERG")      # mojibake repaired
        self.assertNotIn(3, names)                               # "N/A" is no name

    def test_store_uses_bundled_copy_first(self):
        a = AuthStore(Path(tempfile.mkdtemp()) / "t.db")
        t = Talkgroups(a)
        self.assertEqual(t.names()[91], "WORLD-WIDE")
        # survives a restart from the database
        self.assertEqual(Talkgroups(a).count(), t.count())

    def test_skips_entries_without_information(self):
        raw = b'{"results":[{"TG-id":"9","callsign":"DIAL-a-TG","id":"9"},{"TG-id":"2021","callsign":"2021","id":"2021"},{"TG-id":"x","callsign":"Y"}]}'
        self.assertEqual(parse(raw), {9: "DIAL-a-TG"})


if __name__ == "__main__":
    unittest.main()
