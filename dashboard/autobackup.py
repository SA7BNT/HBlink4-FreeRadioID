"""Encrypted scheduled archives with fixed SFTP/Drive backends, never arbitrary commands."""
from __future__ import annotations

import asyncio
import configparser
import contextlib
import copy
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import backup

HOST_CONFIG = Path("/etc/hblink4-autobackup.json")
AUTO_NAME = re.compile(r"auto-(\d{8}T\d{6}Z)-[a-f0-9]{32}\.tar\.gz\.age\Z")
SECRETS = ("password", "client_secret", "token")
DEFAULT = {"enabled": False, "frequency": "daily", "at": "03:00", "timezone": "UTC", "weekday": 0,
           "keep_daily": 7, "keep_weekly": 4, "bandwidth_mib": 2, "targets": [], "key_exported": False,
           "enabled_since": 0}


class AutoError(backup.BackupError):
    pass


def _tool(args, *, data=None, timeout=900):
    try:
        env = {key: value for key, value in os.environ.items() if not key.startswith("RCLONE_")}
        result = subprocess.run(args, input=data, capture_output=True, timeout=timeout, env=env)
    except (OSError, subprocess.SubprocessError):
        raise AutoError("Backup-Werkzeug konnte nicht ausgeführt werden oder hat das Zeitlimit überschritten.") from None
    if result.returncode:
        # Never forward provider output: OAuth responses may contain tokens.
        error = result.stderr.lower()
        for needle, hint in ((b"invalid_grant", "Google-Drive-Autorisierung ist abgelaufen oder widerrufen. Erneut autorisieren."),
                             (b"invalid_client", "Google OAuth Client ID und Client Secret prüfen."),
                             (b"knownhosts", "SFTP-Hostschlüssel stimmt nicht oder fehlt."),
                             (b"permission denied", "Zugriff verweigert. Zielkonto und Schreibrechte prüfen."),
                             (b"no space left", "Der Zielspeicher ist voll.")):
            if needle in error:
                raise AutoError(hint)
        raise AutoError(f"{Path(args[0]).name}: Vorgang fehlgeschlagen (Exit {result.returncode}). Zugangsdaten, Hostschlüssel und Netzwerk prüfen.")
    return result.stdout


def _atomic_bytes(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex)
    try:
        with open(temp, "xb") as out:
            os.chmod(temp, 0o600)
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


