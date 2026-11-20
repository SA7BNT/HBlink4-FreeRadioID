"""
Dashboard settings in SQLite.

First start: every setting comes from dashboard/config.json (or its default).
After that the database wins, even if config.json changes; "reset to
config.json" takes a section from the file again.
"""

import copy
import tempfile
import unittest
from pathlib import Path

from dashboard.auth import AuthStore
from dashboard.settings import SettingsError, SettingsStore, get_path


def make(cfg):
    a = AuthStore(Path(tempfile.mkdtemp()) / "s.db")
    return a, SettingsStore(a)


FILE_CFG = {
    "dashboard_title": "From File",
    "auth": {"public_dashboard": False, "session_hours": 8},
    "user_database": {"filter": {"countries": ["Germany"]}, "refresh": {"time_of_day": "04:10"}},
    "freeradioid": {"avatar_url": ""},
}


class TestSettings(unittest.TestCase):
    def test_first_start_uses_config_then_db_wins(self):
        a, st = make(FILE_CFG)
        cfg = copy.deepcopy(FILE_CFG)
        self.assertGreater(st.seed_and_apply(cfg), 20)
        self.assertEqual(cfg["dashboard_title"], "From File")
        self.assertFalse(cfg["auth"]["public_dashboard"])
        self.assertFalse(cfg["freeradioid"]["avatars"])           # empty avatar_url meant "off"
        self.assertEqual(cfg["web"]["port"], 8080)                  # default filled in

        st.update(cfg, {"dashboard_title": "From Admin", "auth.session_hours": "24"}, "boss")
        # next start with an edited config.json: the database still wins
        cfg2 = copy.deepcopy(FILE_CFG)
        cfg2["dashboard_title"] = "Edited File"
        self.assertEqual(st.seed_and_apply(cfg2), 0)
        self.assertEqual(cfg2["dashboard_title"], "From Admin")
        self.assertEqual(cfg2["auth"]["session_hours"], 24)

    def test_upstream_sample_title_becomes_fork_default(self):
        a, st = make({})
        cfg = {"dashboard_title": "HBlink4 Dashboard", "server_name": "HBlink4 Server"}
        st.seed_and_apply(cfg)
        self.assertEqual(cfg["dashboard_title"], "DMR Die N8schicht")
        self.assertEqual(cfg["server_name"], "dmr.die-n8schicht.de")

    def test_update_is_in_place(self):
        a, st = make(FILE_CFG)
        cfg = copy.deepcopy(FILE_CFG)
        st.seed_and_apply(cfg)
        udb = cfg["user_database"]          # code holds this reference
        st.update(cfg, {"user_database.filter.countries": "Germany, Austria , Switzerland"}, "boss")
        self.assertEqual(udb["filter"]["countries"], ["Germany", "Austria", "Switzerland"])
        st.update(cfg, {"user_database.filter.countries": "ALL"}, "boss")
        self.assertEqual(udb["filter"]["countries"], "all")

    def test_validation(self):
        a, st = make(FILE_CFG)
        cfg = copy.deepcopy(FILE_CFG)
        st.seed_and_apply(cfg)
        for bad in ({"auth.session_hours": 0}, {"user_database.refresh.time_of_day": "25:00"},
                    {"network_info.url": "javascript:alert(1)"}, {"user_database.source_url": "http://evil"},
                    {"auth.superadmin.password": "x"}, {"web.port": 70000}):
            with self.assertRaises(SettingsError):
                st.update(cfg, bad, "boss")
        self.assertEqual(cfg["auth"]["session_hours"], 8)            # nothing applied

    def test_reset_section(self):
        a, st = make(FILE_CFG)
        cfg = copy.deepcopy(FILE_CFG)
        st.seed_and_apply(cfg)
        st.update(cfg, {"dashboard_title": "Admin", "auth.session_hours": 48}, "boss")
        st.reset_section(cfg, FILE_CFG, "appearance", "boss")
        self.assertEqual(cfg["dashboard_title"], "From File")
        self.assertEqual(cfg["auth"]["session_hours"], 48)           # other section untouched
        self.assertEqual(get_path(cfg, "auth.session_hours"), 48)

    def test_describe(self):
        a, st = make(FILE_CFG)
        cfg = copy.deepcopy(FILE_CFG)
        st.seed_and_apply(cfg)
        sections = st.describe(cfg)
        keys = [f["key"] for s in sections for f in s["fields"]]
        self.assertIn("https.enabled", keys)
        self.assertNotIn("auth.superadmin", " ".join(keys))


class TestSettingsApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from dashboard import server
        cls.server = server
        server.dashboard_config.setdefault("auth", {})["superadmin"] = {"username": "boss", "password": "SuperSecret1"}
        server.state.auth = AuthStore(Path(tempfile.mkdtemp()) / "api.db")
        server.state.auth.sync_superadmin(server.dashboard_config["auth"]["superadmin"])
        server.state.settings = SettingsStore(server.state.auth)
        server.state.settings.seed_and_apply(server.dashboard_config)
        cls.client = TestClient(server.app)

    def test_admin_changes_title_live(self):
        h = {"X-Dashboard": "1"}
        self.client.cookies.clear()
        self.assertEqual(self.client.put("/api/admin/settings", json={"changes": {}}, headers=h).status_code, 401)
        self.client.post("/api/auth/login", json={"username": "boss", "password": "SuperSecret1"}, headers=h)
        r = self.client.put("/api/admin/settings", json={"changes": {"dashboard_title": "Live Title"}}, headers=h)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.get("/api/config").json()["dashboard_title"], "Live Title")
        r = self.client.put("/api/admin/settings", json={"changes": {"https.domain": "dmr.example.org"}}, headers=h)
        self.assertTrue(r.json()["restart_required"])
        r = self.client.put("/api/admin/settings", json={"changes": {"auth.session_hours": "x"}}, headers=h)
        self.assertEqual(r.status_code, 400)
        self.client.post("/api/auth/logout", headers=h)
        self.client.cookies.clear()


if __name__ == '__main__':
    unittest.main()


class TestNewKinds(unittest.TestCase):
    def test_color_image_links(self):
        from dashboard.settings import FIELD_BY_KEY, validate, SettingsError
        self.assertEqual(validate(FIELD_BY_KEY["theme.accent"], "#AABBCC"), "#aabbcc")
        self.assertEqual(validate(FIELD_BY_KEY["theme.accent"], ""), "")
        with self.assertRaises(SettingsError):
            validate(FIELD_BY_KEY["theme.accent"], "red")
        self.assertEqual(validate(FIELD_BY_KEY["logo"], "/branding/logo?v=1"), "/branding/logo?v=1")
        with self.assertRaises(SettingsError):
            validate(FIELD_BY_KEY["logo"], "javascript:alert(1)")
        links = validate(FIELD_BY_KEY["links.items"], [{"label": "Wiki", "url": "https://x.de", "icon": "📘"},
                                                       {"label": "", "url": ""}])
        self.assertEqual(links, [{"label": "Wiki", "url": "https://x.de", "icon": "📘", "new_tab": True}])
        with self.assertRaises(SettingsError):
            validate(FIELD_BY_KEY["links.items"], [{"label": "x", "url": "javascript:alert(1)"}])

    def test_openbridge_is_validated_on_save(self):
        from dashboard.settings import SettingsError
        a, st = make(FILE_CFG)
        cfg = copy.deepcopy(FILE_CFG)
        st.seed_and_apply(cfg)
        trunk = {"name": "T", "network_id": 2624, "local_port": 51084, "target_address": "x.example",
                 "target_port": 62046, "passphrase": "secret", "all_talkgroups": True, "proto_ver": 5,
                 "enhanced_obp": True}
        st.update(cfg, {"dmr.openbridge_connections": [trunk]}, "boss")
        with self.assertRaises(SettingsError):
            st.update(cfg, {"dmr.openbridge_connections": [dict(trunk, proto_ver=9)]}, "boss")
        with self.assertRaises(SettingsError):
            st.update(cfg, {"dmr.openbridge_connections": [{"name": "no-port"}]}, "boss")
