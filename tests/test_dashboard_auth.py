"""
Dashboard user management and IP visibility.

The rule under test: IP addresses reach admins only. Anonymous visitors and
plain users get every API response and WebSocket message with address fields
removed and IPs inside text masked. Secrets in config.json never leave the
server.
"""

import tempfile
import time
import unittest
from pathlib import Path

from dashboard.auth import (AuthError, AuthStore, hash_password, redact_ips, sniff_image,
                            verify_password)


def store():
    d = tempfile.mkdtemp()
    s = AuthStore(Path(d) / "t.db")
    s.sync_superadmin({"username": "boss", "password": "SuperSecret1"})
    return s


class TestPasswords(unittest.TestCase):
    def test_hash_roundtrip(self):
        h = hash_password("correct horse")
        self.assertTrue(verify_password("correct horse", h))
        self.assertFalse(verify_password("wrong", h))
        self.assertFalse(verify_password("x", "garbage"))


class TestRedaction(unittest.TestCase):
    def test_fields_and_text(self):
        data = {"repeaters": [{"repeater_id": 1, "address": "203.0.113.5:62031", "callsign": "DL1ABC"}],
                "remote_address": "10.0.0.1", "remote_port": 62031,
                "msg": "connected from 198.51.100.7:5000", "v6": "peer [2001:db8::1]:62031 up",
                "time": "03:17:00", "freq": "438.6125"}
        out = redact_ips(data)
        self.assertNotIn("address", out["repeaters"][0])
        self.assertNotIn("remote_address", out)
        self.assertNotIn("remote_port", out)
        self.assertNotIn("198.51.100.7", out["msg"])
        self.assertNotIn("2001:db8", out["v6"])
        self.assertEqual(out["time"], "03:17:00")       # not an IP
        self.assertEqual(out["freq"], "438.6125")
        self.assertEqual(data["repeaters"][0]["address"], "203.0.113.5:62031")  # original untouched


class TestUsers(unittest.TestCase):
    def setUp(self):
        self.s = store()
        _, self.boss = self.s.login("boss", "SuperSecret1")

    def test_superadmin_from_config(self):
        self.assertEqual(self.boss["role"], "superadmin")
        self.assertTrue(self.boss["from_config"])
        # password change in config is picked up
        self.s.sync_superadmin({"username": "boss", "password": "NewSecret22"})
        with self.assertRaises(AuthError):
            self.s.login("boss", "SuperSecret1")
        self.s.login("boss", "NewSecret22")

    def test_superadmin_cannot_be_managed_in_ui(self):
        with self.assertRaises(AuthError):
            self.s.update_user(self.boss, self.boss["id"], {"role": "user"})
        with self.assertRaises(AuthError):
            self.s.delete_user(self.boss, self.boss["id"])

    def test_admin_limits(self):
        admin = self.s.create_user(self.boss, "adm", "password1", "admin")
        _, admin = self.s.login("adm", "password1")
        user = self.s.create_user(admin, "usr", "password1", "user", dmr_id="2621234")
        self.assertEqual(user["dmr_id"], 2621234)
        with self.assertRaises(AuthError):          # only the superadmin creates admins
            self.s.create_user(admin, "adm2", "password1", "admin")
        with self.assertRaises(AuthError):          # nor promotes to admin
            self.s.update_user(admin, user["id"], {"role": "admin"})
        _, plain = self.s.login("usr", "password1")
        with self.assertRaises(AuthError):          # users manage nobody
            self.s.create_user(plain, "x1", "password1", "user")

    def test_disable_kills_sessions(self):
        u = self.s.create_user(self.boss, "usr", "password1", "user")
        token, _ = self.s.login("usr", "password1")
        self.assertIsNotNone(self.s.session_user(token))
        self.s.update_user(self.boss, u["id"], {"disabled": True})
        self.assertIsNone(self.s.session_user(token))
        with self.assertRaises(AuthError):
            self.s.login("usr", "password1")

    def test_throttling(self):
        for _ in range(5):
            with self.assertRaises(AuthError):
                self.s.login("boss", "nope", ip="192.0.2.9")
        with self.assertRaises(AuthError) as ctx:
            self.s.login("boss", "SuperSecret1", ip="192.0.2.9")
        self.assertIn("Too many", str(ctx.exception))

    def test_session_expiry(self):
        token, _ = self.s.login("boss", "SuperSecret1", hours=-1)
        self.assertIsNone(self.s.session_user(token))

    def test_validation(self):
        for bad in [("a", "password1"), ("ok-name", "short"), ("bad name", "password1")]:
            with self.assertRaises(AuthError):
                self.s.create_user(self.boss, *bad)


class TestImages(unittest.TestCase):
    def test_sniff(self):
        self.assertEqual(sniff_image(b"\x89PNG\r\n\x1a\nxxxx"), "image/png")
        self.assertEqual(sniff_image(b"\xff\xd8\xff\xe0"), "image/jpeg")
        self.assertIsNone(sniff_image(b"<svg onload=alert(1)>"))


class TestApi(unittest.TestCase):
    """HTTP level: secrets, CSRF header, admin-only IPs."""

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from dashboard import server
        cls.server = server
        server.dashboard_config["auth"] = {"superadmin": {"username": "boss", "password": "SuperSecret1"},
                                           "public_dashboard": True}
        server.state.auth = AuthStore(Path(tempfile.mkdtemp()) / "api.db")
        server.state.auth.sync_superadmin(server.dashboard_config["auth"]["superadmin"])
        server.state.repeaters[262999] = {"repeater_id": 262999, "callsign": "DB0TST",
                                          "address": "203.0.113.77:62031", "status": "connected",
                                          "connected_at": time.time()}
        cls.client = TestClient(server.app)

    @classmethod
    def tearDownClass(cls):
        cls.server.state.repeaters.pop(262999, None)

    def login(self):
        r = self.client.post("/api/auth/login", json={"username": "boss", "password": "SuperSecret1"},
                             headers={"X-Dashboard": "1"})
        self.assertEqual(r.status_code, 200)

    def test_config_has_no_secrets(self):
        cfg = self.client.get("/api/config").json()
        self.assertNotIn("auth", cfg)
        self.assertNotIn("SuperSecret1", str(cfg))

    def test_csrf_header_required(self):
        r = self.client.post("/api/auth/login", json={"username": "boss", "password": "SuperSecret1"})
        self.assertEqual(r.status_code, 403)

    def test_ips_only_for_admin(self):
        self.client.cookies.clear()
        anon = self.client.get("/api/repeaters").json()
        self.assertNotIn("203.0.113.77", str(anon))
        detail = self.client.get("/api/repeater/262999").json()
        self.assertNotIn("203.0.113.77", str(detail))
        with self.client.websocket_connect("/ws") as ws:
            self.assertNotIn("203.0.113.77", ws.receive_text())
        self.login()
        admin = self.client.get("/api/repeaters").json()
        self.assertIn("203.0.113.77", str(admin))
        with self.client.websocket_connect("/ws") as ws:
            self.assertIn("203.0.113.77", ws.receive_text())
        self.client.post("/api/auth/logout", headers={"X-Dashboard": "1"})
        self.client.cookies.clear()

    def test_admin_api_needs_admin(self):
        self.client.cookies.clear()
        self.assertEqual(self.client.get("/api/admin/users").status_code, 401)


if __name__ == '__main__':
    unittest.main()
