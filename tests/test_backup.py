"""Real archive/database round trips, failed restores and HTTP access boundaries."""
import io
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from dashboard import backup
from dashboard.auth import AuthStore
from hblink4.settings_db import SettingsDB


class BackupFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / "source"
        self.data = self.root / "dashboard/data"
        self.data.mkdir(parents=True)
        for name, content in {
            "hblink4/hblink.py": "# custom HBlink4 code\n",
            "dashboard/server.py": "# custom dashboard\n",
            "dashboard/config.json": json.dumps({"auth": {"superadmin": {"username": "source", "password": "source-secret"}},
                                                 "https": {"enabled": False}, "comment": str(self.root) + "/unchanged"}),
            "config/config.json": json.dumps({"global": {"settings_db": "dashboard/data/dashboard.db"},
                                               "freeradioid": {"passphrase": "private"}}),
            "dashboard/static/own-logo.svg": "<svg>my-logo</svg>",
            "VERSION": "1.4.0\n",
            "venv/excluded": "machine-specific",
        }.items():
            p = self.root / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        self.auth = AuthStore(self.data / "dashboard.db")
        self.addCleanup(self.auth._db.close)
        self.auth.sync_superadmin({"username": "source", "password": "source-secret"})
        _, self.actor = self.auth.login("source", "source-secret")
        SettingsDB(self.auth._q, self.auth._x)
        self.auth._db.execute("INSERT INTO settings(key,value,updated_at,updated_by) VALUES(?,?,?,?)",
                              ("dmr.test.password", json.dumps(str(self.root) + "/literal-password"), time.time(), "test"))
        self.control = self.data / backup.CONTROL
        self.control.mkdir()
        (self.control / "old-backup.tar.gz").write_text("must not recurse")
        self.archive = self.control / "fixture.tar.gz"

    def create(self):
        return backup.create_archive(self.root, self.data, self.archive, version={"version": "1.4.0"},
                                     system_units={u: "[Service]\nUser=source\n" for u in backup.UNITS})

    def unpack(self):
        dest = self.base / ("unpack-" + str(time.time_ns()))
        dest.mkdir()
        manifest = backup.unpack_archive(self.archive, dest)
        return dest, manifest

    def rewrite(self, change=None, extra=None):
        entries = []
        with tarfile.open(self.archive) as tar:
            for m in tar:
                entries.append((m, tar.extractfile(m).read()))
        with tarfile.open(self.archive, "w:gz") as tar:
            for member, data in entries:
                if change:
                    data = change(member.name, data)
                member.size = len(data)
                tar.addfile(member, io.BytesIO(data))
            if extra:
                tar.addfile(*extra)


