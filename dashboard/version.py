"""
Version of this installation and the check for updates on GitHub.

The local version comes from hblink4.__version__ plus the git commit the
installation is on. The update check asks the GitHub API (no login, a few
requests a day) whether the configured branch has newer commits.
"""
import json
import re
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REPO = "dhartmann1979/HBlink4-FreeRadioID"
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9_./-]{1,100}$")

_local: Optional[Dict[str, Any]] = None
_cache: Dict[str, Any] = {}


def _package_version() -> str:
    try:
        text = (REPO_ROOT / "hblink4" / "__init__.py").read_text(encoding="utf-8")
        m = re.search(r"__version__\s*=\s*['\"]([^'\"]+)", text)
        return m.group(1) if m else ""
    except OSError:
        return ""


def _git(*args: str) -> str:
    try:
        out = subprocess.run(["git", "-C", str(REPO_ROOT), *args], capture_output=True, text=True, timeout=5)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def local_version(refresh: bool = False) -> Dict[str, Any]:
    """{'version', 'commit', 'short', 'date', 'branch'} – commit/date empty without git."""
    global _local
    if _local is None or refresh:
        line = _git("log", "-1", "--format=%H|%cI")
        commit, _, date = line.partition("|")
        _local = {"version": _package_version(), "commit": commit, "short": commit[:7],
                  "date": date, "branch": _git("rev-parse", "--abbrev-ref", "HEAD")}
    return dict(_local)


def _fetch_json(url: str, timeout: float = 6) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "HBlink4-FreeRadioID-Dashboard",
                                               "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read(2_000_000).decode("utf-8", errors="replace"))


def check(repo: str = DEFAULT_REPO, branch: str = "main", max_age: float = 6 * 3600,
          force: bool = False, fetch=_fetch_json) -> Dict[str, Any]:
    """Compare the local commit with the newest commit of repo/branch on GitHub."""
    repo = (repo or DEFAULT_REPO).strip()
    branch = (branch or "main").strip()
    key = f"{repo}@{branch}"
    hit = _cache.get(key)
    if hit and not force and time.time() - hit["checked_at"] < max_age:
        return hit
    loc = local_version(refresh=force)
    res: Dict[str, Any] = {"repo": repo, "branch": branch, "local": loc, "checked_at": time.time(),
                           "available": False, "behind": 0, "latest": None, "commits": [], "error": "",
                           "url": f"https://github.com/{repo}"}
    if not _REPO_RE.match(repo) or not _BRANCH_RE.match(branch):
        res["error"] = "invalid repository or branch name"
    else:
        try:
            head = fetch(f"https://api.github.com/repos/{repo}/commits/{branch}")
            sha = head.get("sha", "")
            c = head.get("commit") or {}
            res["latest"] = {"commit": sha, "short": sha[:7], "date": (c.get("committer") or {}).get("date", ""),
                             "message": (c.get("message") or "").split("\n")[0][:140]}
            if loc["commit"] and sha and sha != loc["commit"]:
                cmp = fetch(f"https://api.github.com/repos/{repo}/compare/{loc['commit']}...{sha}")
                status = cmp.get("status")
                res["behind"] = int(cmp.get("ahead_by") or 0)
                res["available"] = status in ("ahead", "diverged") and res["behind"] > 0
                res["commits"] = [{"short": (x.get("sha") or "")[:7],
                                   "message": ((x.get("commit") or {}).get("message") or "").split("\n")[0][:140],
                                   "date": (((x.get("commit") or {}).get("committer")) or {}).get("date", "")}
                                  for x in reversed(cmp.get("commits") or [])][:10]
                res["url"] = f"https://github.com/{repo}/compare/{loc['short']}...{sha[:7]}"
            elif not loc["commit"]:
                res["error"] = "no git checkout here – cannot compare"
        except Exception as e:   # network, rate limit, unknown commit (local changes not pushed)
            res["error"] = str(e)[:200]
    _cache[key] = res
    return res
