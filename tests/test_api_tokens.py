"""
API tokens for the HobbyRadioPortal: what a token may and may not do.
"""
import tempfile
import unittest
from pathlib import Path

from dashboard.auth import AuthStore, AuthError


class TestApiTokenStore(unittest.TestCase):
    def setUp(self):
        self.store = AuthStore(Path(tempfile.mkdtemp()) / "t.db")

    def test_create_list_use_revoke(self):
        t = self.store.create_api_token("HobbyRadioPortal", "boss")
        self.assertTrue(t["token"].startswith("hb4p_"))
        self.assertEqual(t["hint"], t["token"][-4:])
        listed = self.store.list_api_tokens()
        self.assertEqual(len(listed), 1)
        self.assertNotIn("token", listed[0])
        u = self.store.token_user(t["token"], "127.0.0.1", "dirk<script>")
        self.assertEqual(u["role"], "admin")
        self.assertTrue(u["api"])
        self.assertEqual(u["username"], "api:HobbyRadioPortal (dirkscript)")
        self.assertEqual(self.store.list_api_tokens()[0]["last_ip"], "127.0.0.1")
        self.assertIsNone(self.store.token_user(t["token"] + "x"))
        self.assertIsNone(self.store.token_user("nope"))
        self.store.revoke_api_token(t["id"], "boss")
        self.assertIsNone(self.store.token_user(t["token"]))
        with self.assertRaises(AuthError):
            self.store.revoke_api_token(t["id"])

    def test_bad_names(self):
        for name in ("", "x", "a" * 41, "bad;name"):
            with self.assertRaises(AuthError):
                self.store.create_api_token(name)


class TestApiTokenHttp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from dashboard import server
        cls.server = server
        server.dashboard_config["auth"] = {"superadmin": {"username": "boss", "password": "SuperSecret1"},
                                           "public_dashboard": False}
        server.state.auth = AuthStore(Path(tempfile.mkdtemp()) / "http.db")
        server.state.auth.sync_superadmin(server.dashboard_config)
        cls.client = TestClient(server.app)
        cls.token = server.state.auth.create_api_token("portal", "boss")["token"]
        cls.H = {"Authorization": "Bearer " + cls.token, "X-Dashboard": "1", "X-Portal-User": "dirk"}

    @classmethod
    def tearDownClass(cls):
        cls.server.dashboard_config["auth"]["public_dashboard"] = True

    def test_allowed_paths(self):
        r = self.client.get("/api/admin/settings", headers=self.H)
        self.assertEqual(r.status_code, 200)
        ids = [s["id"] for s in r.json()["sections"]]
        self.assertIn("aprs_daprs", ids)
        self.assertEqual(self.client.get("/api/admin/aprs", headers=self.H).status_code, 200)
        self.assertEqual(self.client.get("/api/version", headers=self.H).status_code, 200)

    def test_without_token_private_dashboard(self):
        self.assertEqual(self.client.get("/api/admin/settings").status_code, 401)
        self.assertEqual(self.client.get("/api/admin/settings", headers={"Authorization": "Bearer hb4p_wrong"}).status_code, 401)

    def test_forbidden_paths(self):
        for path in ("/api/admin/users", "/api/admin/sessions", "/api/admin/api-tokens", "/api/admin/backup"):
            self.assertIn(self.client.get(path, headers=self.H).status_code, (401, 403), path)
        r = self.client.post("/api/admin/api-tokens", json={"name": "evil"}, headers=self.H)
        self.assertIn(r.status_code, (401, 403))
        r = self.client.post("/api/auth/password", json={"old": "x", "new": "yyyyyyyyyy"}, headers=self.H)
        self.assertEqual(r.status_code, 401)

    def test_locked_settings(self):
        r = self.client.put("/api/admin/settings", json={"changes": {"auth.public_dashboard": True}}, headers=self.H)
        self.assertEqual(r.status_code, 403)
        r = self.client.post("/api/admin/settings/reset", json={"section": "https"}, headers=self.H)
        self.assertEqual(r.status_code, 403)

    def test_change_daprs_setting_is_audited(self):
        r = self.client.put("/api/admin/settings", json={"changes": {"daprs.ssid": 9}}, headers=self.H)
        self.assertEqual(r.status_code, 200, r.text)
        log = self.server.state.auth.audit_log(20)
        self.assertTrue(any(e["username"] == "api:portal (dirk)" and e["action"] == "settings_changed" for e in log))
        self.client.put("/api/admin/settings", json={"changes": {"daprs.ssid": 7}}, headers=self.H)

    def test_csrf_header_still_required(self):
        h = dict(self.H)
        h.pop("X-Dashboard")
        self.assertEqual(self.client.put("/api/admin/settings", json={"changes": {}}, headers=h).status_code, 403)


if __name__ == "__main__":
    unittest.main()
