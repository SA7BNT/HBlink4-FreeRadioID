#!/usr/bin/env python3
"""
Helper for scripts/setup.sh -- reads and writes the configuration.

    setup_apply.py current <repo>               current values as JSON (for the defaults)
    setup_apply.py write   <repo> <answers.json> write config files + settings database
    setup_apply.py hash                          password from stdin -> scrypt hash

The config files get only what a first start needs; every value the admin
area manages is also written straight into the settings database
(dashboard/data/dashboard.db), so a re-run of setup.sh on an existing
installation changes the running configuration as well.
"""
import json
import socket
import sys
from pathlib import Path

REPO = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _get(d, path, default=None):
    for p in path.split("."):
        if not isinstance(d, dict) or p not in d:
            return default
        d = d[p]
    return d


def current() -> dict:
    from hblink4.settings_db import SettingsDB, resolve_db_path
    dmr_cfg = _load(REPO / "config" / "config.json")
    dash_cfg = _load(REPO / "dashboard" / "config.json")
    db_path = resolve_db_path(REPO, _get(dmr_cfg, "global.settings_db"))
    stored = {}
    if db_path.exists():
        try:
            stored = SettingsDB.open(db_path).stored()
        except Exception:
            stored = {}

    def val(key, file_cfg, path, default):
        if key in stored:
            return stored[key]
        return _get(file_cfg, path, default)

    def tgs(v):
        return "all" if v is None else ("none" if v == [] else ", ".join(str(x) for x in v))

    host = socket.getfqdn() or "dmr.example.org"
    return {
        "server_name": val("server_name", dash_cfg, "server_name", "dmr.die-n8schicht.de" if "n8schicht" in host else host),
        "dashboard_title": val("dashboard_title", dash_cfg, "dashboard_title", "DMR Die N8schicht"),
        "server_description": val("server_description", dash_cfg, "server_description", "N8schicht DMR Network"),
        "dmr_port": val("dmr.global.port_ipv4", dmr_cfg, "global.port_ipv4", 62031),
        "web_port": val("web.port", dash_cfg, "web.port", 8080),
        "passphrase": val("dmr.repeater_configurations.default.passphrase", dmr_cfg,
                          "repeater_configurations.default.passphrase", "passw0rd"),
        "ts1": tgs(val("dmr.repeater_configurations.default.slot1_talkgroups", dmr_cfg,
                       "repeater_configurations.default.slot1_talkgroups", [262, 2620, 26500])),
        "ts2": tgs(val("dmr.repeater_configurations.default.slot2_talkgroups", dmr_cfg,
                       "repeater_configurations.default.slot2_talkgroups", [262, 2620, 26500])),
        "frid": bool(val("dmr.freeradioid.enabled", dmr_cfg, "freeradioid.enabled", True)),
        "guest_tgs": tgs(val("dmr.freeradioid.guest_talkgroups", dmr_cfg, "freeradioid.guest_talkgroups",
                             [26501, 26502, 26503])),
        "public": bool(val("auth.public_dashboard", dash_cfg, "auth.public_dashboard", True)),
        "admin_user": _get(dash_cfg, "auth.superadmin.username", "") or "admin",
        "admin_has_password": bool(_get(dash_cfg, "auth.superadmin.password_hash") or
                                   _get(dash_cfg, "auth.superadmin.password")),
        "https": bool(val("https.enabled", dash_cfg, "https.enabled", False)),
        "domain": val("https.domain", dash_cfg, "https.domain", "") or "",
        "email": val("https.email", dash_cfg, "https.email", "") or "",
    }


