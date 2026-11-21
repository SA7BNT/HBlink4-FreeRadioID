"""
Settings in SQLite, shared by the HBlink4 DMR server and the dashboard.

The database (dashboard/data/dashboard.db by default) is the main
configuration. The JSON config files only hold what is needed to start for the
first time; on that first start every setting that is not yet in the database
is copied from the file (or its built-in default). From then on the database
wins and the settings are edited in the dashboard's admin area.

Keys of the DMR server start with "dmr." followed by the path in
config/config.json (dmr.global.port_ipv4, dmr.freeradioid.enabled, ...).
Keys without that prefix belong to the dashboard (dashboard/config.json).
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

DEFAULT_DB = "dashboard/data/dashboard.db"
DMR_PREFIX = "dmr."

_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at REAL NOT NULL,
    updated_by TEXT NOT NULL DEFAULT ''
);
"""


class SettingsError(Exception):
    """Invalid value; the message is user-facing."""


@dataclass(frozen=True)
class Field:
    key: str                 # dotted path; "dmr." prefix = HBlink4 config
    section: str
    label: str
    kind: str                # text, longtext, password, bool, int, float, url, time,
                             # countries, choice, tglist, json, color, image, links, words
    default: Any
    help: str = ""
    restart: bool = False    # needs a restart of its program to take effect
    choices: Tuple[Tuple[str, str], ...] = ()
    min: float = 0
    max: float = 0
    depends: str = ""        # key of a bool field: only shown/used while that one is on
    advanced: bool = False   # behind "Show advanced options" in the admin area

    @property
    def is_dmr(self) -> bool:
        return self.key.startswith(DMR_PREFIX)

    @property
    def path(self) -> str:
        """Path inside the program's own config dict."""
        return self.key[len(DMR_PREFIX):] if self.is_dmr else self.key


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

def _tg_items(value: Any) -> List[Any]:
    if isinstance(value, list):
        return value
    return [x for x in re.split(r"[,\s;]+", str(value or "").strip()) if x]


def validate(field: Field, value: Any) -> Any:
    k = field.kind
    if k == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and value in (0, 1):
            return bool(value)
        if isinstance(value, str) and value.lower() in ("true", "false", "1", "0", "on", "off", "yes", "no"):
            return value.lower() in ("true", "1", "on", "yes")
        raise SettingsError(f"{field.label}: yes or no expected.")
    if k in ("int", "float"):
        try:
            v = int(value) if k == "int" else float(value)
        except (TypeError, ValueError):
            raise SettingsError(f"{field.label}: number expected.")
        if not field.min <= v <= field.max:
            raise SettingsError(f"{field.label}: {field.min:g}–{field.max:g}.")
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
        v = str(value if value is not None else "")
        if v not in [c[0] for c in field.choices]:
            raise SettingsError(f"{field.label}: invalid choice.")
        return v
    if k == "countries":
        if isinstance(value, str) and value.strip().lower() == "all":
            return "all"
        items = value if isinstance(value, list) else str(value or "").split(",")
        items = [str(x).strip() for x in items if str(x).strip()]
        if not items:
            raise SettingsError(f"{field.label}: at least one country, or all.")
        return items[:100]
    if k == "tglist":
        # None = all talkgroups, [] = none, [1, 2] = only those
        if value is None or (isinstance(value, str) and value.strip().lower() in ("all", "*")):
            return None
        if isinstance(value, str) and value.strip().lower() in ("", "none", "-"):
            return []
        out = []
        for x in _tg_items(value):
            try:
                tg = int(x)
            except (TypeError, ValueError):
                raise SettingsError(f"{field.label}: talkgroup numbers, 'all' or 'none'.")
            if not 1 <= tg <= 16777215:
                raise SettingsError(f"{field.label}: talkgroup {tg} out of range.")
            if tg not in out:
                out.append(tg)
        return out
    if k == "json":
        if isinstance(value, str):
            try:
                value = json.loads(value or "[]")
            except ValueError as e:
                raise SettingsError(f"{field.label}: invalid JSON ({e}).")
        if not isinstance(value, list) or not all(isinstance(x, dict) for x in value):
            raise SettingsError(f"{field.label}: a JSON list of objects [ {{…}}, … ] is expected.")
        return value
    if k == "color":
        v = str(value or "").strip()
        if v and not re.fullmatch(r"#[0-9a-fA-F]{6}", v):
            raise SettingsError(f"{field.label}: colour as #rrggbb or empty.")
        return v.lower()
    if k == "image":
        v = str(value or "").strip()
        if v and not (v.startswith("/") or re.match(r"^https?://", v, re.I)):
            raise SettingsError(f"{field.label}: a path like /static/logo.svg or a https:// address.")
        if any(c in v for c in "\"'<>"):
            raise SettingsError(f"{field.label}: invalid characters.")
        return v[:500]
    if k == "links":
        if isinstance(value, str):
            try:
                value = json.loads(value or "[]")
            except ValueError as e:
                raise SettingsError(f"{field.label}: invalid JSON ({e}).")
        if not isinstance(value, list):
            raise SettingsError(f"{field.label}: a list of links is expected.")
        out = []
        for i, item in enumerate(value[:30], 1):
            if not isinstance(item, dict):
                raise SettingsError(f"{field.label}: entry {i} is not a link.")
            label = str(item.get("label") or "").strip()[:40]
            url = str(item.get("url") or "").strip()[:500]
            if not label and not url:
                continue
            if not label:
                raise SettingsError(f"{field.label}: link {i} needs a name.")
            if not (url.startswith("/") or url.startswith("#") or re.match(r"^(https?://|mailto:)", url, re.I)):
                raise SettingsError(f"{field.label}: link „{label}“ must start with https://, mailto:, / or #.")
            out.append({"label": label, "url": url, "icon": str(item.get("icon") or "").strip()[:8],
                        "new_tab": bool(item.get("new_tab", True))})
        return out
    if k == "words":
        items = value if isinstance(value, list) else re.split(r"[,;\n]+", str(value or ""))
        out = []
        for x in items:
            w = str(x).strip().lower()[:40]
            if w and w not in out:
                out.append(w)
        return out[:100]
    if k == "password":
        v = str(value if value is not None else "")
        if not v.strip():
            raise SettingsError(f"{field.label}: must not be empty.")
        return v[:120]
    if k in ("text", "longtext"):
        v = str(value if value is not None else "")
        return v[:2000] if k == "longtext" else v.strip()[:200]
    raise SettingsError(f"Unknown setting type {k}")


