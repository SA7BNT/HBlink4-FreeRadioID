"""
FreeRadioID avatars cached in SQLite.

Only IDs registered at FreeRadioID are looked up. Their image is
api/idimg-<dmr_id>.png; IDs without an own image get a grey placeholder there,
which counts as "no avatar". Avatars are shrunk and stored in the database
during the ID sync; /api/avatar answers from the database.
"""

import asyncio
import io
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from dashboard.auth import AuthStore
from dashboard.avatars import RECHECK_IMAGE, AvatarStore


def png(color, size=500):
    buf = io.BytesIO()
    Image.new("RGB", (size, size), color).save(buf, "PNG")
    return buf.getvalue()


PLACEHOLDER = png("grey", 64)
REAL = png("red")


class FakeFetch:
    def __init__(self):
        self.calls = []

    def __call__(self, url, limit=0):
        self.calls.append(url)
        return REAL if url.endswith("idimg-265800.png") else PLACEHOLDER


def store(fetch, url="https://freeradioid.net/api/idimg-{id}.png"):
    a = AuthStore(Path(tempfile.mkdtemp()) / "a.db")
    return AvatarStore(a, lambda: url, fetch=fetch)


class TestAvatarStore(unittest.TestCase):
    def test_sync_stores_small_images_and_placeholders(self):
        f = FakeFetch()
        st = store(f, "https://freeradioid.net/avatar-{id}.png")       # old URL: migrated
        asyncio.run(st.sync([265800, 100001]))
        has, mime, data = st.get(265800)
        self.assertTrue(has)
        self.assertEqual(mime, "image/webp")
        self.assertLess(len(data), len(REAL))
        self.assertEqual(Image.open(io.BytesIO(data)).size, (192, 192))
        self.assertEqual(st.get(100001)[0], False)
        self.assertTrue(all("api/idimg-" in u for u in f.calls))
        self.assertEqual(st.stats(), {"with_avatar": 1, "without": 1})

    def test_recheck_schedule(self):
        f = FakeFetch()
        st = store(f)
        asyncio.run(st.sync([265800, 100001]))
        self.assertEqual(st.due([265800, 100001]), [])                # everything fresh
        st._a._x("UPDATE frid_avatars SET fetched_at=? WHERE dmr_id=265800", (time.time() - RECHECK_IMAGE - 1,))
        self.assertEqual(st.due([265800, 100001, 2620001]), [2620001, 265800])   # new first, then stale

    def test_prune_unregistered(self):
        st = store(FakeFetch())
        asyncio.run(st.sync([265800, 100001]))
        st.prune([265800])
        self.assertIsNone(st.get(100001))


class TestAvatarApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from dashboard import server
        cls.server = server
        cls.client = TestClient(server.app)

    def setUp(self):
        s = self.server
        s.dashboard_config.setdefault("freeradioid", {})["avatars"] = True
        self.fetch = FakeFetch()
        self._orig = s.state.avatars
        s.state.avatars = store(self.fetch)
        self.p = mock.patch.object(s.state.user_db, "is_frid", side_effect=lambda i: i in (265800, 100001))
        self.p.start()

    def tearDown(self):
        self.p.stop()
        self.server.state.avatars = self._orig

    def test_only_freeradioid_ids_are_looked_up(self):
        self.assertEqual(self.client.get("/api/avatar/3112001").status_code, 404)
        self.assertEqual(self.fetch.calls, [])                          # not registered: no request

    def test_served_from_database(self):
        r = self.client.get("/api/avatar/265800")                       # not synced yet: fetched once
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-type"], "image/webp")
        n = len(self.fetch.calls)
        self.assertEqual(self.client.get("/api/avatar/265800").status_code, 200)
        self.assertEqual(len(self.fetch.calls), n)                      # second time from SQLite
        self.assertEqual(self.client.get("/api/avatar/100001").status_code, 404)   # placeholder

    def test_switched_off(self):
        self.server.dashboard_config["freeradioid"]["avatars"] = False
        self.assertEqual(self.client.get("/api/avatar/265800").status_code, 404)
        self.assertEqual(self.fetch.calls, [])


if __name__ == '__main__':
    unittest.main()
