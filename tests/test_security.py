"""
Hardening of the web interface (N8 security, N8 update guard) and of the text fields a device sends.
"""
import asyncio
import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from dashboard import n8_security, n8_update_guard as guard
from dashboard.auth import AuthStore
from hblink4.hblink import _clean_text_field

ROOT = Path(__file__).resolve().parent.parent


class TestHeadersAndDocs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from dashboard import server
        cls.server = server
        server.dashboard_config["auth"] = {"superadmin": {"username": "boss", "password": "SuperSecret1"},
                                           "public_dashboard": True}
        server.state.auth = AuthStore(Path(tempfile.mkdtemp()) / "sec.db")
        cls.client = TestClient(server.app)

    def test_api_docs_are_off(self):
        for path in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
            r = self.client.get(path)
            self.assertNotEqual(r.status_code, 200, path)
            self.assertNotIn("swagger", r.text.lower())
            self.assertNotIn('"paths"', r.text)

    def test_security_headers(self):
        r = self.client.get("/api/version")
        h = r.headers
        csp = h["content-security-policy"]
        for part in ("default-src 'self'", "object-src 'none'", "base-uri 'self'", "frame-ancestors 'self'", "form-action 'self'"):
            self.assertIn(part, csp)
        self.assertNotIn("http:", csp.replace("https:", ""))          # no plain-HTTP sources
        self.assertIn("camera=()", h["permissions-policy"])
        self.assertEqual(h["x-content-type-options"], "nosniff")
        self.assertEqual(h["x-frame-options"], "SAMEORIGIN")
        self.assertNotIn("strict-transport-security", h)                 # plain HTTP: no HSTS
        self.assertNotIn("server", h)

    def test_hsts_behind_https_proxy(self):
        r = self.client.get("/api/version", headers={"X-Forwarded-Proto": "https"})
        self.assertIn("max-age=", r.headers["strict-transport-security"])

    def test_uvicorn_without_server_header(self):
        self.assertFalse(n8_security.UVICORN_KWARGS["server_header"])
        srv = self.server._Server.make(self.server.app, host="127.0.0.1", port=1)
        self.assertFalse(srv.config.server_header)


class TestCleanTextField(unittest.TestCase):
    def test_markup_dropped_length_kept(self):
        raw = b'DL1<img src=x onerror=alert(1)>'
        out = _clean_text_field(raw)
        self.assertEqual(len(out), len(raw))
        for ch in b'<>"\'`':
            self.assertNotIn(ch, out)

    def test_url_keeps_ampersand(self):
        self.assertEqual(_clean_text_field(b"https://x.de/?a=1&b=2", keep_amp=True), b"https://x.de/?a=1&b=2")
        self.assertNotIn(b"&", _clean_text_field(b"A&B"))

    def test_control_codes(self):
        self.assertEqual(_clean_text_field(b"A\x1bB\x7f\x00").rstrip(b" "), b"AB\x00")


class TestUpdateGuard(unittest.TestCase):
    def repo(self):
        d = Path(tempfile.mkdtemp())
        r = d / "repo"
        subprocess.run(["git", "init", "-q", str(r)], check=True)
        g = ["git", "-C", str(r), "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run(g + ["commit", "-q", "--allow-empty", "-m", "a"], check=True)
        old = subprocess.check_output(["git", "-C", str(r), "rev-parse", "HEAD"], text=True).strip()
        (r / "f").write_text("new")
        subprocess.run(["git", "-C", str(r), "add", "f"], check=True)
        subprocess.run(g + ["commit", "-q", "-m", "b"], check=True)
        new = subprocess.check_output(["git", "-C", str(r), "rev-parse", "HEAD"], text=True).strip()
        st = d / "state"
        return r, st, old, new

    def test_rollback_after_three_starts(self):
        r, st, old, new = self.repo()
        guard.mark_pending(st, old, new, "1.4.0", "1.5.0")
        for _ in range(4):
            subprocess.run(["sh", str(ROOT / "scripts" / "n8-update-guard.sh"), str(r), str(st)], check=True,
                           capture_output=True)
        head = subprocess.check_output(["git", "-C", str(r), "rev-parse", "HEAD"], text=True).strip()
        self.assertEqual(head, old)
        res = guard.last_result(st)
        self.assertFalse(res["ok"])
        self.assertEqual((res["from"], res["to"]), ("1.4.0", "1.5.0"))
        self.assertFalse(guard.pending(st))

    def test_no_pending_nothing_happens(self):
        r, st, old, new = self.repo()
        p = subprocess.run(["sh", str(ROOT / "scripts" / "n8-update-guard.sh"), str(r), str(st)], capture_output=True)
        self.assertEqual(p.returncode, 0)
        self.assertIsNone(guard.last_result(st))

    def test_startup_check_confirms(self):
        import http.server
        import threading
        srv = http.server.HTTPServer(("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            st = Path(tempfile.mkdtemp())
            guard.mark_pending(st, "a" * 40, "b" * 40, "1.4.0", "1.5.0")
            res = asyncio.run(guard.startup_check(st, guard.local_url("0.0.0.0", srv.server_port, "/"), timeout=10, delay=0))
            self.assertTrue(res["ok"])
            self.assertFalse(guard.pending(st))
            self.assertEqual(guard.last_result(st)["to"], "1.5.0")
        finally:
            srv.shutdown()

    def test_unit_runs_the_guard(self):
        unit = (ROOT / "hblink4-dash.service").read_text()
        self.assertIn("ExecStartPre=-/bin/sh scripts/n8-update-guard.sh", unit)


class TestKitFiles(unittest.TestCase):
    """The N8 Kit is shared with N8 Crosslink, adn-frn-server and N8-HotSpot: it must be complete."""

    def test_pages_load_kit_and_dictionary(self):
        for page in ("dashboard.html", "aprs.html"):
            html = (ROOT / "dashboard" / "static" / page).read_text()
            for f in ("/static/n8-kit.css", "/static/n8-kit.js", "/static/i18n-dashboard.js"):
                self.assertIn(f, html, (page, f))

    def test_dictionary_parses(self):
        js = (ROOT / "dashboard" / "static" / "i18n-dashboard.js").read_text()
        self.assertIn("base: 'en'", js)
        self.assertIn("'Last heard': 'Zuletzt gehört'", js)


if __name__ == "__main__":
    unittest.main()


class TestBackupAndGuard(unittest.TestCase):
    """Backups (PR #2) and the N8 update guard must not get in each other's way."""

    def test_guard_state_not_in_backups(self):
        from dashboard import backup
        for name in ("update.pending", "update.pending.tmp", "update.result"):
            self.assertIn(name, backup.EXCLUDED)

    def test_update_and_backup_exclude_each_other(self):
        src = (ROOT / "dashboard" / "server.py").read_text()
        install = src[src.index("async def api_admin_update_install"):src.index("@app.get(\"/api/admin/system\")")]
        self.assertIn("_backup_idle()", install)
        idle = src[src.index("def _backup_idle():"):src.index("@app.exception_handler(_backup.BackupError)")]
        self.assertIn("_update_lock.locked()", idle)

    def test_backup_routes_hidden_from_docs(self):
        from fastapi.testclient import TestClient
        from dashboard import server
        c = TestClient(server.app)
        self.assertNotEqual(c.get("/openapi.json").status_code, 200)
        r = c.get("/api/admin/backup")
        self.assertIn(r.status_code, (401, 403))
        self.assertIn("default-src 'self'", r.headers["content-security-policy"])