class Vault:
    def __init__(self, root, data, host=None):
        self.root, self.data = Path(root).resolve(), Path(data).resolve()
        self.path = self.data / "autobackup-config.age"
        self._host = host
        self.lock = threading.RLock()

    def host(self):
        if self._host is not None:
            cfg = dict(self._host)
        else:
            try:
                st = HOST_CONFIG.stat()
                if HOST_CONFIG.is_symlink() or st.st_uid != 0 or st.st_mode & 0o022:
                    raise AutoError("AutoBackup-Installation ist nicht geschützt.")
                cfg = json.loads(HOST_CONFIG.read_text())
            except (OSError, ValueError):
                raise AutoError("AutoBackup vorbereiten: sudo ./scripts/install_autobackup.sh") from None
        if cfg.get("root") != str(self.root) or cfg.get("data_dir") != str(self.data):
            raise AutoError("AutoBackup gehört zu einer anderen Installation.")
        key = Path(cfg.get("identity", ""))
        if (not key.is_absolute() or key.is_symlink() or not key.is_file() or key.resolve().is_relative_to(self.root)
                or key.resolve().is_relative_to(self.data) or key.stat().st_mode & 0o077):
            raise AutoError("Der private Schlüssel muss geschützt außerhalb von Programm und Backup-Daten liegen.")
        if not re.fullmatch(r"[a-f0-9]{32}", cfg.get("instance", "")):
            raise AutoError("Ungültige AutoBackup-Installationskennung.")
        if not all(shutil.which(tool) for tool in ("age", "age-keygen", "rclone")):
            raise AutoError("age/rclone fehlen. Bitte sudo ./scripts/install_autobackup.sh ausführen.")
        return cfg

    def read(self):
        with self.lock:
            host = self.host()
            if not self.path.exists():
                return copy.deepcopy(DEFAULT)
            try:
                raw = _tool(["age", "--decrypt", "--identity", host["identity"], str(self.path)], timeout=30)
                cfg = json.loads(raw)
                if not isinstance(cfg, dict) or not DEFAULT.keys() <= cfg.keys() or not isinstance(cfg["targets"], list):
                    raise ValueError()
            except (AutoError, ValueError):
                raise AutoError("Gespeicherte AutoBackup-Zugangsdaten sind auf diesem Server gesperrt. Mit dem ursprünglichen Schlüssel entsperren oder Ziele neu einrichten.") from None
            return cfg

    def write(self, cfg):
        with self.lock:
            host = self.host()
            recipient = backup.identity_recipient(host["identity"])
            raw = _tool(["age", "--encrypt", "--recipient", recipient], data=json.dumps(cfg).encode(), timeout=30)
            _atomic_bytes(self.path, raw)

    def public(self):
        cfg = self.read()
        result = copy.deepcopy(cfg)
        for target in result["targets"]:
            for key in SECRETS:
                target[key + "_set"] = bool(target.pop(key, ""))
        return result

    def refresh_token(self, target_id, token):
        with self.lock:
            cfg = self.read()
            for target in cfg["targets"]:
                if target["id"] == target_id and target["type"] == "drive":
                    target["token"] = token
            self.write(cfg)

    def recover(self, identity):
        backup.identity_recipient(identity)
        try:
            value = json.loads(_tool(["age", "--decrypt", "--identity", str(identity), str(self.path)], timeout=30))
        except (AutoError, ValueError):
            raise AutoError("Der Schlüssel kann die gespeicherten Zugangsdaten nicht entsperren.") from None
        value["enabled"] = False
        self.write(validate(value, DEFAULT))

    def reset(self):
        if self.path.exists():
            folder = self.data / backup.CONTROL
            folder.mkdir(parents=True, exist_ok=True)
            _atomic_bytes(folder / ("locked-autobackup-" + uuid.uuid4().hex + ".age"), self.path.read_bytes())
        self.write(copy.deepcopy(DEFAULT))


def _line(value, label, maximum=500):
    value = str(value or "").strip()
    if len(value) > maximum or any(c in value for c in "\r\n\x00"):
        raise AutoError(f"Ungültiges Feld: {label}")
    return value


