"""
Configuration converter: takes the configuration of another DMR server and
proposes the matching HBlink4 settings for the admin area.

Supported sources
-----------------
* HBlink3 ``hblink.cfg`` (INI) and optionally its ``rules.py`` (conference bridges)
* FreeDMR / FDMR+ / FDMR-Secure-Server ``hblink.cfg`` / ``freedmr.cfg`` (INI)
* ADN DMR Peer Server ``adn-server.yaml`` (YAML, same keys as FreeDMR)
* HBlink4 ``config/config.json`` (JSON) – e.g. from another HBlink4 installation

Nothing is executed: INI via configparser, YAML via yaml.safe_load, rules.py via
``ast`` (only the literal ``BRIDGES = {...}``), JSON via json.

The result is a list of *items*. Each item proposes one setting (or adds entries
to a list setting such as OpenBridge trunks) and carries a short explanation and
warnings. The admin picks the items and applies them; the normal settings
validation (incl. HBlink4's own trunk/pattern checks) runs on apply.
"""
from __future__ import annotations

import ast
import configparser
import json
import re
from typing import Any, Dict, List, Optional, Tuple

MAX_INPUT = 512 * 1024

FORMATS = {
    "auto": "Auto-detect",
    "hblink3": "HBlink3 – hblink.cfg (+ rules.py)",
    "freedmr": "FreeDMR / FDMR+ / FDMR-Secure-Server – hblink.cfg",
    "adn": "ADN DMR Peer Server – adn-server.yaml",
    "hblink4": "HBlink4 – config/config.json",
}

_SHORT = {"hblink3": "HBlink3", "freedmr": "FreeDMR", "adn": "ADN", "hblink4": "HBlink4"}

# Sections that have no counterpart in HBlink4 (with the reason shown to the admin)
_SKIP_SECTIONS = {
    "REPORTS": "Reporting socket for HBmonitor – HBlink4 has its own dashboard.",
    "LOGGER": "Logging – HBlink4 logs to the systemd journal (journalctl -u hblink4).",
    "ALIASES": "ID/talkgroup lists – the dashboard loads callsigns and the N8schicht talkgroup list itself.",
    "MYSQL": "MySQL login – HBlink4 uses SQLite.",
    "ALLSTAR": "AllStar control – not part of HBlink4.",
    "PROXY": "Hotspot proxy – HBlink4 accepts hotspots directly on its DMR port.",
    "OBP_PROXY": "OBP proxy – not needed, every HBlink4 trunk has its own port.",
    "DATABASE": "Database login – HBlink4 uses SQLite.",
    "SELF_SERVICE": "Self-service passwords – use the FreeRadioID secure login instead.",
}


class ConvertError(Exception):
    """Input that cannot be read; the message is user-facing."""


# ------------------------------------------------------------------ parsing
def _parse_ini(text: str) -> Dict[str, Dict[str, str]]:
    cp = configparser.ConfigParser(interpolation=None, strict=False,
                                   inline_comment_prefixes=None, delimiters=(":", "="))
    cp.optionxform = str.upper  # keys as in the file, upper case
    try:
        cp.read_string(text)
    except configparser.Error as e:
        raise ConvertError(f"The file is not a valid hblink.cfg: {str(e).splitlines()[0]}")
    return {s: {k: v.strip() for k, v in cp.items(s)} for s in cp.sections()}


def _parse_yaml(text: str) -> Dict[str, Any]:
    try:
        import yaml
    except ImportError:
        raise ConvertError("Reading YAML needs PyYAML: ./venv/bin/pip install pyyaml")
    try:
        data = yaml.safe_load(text)
    except Exception as e:
        raise ConvertError(f"The YAML file cannot be read: {str(e).splitlines()[0]}")
    if not isinstance(data, dict):
        raise ConvertError("The YAML file does not contain a configuration.")
    return data


def parse_rules(text: str) -> Dict[str, List[Dict[str, Any]]]:
    """BRIDGES = {...} from an HBlink3/FreeDMR rules.py, without executing it."""
    if not text or not text.strip():
        return {}
    try:
        tree = ast.parse(text)
    except SyntaxError as e:
        raise ConvertError(f"rules.py cannot be read (line {e.lineno}).")
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "BRIDGES" for t in node.targets):
            try:
                bridges = ast.literal_eval(node.value)
            except ValueError:
                raise ConvertError("BRIDGES in rules.py contains code instead of plain values.")
            if not isinstance(bridges, dict):
                raise ConvertError("BRIDGES in rules.py is not a dictionary.")
            return {str(k): [e for e in v if isinstance(e, dict)] for k, v in bridges.items() if isinstance(v, list)}
    return {}


