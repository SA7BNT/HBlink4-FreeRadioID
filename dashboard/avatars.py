"""
FreeRadioID avatars, cached in SQLite.

Only IDs registered at FreeRadioID have avatars. Their per-ID image is
https://freeradioid.net/api/idimg-<dmr_id>.png (500x500 PNG, ~750 KB); IDs
without an own image get a grey placeholder there. The avatars are fetched
together with the FreeRadioID ID list (daily and at startup), scaled to
192 px WebP (~6-10 KB, Pillow) and stored in dashboard.db, so the dashboard
answers from the database instantly and never asks FreeRadioID about
unregistered IDs.

Re-checks: IDs without an avatar daily (the placeholder is only ~5 KB),
real avatars weekly.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import sqlite3
import time
import urllib.request
from typing import Callable, Dict, Iterable, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_URL = "https://freeradioid.net/api/idimg-{id}.png"
OLD_URLS = ("https://freeradioid.net/avatar-{id}.png",)   # per account, not per DMR-ID
SIZE = 192
RECHECK_NONE = 24 * 3600
RECHECK_IMAGE = 7 * 24 * 3600

_SCHEMA = """
CREATE TABLE IF NOT EXISTS frid_avatars (
    dmr_id     INTEGER PRIMARY KEY,
    has_avatar INTEGER NOT NULL,
    mime       TEXT,
    data       BLOB,
    fetched_at REAL NOT NULL
);
"""


def _sniff(data: bytes) -> Optional[str]:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def shrink(data: bytes) -> Tuple[Optional[str], bytes]:
    """192 px WebP; the original if Pillow is missing or the image is unreadable."""
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(data))
        im = im.convert("RGBA") if im.mode in ("P", "LA", "RGBA") else im.convert("RGB")
        im.thumbnail((SIZE, SIZE), Image.LANCZOS)
        out = io.BytesIO()
        im.save(out, "WEBP", quality=82, method=4)
        return "image/webp", out.getvalue()
    except Exception:
        return _sniff(data), data


def _http_get(url: str, limit: int = 4 * 1024 * 1024) -> Optional[bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": "HBlink4-Dashboard FreeRadioID avatars"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        if resp.status != 200:
            return None
        return resp.read(limit + 1)[:limit]


class AvatarStore:
    def __init__(self, auth_store, url_getter: Callable[[], str], fetch=_http_get):
        """Shares the dashboard database with AuthStore."""
        self._a = auth_store
        self._url_getter = url_getter
        self._fetch = fetch
        self._placeholder_md5: Optional[str] = None
        self._sync_lock = asyncio.Lock()
        with self._a._lock:
            self._a._db.executescript(_SCHEMA)

    # ------------------------------------------------------------ reading
    def get(self, dmr_id: int) -> Optional[Tuple[bool, Optional[str], Optional[bytes]]]:
        """(has_avatar, mime, data) or None if never checked."""
        rows = self._a._q("SELECT has_avatar, mime, data FROM frid_avatars WHERE dmr_id=?", (dmr_id,))
        if not rows:
            return None
        r = rows[0]
        return bool(r["has_avatar"]), r["mime"], (bytes(r["data"]) if r["data"] is not None else None)

    def version(self) -> int:
        """Changes whenever the cache changes; pages put it into the image URLs."""
        rows = self._a._q("SELECT COUNT(*) AS n, COALESCE(MAX(fetched_at), 0) AS t FROM frid_avatars WHERE has_avatar=1")
        return int(rows[0]["t"]) * 1000 + rows[0]["n"] % 1000

    def stats(self) -> Dict[str, int]:
        rows = self._a._q("SELECT has_avatar, COUNT(*) AS n FROM frid_avatars GROUP BY has_avatar")
        d = {bool(r["has_avatar"]): r["n"] for r in rows}
        return {"with_avatar": d.get(True, 0), "without": d.get(False, 0)}

    # ------------------------------------------------------------ fetching
    def url(self, dmr_id: int) -> str:
        tpl = self._url_getter() or ""
        if tpl in OLD_URLS:
            tpl = DEFAULT_URL
        return tpl.replace("{id}", str(dmr_id)) if tpl else ""

    def fetch_one(self, dmr_id: int) -> bool:
        """Download, shrink and store one avatar (blocking). True if it has one."""
        if not self.url(dmr_id):
            return False
        if self._placeholder_md5 is None:
            ph = self._fetch(self.url(0))
            self._placeholder_md5 = hashlib.md5(ph).hexdigest() if ph else ""
        data = self._fetch(self.url(dmr_id))
        now = time.time()
        if not data or hashlib.md5(data).hexdigest() == self._placeholder_md5 or not _sniff(data):
            self._a._x("INSERT OR REPLACE INTO frid_avatars (dmr_id, has_avatar, mime, data, fetched_at) "
                       "VALUES (?,0,NULL,NULL,?)", (dmr_id, now))
            return False
        mime, small = shrink(data)
        self._a._x("INSERT OR REPLACE INTO frid_avatars (dmr_id, has_avatar, mime, data, fetched_at) "
                   "VALUES (?,1,?,?,?)", (dmr_id, mime, sqlite3.Binary(small), now))
        return True

    def due(self, registered: Iterable[int]) -> list:
        """Registered IDs never checked or due for a re-check (oldest first)."""
        now = time.time()
        known = {r["dmr_id"]: (r["has_avatar"], r["fetched_at"])
                 for r in self._a._q("SELECT dmr_id, has_avatar, fetched_at FROM frid_avatars")}
        out = []
        for rid in registered:
            k = known.get(rid)
            if k is None:
                out.append((0, rid))
            elif now - k[1] > (RECHECK_IMAGE if k[0] else RECHECK_NONE):
                out.append((k[1], rid))
        return [rid for _, rid in sorted(out)]

    def prune(self, registered: Iterable[int]) -> None:
        """Forget avatars of IDs no longer registered at FreeRadioID."""
        keep = set(registered)
        if not keep:
            return
        for r in self._a._q("SELECT dmr_id FROM frid_avatars"):
            if r["dmr_id"] not in keep:
                self._a._x("DELETE FROM frid_avatars WHERE dmr_id=?", (r["dmr_id"],))

    async def sync(self, registered: Iterable[int], concurrency: int = 4) -> Dict[str, int]:
        """Bring the cache up to date for the registered IDs (runs in the background)."""
        if self._sync_lock.locked():
            return {"skipped": 1}
        async with self._sync_lock:
            registered = list(registered)
            self.prune(registered)
            todo = self.due(registered)
            if not todo:
                return {"fetched": 0}
            t0 = time.time()
            sem = asyncio.Semaphore(concurrency)
            found = errors = 0

            async def one(rid):
                nonlocal found, errors
                async with sem:
                    try:
                        if await asyncio.to_thread(self.fetch_one, rid):
                            found += 1
                    except Exception as e:
                        errors += 1
                        logger.debug(f"Avatar {rid}: {e}")

            await asyncio.gather(*(one(rid) for rid in todo))
            s = self.stats()
            logger.info(f"🖼️ FreeRadioID avatars: checked {len(todo)} IDs in {time.time() - t0:.0f}s "
                        f"({found} with image, {errors} errors); cache now {s['with_avatar']} avatars")
            return {"fetched": len(todo), "found": found, "errors": errors}
