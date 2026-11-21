import unittest
from dashboard import version as v


class TestUpdateCheck(unittest.TestCase):
    def setUp(self):
        v._cache.clear()
        loc = {"version": "4.8.0", "commit": "a" * 40, "short": "aaaaaaa", "date": "2026-11-20T10:00:00+01:00",
               "branch": "main"}
        self._orig = v.local_version
        v.local_version = lambda refresh=False: dict(loc)

    def tearDown(self):
        v.local_version = self._orig

    def fake(self, head_sha, status="ahead", ahead=2):
        calls = []

        def fetch(url):
            calls.append(url)
            if "/commits/" in url:
                return {"sha": head_sha, "commit": {"message": "New thing\n\nbody",
                                                    "committer": {"date": "2026-11-21T12:00:00Z"}}}
            return {"status": status, "ahead_by": ahead,
                    "commits": [{"sha": "b" * 40, "commit": {"message": "one", "committer": {"date": "x"}}},
                                {"sha": head_sha, "commit": {"message": "two", "committer": {"date": "y"}}}]}
        return fetch, calls

    def test_update_available(self):
        fetch, _ = self.fake("c" * 40)
        r = v.check("me/repo", "main", fetch=fetch, force=True)
        self.assertTrue(r["available"]); self.assertEqual(r["behind"], 2)
        self.assertEqual(r["latest"]["message"], "New thing")
        self.assertEqual(r["commits"][0]["message"], "two")      # newest first
        self.assertIn("compare/aaaaaaa...ccccccc", r["url"])

    def test_up_to_date(self):
        fetch, calls = self.fake("a" * 40)
        r = v.check("me/repo", "main", fetch=fetch, force=True)
        self.assertFalse(r["available"]); self.assertEqual(len(calls), 1)

    def test_local_ahead_is_no_update(self):
        fetch, _ = self.fake("c" * 40, status="behind", ahead=0)
        self.assertFalse(v.check("me/repo", "main", fetch=fetch, force=True)["available"])

    def test_cached(self):
        fetch, calls = self.fake("c" * 40)
        v.check("me/repo", "main", fetch=fetch, force=True)
        n = len(calls)
        v.check("me/repo", "main", fetch=fetch)
        self.assertEqual(len(calls), n)          # second call served from cache

    def test_bad_repo_name_not_fetched(self):
        fetch, calls = self.fake("c" * 40)
        r = v.check("../../etc", "main", fetch=fetch, force=True)
        self.assertTrue(r["error"]); self.assertEqual(calls, [])

    def test_network_error(self):
        def boom(url): raise OSError("offline")
        r = v.check("me/repo", "main", fetch=boom, force=True)
        self.assertFalse(r["available"]); self.assertIn("offline", r["error"])

    def test_local_version_real_repo(self):
        v._local = None
        loc = self._orig()
        self.assertTrue(loc["version"])


if __name__ == "__main__":
    unittest.main()


class TestChangelog(unittest.TestCase):
    TEXT = """# Änderungen

## [1.2.0] – 2026-12-10
### Neu
- drei

## [1.1.0] - 2026-12-01
- zwei

## [1.0.0] – 2026-11-21
- eins
"""

    def test_sections_and_since(self):
        s = v.changelog_sections(self.TEXT)
        self.assertEqual([x["version"] for x in s], ["1.2.0", "1.1.0", "1.0.0"])
        self.assertEqual(s[1]["date"], "2026-12-01")
        self.assertIn("- drei", s[0]["body"])
        self.assertEqual([x["version"] for x in v.changes_since(self.TEXT, "1.0.0")], ["1.2.0", "1.1.0"])
        self.assertEqual(v.changes_since(self.TEXT, "1.2.0"), [])

    def test_vtuple(self):
        self.assertGreater(v.vtuple("1.10.0"), v.vtuple("1.9.9"))
        self.assertEqual(v.vtuple("v2.0.1"), (2, 0, 1))
        self.assertEqual(v.vtuple("junk"), (0, 0, 0))

    def test_repo_changelog_matches_version(self):
        import subprocess, sys
        r = subprocess.run([sys.executable, str(v.REPO_ROOT / "scripts" / "release.py"), "--check"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_check_includes_remote_version_and_notes(self):
        import base64
        loc = {"version": "1.0.0", "commit": "a" * 40, "short": "aaaaaaa", "date": "", "branch": "main"}
        orig = v.local_version
        v.local_version = lambda refresh=False: dict(loc)
        v._cache.clear()
        try:
            def fetch(url):
                if "/commits/" in url:
                    return {"sha": "c" * 40, "commit": {"message": "x", "committer": {"date": "d"}}}
                if "/compare/" in url:
                    return {"status": "ahead", "ahead_by": 2, "commits": []}
                if "VERSION" in url:
                    return {"content": base64.b64encode(b"1.2.0\n").decode()}
                return {"content": base64.b64encode(self.TEXT.encode()).decode()}
            r = v.check("me/repo", "main", fetch=fetch, force=True)
            self.assertEqual(r["remote_version"], "1.2.0")
            self.assertEqual([n["version"] for n in r["release_notes"]], ["1.2.0", "1.1.0"])
        finally:
            v.local_version = orig
