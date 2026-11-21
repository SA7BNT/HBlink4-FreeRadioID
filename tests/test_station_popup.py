"""Station popup for any radio ID: /api/station/{id}."""
import tempfile
import unittest
from pathlib import Path


class TestStationInfo(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from dashboard import server
        cls.server = server
        server.dashboard_config.setdefault("freeradioid", {})["profile_lookup"] = False   # no network in tests
        cls.client = TestClient(server.app)

    def setUp(self):
        self._lh = list(self.server.state.last_heard)
        self.server.state.last_heard = [{"radio_id": 2620001, "callsign": "DO1TST", "source_name": "DB0XX (262001)",
                                         "slot": 2, "talkgroup": 262, "call_type": "group", "is_data": False,
                                         "frid": False, "last_heard": 1795300000, "active": False,
                                         "source_repeater_id": 262001}]

    def tearDown(self):
        self.server.state.last_heard = self._lh

    def test_heard_station(self):
        r = self.client.get("/api/station/2620001")
        self.assertEqual(r.status_code, 200)
        j = r.json()
        self.assertEqual(j["heard"]["talkgroup"], 262)
        self.assertEqual(j["callsign"], "DO1TST")
        self.assertNotIn("source_repeater_id", j["heard"])      # only what the popup needs
        self.assertFalse(j["connected"])

    def test_unknown_and_invalid(self):
        j = self.client.get("/api/station/1234567").json()
        self.assertIsNone(j["heard"])
        self.assertEqual(self.client.get("/api/station/0").status_code, 400)
        self.assertEqual(self.client.get("/api/station/99999999999").status_code, 400)


if __name__ == "__main__":
    unittest.main()