# ------------------------------------------------------------ DMR settings

DMR_SECTIONS: List[Tuple[str, str]] = [
    ("dmr_server", "🛰️ DMR server"),
    ("dmr_access", "🔑 Repeater & hotspot access"),
    ("dmr_frid", "🛡️ FreeRadioID login"),
    ("dmr_types", "📲 Device detection"),
    ("dmr_advanced", "🧩 Patterns, blacklist & links (JSON)"),
    ("dmr_obp", "↔️ OpenBridge trunks"),
]

DMR_FIELDS: List[Field] = [
    Field("dmr.global.port_ipv4", "dmr_server", "DMR port (UDP)", "int", 62031,
          "Repeaters and hotspots connect here (HomeBrew standard 62031)", restart=True, min=1, max=65535),
    Field("dmr.global.max_missed", "dmr_server", "Missed pings before drop", "int", 3, min=1, max=20, advanced=True),
    Field("dmr.global.timeout_duration", "dmr_server", "Ping timeout (s)", "int", 30, min=5, max=600, advanced=True),
    Field("dmr.global.stream_timeout", "dmr_server", "Stream end without terminator (s)", "float", 2.0, min=0.5, max=30,
          advanced=True),
    Field("dmr.global.stream_hang_time", "dmr_server", "Talkgroup hang time (s)", "float", 10.0,
          "How long a slot stays reserved for the last talkgroup", min=0, max=120),
    Field("dmr.global.reconnect_report_threshold", "dmr_server", "Log reconnects shorter than (s)", "int", 3600,
          "0 = log every reconnect", min=0, max=604800, advanced=True),

    Field("dmr.repeater_configurations.default.passphrase", "dmr_access", "Passphrase (default)", "password", "passw0rd",
          "Shared password for repeaters/hotspots without their own pattern. With FreeRadioID login on, "
          "this is the guest password."),
    Field("dmr.repeater_configurations.default.slot1_talkgroups", "dmr_access", "Talkgroups TS1", "tglist", [8],
          "Comma separated, all = every talkgroup, none = no talkgroups"),
    Field("dmr.repeater_configurations.default.slot2_talkgroups", "dmr_access", "Talkgroups TS2", "tglist", [8]),
    Field("dmr.repeater_configurations.default.trust", "dmr_access", "Trust TGs requested by the device (RPTO)",
          "bool", False, "On = the device's own talkgroup list wins; off = only the lists above"),
    Field("dmr.repeater_configurations.default.default_unit_calls", "dmr_access", "Private (unit) calls", "bool", False),

    Field("dmr.freeradioid.enabled", "dmr_frid", "FreeRadioID secure login", "bool", True,
          "Logins are checked against the personal FreeRadioID.net password; the passphrase above becomes the guest login"),
    Field("dmr.freeradioid.guest_talkgroups", "dmr_frid", "Guest TX talkgroups", "tglist", [26501, 26502, 26503],
          "Guests may only transmit here; all = no restriction, none = guests cannot transmit",
          depends="dmr.freeradioid.enabled"),
    Field("dmr.freeradioid.timeout", "dmr_frid", "API timeout (s)", "float", 3.0, min=1, max=15,
          depends="dmr.freeradioid.enabled", advanced=True),

    Field("dmr.connection_type_detection.app_packages", "dmr_types", "📲 Apps – package name contains", "words",
          ["z3dmr", "droidstar", "dudestar", "bluedv", "dvsm", "peanut"],
          "Phone/PC apps without radio hardware. Comma separated, upper/lower case does not matter. "
          "Connected stations are sorted again right after saving."),
    Field("dmr.connection_type_detection.hotspot_packages", "dmr_types", "📱 Hotspots – package name contains", "words",
          ["mmdvm_hs", "dvmega", "zumspot", "jumbospot", "nanodv", "openspot", "dmo", "simplex"]),
    Field("dmr.connection_type_detection.repeater_packages", "dmr_types", "📶 Repeaters – package name contains", "words",
          ["repeater", "duplex", "stm32", "unknown"]),
    Field("dmr.connection_type_detection.network_packages", "dmr_types", "🔗 Networks – package name contains", "words",
          ["hblink", "freedmr", "brandmeister", "xlx", "dmr+", "tgif", "ipsc"]),
    Field("dmr.connection_type_detection.app_software", "dmr_types", "📲 Apps – software contains", "words",
          ["z3dmr", "droidstar", "dudestar", "bluedv", "dvsm", "peanut"],
          "Checked when the package name says nothing", advanced=True),
    Field("dmr.connection_type_detection.hotspot_software", "dmr_types", "📱 Hotspots – software contains", "words",
          ["pi-star", "pistar", "ps4", "wpsd"], advanced=True),
    Field("dmr.connection_type_detection.network_software", "dmr_types", "🔗 Networks – software contains", "words",
          ["hblink", "freedmr", "brandmeister", "xlx"], advanced=True),

    Field("dmr.repeater_configurations.patterns", "dmr_advanced", "Repeater patterns", "json", [],
          "Own rules per ID, ID range or callsign: passphrase, talkgroups, freeradioid on/off. "
          "See config/config_advanced_sample.json"),
    Field("dmr.blacklist.patterns", "dmr_advanced", "Blacklist", "json", [],
          "Blocked IDs, ID ranges or callsigns, each with a reason"),
    Field("dmr.outbound_connections", "dmr_advanced", "Outbound links to other servers", "json", [], restart=True),
    Field("dmr.openbridge_connections", "dmr_obp", "OpenBridge trunks", "json", [], restart=True),
    Field("dmr.openbridge_transit", "dmr_obp", "Pass calls between trunks", "bool", True,
          "On: a call coming in over one trunk also goes out over all other trunks that carry the talkgroup. "
          "Loops are stopped by the echo guard and the FreeDMR hop counter."),
]