def detect(text: str) -> str:
    t = text.lstrip()
    if t.startswith("{"):
        return "hblink4"
    if re.search(r"^\s*SYSTEMS\s*:\s*$", text, re.M) and re.search(r"^\s*GLOBAL\s*:\s*$", text, re.M):
        return "adn"
    if re.search(r"^\s*\[GLOBAL\]", text, re.M | re.I):
        if re.search(r"^\s*(SERVER_ID|ENHANCED_OBP|PROTO_VER|SINGLE_MODE|TS1_STATIC|GEN_STAT_BRIDGES)\s*[:=]", text, re.M | re.I):
            return "freedmr"
        return "hblink3"
    raise ConvertError("Unknown format. Expected an hblink.cfg (HBlink3/FreeDMR), an ADN YAML file or an HBlink4 config.json.")


# ------------------------------------------------------------------ helpers
def _bool(v: Any, default: bool = False) -> bool:
    if isinstance(v, bool):
        return v
    if v is None or str(v).strip() == "":
        return default
    return str(v).strip().lower() in ("true", "yes", "1", "on")


def _int(v: Any, default: Optional[int] = None) -> Optional[int]:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


def parse_acl(acl: Any) -> Tuple[str, List[Tuple[int, int]]]:
    """'PERMIT:1,2,10-20' -> ('PERMIT', [(1,1),(2,2),(10,20)]); 'PERMIT:ALL' -> ('PERMIT', [])"""
    s = str(acl or "").strip()
    if not s:
        return "PERMIT", []
    action, _, rest = s.partition(":")
    action = action.strip().upper()
    if action not in ("PERMIT", "DENY"):
        return "PERMIT", []
    if rest.strip().upper() == "ALL":
        return action, []
    out = []
    for part in rest.split(","):
        part = part.strip()
        if not part:
            continue
        a, _, b = part.partition("-")
        lo, hi = _int(a), _int(b) if b else _int(a)
        if lo is not None and hi is not None:
            out.append((min(lo, hi), max(lo, hi)))
    return action, out


def _expand(ranges: List[Tuple[int, int]], limit: int = 3000) -> Optional[List[int]]:
    total = sum(hi - lo + 1 for lo, hi in ranges)
    if total > limit:
        return None
    out: List[int] = []
    for lo, hi in ranges:
        out.extend(range(lo, hi + 1))
    return sorted(set(out))


def _tg_csv(v: Any) -> List[int]:
    return [t for t in (_int(x) for x in re.split(r"[,\s;]+", str(v or "")) if x) if t]


def _deny_pattern(name: str, ranges: List[Tuple[int, int]], reason: str) -> Dict[str, Any]:
    ids = [lo for lo, hi in ranges if lo == hi]
    rng = [[lo, hi] for lo, hi in ranges if lo != hi]
    match: Dict[str, Any] = {}
    if ids:
        match["ids"] = ids
    if rng:
        match["id_ranges"] = rng
    return {"name": name, "description": "Imported", "match": match, "reason": reason}


class _Out:
    def __init__(self):
        self.items: List[Dict[str, Any]] = []
        self.skipped: List[Dict[str, str]] = []
        self.notes: List[str] = []

    def item(self, group: str, title: str, key: str, value: Any, detail: str = "", *, merge: str = "",
             selected: bool = True, warnings: Optional[List[str]] = None, show: Any = None) -> None:
        self.items.append({"id": f"i{len(self.items) + 1}", "group": group, "title": title, "key": key,
                           "value": value, "detail": detail, "merge": merge, "selected": selected,
                           "warnings": warnings or [], "show": show if show is not None else value})

    def skip(self, section: str, reason: str) -> None:
        self.skipped.append({"section": section, "reason": reason})


