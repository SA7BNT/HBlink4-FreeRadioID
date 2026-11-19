"""
Dashboard settings stored in SQLite and edited in the admin area.

On the first start every setting is seeded from dashboard/config.json (or its
built-in default). From then on the database is authoritative: the values are
applied over the loaded config at startup and whenever an admin saves them.
"Reset to config.json" in the admin area drops the stored values of a section
and seeds them again from the file.

Not here on purpose: the superadmin (config.json only) and anything belonging
to the HBlink4 server process (config/config.json).
"""
from __future__ import annotations

import copy
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

RADIOID_URL = "https://database.radioid.net/static/user.csv"
FREERADIOID_ALL_URL = "https://freeradioid.net/api/subscriber_ids.csv"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at REAL NOT NULL,
    updated_by TEXT NOT NULL DEFAULT ''
);
"""


@dataclass(frozen=True)
class Field:
    key: str                 # dotted path into the dashboard config
    section: str
    label: str
    kind: str                # text, longtext, bool, int, url, time, countries, choice
    default: Any
    help: str = ""
    restart: bool = False
    choices: Tuple[Tuple[str, str], ...] = ()
    min: int = 0
    max: int = 0


SECTIONS: List[Tuple[str, str]] = [
    ("general", "🏷️ General"),
    ("announcement", "📢 Announcement & link"),
    ("access", "🔐 Access"),
    ("freeradioid", "🛡️ FreeRadioID"),
    ("userdb", "📘 Callsign database"),
    ("https", "🔒 HTTPS & web"),
]

FIELDS: List[Field] = [
    Field("dashboard_title", "general", "Dashboard title", "text", "DMR Die N8schicht"),
    Field("server_name", "general", "Server name (subtitle)", "text", "dmr.die-n8schicht.de"),
    Field("server_description", "general", "Description", "text", "Amateur Radio DMR Network"),
    Field("logo", "general", "Logo URL", "text", "", "Empty = N8schicht logo. Files in dashboard/static/ as /static/name.svg"),
    Field("favicon", "general", "Favicon URL", "text", "", "Empty = same as the logo"),

    Field("announcement.enabled", "announcement", "Show announcement banner", "bool", False),
    Field("announcement.text", "announcement", "Announcement text", "longtext", ""),
    Field("network_info.enabled", "announcement", "Show network info button", "bool", False),
    Field("network_info.button_text", "announcement", "Button text", "text", "Network Info"),
    Field("network_info.url", "announcement", "Button link", "url", ""),

    Field("auth.public_dashboard", "access", "Public dashboard", "bool", True,
          "Off = nothing is visible without login. IP addresses are always admin-only."),
    Field("auth.session_hours", "access", "Session length (hours)", "int", 12, min=1, max=720),
    Field("auth.remember_days", "access", "\"Stay logged in\" (days)", "int", 30, min=1, max=365),

    Field("freeradioid.avatars", "freeradioid", "Show avatars", "bool", True,
          "From FreeRadioID; its grey placeholder is shown as initials"),
    Field("freeradioid.profile_lookup", "freeradioid", "Look up profiles", "bool", True,
          "Name and location in the station detail view"),
    Field("user_database.freeradioid.enabled", "freeradioid", "Load FreeRadioID ID list", "bool", True,
          "Registered IDs get the FRID tag in Last Heard"),

    Field("user_database.source_url", "userdb", "Source", "choice", RADIOID_URL,
          choices=((RADIOID_URL, "radioid.net"), (FREERADIOID_ALL_URL, "FreeRadioID (radioid.net + FreeRadioID)"))),
    Field("user_database.filter.countries", "userdb", "Countries", "countries", ["United States", "Canada"],
          "Comma separated, exactly as radioid.net spells them, or: all"),
    Field("user_database.refresh.time_of_day", "userdb", "Daily refresh at", "time", "03:17"),

    Field("https.enabled", "https", "HTTPS with Let's Encrypt", "bool", False,
          "Needs certbot, the domain pointing here and ports 80/443 open", restart=True),
    Field("https.domain", "https", "Domain", "text", "", restart=True),
    Field("https.email", "https", "E-mail for Let's Encrypt", "text", "", restart=True),
    Field("https.staging", "https", "Test certificates (staging)", "bool", False, restart=True),
    Field("https.port", "https", "HTTPS port", "int", 443, min=1, max=65535, restart=True),
    Field("https.http_port", "https", "HTTP port (challenge + redirect)", "int", 80, min=1, max=65535, restart=True),
    Field("web.port", "https", "Plain web port", "int", 8080,
          "Redirects to HTTPS when HTTPS is on", restart=True, min=1, max=65535),
]
FIELD_BY_KEY: Dict[str, Field] = {f.key: f for f in FIELDS}
# Values from the original HBlink4 sample config that nobody chose on purpose
_UPSTREAM_DEFAULTS = {
    "dashboard_title": ("HBlink4 Dashboard",),
    "server_name": ("HBlink4 Server",),
}


class SettingsError(Exception):
    """Invalid value; message is user-facing."""


# ------------------------------------------------------------ dict helpers

def get_path(cfg: Dict[str, Any], key: str, default: Any = None) -> Any:
    cur: Any = cfg
    for part in key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def set_path(cfg: Dict[str, Any], key: str, value: Any) -> None:
    """Set in place, so code holding a reference to a nested dict sees it."""
    parts = key.split(".")
    cur = cfg
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


# -------------------------------------------------------------- validation

def validate(field: Field, value: Any) -> Any:
    k = field.kind
    if k == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in ("true", "false", "1", "0", "on", "off"):
            return value.lower() in ("true", "1", "on")
        raise SettingsError(f"{field.label}: yes or no expected.")
    if k == "int":
        try:
            v = int(value)
        except (TypeError, ValueError):
            raise SettingsError(f"{field.label}: number expected.")
        if not field.min <= v <= field.max:
            raise SettingsError(f"{field.label}: {field.min}–{field.max}.")
        return v
    if k == "time":
        v = str(value or "").strip()
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v):
            raise SettingsError(f"{field.label}: time as HH:MM.")
        return v
    if k == "url":
        v = str(value or "").strip()
        if v and not re.match(r"^https?://", v, re.I):
            raise SettingsError(f"{field.label}: must start with http:// or https://")
        return v[:500]
    if k == "choice":
        v = str(value or "")
        if v not in [c[0] for c in field.choices]:
            raise SettingsError(f"{field.label}: invalid choice.")
        return v
    if k == "countries":
        if value == "all" or (isinstance(value, str) and value.strip().lower() == "all"):
            return "all"
        items = value if isinstance(value, list) else str(value or "").split(",")
        items = [str(x).strip() for x in items if str(x).strip()]
        if not items:
            raise SettingsError(f"{field.label}: at least one country, or all.")
        return items[:100]
    if k in ("text", "longtext"):
        v = str(value if value is not None else "")
        return v[:2000] if k == "longtext" else v.strip()[:200]
    raise SettingsError(f"Unknown setting type {k}")


# ------------------------------------------------------------------ store

class SettingsStore:
    def __init__(self, auth_store):
        """Shares the dashboard database (and its lock) with AuthStore."""
        self._a = auth_store
        with self._a._lock:
            self._a._db.executescript(_SCHEMA)

    def _stored(self) -> Dict[str, Any]:
        out = {}
        for row in self._a._q("SELECT key, value FROM settings"):
            try:
                out[row["key"]] = json.loads(row["value"])
            except ValueError:
                pass
        return out

    def _write(self, key: str, value: Any, who: str) -> None:
        self._a._x("INSERT OR REPLACE INTO settings (key, value, updated_at, updated_by) VALUES (?,?,?,?)",
                   (key, json.dumps(value), time.time(), who))

    def seed_and_apply(self, cfg: Dict[str, Any]) -> int:
        """
        First start: store every missing setting from cfg (config.json) or its
        default. Then apply all stored values over cfg. Returns how many were seeded.
        """
        stored = self._stored()
        seeded = 0
        for f in FIELDS:
            if f.key in stored:
                continue
            raw = get_path(cfg, f.key, f.default)
            if raw in _UPSTREAM_DEFAULTS.get(f.key, ()):
                raw = f.default   # untouched upstream sample value -> fork default
            if f.key == "freeradioid.avatars" and get_path(cfg, "freeradioid.avatar_url", None) == "":
                raw = False   # old config switched avatars off with an empty URL
            try:
                value = validate(f, raw)
            except SettingsError:
                value = f.default
            self._write(f.key, value, "config.json")
            stored[f.key] = value
            seeded += 1
        for key, value in stored.items():
            if key in FIELD_BY_KEY:
                set_path(cfg, key, value)
        return seeded

    def describe(self, cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
        meta = {r["key"]: (r["updated_at"], r["updated_by"])
                for r in self._a._q("SELECT key, updated_at, updated_by FROM settings")}
        sections = []
        for sid, title in SECTIONS:
            fields = []
            for f in FIELDS:
                if f.section != sid:
                    continue
                ts, by = meta.get(f.key, (None, ""))
                fields.append({"key": f.key, "label": f.label, "kind": f.kind, "help": f.help,
                               "restart": f.restart, "choices": [list(c) for c in f.choices],
                               "min": f.min, "max": f.max,
                               "value": get_path(cfg, f.key, f.default),
                               "updated_at": ts, "updated_by": by})
            sections.append({"id": sid, "title": title, "fields": fields})
        return sections

    def update(self, cfg: Dict[str, Any], changes: Dict[str, Any], who: str) -> List[str]:
        """Validate all, then store and apply. Returns the keys that changed."""
        clean = {}
        for key, raw in changes.items():
            f = FIELD_BY_KEY.get(key)
            if not f:
                raise SettingsError(f"Unknown setting {key}.")
            clean[key] = validate(f, raw)
        changed = []
        for key, value in clean.items():
            if get_path(cfg, key) != value:
                changed.append(key)
            self._write(key, value, who)
            set_path(cfg, key, value)
        return changed

    def reset_section(self, cfg: Dict[str, Any], file_cfg: Dict[str, Any], section: str, who: str) -> None:
        """Drop the stored values of a section and seed them again from config.json."""
        keys = [f.key for f in FIELDS if f.section == section]
        if not keys:
            raise SettingsError("Unknown section.")
        for key in keys:
            self._a._x("DELETE FROM settings WHERE key=?", (key,))
        fresh = copy.deepcopy(file_cfg)
        self.seed_and_apply(fresh)
        for key in keys:
            set_path(cfg, key, get_path(fresh, key))