class TestArchives(BackupFixture):
    def test_other_python_minor_is_inspectable_and_reported_in_preview(self):
        self.create()
        source = '3.11.9' if sys.version_info[:2] != (3, 11) else '3.12.1'
        def change_python(name, content):
            if name == 'manifest.json':
                value = json.loads(content)
                value['python'] = source
                return json.dumps(value).encode()
            return content
        self.rewrite(change_python)
        _, manifest = self.unpack()
        preview = backup.summary(manifest)
        self.assertEqual(preview['python'], source)
        self.assertTrue(preview['python_migration'])
        self.assertEqual(preview['target_python'], backup.platform.python_version())

    def test_python_below_supported_minimum_is_rejected(self):
        self.create()
        def old_python(name, content):
            if name == 'manifest.json':
                value = json.loads(content); value['python'] = '3.8.20'
                return json.dumps(value).encode()
            return content
        self.rewrite(old_python)
        with self.assertRaisesRegex(backup.BackupError, '3.9'):
            self.unpack()

    def test_full_roundtrip_copies_wal_users_code_branding_and_data(self):
        self.auth._db.execute("INSERT INTO audit(ts,username,action) VALUES(?,?,?)", (time.time(), "source", "wal-only"))
        aprs = sqlite3.connect(self.data / "aprs.db")
        aprs.execute("CREATE TABLE stations(call TEXT)")
        aprs.execute("INSERT INTO stations VALUES('SA7BNT-5')")
        aprs.commit()
        aprs.close()
        (self.data / "stats.json").write_text('{"calls":42}')
        manifest = self.create()
        dest, checked = self.unpack()
        self.assertEqual(checked, manifest)
        self.assertEqual((dest / "app/dashboard/static/own-logo.svg").read_text(), "<svg>my-logo</svg>")
        self.assertEqual(json.loads((dest / "data/stats.json").read_text())["calls"], 42)
        restored = AuthStore(dest / "data/dashboard.db")
        try:
            _, user = restored.login("source", "source-secret")
            self.assertEqual(user["role"], "superadmin")
            self.assertTrue(restored._db.execute("SELECT 1 FROM audit WHERE action='wal-only'").fetchone())
        finally:
            restored._db.close()
        self.assertFalse(any("venv" in Path(name).parts or backup.CONTROL in name or name.endswith("-wal") for name in manifest["files"]))

    def test_tampering_is_rejected(self):
        self.create()
        self.rewrite(lambda name, data: b"changed code" if name == "app/hblink4/hblink.py" else data)
        with self.assertRaisesRegex(backup.BackupError, "Checksum"):
            self.unpack()

    def test_traversal_symlinks_duplicate_and_devices_are_rejected(self):
        for name, kind in [("../escape", tarfile.REGTYPE), ("/absolute", tarfile.REGTYPE),
                           ("app/link", tarfile.SYMTYPE), ("app/device", tarfile.CHRTYPE),
                           ("app/hblink4/hblink.py", tarfile.REGTYPE)]:
            with self.subTest(name=name):
                self.create()
                member = tarfile.TarInfo(name)
                member.type = kind
                member.linkname = "/etc/passwd"
                self.rewrite(extra=(member, io.BytesIO()))
                with self.assertRaises(backup.BackupError):
                    self.unpack()
        self.assertFalse((self.base / "escape").exists())

    def test_missing_manifest_file_and_invalid_database_are_rejected(self):
        self.create()
        def remove_required(name, content):
            if name == "manifest.json":
                value = json.loads(content)
                del value["files"]["data/dashboard.db"]
                return json.dumps(value).encode()
            return content
        self.rewrite(remove_required)
        with self.assertRaises(backup.BackupError):
            self.unpack()

    def test_sqlite_corruption_is_detected_even_with_matching_checksum(self):
        self.create()
        bad = b"SQLite format 3\x00" + b"broken database" * 100
        def resign(name, content):
            if name == "data/dashboard.db":
                return bad
            if name == "manifest.json":
                value = json.loads(content)
                old_size = value["files"]["data/dashboard.db"]["size"]
                value["files"]["data/dashboard.db"].update(size=len(bad), sha256=hashlib.sha256(bad).hexdigest())
                value["unpacked_bytes"] += len(bad) - old_size
                return json.dumps(value).encode()
            return content
        self.rewrite(resign)
        with self.assertRaisesRegex(backup.BackupError, "database"):
            self.unpack()

    def test_internal_certificate_links_materialize_external_links_fail(self):
        certs = self.data / "certificates"
        certs.mkdir()
        (certs / "real.pem").write_text("certificate")
        (certs / "live.pem").symlink_to("real.pem")
        self.create()
        dest, _ = self.unpack()
        self.assertFalse((dest / "data/certificates/live.pem").is_symlink())
        self.assertEqual((dest / "data/certificates/live.pem").read_text(), "certificate")
        (certs / "external").symlink_to(self.base / "outside")
        with self.assertRaisesRegex(backup.BackupError, "External symlink"):
            self.create()

    def test_paths_adapt_but_credentials_and_comments_are_unchanged(self):
        cert = self.base / "external-cert.pem"
        cert.write_text("certificate")
        cfg = json.loads((self.root / "dashboard/config.json").read_text())
        cfg["https"]["cert_file"] = str(cert)
        (self.root / "dashboard/config.json").write_text(json.dumps(cfg))
        self.create()
        dest, manifest = self.unpack()
        target, data = self.base / "target", self.base / "target/dashboard/data"
        backup.adapt_paths(dest, manifest, target, data)
        cfg = json.loads((dest / "app/dashboard/config.json").read_text())
        self.assertEqual(cfg["https"]["cert_file"], str(data / "backup-tls/cert_file.pem"))
        self.assertEqual(cfg["comment"], str(self.root) + "/unchanged")
        db = sqlite3.connect(dest / "data/dashboard.db")
        try:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)
            self.assertEqual(json.loads(db.execute("SELECT value FROM settings WHERE key='dmr.test.password'").fetchone()[0]),
                             str(self.root) + "/literal-password")
        finally:
            db.close()

    def test_size_limit(self):
        self.create()
        with patch.object(backup, "MAX_UNPACKED", 10):
            with self.assertRaisesRegex(backup.BackupError, "size/file"):
                self.unpack()

    def test_git_bundle_preserves_commit_without_host_git_config(self):
        subprocess.run(["git", "init", str(self.root)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.root), "add", "hblink4/hblink.py"], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                        "commit", "-m", "source version"], check=True, capture_output=True)
        self.create()
        dest, _ = self.unpack()
        self.assertTrue((dest / "metadata/repository.bundle").is_file())
        self.assertFalse((dest / "app/.git").exists())