# ------------------------------------------------------------------ hblink.cfg style (HBlink3, FreeDMR, ADN)
def _convert_systems(glob: Dict[str, Any], systems: Dict[str, Dict[str, Any]], other: Dict[str, Any],
                     flavor: str, bridges: Dict[str, List[Dict[str, Any]]], out: _Out) -> None:
    fdmr = flavor in ("freedmr", "adn")
    server_id = _int(glob.get("SERVER_ID"), 0) or 0

    for sec in other:
        if sec.upper() in _SKIP_SECTIONS:
            out.skip(sec, _SKIP_SECTIONS[sec.upper()])
        elif sec.upper() != "GLOBAL":
            out.skip(sec, "Not used by HBlink4.")

    # talkgroups per system and timeslot from the conference bridges (rules.py)
    by_sys: Dict[str, Dict[int, set]] = {}
    for bname, entries in bridges.items():
        for e in entries:
            sysname, ts, tg = str(e.get("SYSTEM", "")), _int(e.get("TS"), 0), _int(e.get("TGID"), 0)
            if sysname and ts in (1, 2) and tg:
                by_sys.setdefault(sysname, {1: set(), 2: set()})[ts].add(tg)
    if bridges:
        out.notes.append(f"rules.py: {len(bridges)} conference bridge{'' if len(bridges) == 1 else 's'} read. HBlink4 has no conference bridges; "
                         "their talkgroups become the talkgroup lists of the master, the trunks and the links.")

    # -- global
    mm = _int(glob.get("MAX_MISSED"))
    if mm and 1 <= mm <= 20:
        out.item("🛰️ DMR server", "Missed pings before a station is dropped", "dmr.global.max_missed", mm,
                 f"GLOBAL → MAX_MISSED: {mm}")
    if _bool(glob.get("USE_ACL"), True):
        act, rng = parse_acl(glob.get("SUB_ACL"))
        if act == "DENY" and rng and rng != [(1, 1)]:
            out.item("🚫 Blacklist", "Blocked radio IDs (global)", "dmr.blacklist.patterns",
                     [_deny_pattern("Imported: GLOBAL SUB_ACL", rng, "Blocked in the old server (SUB_ACL)")],
                     f"GLOBAL → SUB_ACL: {glob.get('SUB_ACL')}", merge="name")

    masters = [(n, s) for n, s in systems.items() if str(s.get("MODE", "")).upper() == "MASTER"]
    active_masters = [(n, s) for n, s in masters if _bool(s.get("ENABLED"), True)]
    obps = [(n, s) for n, s in systems.items() if str(s.get("MODE", "")).upper() == "OPENBRIDGE"]
    peers = [(n, s) for n, s in systems.items() if str(s.get("MODE", "")).upper() in ("PEER", "XLXPEER")]

    # -- master -> DMR port, password, talkgroups
    if active_masters:
        # FreeDMR: the hotspot-facing master is usually "SYSTEM" behind the proxy (62031)
        name, m = next(((n, s) for n, s in active_masters if n.upper() == "SYSTEM"), active_masters[0])
        port = _int(m.get("PORT"))
        if fdmr and name.upper() == "SYSTEM":
            proxy = other.get("PROXY") if isinstance(other.get("PROXY"), dict) else {}
            port = _int(proxy.get("LISTEN_PORT"), 62031) if proxy else 62031
            detail = f"[{name}] is behind the hotspot proxy; hotspots use port {port}"
        else:
            detail = f"[{name}] → PORT: {m.get('PORT')}"
        if port and 1 <= port <= 65535:
            out.item("🛰️ DMR server", "DMR port for repeaters and hotspots", "dmr.global.port_ipv4", port, detail,
                     warnings=[] if port == 62031 else ["Hotspots must then use this port instead of 62031."])
        pw = str(m.get("PASSPHRASE") or "")
        if pw and not pw.startswith("<"):
            out.item("🔑 Access", "Default passphrase (hotspot password)", "dmr.repeater_configurations.default.passphrase",
                     pw, f"[{name}] → PASSPHRASE", show="••••••••",
                     warnings=["With the FreeRadioID login on, this becomes the guest password."])
        elif fdmr and _bool(glob.get("ALLOW_NULL_PASSPHRASE")):
            out.notes.append("The old server allowed hotspots without password (ALLOW_NULL_PASSPHRASE). "
                             "HBlink4 needs a passphrase; with the FreeRadioID login, registered users log in with their own.")
        hang = _int(m.get("GROUP_HANGTIME"))
        if hang is not None and 0 <= hang <= 120:
            out.item("🛰️ DMR server", "Talkgroup hang time (s)", "dmr.global.stream_hang_time", float(hang),
                     f"[{name}] → GROUP_HANGTIME: {hang}")
        for ts in (1, 2):
            static = _tg_csv(m.get(f"TS{ts}_STATIC")) if fdmr else []
            act, rng = parse_acl(m.get(f"TGID_TS{ts}_ACL"))
            from_rules = sorted(by_sys.get(name, {}).get(ts, set()))
            warn: List[str] = []
            if static:
                tgs, src = static, f"[{name}] → TS{ts}_STATIC: {m.get(f'TS{ts}_STATIC')}"
            elif from_rules:
                tgs, src = from_rules, f"rules.py: bridges of [{name}] on TS{ts}"
            elif act == "PERMIT" and rng:
                tgs = _expand(rng)
                src = f"[{name}] → TGID_TS{ts}_ACL: {m.get(f'TGID_TS{ts}_ACL')}"
                if tgs is None:
                    tgs, warn = None, ["The ACL is a large range; imported as „all talkgroups“."]
            else:
                tgs, src = None, f"[{name}] → TGID_TS{ts}_ACL: {m.get(f'TGID_TS{ts}_ACL') or 'PERMIT:ALL'}"
                if act == "DENY" and rng:
                    warn = ["DENY lists cannot be taken over; imported as „all talkgroups“."]
            if fdmr and not static:
                warn.append("FreeDMR hotspots choose their talkgroups themselves (dial-a-TG). "
                            "In HBlink4 switch on „Trust TGs requested by the device“ for that.")
            out.item("🔑 Access", f"Talkgroups TS{ts}", f"dmr.repeater_configurations.default.slot{ts}_talkgroups",
                     tgs, src, warnings=warn, show="all" if tgs is None else ", ".join(map(str, tgs)))
        if fdmr:
            out.item("🔑 Access", "Trust TGs requested by the device (RPTO)", "dmr.repeater_configurations.default.trust",
                     True, "FreeDMR lets every hotspot pick its talkgroups", selected=False)
        reg_act, reg_rng = parse_acl(m.get("REG_ACL"))
        if reg_act == "DENY" and reg_rng and reg_rng != [(1, 1)]:
            out.item("🚫 Blacklist", f"Blocked repeater IDs ([{name}])", "dmr.blacklist.patterns",
                     [_deny_pattern(f"Imported: {name} REG_ACL", reg_rng, "Not allowed to register (REG_ACL)")],
                     f"[{name}] → REG_ACL: {m.get('REG_ACL')}", merge="name")
        sub_act, sub_rng = parse_acl(m.get("SUB_ACL"))
        if sub_act == "DENY" and sub_rng and sub_rng != [(1, 1)]:
            out.item("🚫 Blacklist", f"Blocked radio IDs ([{name}])", "dmr.blacklist.patterns",
                     [_deny_pattern(f"Imported: {name} SUB_ACL", sub_rng, "Blocked in the old server (SUB_ACL)")],
                     f"[{name}] → SUB_ACL: {m.get('SUB_ACL')}", merge="name")
        for n, s in masters:
            if n != name:
                out.skip(n, f"Additional MASTER (port {s.get('PORT')}): HBlink4 has one DMR port for all stations. "
                            "Own passwords/talkgroups per station are possible with patterns.")
    elif masters:
        out.notes.append("All MASTER sections are disabled; DMR port and password were not taken over.")

    # -- OpenBridge
    trunks: List[Dict[str, Any]] = []
    wildcard_taken = False
    for name, o in obps:
        enabled = _bool(o.get("ENABLED"), True)
        proto = _int(o.get("PROTO_VER"), 1) or 1
        enhanced = _bool(o.get("ENHANCED_OBP"), False) if fdmr else False
        warn: List[str] = []
        if fdmr and server_id:
            network_id = server_id
            warn.append(f"FreeDMR sends its SERVER_ID ({server_id}) as the network ID; the stanza's NETWORK_ID "
                        f"({o.get('NETWORK_ID')}) is the far end's ID.")
        else:
            network_id = _int(o.get("NETWORK_ID"), 0) or 0
        if not network_id:
            network_id = 1
            warn.append("No network ID found – please enter the one agreed with the far end.")
        target = str(o.get("TARGET_IP") or "").strip()
        if not target:
            warn.append("TARGET_IP is empty (the far end connects to us) – please enter its address.")
            target = "set.me.example"
        t: Dict[str, Any] = {
            "enabled": enabled, "name": name, "description": f"Imported from {_SHORT[flavor]}",
            "network_id": network_id, "local_address": str(o.get("IP") or "0.0.0.0").strip() or "0.0.0.0",
            "local_port": _int(o.get("PORT"), 62035) or 62035, "target_address": target,
            "target_port": _int(o.get("TARGET_PORT"), 62035) or 62035,
            "passphrase": str(o.get("PASSPHRASE") or "")[:80], "preserve_source_peer": False,
        }
        if not t["passphrase"] or t["passphrase"].startswith("<"):
            t["passphrase"] = "change-me"
            warn.append("No passphrase in the file – please enter it.")
        if fdmr and (enhanced or proto > 1):
            t.update({"enhanced_obp": enhanced, "proto_ver": max(2, proto) if proto > 1 else 1,
                      "relax_checks": _bool(o.get("RELAX_CHECKS"), False)})
        # talkgroups: ACL list, else rules.py, else everything
        act, rng = parse_acl(o.get("TGID_ACL") or o.get("TGID_TS1_ACL"))
        rules_tgs = by_sys.get(name, {})
        rules_list = sorted(rules_tgs.get(1, set()) | rules_tgs.get(2, set()))
        tg_list = _expand(rng) if act == "PERMIT" and rng else None
        if tg_list:
            t["talkgroup_slots"] = {str(tg): "2" for tg in tg_list}
        elif rules_list:
            t["talkgroup_slots"] = {str(tg): "2" for tg in rules_list}
            warn.append(f"Talkgroups from rules.py ({len(rules_list)}); all on TS2 – adjust if needed.")
        else:
            if act == "DENY" and rng:
                warn.append("DENY lists cannot be taken over; the trunk carries all talkgroups.")
            if not enabled or not wildcard_taken:
                t.update({"all_talkgroups": True, "default_slot": 2, "talkgroup_slots": {}})
                wildcard_taken = wildcard_taken or enabled
            else:
                t.update({"all_talkgroups": True, "default_slot": 2, "talkgroup_slots": {}, "enabled": False})
                warn.append("Only one active trunk may carry all talkgroups – imported switched off.")
        trunks.append(t)
        out.item("↔️ OpenBridge", f"Trunk {name}", "dmr.openbridge_connections", [t],
                 f"{'FreeDMR v%d' % t.get('proto_ver', 1) if t.get('proto_ver', 1) > 1 or t.get('enhanced_obp') else 'Classic OBP'}"
                 f" · {target}:{t['target_port']} · our port {t['local_port']} · "
                 f"{'all TGs' if t.get('all_talkgroups') else str(len(t.get('talkgroup_slots', {}))) + ' TGs'}"
                 f"{'' if enabled else ' · switched off'}",
                 merge="name", selected=True, warnings=warn, show=None)

    # -- peers (links to other masters)
    for name, p in peers:
        mode = str(p.get("MODE", "")).upper()
        enabled = _bool(p.get("ENABLED"), True)
        warn = []
        rid = _int(p.get("RADIO_ID"), 0) or 0
        addr = str(p.get("MASTER_IP") or "").strip()
        if name.upper() == "ECHOTEST" or addr in ("127.0.0.1", "localhost", "::1"):
            out.skip(name, "Local helper (echo/parrot via loopback) – HBlink4 does not need it.")
            continue
        rules_tgs = by_sys.get(name, {})
        if mode == "XLXPEER":
            options = ""
            warn.append(f"XLX link (module {p.get('XLXMODULE') or '?'}): HBlink4 connects like a hotspot – "
                        "select the module on the XLX side after the import.")
        else:
            options = str(p.get("OPTIONS") or "").strip()
            if not options and (rules_tgs.get(1) or rules_tgs.get(2)):
                options = ";".join(f"TS{ts}=" + ",".join(map(str, sorted(rules_tgs[ts])))
                                   for ts in (1, 2) if rules_tgs.get(ts))
        link = {
            "enabled": enabled, "name": name, "address": addr or "set.me.example",
            "port": _int(p.get("MASTER_PORT"), 62031) or 62031, "radio_id": rid or 1,
            "passphrase": str(p.get("PASSPHRASE") or "passw0rd"), "callsign": str(p.get("CALLSIGN") or ""),
            "options": options, "description": str(p.get("DESCRIPTION") or "Imported link")[:200],
            "location": str(p.get("LOCATION") or "")[:100],
            "rx_frequency": _int(p.get("RX_FREQ"), 0) or 0, "tx_frequency": _int(p.get("TX_FREQ"), 0) or 0,
            "colorcode": _int(p.get("COLORCODE"), 1) or 1,
        }
        if not rid:
            warn.append("RADIO_ID missing – please enter it.")
        out.item("🔗 Links to other servers", f"Link {name}", "dmr.outbound_connections", [link],
                 f"{mode} → {link['address']}:{link['port']} as {link['callsign'] or rid}"
                 f"{'' if enabled else ' · switched off'}",
                 merge="name", selected=enabled, warnings=warn, show=None)