def validate(body, previous):
    cfg = copy.deepcopy(DEFAULT)
    for key in ("enabled", "frequency", "at", "timezone", "weekday", "keep_daily", "keep_weekly", "bandwidth_mib"):
        cfg[key] = body.get(key, previous.get(key, cfg[key]))
    if not isinstance(cfg["enabled"], bool) or cfg["frequency"] not in ("daily", "weekly"):
        raise AutoError("Zeitplan: täglich oder wöchentlich auswählen.")
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", str(cfg["at"])):
        raise AutoError("Uhrzeit als HH:MM angeben.")
    try:
        ZoneInfo(cfg["timezone"])
    except (ValueError, TypeError, ZoneInfoNotFoundError):
        raise AutoError("Unbekannte Zeitzone, z. B. Europe/Stockholm oder Europe/Berlin verwenden.") from None
    for key, lo, hi in (("weekday", 0, 6), ("keep_daily", 1, 90), ("keep_weekly", 0, 52), ("bandwidth_mib", 1, 100)):
        try:
            cfg[key] = int(cfg[key])
        except (ValueError, TypeError):
            raise AutoError(f"{key}: ganze Zahl erforderlich.") from None
        if not lo <= cfg[key] <= hi:
            raise AutoError(f"{key}: Wert zwischen {lo} und {hi} verwenden.")
    cfg["key_exported"] = previous.get("key_exported", False)
    cfg["enabled_since"] = previous.get("enabled_since", 0)
    if cfg["enabled"] and not previous.get("enabled"):
        cfg["enabled_since"] = time.time()
    old = {t["id"]: t for t in previous.get("targets", [])}
    targets = body.get("targets", previous.get("targets", []))
    if not isinstance(targets, list) or len(targets) > 5:
        raise AutoError("Maximal fünf SFTP-/Google-Drive-Ziele sind möglich.")
    ids = set()
    for value in targets:
        if not isinstance(value, dict) or value.get("type") not in ("sftp", "drive"):
            raise AutoError("Als Ziel sind ausschließlich SFTP und Google Drive verfügbar.")
        ident = value.get("id") or uuid.uuid4().hex
        if not isinstance(ident, str) or not re.fullmatch(r"[a-f0-9]{32}", ident) or ident in ids:
            raise AutoError("Ungültige oder doppelte Zielkennung.")
        ids.add(ident)
        prior = old.get(ident, {}) if old.get(ident, {}).get("type") == value["type"] else {}
        target = {"id": ident, "type": value["type"], "label": _line(value.get("label"), "Name", 80),
                  "enabled": value.get("enabled", True), "path": _line(value.get("path"), "Zielordner")}
        if not target["label"] or not isinstance(target["enabled"], bool):
            raise AutoError("Zielname und Aktivierung prüfen.")
        if not target["path"] or ".." in PurePosixPath(target["path"]).parts or ":" in target["path"] or "\\" in target["path"]:
            raise AutoError("Einen normalen Zielordner ohne '..', ':' oder Backslash angeben.")
        for key in SECRETS:
            target[key] = str(value.get(key) or prior.get(key) or "")
        if target["type"] == "sftp":
            target["host"] = _line(value.get("host"), "SFTP-Host", 253)
            target["user"] = _line(value.get("user"), "SFTP-Benutzer", 128)
            if not re.fullmatch(r"[A-Za-z0-9._:-]+", target["host"]) or not re.fullmatch(r"[A-Za-z0-9_.@-]+", target["user"]):
                raise AutoError("SFTP-Host und Benutzer prüfen.")
            try:
                target["port"] = int(value.get("port", 22))
            except (ValueError, TypeError):
                raise AutoError("Ungültiger SFTP-Port.") from None
            if not 1 <= target["port"] <= 65535:
                raise AutoError("SFTP-Port: 1–65535.")
            target["key_file"] = _line(value.get("key_file"), "SSH-Schlüsseldatei", 1000)
            if target["key_file"] and not Path(target["key_file"]).is_absolute():
                raise AutoError("SSH-Schlüsseldatei als absoluten Serverpfad angeben.")
            target["known_hosts"] = str(value.get("known_hosts") or "").strip()
            if not target["known_hosts"] or len(target["known_hosts"]) > 16384 or "\x00" in target["known_hosts"]:
                raise AutoError("SFTP benötigt den geprüften SSH-Hostschlüssel als known_hosts-Zeile.")
            if not target["password"] and not target["key_file"]:
                raise AutoError("SFTP-Passwort oder SSH-Schlüsseldatei angeben.")
        else:
            target["client_id"] = _line(value.get("client_id"), "OAuth Client ID", 500)
            target["folder_id"] = _line(value.get("folder_id"), "Drive Folder ID", 200)
            if not target["client_id"] or not target["client_secret"]:
                raise AutoError("Google Drive benötigt einen eigenen OAuth-Client mit Client ID und Client Secret.")
            _line(target["client_secret"], "Client Secret", 2000)
            try:
                token = json.loads(target["token"])
                if not isinstance(token, dict) or not token.get("refresh_token"):
                    raise ValueError()
                target["token"] = json.dumps(token, separators=(",", ":"))
            except (ValueError, TypeError):
                raise AutoError("Google-Drive-Token als JSON mit refresh_token angeben.") from None
            if target["folder_id"] and not re.fullmatch(r"[A-Za-z0-9_-]+", target["folder_id"]):
                raise AutoError("Ungültige Drive Folder ID.")
        if len(target["password"]) > 4096 or len(target["token"]) > 16384:
            raise AutoError("Zugangsdaten sind zu lang.")
        cfg["targets"].append(target)
    if cfg["enabled"] and (not cfg["key_exported"] or not any(t["enabled"] for t in cfg["targets"])):
        raise AutoError("Zuerst den Wiederherstellungsschlüssel exportieren und mindestens ein Ziel einrichten.")
    return cfg


