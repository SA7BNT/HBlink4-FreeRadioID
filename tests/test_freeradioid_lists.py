"""
FreeRadioID.net DMR-ID lists in the dashboard user database.

FreeRadioID publishes header-less CSV (id,callsign,name,city,state,country)
with odd line endings, and a JSON list of the IDs registered there. Both must
load, and registered IDs must resolve even when the main list filters them out.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dashboard.user_db import UserDatabase, filter_rows_from_csv_bytes, parse_freeradioid_json

_FRID_CSV = (
    b"100001,JIGEN,Antonio,Avellino,8,Italy\n\r"
    b"265800,XL0DIG,N8schicht Crosslink,Internet,N8schicht,Germany\n\r"
    b"3100001,W1AAA,Al,Boston,MA,United States\n\r"
)

_FRID_JSON = json.dumps({"count": 2, "results": [
    {"id": 100001, "callsign": "JIGEN", "fname": "Antonio", "city": "Avellino",
     "state": 0, "country": "Italy", "remarks": "FreeRadioID.net"},
    {"id": 265800, "callsign": "XL0DIG", "fname": "N8schicht Crosslink", "city": "Internet",
     "state": "N8schicht", "country": "Germany"},
    {"id": "bad", "callsign": "X"},
    {"id": 5, "callsign": ""},
]}).encode()


class TestFreeRadioIDCsv(unittest.TestCase):
    def test_headerless_csv_with_country_filter(self):
        rows = filter_rows_from_csv_bytes(_FRID_CSV, {"countries": ["Germany"]})
        self.assertEqual(rows, {265800: "XL0DIG"})

    def test_headerless_csv_all_countries(self):
        rows = filter_rows_from_csv_bytes(_FRID_CSV, {"countries": "all"})
        self.assertEqual(set(rows), {100001, 265800, 3100001})


class TestFreeRadioIDRegistry(unittest.TestCase):
    def test_parse_json_skips_bad_rows(self):
        users = parse_freeradioid_json(_FRID_JSON)
        self.assertEqual(set(users), {100001, 265800})
        self.assertEqual(users[265800]["name"], "N8schicht Crosslink")
        self.assertEqual(users[100001]["state"], "")  # numeric state codes dropped

    def test_registry_lookup_and_persistence(self):
        with tempfile.TemporaryDirectory() as d:
            db = UserDatabase(Path(d))
            with mock.patch("dashboard.user_db._http_get_with_conditional",
                            return_value=(200, _FRID_JSON, None)):
                self.assertEqual(db._refresh_frid_sync({}), "ok")
            # Registered IDs resolve even though the main list is empty
            self.assertEqual(db.get(100001), "JIGEN")
            self.assertTrue(db.is_frid(265800))
            self.assertFalse(db.is_frid(1))
            self.assertEqual(db.status_dict()["freeradioid_rows"], 2)

            reloaded = UserDatabase(Path(d))
            reloaded.load_from_disk()
            self.assertEqual(reloaded.frid_info(265800)["callsign"], "XL0DIG")
            self.assertIsNotNone(reloaded.frid_age_hours())

    def test_failed_refresh_keeps_registry(self):
        with tempfile.TemporaryDirectory() as d:
            db = UserDatabase(Path(d))
            with mock.patch("dashboard.user_db._http_get_with_conditional",
                            return_value=(200, _FRID_JSON, None)):
                db._refresh_frid_sync({})
            with mock.patch("dashboard.user_db._http_get_with_conditional",
                            side_effect=OSError("down")):
                self.assertEqual(db._refresh_frid_sync({}), "error")
            self.assertTrue(db.is_frid(100001))


if __name__ == '__main__':
    unittest.main()