def _norm_yaml_systems(data: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Dict[str, Any]], Dict[str, Any]]:
    glob = {str(k).upper(): v for k, v in (data.get("GLOBAL") or {}).items()}
    systems = {str(n): {str(k).upper(): v for k, v in (s or {}).items()} for n, s in (data.get("SYSTEMS") or {}).items()}
    other = {k: v for k, v in data.items() if k not in ("GLOBAL", "SYSTEMS")}
    return glob, systems, other


# ------------------------------------------------------------------ HBlink4 config.json
_HB4_KEYS = [
    ("🛰️ DMR server", "global.port_ipv4", "DMR port"), ("🛰️ DMR server", "global.max_missed", "Missed pings"),
    ("🛰️ DMR server", "global.timeout_duration", "Ping timeout"), ("🛰️ DMR server", "global.stream_timeout", "Stream timeout"),
    ("🛰️ DMR server", "global.stream_hang_time", "Hang time"),
    ("🔑 Access", "repeater_configurations.default.passphrase", "Default passphrase"),
    ("🔑 Access", "repeater_configurations.default.slot1_talkgroups", "Talkgroups TS1"),
    ("🔑 Access", "repeater_configurations.default.slot2_talkgroups", "Talkgroups TS2"),
    ("🔑 Access", "repeater_configurations.default.trust", "Trust device TGs"),
    ("🛡️ FreeRadioID", "freeradioid.enabled", "FreeRadioID login"),
    ("🛡️ FreeRadioID", "freeradioid.guest_talkgroups", "Guest talkgroups"),
]