class TestDirectoryTransaction(unittest.TestCase):
    def test_failed_directory_rename_restores_the_original_installation(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root, stage, work = base / "target", base / "stage", base / "work"
            data = root / "dashboard/data"
            for p in (data / backup.CONTROL, stage / "app/dashboard", stage / "data", work):
                p.mkdir(parents=True, exist_ok=True)
            (root / "original").write_text("keep me")
            (data / backup.CONTROL / "recovery").touch()
            original_replace = os.replace
            def fail_new_root(source, target):
                if Path(source) == stage / "app":
                    raise OSError("simulated disk error")
                return original_replace(source, target)
            swap = backup.InstallationSwap(root, data, stage, work)
            with patch.object(backup.os, "replace", side_effect=fail_new_root):
                with self.assertRaisesRegex(OSError, "disk error"):
                    swap.apply()
            self.assertEqual((root / "original").read_text(), "keep me")
            self.assertTrue((data / backup.CONTROL / "recovery").exists())

    def test_commit_and_failure_rollback_keep_old_code_data_venv_and_control(self):
        for external in (False, True):
            with self.subTest(external=external), tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                root, stage, work = base / "target", base / "stage", base / "work"
                data = base / "external-data" if external else root / "dashboard/data"
                for p in (root, data, stage / "app/dashboard", stage / "data", work):
                    p.mkdir(parents=True, exist_ok=True)
                (root / "old-code").write_text("old")
                (root / "venv").mkdir()
                (root / "venv/old-interpreter").write_text("old environment")
                (data / "old.db").write_text("old DB")
                (data / backup.CONTROL).mkdir()
                (data / backup.CONTROL / "recovery.tar.gz").write_text("recovery")
                (stage / "app/new-code").write_text("new")
                (stage / "data/new.db").write_text("new DB")
                swap = backup.InstallationSwap(root, data, stage, work)
                swap.apply()
                self.assertTrue((root / "new-code").is_file())
                self.assertTrue((data / "new.db").is_file())
                self.assertTrue((data / backup.CONTROL / "recovery.tar.gz").is_file())
                (root / "venv").mkdir()
                (root / "venv/broken-new-interpreter").touch()
                swap.rollback()
                self.assertEqual((root / "old-code").read_text(), "old")
                self.assertEqual((data / "old.db").read_text(), "old DB")
                self.assertTrue((root / "venv/old-interpreter").exists())
                self.assertFalse((root / "new-code").exists())
                self.assertTrue((data / backup.CONTROL / "recovery.tar.gz").exists())


class TestRestoreWorker(BackupFixture):
    """Exercise real filesystem swaps and DB recovery; emulate systemd/pip only."""
    def prepare_request(self, source_python=None):
        self.create()
        if source_python:
            def change_python(name, content):
                if name == 'manifest.json':
                    value = json.loads(content); value['python'] = source_python
                    return json.dumps(value).encode()
                return content
            self.rewrite(change_python)
        token = "a" * 32
        upload = self.control / (token + ".tar.gz")
        upload.write_bytes(self.archive.read_bytes())
        backup.atomic_json(self.control / "restore-request.json", {"token": token, "sha256": backup.digest(upload)})
        (self.root / "hblink4/hblink.py").write_text("# destination before restore\n")
        self.auth.sync_superadmin({"username": "destination", "password": "destination-secret"})
        return {"root": str(self.root), "data_dir": str(self.data), "uid": os.getuid(), "gid": os.getgid(), "home": str(self.base)}

    def run_worker(self, cfg, *, fail_start=False, restart_loop=False, fail_prepare=False):
        starts = []
        self.system_commands = []
        self.owner_commands = []
        self.prepared_paths = []
        def prepare(cfg, stage, prepared):
            self.assertFalse(any(cmd[:2] == ['systemctl', 'stop'] for cmd in self.system_commands))
            self.prepared_paths.append(prepared)
            (prepared / 'wheels').mkdir(parents=True)
            if fail_prepare:
                raise backup.BackupError('incompatible saved package')
        def commands(args, **kw):
            self.system_commands.append(args)
            if args[:2] == ["systemctl", "start"]:
                starts.append(args)
                if fail_start and len(starts) == 1:
                    raise backup.BackupError("simulated service startup failure")
            if "NRestarts" in args and restart_loop and len(starts) == 1:
                return "1"
            return ""
        def owner_commands(cfg, args, **kw):
            self.owner_commands.append(args)
            if len(args) > 3 and 'shutil.rmtree(sys.argv[1])' in args[3]:
                shutil.rmtree(args[-1])
                return ''
            if args[-1].endswith("setup-bootstrap.json"):
                Path(args[-1]).unlink(missing_ok=True)
                return ""
            if "--write-status" in args:
                backup.atomic_json(self.control / "restore-status.json", json.loads(kw["input"]))
                return ""
            if "--write-file" in args:
                (self.control / args[-1]).write_bytes(kw["stdin"].read())
                return ""
            if "--snapshot" in args:
                return backup.create_archive(self.root, self.data, Path(args[-1]),
                                             system_units={u: "[Service]" for u in backup.UNITS})
            if "venv" in args:
                path = Path(args[-1])
                (path / "bin").mkdir(parents=True)
                (path / "bin/python").write_text("new interpreter")
            return ""
        with patch.object(backup, "LOCK_PATH", str(self.base / "worker.lock")), \
             patch.object(backup, "command", side_effect=commands), \
             patch.object(backup, "_as_owner", side_effect=owner_commands), \
             patch.object(backup, "prepare_python_packages", side_effect=prepare), \
             patch.object(backup.time, "sleep"):
            # The snapshot mock still needs the real stdlib metadata command.
            real_create = backup.create_archive
            real_command = globals().get("_real_backup_command")
            def snapshot_command(cfg, args, **kw):
                if "--snapshot" in args:
                    with patch.object(backup, "command", real_command):
                        return real_create(self.root, self.data, Path(args[-1]),
                                           system_units={u: "[Service]" for u in backup.UNITS})
                return owner_commands(cfg, args, **kw)
            with patch.object(backup, "_as_owner", side_effect=snapshot_command):
                backup.restore_worker(cfg)

    def test_cross_minor_restore_uses_preflight_wheels_and_cleans_probe(self):
        source = '3.11.9' if sys.version_info[:2] != (3, 11) else '3.12.1'
        cfg = self.prepare_request(source)
        self.run_worker(cfg)
        self.assertEqual(len(self.prepared_paths), 1)
        self.assertFalse(self.prepared_paths[0].exists())
        install = next(args for args in self.owner_commands if 'install' in args)
        self.assertIn('--no-index', install)
        self.assertIn('--find-links', install)
        self.assertTrue(any(args[-2:] == ['pip', 'check'] for args in self.owner_commands))
        self.assertEqual(json.loads((self.control / 'restore-status.json').read_text())['state'], 'complete')

    def test_incompatible_python_packages_never_stop_or_replace_destination(self):
        source = '3.11.9' if sys.version_info[:2] != (3, 11) else '3.12.1'
        cfg = self.prepare_request(source)
        with self.assertRaisesRegex(backup.BackupError, 'incompatible'):
            self.run_worker(cfg, fail_prepare=True)
        self.assertFalse(any(cmd[0] == 'systemctl' for cmd in self.system_commands))
        self.assertEqual((self.root / 'hblink4/hblink.py').read_text(), '# destination before restore\n')
        self.assertTrue((self.root / 'venv/excluded').exists())
        self.assertFalse(self.prepared_paths[0].exists())
        self.assertEqual(json.loads((self.control / 'restore-status.json').read_text())['state'], 'failed')

    def test_cross_minor_start_failure_rolls_back_original_environment(self):
        source = '3.11.9' if sys.version_info[:2] != (3, 11) else '3.12.1'
        cfg = self.prepare_request(source)
        with self.assertRaisesRegex(backup.BackupError, 'simulated'):
            self.run_worker(cfg, fail_start=True)
        self.assertTrue((self.root / 'venv/excluded').exists())
        self.assertEqual((self.root / 'hblink4/hblink.py').read_text(), '# destination before restore\n')
        self.assertFalse(self.prepared_paths[0].exists())

    def test_success_restores_source_account_and_creates_destination_recovery(self):
        cfg = self.prepare_request()
        self.run_worker(cfg)
        self.assertEqual((self.root / "hblink4/hblink.py").read_text(), "# custom HBlink4 code\n")
        auth = AuthStore(self.data / "dashboard.db")
        try:
            auth.login("source", "source-secret")
            self.assertEqual(auth._db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 1)
        finally:
            auth._db.close()
        status = json.loads((self.control / "restore-status.json").read_text())
        self.assertEqual(status["state"], "complete")
        self.assertTrue((self.control / status["recovery"]).exists())
        recovery = self.base / "recovery-preview"
        backup.unpack_archive(self.control / status["recovery"], recovery)
        self.assertEqual((recovery / "app/hblink4/hblink.py").read_text(), "# destination before restore\n")

    def test_failed_start_rolls_back_code_database_and_original_environment(self):
        cfg = self.prepare_request()
        with self.assertRaisesRegex(backup.BackupError, "simulated"):
            self.run_worker(cfg, fail_start=True)
        self.assertEqual((self.root / "hblink4/hblink.py").read_text(), "# destination before restore\n")
        self.assertEqual((self.root / "venv/excluded").read_text(), "machine-specific")
        auth = AuthStore(self.data / "dashboard.db")
        try:
            auth.login("destination", "destination-secret")
        finally:
            auth._db.close()
        self.assertEqual(json.loads((self.control / "restore-status.json").read_text())["state"], "failed")

    def test_restart_loop_is_detected_and_rolled_back(self):
        cfg = self.prepare_request()
        with self.assertRaisesRegex(backup.BackupError, "restarted unexpectedly"):
            self.run_worker(cfg, restart_loop=True)
        self.assertEqual((self.root / "hblink4/hblink.py").read_text(), "# destination before restore\n")
        self.assertTrue((self.root / "venv/excluded").exists())

    def test_initial_setup_enables_units_only_after_successful_restore(self):
        cfg = self.prepare_request()
        backup.atomic_json(self.control / "setup-bootstrap.json", {"fresh": True})
        request_path = self.control / "restore-request.json"
        request = json.loads(request_path.read_text())
        request["initial_setup"] = True
        backup.atomic_json(request_path, request)
        self.run_worker(cfg)
        self.assertIn(["systemctl", "enable", *backup.UNITS], self.system_commands)
        self.assertFalse((self.control / "setup-bootstrap.json").exists())

    def test_initial_setup_failure_leaves_bootstrap_stopped_and_disabled(self):
        cfg = self.prepare_request()
        request_path = self.control / "restore-request.json"
        request = json.loads(request_path.read_text())
        request["initial_setup"] = True
        backup.atomic_json(request_path, request)
        with self.assertRaises(backup.BackupError):
            self.run_worker(cfg, fail_start=True)
        self.assertEqual(sum(cmd[:2] == ["systemctl", "start"] for cmd in self.system_commands), 1)
        self.assertIn(["systemctl", "disable", *backup.UNITS], self.system_commands)
        self.assertEqual((self.root / "hblink4/hblink.py").read_text(), "# destination before restore\n")


_real_backup_command = backup.command


class TestBackupApi(BackupFixture):
    def setUp(self):
        super().setUp()
        from dashboard import server
        from fastapi.testclient import TestClient
        self.server = server
        self.saved_auth = server.state.auth
        self.saved_dir = server.state._data_dir
        self.saved_auto = server._auto_cache
        server.state.auth, server.state._data_dir = self.auth, self.data
        self.addCleanup(setattr, server.state, "auth", self.saved_auth)
        self.addCleanup(setattr, server.state, "_data_dir", self.saved_dir)
        self.addCleanup(setattr, server, "_auto_cache", self.saved_auto)
        self.client = TestClient(server.app)
        self.addCleanup(self.client.close)
        self.headers = {"X-Dashboard": "1"}

    def login(self):
        self.assertEqual(self.client.post("/api/auth/login", json={"username": "source", "password": "source-secret"},
                                          headers=self.headers).status_code, 200)

    def test_all_backup_routes_deny_anonymous_and_regular_admin(self):
        urls = [("GET", "/api/admin/backup"), ("POST", "/api/admin/backup/create"),
                ("GET", "/api/admin/backup/download/anything"), ("POST", "/api/admin/backup/inspect"),
                ("POST", "/api/admin/backup/restore"), ("DELETE", "/api/admin/backup/anything")]
        for method, url in urls:
            response = self.client.request(method, url, json={} if method in ("POST", "DELETE") else None, headers=self.headers)
            self.assertEqual(response.status_code, 401, url)
        self.auth.create_user(self.actor, "ordinary", "password1", "admin")
        self.client.post("/api/auth/login", json={"username": "ordinary", "password": "password1"}, headers=self.headers)
        for method, url in urls:
            response = self.client.request(method, url, json={} if method in ("POST", "DELETE") else None, headers=self.headers)
            self.assertEqual(response.status_code, 403, url)

    def test_delete_requires_csrf_and_confirmation_then_removes_only_chosen_file(self):
        self.login()
        name = 'backup-20261124T010203-' + 'a' * 32 + '.tar.gz'
        path = self.control / name
        path.write_bytes(b'archive')
        url = '/api/admin/backup/' + name
        self.assertEqual(self.client.request('DELETE', url, json={'confirmation': name}).status_code, 403)
        self.assertEqual(self.client.request('DELETE', url, json={}, headers=self.headers).status_code, 400)
        self.assertTrue(path.exists())
        result = self.client.request('DELETE', url, json={'confirmation': name}, headers=self.headers)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertFalse(path.exists())
        self.assertTrue((self.control / 'old-backup.tar.gz').exists())
        self.assertNotIn(name, [item['name'] for item in self.client.get('/api/admin/backup').json()['archives']])
        self.assertEqual(self.client.get('/api/admin/backup/download/' + name).status_code, 404)
        self.assertEqual(self.client.request('DELETE', url, json={'confirmation': name}, headers=self.headers).status_code, 404)

    def test_delete_recovery_and_auto_archives_preserves_remote_catalog(self):
        self.login()
        recovery = 'before-restore-20261124010203.tar.gz'
        remote_name = 'auto-20261124T010203Z-' + 'b' * 32 + '.tar.gz.age'
        local_name = 'auto-20261124T010204Z-' + 'c' * 32 + '.tar.gz.age'
        remote_copy = self.base / remote_name
        remote_copy.write_bytes(b'remote copy')
        service = self.server._auto_service()
        service.state_write({'status': 'idle', 'history': [], 'catalog': {
            remote_name: {'name': remote_name, 'local': True, 'targets': ['target-1'], 'created': time.time()},
            local_name: {'name': local_name, 'local': True, 'targets': [], 'created': time.time()}}})
        for name in (recovery, remote_name, local_name):
            with self.subTest(name=name):
                (self.control / name).write_bytes(b'archive')
                result = self.client.request('DELETE', '/api/admin/backup/' + name,
                                             json={'confirmation': name}, headers=self.headers)
                self.assertEqual(result.status_code, 200, result.text)
                self.assertFalse((self.control / name).exists())
        catalog = service.state()['catalog']
        self.assertFalse(catalog[remote_name]['local'])
        self.assertEqual(catalog[remote_name]['targets'], ['target-1'])
        self.assertNotIn(local_name, catalog)
        self.assertEqual(remote_copy.read_bytes(), b'remote copy')

    def test_delete_rejects_unknown_paths_symlinks_and_directories(self):
        self.login()
        victim = self.base / 'keep.txt'
        victim.write_text('keep')
        link = self.control / ('backup-20261124T010203-' + 'd' * 32 + '.tar.gz')
        link.symlink_to(victim)
        directory = self.control / ('backup-20261124T010203-' + 'e' * 32 + '.tar.gz')
        directory.mkdir()
        for name in ('dashboard.db', '..%2Fkeep.txt', link.name, directory.name):
            with self.subTest(name=name):
                result = self.client.request('DELETE', '/api/admin/backup/' + name,
                                             json={'confirmation': name}, headers=self.headers)
                self.assertEqual(result.status_code, 404, result.text)
        self.assertEqual(victim.read_text(), 'keep')
        self.assertTrue(link.is_symlink())
        self.assertTrue(directory.is_dir())

    def test_delete_blocks_running_restore_and_reports_permission_failure(self):
        self.login()
        name = 'backup-20261124T010203-' + 'f' * 32 + '.tar.gz'
        path = self.control / name
        path.write_bytes(b'archive')
        url = '/api/admin/backup/' + name
        backup.atomic_json(self.control / 'restore-status.json', {'state': 'running', 'updated_at': time.time()})
        self.assertEqual(self.client.request('DELETE', url, json={'confirmation': name}, headers=self.headers).status_code, 409)
        self.assertTrue(path.exists())
        backup.atomic_json(self.control / 'restore-status.json', {'state': 'idle'})
        with patch.object(self.server._backup_lock, '_locked', True):
            self.assertEqual(self.client.request('DELETE', url, json={'confirmation': name}, headers=self.headers).status_code, 409)
        with patch.object(self.server._update_lock, '_locked', True):
            self.assertEqual(self.client.request('DELETE', url, json={'confirmation': name}, headers=self.headers).status_code, 409)
        with patch.object(Path, 'unlink', side_effect=PermissionError()):
            result = self.client.request('DELETE', url, json={'confirmation': name}, headers=self.headers)
        self.assertEqual(result.status_code, 409)
        self.assertIn('Dateirechte', result.json()['error'])
        self.assertTrue(path.exists())

    def test_create_download_inspect_and_password_gate(self):
        self.login()
        with patch.object(self.server._version, "REPO_ROOT", self.root), \
             patch.object(self.server.state, "save_all_data"), patch.object(self.server.state.aprs.store, "flush"):
            result = self.client.post("/api/admin/backup/create", json={}, headers=self.headers)
        self.assertEqual(result.status_code, 200, result.text)
        name = result.json()["name"]
        download = self.client.get("/api/admin/backup/download/" + name)
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.headers["cache-control"], "no-store")
        inspected = self.client.post("/api/admin/backup/inspect", content=download.content, headers=self.headers)
        self.assertEqual(inspected.status_code, 200, inspected.text)
        token = inspected.json()["token"]
        denied = self.client.post("/api/admin/backup/restore", json={"token": token, "password": "wrong", "confirmation": "RESTORE"}, headers=self.headers)
        self.assertEqual(denied.status_code, 400)
        self.assertFalse((self.control / "restore-request.json").exists())
        with patch.object(backup, "helper_available", return_value=True), patch.object(backup, "command") as cmd:
            result = self.client.post("/api/admin/backup/restore", json={"token": token, "password": "source-secret", "confirmation": "RESTORE"}, headers=self.headers)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(json.loads((self.control / "restore-request.json").read_text())["token"], token)
        cmd.assert_not_called()  # systemd path unit, no sudo from NoNewPrivileges dashboard

    def test_upload_size_csrf_and_changed_preview(self):
        self.login()
        self.assertEqual(self.client.post("/api/admin/backup/inspect", content=b"bad").status_code, 403)
        with patch.object(backup, "MAX_UPLOAD", 5):
            response = self.client.post("/api/admin/backup/inspect", content=b"too big", headers=self.headers)
        self.assertEqual(response.status_code, 413)
        self.assertFalse(list(self.control.glob("[a-f0-9]" * 32 + ".tar.gz")))
        self.create()
        response = self.client.post("/api/admin/backup/inspect", content=self.archive.read_bytes(), headers=self.headers)
        token = response.json()["token"]
        (self.control / (token + ".tar.gz")).write_bytes(b"changed")
        with patch.object(backup, "helper_available", return_value=True), patch.object(backup, "command") as cmd:
            response = self.client.post("/api/admin/backup/restore", json={"token": token, "password": "source-secret", "confirmation": "RESTORE"}, headers=self.headers)
        self.assertEqual(response.status_code, 409)
        cmd.assert_not_called()


if __name__ == "__main__":
    unittest.main()
