"""
FreeRadioID avatars through the dashboard (/api/avatar/{id}).

FreeRadioID serves api/idimg-<dmr_id>.png: the real image for IDs with an
avatar, the same grey placeholder for all others. The placeholder must come
back as 404 (the page shows initials), real images shrunk and cached on disk.
"""

import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image


def png(color, size=500):
    buf = io.BytesIO()
    Image.new("RGB", (size, size), color).save(buf, "PNG")
    return buf.getvalue()


PLACEHOLDER = png("grey", 64)
REAL = png("red")


class TestAvatarProxy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from dashboard import server
        cls.server = server
        cls.client = TestClient(server.app)

    def setUp(self):
        s = self.server
        self._data_dir = s.state._data_dir
        s.state._data_dir = Path(tempfile.mkdtemp())
        s._avatar_miss.clear()
        s._avatar_placeholder["md5"] = None
        s.dashboard_config.setdefault("freeradioid", {})["avatars"] = True
        s.dashboard_config["freeradioid"]["avatar_url"] = "https://freeradioid.net/avatar-{id}.png"  # old: migrated
        self.calls = []

        def fake_fetch(url, limit=0):
            self.calls.append(url)
            return REAL if url.endswith("idimg-265800.png") else PLACEHOLDER
        self.patch = mock.patch.object(s, "_fetch_bytes", side_effect=fake_fetch)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.server.state._data_dir = self._data_dir

    def test_real_avatar_is_shrunk_and_cached(self):
        r = self.client.get("/api/avatar/265800")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-type"], "image/webp")
        self.assertLess(len(r.content), len(REAL))
        self.assertEqual(Image.open(io.BytesIO(r.content)).size, (192, 192))
        self.assertTrue(all("api/idimg-" in u for u in self.calls))      # old avatar-{id} URL migrated
        n = len(self.calls)
        self.assertEqual(self.client.get("/api/avatar/265800").status_code, 200)
        self.assertEqual(len(self.calls), n)                              # served from disk
        self.assertTrue((self.server.state._data_dir / "avatars" / "265800.webp").exists())

    def test_placeholder_means_no_avatar(self):
        self.assertEqual(self.client.get("/api/avatar/3112001").status_code, 404)
        n = len(self.calls)
        self.assertEqual(self.client.get("/api/avatar/3112001").status_code, 404)
        self.assertEqual(len(self.calls), n)                              # negative cache

    def test_switched_off(self):
        self.server.dashboard_config["freeradioid"]["avatars"] = False
        self.assertEqual(self.client.get("/api/avatar/265800").status_code, 404)
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