def _convert_hblink4(data: Dict[str, Any], out: _Out) -> None:
    def get(path):
        cur = data
        for p in path.split("."):
            if not isinstance(cur, dict) or p not in cur:
                return KeyError
            cur = cur[p]
        return cur
    for group, path, label in _HB4_KEYS:
        v = get(path)
        if v is KeyError:
            continue
        out.item(group, label, "dmr." + path, v, f"config.json → {path}",
                 show="••••••••" if path.endswith("passphrase") else ("all" if v is None else v))
    for path, group, title in (("repeater_configurations.patterns", "🧩 Patterns", "Pattern"),
                               ("blacklist.patterns", "🚫 Blacklist", "Blacklist entry"),
                               ("outbound_connections", "🔗 Links to other servers", "Link"),
                               ("openbridge_connections", "↔️ OpenBridge", "Trunk")):
        v = get(path)
        if not isinstance(v, list):
            continue
        for e in v:
            if isinstance(e, dict) and e.get("name"):
                out.item(group, f"{title} {e['name']}", "dmr." + path, [e], e.get("description", ""), merge="name", show=None)
    for k in data:
        if k not in ("global", "repeater_configurations", "blacklist", "outbound_connections",
                     "openbridge_connections", "freeradioid", "dashboard"):
            out.skip(k, "Start-up setting of HBlink4 – stays in config/config.json.")