def slot_for(cfg, now):
    zone = ZoneInfo(cfg["timezone"])
    local = now.astimezone(zone)
    hour, minute = map(int, cfg["at"].split(":"))
    date = local.date()
    if cfg["frequency"] == "weekly":
        date -= timedelta(days=(date.weekday() - cfg["weekday"]) % 7)
    due = datetime(date.year, date.month, date.day, hour, minute, tzinfo=zone).astimezone(timezone.utc)
    if due > now:
        date -= timedelta(days=7 if cfg["frequency"] == "weekly" else 1)
        due = datetime(date.year, date.month, date.day, hour, minute, tzinfo=zone).astimezone(timezone.utc)
    if due.timestamp() < cfg.get("enabled_since", 0):
        return None
    return f"{cfg['frequency']}:{cfg['timezone']}:{date}:{cfg['at']}"


def next_due(cfg, now):
    if not cfg["enabled"]:
        return None
    zone = ZoneInfo(cfg["timezone"])
    local = now.astimezone(zone)
    date = local.date()
    if cfg["frequency"] == "weekly":
        date += timedelta(days=(cfg["weekday"] - date.weekday()) % 7)
    hour, minute = map(int, cfg["at"].split(":"))
    due = datetime(date.year, date.month, date.day, hour, minute, tzinfo=zone).astimezone(timezone.utc)
    if due <= now:
        date += timedelta(days=7 if cfg["frequency"] == "weekly" else 1)
        due = datetime(date.year, date.month, date.day, hour, minute, tzinfo=zone).astimezone(timezone.utc)
    return due.timestamp()


def keep_names(entries, cfg):
    """Choose only from verified catalog entries, never unknown remote objects."""
    entries = sorted(entries, key=lambda e: e["created"], reverse=True)
    keep, days, weeks = set(), set(), set()
    zone = ZoneInfo(cfg["timezone"])
    for entry in entries:
        date = datetime.fromtimestamp(entry["created"], zone).date()
        week = date.isocalendar()[:2]
        if date not in days and len(days) < cfg["keep_daily"]:
            keep.add(entry["name"])
            days.add(date)
        if week not in weeks and len(weeks) < cfg["keep_weekly"]:
            keep.add(entry["name"])
            weeks.add(week)
    return keep


