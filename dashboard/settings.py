"""
The admin area's settings: dashboard and HBlink4 DMR server, stored in SQLite.

Generic parts (field types, validation, storage) live in hblink4/settings_db.py
so that the DMR server reads the same database. This module adds the dashboard's
own fields and the logic the admin area needs:

* first start: every setting missing from the database is taken from
  dashboard/config.json (dashboard fields) or config/config.json (DMR fields),
  or from its default
* afterwards the database wins; "reset to config file" re-seeds one section
* dashboard settings apply live; DMR settings are pushed to HBlink4 over the
  event link (see dashboard/server.py)

Not here on purpose: the superadmin (dashboard/config.json only) and the bind
addresses / event link, which a program needs before it can read the database.
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from hblink4.settings_db import (  # noqa: E402
    DMR_FIELDS, DMR_SECTIONS, Field, SettingsDB, SettingsError, get_path, set_path, validate,
)

__all__ = ["SettingsStore", "SettingsError", "FIELDS", "FIELD_BY_KEY", "SECTIONS", "SECTION_HELP", "get_path", "set_path",
           "validate", "RADIOID_URL", "FREERADIOID_ALL_URL"]

RADIOID_URL = "https://database.radioid.net/static/user.csv"
FREERADIOID_ALL_URL = "https://freeradioid.net/api/subscriber_ids.csv"

DASH_SECTIONS: List[Tuple[str, str]] = [
    ("appearance", "🎨 Appearance"),
    ("links", "🔗 Link menu"),
    ("announcement", "📢 Announcement"),
    ("access", "🔐 Dashboard access"),
    ("freeradioid", "🛡️ FreeRadioID data"),
    ("userdb", "📘 Callsign database"),
    ("tgstats", "📈 Talkgroups & statistics"),
    ("connect", "🛠️ Configuration tab"),
    ("https", "🔒 HTTPS & web"),
]

# One sentence per section for the admin area (what is this about?)
SECTION_HELP: Dict[str, str] = {
    "appearance": "Titles, colours and logos of the dashboard.",
    "links": "Your own links in the header, e.g. to the network website, a chat group or a wiki.",
    "announcement": "A banner above the dashboard for news and maintenance notices.",
    "access": "Who may see the dashboard and how long a login lasts.",
    "freeradioid": "Avatars, profiles and the FRID tag from FreeRadioID.net.",
    "userdb": "Where callsigns and names for the DMR IDs come from.",
    "tgstats": "Talkgroup statistics and talkgroup names.",
    "connect": "The Configuration tab tells new users how to connect their hotspot.",
    "https": "Encrypted access with a free Let's Encrypt certificate.",
    "dmr_server": "Port and timing of the DMR server.",
    "dmr_access": "Password and talkgroups for repeaters and hotspots.",
    "dmr_frid": "Personal logins via FreeRadioID.net and the guest access.",
    "dmr_advanced": "Rules per station, blocked IDs and links to other servers – for experienced sysops.",
    "dmr_obp": "Trunks to other networks such as FreeDMR.",
}

THEME_PRESETS = (
    ("n8", "🌙 N8 Night (default)"),
    ("ocean", "🌊 Ocean"),
    ("aurora", "🌌 Aurora (green)"),
    ("ember", "🔥 Ember (orange)"),
    ("violet", "🔮 Violet"),
    ("graphite", "⚫ Graphite"),
)

DASH_FIELDS: List[Field] = [
    # -- appearance: titles
    Field("dashboard_title", "appearance", "Title", "text", "DMR Die N8schicht", "Large text next to the logo"),
    Field("server_name", "appearance", "Subtitle", "text", "dmr.die-n8schicht.de", "Small line under the title"),
    Field("server_description", "appearance", "Description", "text", "N8schicht DMR Network",
          "Shown in the Configuration tab"),
    Field("page_title", "appearance", "Browser tab title", "text", "", "Empty = the title"),
    # -- appearance: colours
    Field("theme.preset", "appearance", "Colour profile", "choice", "n8", choices=THEME_PRESETS),
    Field("theme.accent", "appearance", "Accent colour", "color", "", "Buttons, numbers, active tabs. Empty = from the profile"),
    Field("theme.background", "appearance", "Background colour", "color", "", "Empty = from the profile"),
    Field("theme.glow", "appearance", "Background glow", "int", 100, "0 = flat background", min=0, max=150),
    Field("theme.glass", "appearance", "Card brightness", "int", 50, "How bright the glass cards are", min=0, max=100),
    Field("theme.blur", "appearance", "Glass blur effect", "bool", True, "Off = faster on old phones"),
    # -- appearance: logos
    Field("logo", "appearance", "Logo", "image", "", "Empty = N8schicht logo"),
    Field("logo_size", "appearance", "Logo size (px)", "int", 44, min=24, max=96),
    Field("logo_radius", "appearance", "Logo corners (%)", "int", 0, "0 = as drawn, 50 = round", min=0, max=50),
    Field("favicon", "appearance", "Browser icon (favicon)", "image", "", "Empty = same as the logo"),

    # -- link menu
    Field("links.enabled", "links", "Show the link menu", "bool", False),
    Field("links.label", "links", "Menu button text", "text", "🔗 Links", depends="links.enabled"),
    Field("links.style", "links", "Display", "choice", "menu",
          choices=(("menu", "Button with drop-down menu"), ("bar", "Row of buttons under the header")),
          depends="links.enabled"),
    Field("links.items", "links", "Links", "links", [], depends="links.enabled"),
    Field("network_info.enabled", "links", "Extra button in the header", "bool", False,
          "A single button next to the status badges (older option)", advanced=True),
    Field("network_info.button_text", "links", "Button text", "text", "Network Info",
          depends="network_info.enabled", advanced=True),
    Field("network_info.url", "links", "Button link", "url", "", depends="network_info.enabled", advanced=True),

    Field("announcement.enabled", "announcement", "Show announcement banner", "bool", False),
    Field("announcement.text", "announcement", "Announcement text", "longtext", "", depends="announcement.enabled"),

    Field("auth.public_dashboard", "access", "Public dashboard", "bool", True,
          "Off = nothing is visible without login. IP addresses and ports are always admin-only."),
    Field("auth.session_hours", "access", "Session length (hours)", "int", 12, min=1, max=720),
    Field("auth.remember_days", "access", "\"Stay logged in\" (days)", "int", 30, min=1, max=365),

    Field("freeradioid.avatars", "freeradioid", "Show avatars", "bool", True,
          "Images of FreeRadioID-registered IDs, cached in the database"),
    Field("freeradioid.profile_lookup", "freeradioid", "Look up profiles", "bool", True,
          "Name and location in the station detail view"),
    Field("user_database.freeradioid.enabled", "freeradioid", "Load FreeRadioID ID list", "bool", True,
          "Registered IDs get the FRID tag in Last Heard"),

    Field("user_database.source_url", "userdb", "Source", "choice", FREERADIOID_ALL_URL,
          choices=((FREERADIOID_ALL_URL, "FreeRadioID.net (radioid.net + FreeRadioID)"),
                   (RADIOID_URL, "radioid.net only"))),
    Field("user_database.filter.countries", "userdb", "Countries", "countries", "all",
          "all, or comma separated exactly as the list spells them (Germany, Austria, …)"),
    Field("user_database.refresh.time_of_day", "userdb", "Daily refresh at", "time", "03:17"),

    Field("tg_stats.enabled", "tgstats", "Record talkgroup statistics", "bool", True,
          "Every group call (talkgroup, duration, radio ID) is stored in the database for the charts"),
    Field("tg_stats.public", "tgstats", "Visible without login", "bool", True,
          "Off = only logged-in users see the talkgroup and OpenBridge statistics", depends="tg_stats.enabled"),
    Field("tg_stats.retention_days", "tgstats", "Keep calls for (days)", "int", 30, min=1, max=365,
          depends="tg_stats.enabled"),
    Field("talkgroups.list", "tgstats", "Talkgroup list of the N8schicht", "bool", True,
          "Names of ~1400 talkgroups (github.com/dhartmann1979/n8schicht), updated daily with the callsign database"),
    Field("talkgroups.url", "tgstats", "Talkgroup list URL", "text",
          "https://raw.githubusercontent.com/dhartmann1979/n8schicht/main/talkgroup_ids.json",
          "JSON in the format of talkgroup_ids.json", depends="talkgroups.list", advanced=True),
    Field("tg_stats.names", "tgstats", "Own talkgroup names", "longtext", "",
          "One per line: 26500 = N8schicht Ortsrunde. Override the list or add missing talkgroups"),

    Field("connect.enabled", "connect", "Show the Configuration tab", "bool", True,
          "How to connect: server, port, password, talkgroups and ready-made settings"),
    Field("connect.host", "connect", "Server address for hotspots", "text", "",
          "Empty = the subtitle (Appearance → Subtitle)", depends="connect.enabled"),
    Field("connect.show_password", "connect", "Show the default password", "bool", True,
          "With FreeRadioID login this is the guest password. Off = \"ask the sysop\"", depends="connect.enabled"),
    Field("connect.note", "connect", "Note for users", "longtext", "",
          "Shown at the end of the tab: rules, contact, links", depends="connect.enabled"),

    Field("https.enabled", "https", "HTTPS with Let's Encrypt", "bool", False,
          "Needs certbot, the domain pointing here and ports 80/443 open", restart=True),
    Field("https.domain", "https", "Domain", "text", "", restart=True, depends="https.enabled"),
    Field("https.email", "https", "E-mail for Let's Encrypt", "text", "", restart=True, depends="https.enabled"),
    Field("https.staging", "https", "Test certificates (staging)", "bool", False, restart=True,
          depends="https.enabled", advanced=True),
    Field("https.port", "https", "HTTPS port", "int", 443, restart=True, min=1, max=65535,
          depends="https.enabled", advanced=True),
    Field("https.http_port", "https", "HTTP port (challenge + redirect)", "int", 80, restart=True, min=1, max=65535,
          depends="https.enabled", advanced=True),
    Field("web.port", "https", "Plain web port", "int", 8080,
          "Redirects to HTTPS when HTTPS is on", restart=True, min=1, max=65535, advanced=True),
]

FIELDS: List[Field] = DASH_FIELDS + DMR_FIELDS
FIELD_BY_KEY: Dict[str, Field] = {f.key: f for f in FIELDS}
SECTIONS: List[Tuple[str, str]] = DASH_SECTIONS + DMR_SECTIONS

# Values of the original HBlink4 sample configs that nobody chose on purpose
_UPSTREAM_DEFAULTS = {
    "dashboard_title": ("HBlink4 Dashboard",),
    "server_name": ("HBlink4 Server",),
    "server_description": ("Amateur Radio DMR Network",),
    "user_database.filter.countries": (["United States", "Canada"],),
    "user_database.source_url": (RADIOID_URL,),
}
# Values seeded by an older version that switch to the new default as long as
# no admin changed them (updated_by is still the config file)
_MIGRATE_UNTOUCHED = {
    "user_database.source_url": (RADIOID_URL,),
    "user_database.filter.countries": (["United States", "Canada"],),
}
_SEED_SOURCES = ("config.json", "config/config.json", "dashboard/config.json")


class SettingsStore:
    def __init__(self, auth_store, dmr_file_cfg: Optional[Dict[str, Any]] = None):
        """Shares the dashboard database (and its lock) with AuthStore."""
        self._a = auth_store
        self.db = SettingsDB(auth_store._q, auth_store._x)
        self._dmr_file_cfg = dmr_file_cfg or {}

    # -- startup -------------------------------------------------------------
    def seed_and_apply(self, cfg: Dict[str, Any]) -> int:
        """
        First start: store every missing setting (dashboard fields from cfg =
        dashboard/config.json, DMR fields from config/config.json). Then apply
        the dashboard fields over cfg. Returns how many were seeded.
        """
        self._migrate_untouched()
        n = self.db.seed(DASH_FIELDS, cfg, "dashboard/config.json", self._fixup_with_cfg(cfg))
        n += self.db.seed(DMR_FIELDS, self._dmr_file_cfg, "config/config.json")
        self.db.apply(DASH_FIELDS, cfg)
        return n

    @staticmethod
    def _fixup_with_cfg(cfg):
        def fix(f: Field, raw: Any) -> Any:
            if f.key == "freeradioid.avatars" and get_path(cfg, "freeradioid.avatar_url", None) == "":
                return False   # old config switched avatars off with an empty URL
            if raw in _UPSTREAM_DEFAULTS.get(f.key, ()):
                return f.default
            return raw
        return fix

    def _migrate_untouched(self) -> None:
        meta = self.db.meta()
        stored = self.db.stored()
        for key, old_values in _MIGRATE_UNTOUCHED.items():
            if key in stored and stored[key] in old_values and meta.get(key, (0, ""))[1] in _SEED_SOURCES:
                self.db.write(key, FIELD_BY_KEY[key].default, "default")

    # -- admin area ----------------------------------------------------------
    def dmr_values(self) -> Dict[str, Any]:
        stored = self.db.stored()
        return {f.key: stored.get(f.key, f.default) for f in DMR_FIELDS}

    def describe(self, cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
        meta = self.db.meta()
        stored = self.db.stored()
        sections = []
        for sid, title in SECTIONS:
            fields = []
            for f in FIELDS:
                if f.section != sid:
                    continue
                value = stored.get(f.key, f.default) if f.is_dmr else get_path(cfg, f.key, f.default)
                ts, by = meta.get(f.key, (None, ""))
                fields.append({"key": f.key, "label": f.label, "kind": f.kind, "help": f.help,
                               "restart": f.restart, "choices": [list(c) for c in f.choices],
                               "min": f.min, "max": f.max, "value": value, "default": f.default,
                               "depends": f.depends, "advanced": f.advanced,
                               "updated_at": ts, "updated_by": by})
            sections.append({"id": sid, "title": title, "dmr": sid.startswith("dmr_"),
                             "help": SECTION_HELP.get(sid, ""), "fields": fields})
        return sections

    def update(self, cfg: Dict[str, Any], changes: Dict[str, Any], who: str) -> List[str]:
        """Validate everything first, then store. Returns the keys that changed."""
        clean = {}
        for key, raw in changes.items():
            f = FIELD_BY_KEY.get(key)
            if not f:
                raise SettingsError(f"Unknown setting {key}.")
            clean[key] = validate(f, raw)
        self._check_dmr_structure(clean)
        stored = self.db.stored()
        changed = []
        for key, value in clean.items():
            f = FIELD_BY_KEY[key]
            old = stored.get(key) if f.is_dmr else get_path(cfg, key)
            if old != value:
                changed.append(key)
            self.db.write(key, value, who)
            if not f.is_dmr:
                set_path(cfg, key, value)
        return changed

    def _check_dmr_structure(self, clean: Dict[str, Any]) -> None:
        """Patterns, blacklist and trunks must be understood by HBlink4 before they are saved."""
        if "dmr.openbridge_connections" in clean:
            from hblink4.config import parse_openbridge_connections
            try:
                parse_openbridge_connections({"openbridge_connections": clean["dmr.openbridge_connections"]},
                                             raise_errors=True)
            except ValueError as e:
                raise SettingsError(f"OpenBridge: {e}")
        if "dmr.outbound_connections" in clean:
            from hblink4.config import parse_outbound_connections
            try:
                parse_outbound_connections({"outbound_connections": clean["dmr.outbound_connections"]},
                                           raise_errors=True)
            except ValueError as e:
                raise SettingsError(f"Links: {e}")
        if not any(k.startswith("dmr.repeater_configurations") or k.startswith("dmr.blacklist") for k in clean):
            return
        trial: Dict[str, Any] = {}
        merged = {**self.dmr_values(), **clean}
        for f in DMR_FIELDS:
            set_path(trial, f.path, merged.get(f.key, f.default))
        try:
            from hblink4.access_control import RepeaterMatcher
            RepeaterMatcher(trial)
        except Exception as e:
            raise SettingsError(f"Patterns/blacklist not accepted by HBlink4: {e}")

    def reset_section(self, cfg: Dict[str, Any], file_cfg: Dict[str, Any], section: str, who: str) -> None:
        """Drop the stored values of a section and seed them again from its config file."""
        fields = [f for f in FIELDS if f.section == section]
        if not fields:
            raise SettingsError("Unknown section.")
        for f in fields:
            self.db.delete(f.key)
        if fields[0].is_dmr:
            self.db.seed(fields, self._dmr_file_cfg, "config/config.json")
        else:
            fresh = copy.deepcopy(file_cfg)
            self.db.seed(fields, fresh, "dashboard/config.json", self._fixup_with_cfg(fresh))
            self.db.apply(fields, cfg)