# ------------------------------------------------------------------ entry point
def analyze(text: str, fmt: str = "auto", rules: str = "") -> Dict[str, Any]:
    if not text or not text.strip():
        raise ConvertError("Please paste or upload a configuration first.")
    if len(text) > MAX_INPUT or len(rules or "") > MAX_INPUT:
        raise ConvertError("The file is too large (max. 512 KB).")
    flavor = detect(text) if fmt in ("", "auto") else fmt
    if flavor not in FORMATS or flavor == "auto":
        raise ConvertError("Unknown format.")
    out = _Out()
    bridges = parse_rules(rules) if rules else {}
    if flavor == "hblink4":
        try:
            data = json.loads(text)
        except ValueError as e:
            raise ConvertError(f"The JSON file cannot be read: {e}")
        if not isinstance(data, dict):
            raise ConvertError("The JSON file does not contain a configuration.")
        _convert_hblink4(data, out)
    elif flavor == "adn":
        glob, systems, other = _norm_yaml_systems(_parse_yaml(text))
        _convert_systems(glob, systems, other, flavor, bridges, out)
    else:
        ini = _parse_ini(text)
        if "GLOBAL" not in {s.upper() for s in ini}:
            raise ConvertError("No [GLOBAL] section found – is this an hblink.cfg?")
        glob = next(v for k, v in ini.items() if k.upper() == "GLOBAL")
        systems = {k: v for k, v in ini.items() if "MODE" in v}
        other = {k: v for k, v in ini.items() if "MODE" not in v}
        _convert_systems(glob, systems, other, flavor, bridges, out)
    if not out.items:
        out.notes.append("Nothing found that HBlink4 can take over.")
    return {"format": flavor, "format_label": FORMATS[flavor], "items": out.items, "skipped": out.skipped,
            "notes": out.notes, "bridges": len(bridges)}