class Remote:
    def __init__(self, vault, target, cfg, directory):
        self.vault, self.target, self.cfg = vault, target, cfg
        self.directory = Path(directory)
        self.ini = self.directory / "rclone.conf"
        self.path = target["path"].rstrip("/") + "/hblink4-" + vault.host()["instance"]
        self.base = "target:" + self.path

    def __enter__(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        parser = configparser.RawConfigParser()
        target = self.target
        fields = {"type": target["type"]}
        if target["type"] == "sftp":
            known = self.directory / "known_hosts"
            _atomic_bytes(known, target["known_hosts"].encode() + b"\n")
            fields.update(host=target["host"], user=target["user"], port=str(target["port"]),
                          known_hosts_file=str(known), disable_hashcheck="true")
            if target.get("key_file"):
                fields["key_file"] = target["key_file"]
            if target.get("password"):
                fields["pass"] = _tool(["rclone", "obscure", "-"], data=target["password"].encode(), timeout=10).decode().strip()
        else:
            fields.update(client_id=target["client_id"], client_secret=target["client_secret"],
                          token=target["token"], scope="drive.file")
            if target.get("folder_id"):
                fields["root_folder_id"] = target["folder_id"]
        parser["target"] = fields
        with open(self.ini, "x") as out:
            os.chmod(self.ini, 0o600)
            parser.write(out)
        return self

    def call(self, *args):
        return _tool(["rclone", "--config", str(self.ini), "--transfers", "1", "--checkers", "2",
                      "--bwlimit", f"{self.cfg['bandwidth_mib']}M", "--retries", "3", "--low-level-retries", "3",
                      "--contimeout", "15s", "--timeout", "60s", *map(str, args)], timeout=1800)

    def upload(self, path):
        self.call("copyto", path, self.base + "/" + path.name)
        # --download gives a content comparison even on backends without hashes.
        self.call("check", path.parent, self.base, "--one-way", "--download", "--include", path.name)

    def remove(self, name):
        if not AUTO_NAME.fullmatch(name):
            raise AutoError("Ungültiger AutoBackup-Dateiname.")
        self.call("deletefile", self.base + "/" + name)

    def listing(self):
        return json.loads(self.call("lsjson", self.base, "--files-only", "--max-depth", "1"))

    def __exit__(self, *exc):
        try:
            if self.target["type"] == "drive":
                parser = configparser.RawConfigParser()
                parser.read(self.ini)
                token = parser.get("target", "token", fallback="")
                if token and token != self.target["token"]:
                    self.vault.refresh_token(self.target["id"], token)
                    self.target["token"] = token
        finally:
            self.ini.unlink(missing_ok=True)
            (self.directory / "known_hosts").unlink(missing_ok=True)


class AutoBackup:
    def __init__(self, root, data, create, lock, idle, host=None):
        self.root, self.data = Path(root), Path(data)
        self.vault = Vault(root, data, host)
        self.create, self.lock, self.idle = create, lock, idle
        self.control = self.data / backup.CONTROL
        self.state_path = self.control / "autobackup-state.json"
        self.task = None

    def state(self):
        try:
            return json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return {"status": "idle", "history": [], "catalog": {}}

    def state_write(self, value):
        backup.atomic_json(self.state_path, value)

    def status(self):
        state = self.state()
        try:
            host = self.vault.host()
        except AutoError as exc:
            return {"ready": False, "config": copy.deepcopy(DEFAULT), "state": state, "error": str(exc)}
        try:
            cfg = self.vault.public()
            return {"ready": True, "locked": False, "config": cfg, "instance": host["instance"], "state": state,
                    "next_due": next_due(cfg, datetime.now(timezone.utc))}
        except AutoError as exc:
            return {"ready": True, "locked": True, "config": copy.deepcopy(DEFAULT), "state": state, "error": str(exc)}

    async def run(self, slot=None):
        self.idle()
        async with self.lock:
            cfg = await asyncio.to_thread(self.vault.read)
            if not cfg["key_exported"] or not any(t["enabled"] for t in cfg["targets"]):
                raise AutoError("Zuerst Schlüssel exportieren und mindestens ein Ziel einrichten.")
            state = self.state()
            now = time.time()
            state.update(status="running", last_attempt=now, error="")
            if slot:
                state["schedule_attempts"] = state.get("schedule_attempts", 0) + 1 if state.get("schedule_slot") == slot else 1
                state["schedule_slot"] = slot
                state["schedule_last_attempt"] = now
            state["active_slot"] = slot
            self.state_write(state)
            try:
                result = await self._snapshot_and_upload(cfg, state)
                state.update(status="success", last_success=time.time(), error="", last_name=result)
            except Exception as exc:
                state.update(status="failed", error=str(exc) if isinstance(exc, backup.BackupError) else "AutoBackup fehlgeschlagen. Speicherplatz und Dienste prüfen.")
            if slot:
                state["schedule_state"] = state["status"]
            state["active_slot"] = None
            state["history"] = ([{"at": now, "status": state["status"], "error": state.get("error", ""),
                                  "name": state.get("last_name", "")}] + state.get("history", []))[:100]
            self.state_write(state)
            return {"ok": state["status"] == "success", "state": state}

    async def _snapshot_and_upload(self, cfg, state):
        host = self.vault.host()
        self.control.mkdir(parents=True, exist_ok=True)
        name = "auto-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex + ".tar.gz.age"
        with tempfile.TemporaryDirectory(prefix="auto-job-", dir=self.control) as tmp:
            work = Path(tmp)
            plain, cipher = work / "backup.tar.gz", work / name
            await self.create(plain)
            await asyncio.to_thread(backup.unpack_archive, plain, work / "check")
            recipient = await asyncio.to_thread(backup.identity_recipient, host["identity"])
            await asyncio.to_thread(backup.encrypt_archive, plain, cipher, recipient)
            verified = work / "verified.tar.gz"
            await asyncio.to_thread(backup.plain_archive, cipher, verified, host["identity"])
            if await asyncio.to_thread(backup.digest, plain) != await asyncio.to_thread(backup.digest, verified):
                raise AutoError("Die lokale Entschlüsselungsprüfung ist fehlgeschlagen.")
            final = self.control / name
            os.replace(cipher, final)
            catalog = state.setdefault("catalog", {})
            catalog[name] = {"name": name, "created": time.time(), "targets": [], "local": True}
            self.state_write(state)
            errors = []
            for target in cfg["targets"]:
                if not target["enabled"]:
                    continue
                try:
                    await asyncio.to_thread(self._upload, target, cfg, work, final)
                    catalog[name]["targets"].append(target["id"])
                    self.state_write(state)
                except AutoError as exc:
                    errors.append(target["label"] + ": " + str(exc))
            if errors:
                raise AutoError("; ".join(errors))
            # Pruning starts only after every configured upload was verified.
            await asyncio.to_thread(self._prune, cfg, state, work)
        return name

    def _upload(self, target, cfg, work, path):
        with Remote(self.vault, target, cfg, work / target["id"]) as remote:
            remote.upload(path)

    def _prune(self, cfg, state, work):
        catalog = state["catalog"]
        local = [e for e in catalog.values() if e.get("local")]
        keep = keep_names(local, cfg)
        for entry in local:
            if entry["name"] not in keep and AUTO_NAME.fullmatch(entry["name"]):
                path = self.control / entry["name"]
                if not path.is_symlink():
                    path.unlink(missing_ok=True)
                entry["local"] = False
        for target in cfg["targets"]:
            if not target["enabled"]:
                continue
            entries = [e for e in catalog.values() if target["id"] in e["targets"]]
            keep = keep_names(entries, cfg)
            with Remote(self.vault, target, cfg, work / (target["id"] + "-prune")) as remote:
                present = {e["Name"] for e in remote.listing() if not e.get("IsDir")}
                for entry in entries:
                    if entry["name"] not in keep:
                        if entry["name"] in present:
                            remote.remove(entry["name"])
                        entry["targets"].remove(target["id"])
        known = {t["id"] for t in cfg["targets"]}
        for entry in catalog.values():
            entry["targets"] = [ident for ident in entry["targets"] if ident in known]
        state["catalog"] = {name: entry for name, entry in catalog.items() if entry.get("local") or entry["targets"]}

    def test_target(self, ident):
        cfg = self.vault.read()
        target = next((t for t in cfg["targets"] if t["id"] == ident), None)
        if target is None:
            raise AutoError("Ziel nicht gefunden.")
        self.control.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="auto-test-", dir=self.control) as tmp:
            folder = Path(tmp)
            probe = folder / (".hb4-probe-" + uuid.uuid4().hex)
            probe.write_bytes(os.urandom(64))
            with Remote(self.vault, target, cfg, folder / "config") as remote:
                try:
                    remote.upload(probe)
                except Exception:
                    with contextlib.suppress(AutoError):
                        remote.call("deletefile", remote.base + "/" + probe.name)
                    raise
                else:
                    remote.call("deletefile", remote.base + "/" + probe.name)
        return {"ok": True}

    async def loop(self):
        # Resume a failed/interrupted scheduled slot at most three times.
        state = self.state()
        if state.get("status") == "running":
            state.update(status="failed", error="Der vorherige Lauf wurde unterbrochen.")
            if state.get("active_slot"):
                state["schedule_state"] = "failed"
            self.state_write(state)
        while True:
            try:
                cfg = await asyncio.to_thread(self.vault.read)
                if cfg["enabled"]:
                    now = datetime.now(timezone.utc)
                    slot = slot_for(cfg, now)
                    state = self.state()
                    retry = (state.get("schedule_state") == "failed" and state.get("schedule_attempts", 0) < 3
                             and time.time() - state.get("schedule_last_attempt", 0) > 900 * max(1, state.get("schedule_attempts", 1)))
                    if slot and (slot != state.get("schedule_slot") or retry) and not self.lock.locked():
                        await self.run(slot)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass  # prerequisites/config errors are visible in the admin status
            await asyncio.sleep(60)