def write(answers: dict) -> None:
    from dashboard.settings import FIELD_BY_KEY
    from hblink4.settings_db import SettingsDB, SettingsError, resolve_db_path, validate

    a = answers
    # ---- config/config.json: first-start minimum
    dmr_path = REPO / "config" / "config.json"
    dmr = _load(dmr_path) or _load(REPO / "config" / "config_sample.json")
    g = dmr.setdefault("global", {})
    g.setdefault("bind_ipv4", "0.0.0.0")
    g["port_ipv4"] = int(a["dmr_port"])
    g.setdefault("settings_db", "dashboard/data/dashboard.db")
    g.setdefault("logging", {"file": "logs/hblink.log", "console_level": "INFO", "file_level": "INFO", "retention_days": 14})
    dmr.setdefault("dashboard", {"enabled": True, "transport": "unix", "unix_socket": "/tmp/hblink4.sock"})
    rc = dmr.setdefault("repeater_configurations", {}).setdefault("default", {})
    rc["passphrase"] = a["passphrase"]
    rc["slot1_talkgroups"] = validate(FIELD_BY_KEY["dmr.repeater_configurations.default.slot1_talkgroups"], a["ts1"])
    rc["slot2_talkgroups"] = validate(FIELD_BY_KEY["dmr.repeater_configurations.default.slot2_talkgroups"], a["ts2"])
    fr = dmr.setdefault("freeradioid", {})
    fr["enabled"] = bool(a["frid"])
    fr["guest_talkgroups"] = validate(FIELD_BY_KEY["dmr.freeradioid.guest_talkgroups"], a["guest_tgs"])
    dmr_path.parent.mkdir(parents=True, exist_ok=True)
    dmr_path.write_text(json.dumps(dmr, indent=4, ensure_ascii=False) + "\n")

    # ---- dashboard/config.json: first-start minimum + superadmin
    dash_path = REPO / "dashboard" / "config.json"
    dash = _load(dash_path) or _load(REPO / "dashboard" / "config_sample.json")
    sa = dash.setdefault("auth", {}).setdefault("superadmin", {})
    sa["username"] = a["admin_user"]
    if a.get("admin_hash"):
        sa["password_hash"] = a["admin_hash"]
        sa["password"] = ""
    dash.setdefault("web", {"bind": "0.0.0.0"})["port"] = int(a["web_port"])
    dash["dashboard_title"] = a["dashboard_title"]
    dash["server_name"] = a["server_name"]
    dash["server_description"] = a["server_description"]
    contact = a.get("email") or f"sysop@{a['server_name']}"
    dash.setdefault("user_database", {})["user_agent"] = f"HBlink4-Dashboard {a['dashboard_title']} (contact={contact})"
    h = dash.setdefault("https", {})
    h["enabled"] = bool(a["https"])
    h["domain"] = a["domain"]
    h["email"] = a["email"]
    dash_path.write_text(json.dumps(dash, indent=4, ensure_ascii=False) + "\n")

    # ---- settings database: the values that count after the first start
    db_path = resolve_db_path(REPO, g.get("settings_db"))
    db = SettingsDB.open(db_path)
    values = {
        "dashboard_title": a["dashboard_title"], "server_name": a["server_name"],
        "server_description": a["server_description"], "web.port": a["web_port"],
        "auth.public_dashboard": a["public"],
        "https.enabled": a["https"], "https.domain": a["domain"], "https.email": a["email"],
        "dmr.global.port_ipv4": a["dmr_port"],
        "dmr.repeater_configurations.default.passphrase": a["passphrase"],
        "dmr.repeater_configurations.default.slot1_talkgroups": a["ts1"],
        "dmr.repeater_configurations.default.slot2_talkgroups": a["ts2"],
        "dmr.freeradioid.enabled": a["frid"],
        "dmr.freeradioid.guest_talkgroups": a["guest_tgs"],
    }
    for key, raw in values.items():
        try:
            db.write(key, validate(FIELD_BY_KEY[key], raw), "setup.sh")
        except SettingsError as e:
            print(f"✗ {e}", file=sys.stderr)
            sys.exit(2)


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "current":
        print(json.dumps(current(), ensure_ascii=False))
    elif cmd == "write":
        write(json.loads(Path(sys.argv[3]).read_text()))
    elif cmd == "hash":
        from dashboard.auth import hash_password
        print(hash_password(sys.stdin.read().rstrip("\n")))
    else:
        print(__doc__, file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