def merge_list(current: List[Dict[str, Any]], new: List[Dict[str, Any]], key: str = "name") -> List[Dict[str, Any]]:
    """Add entries; an entry with the same name replaces the old one."""
    out = [dict(e) for e in (current or [])]
    for e in new:
        idx = next((i for i, c in enumerate(out) if c.get(key) == e.get(key)), None)
        if idx is None:
            out.append(e)
        else:
            out[idx] = e
    return out


# ------------------------------------------------------------ single OBP stanzas
def parse_obp_stanzas(text: str, own_hosts: Optional[List[str]] = None) -> Dict[str, Any]:
    """OpenBridge sections pasted from an hblink.cfg (FreeDMR/HBlink3), without [GLOBAL].

    If TARGET_IP is one of our own host names, the stanza was written for the far end
    (it points at us): PORT/TARGET_PORT are then swapped and the far end's address is
    left open (mirrored=True) so the caller can keep the one it already knows."""
    if not text or not text.strip():
        raise ConvertError("Nothing pasted.")
    if len(text) > MAX_INPUT:
        raise ConvertError("Too much text (max. 512 KB).")
    ini = _parse_ini(text)
    systems = {k: v for k, v in ini.items() if str(v.get("MODE", "")).upper() == "OPENBRIDGE"}
    if not systems:
        raise ConvertError("No OpenBridge section found (expected [NAME] with MODE: OPENBRIDGE).")
    fdmr = any(k in v for v in systems.values() for k in ("ENHANCED_OBP", "PROTO_VER", "RELAX_CHECKS"))
    out = _Out()
    _convert_systems({}, systems, {}, "freedmr" if fdmr else "hblink3", {}, out)
    own = {h.strip().lower().rstrip(".") for h in (own_hosts or []) if h and h.strip()}
    trunks: List[Dict[str, Any]] = []
    notes: List[str] = []
    for it in out.items:
        if it["key"] != "dmr.openbridge_connections":
            continue
        for t in it["value"]:
            src = next((v for k, v in systems.items() if k == t["name"]), {})
            tip = str(src.get("TARGET_IP") or "").strip().lower().rstrip(".")
            w = [x for x in it.get("warnings") or [] if "TARGET_IP is empty" not in x]
            if tip and tip in own:
                t["local_port"] = _int(src.get("TARGET_PORT"), t["local_port"]) or t["local_port"]
                t["target_port"] = _int(src.get("PORT"), t["target_port"]) or t["target_port"]
                t["target_address"] = ""
                t["mirrored"] = True
                w = [x for x in w if "TARGET_IP" not in x]
                w.append("This is the far end's view (TARGET_IP is our server): ports swapped, "
                         "please check the far end's address.")
            t["description"] = "Pasted stanza"
            trunks.append(t)
            notes += [f"{t['name']}: {x}" for x in w]
    return {"trunks": trunks, "notes": notes}