# ------------------------------------------------------------------ store

class SettingsDB:
    """
    Key/value settings (JSON values) in the settings table.

    query(sql, args) -> rows and execute(sql, args) are provided by the owner,
    so the dashboard can share its database connection and lock.
    """

    def __init__(self, query: Callable, execute: Callable):
        self._q = query
        self._x = execute
        for stmt in _SCHEMA.strip().split(";"):
            if stmt.strip():
                self._x(stmt, ())

    @classmethod
    def open(cls, path: Path) -> "SettingsDB":
        """Own connection, used by the HBlink4 process."""
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.DatabaseError:
            pass
        lock = threading.RLock()

        def q(sql, args=()):
            with lock:
                return conn.execute(sql, args).fetchall()

        def x(sql, args=()):
            with lock:
                return conn.execute(sql, args)
        inst = cls(q, x)
        inst._conn = conn
        return inst

    def stored(self) -> Dict[str, Any]:
        out = {}
        for row in self._q("SELECT key, value FROM settings", ()):
            try:
                out[row["key"]] = json.loads(row["value"])
            except ValueError:
                pass
        return out

    def meta(self) -> Dict[str, Tuple[float, str]]:
        return {r["key"]: (r["updated_at"], r["updated_by"])
                for r in self._q("SELECT key, updated_at, updated_by FROM settings", ())}

    def write(self, key: str, value: Any, who: str) -> None:
        self._x("INSERT OR REPLACE INTO settings (key, value, updated_at, updated_by) VALUES (?,?,?,?)",
                (key, json.dumps(value), time.time(), who))

    def delete(self, key: str) -> None:
        self._x("DELETE FROM settings WHERE key=?", (key,))

    def seed(self, fields: Iterable[Field], file_cfg: Dict[str, Any], source: str,
             fixups: Optional[Callable[[Field, Any], Any]] = None) -> int:
        """
        Store every field that is not in the database yet, from file_cfg (the
        program's config file) or the field default. Never overwrites.
        """
        stored = self.stored()
        n = 0
        for f in fields:
            if f.key in stored:
                continue
            raw = get_path(file_cfg, f.path, f.default)
            if fixups:
                raw = fixups(f, raw)
            try:
                value = validate(f, raw)
            except SettingsError:
                value = f.default
            # INSERT OR IGNORE: the other program may be seeding at the same moment
            self._x("INSERT OR IGNORE INTO settings (key, value, updated_at, updated_by) VALUES (?,?,?,?)",
                    (f.key, json.dumps(value), time.time(), source))
            n += 1
        return n

    def apply(self, fields: Iterable[Field], cfg: Dict[str, Any]) -> None:
        """Write the stored values of fields into cfg (in place)."""
        stored = self.stored()
        for f in fields:
            if f.key in stored:
                set_path(cfg, f.path, stored[f.key])


def resolve_db_path(repo_root: Path, configured: Optional[str] = None) -> Path:
    p = Path(configured or DEFAULT_DB)
    return p if p.is_absolute() else Path(repo_root) / p
