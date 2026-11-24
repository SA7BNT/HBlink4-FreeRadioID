"""Initial setup: validate before changing the target; use the existing worker."""
import importlib.util
import json
import os
import pwd
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import sys
from pathlib import Path
from unittest.mock import Mock, patch

from dashboard import backup
from test_backup import BackupFixture

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("setup_restore", REPO / "scripts/setup_restore.py")
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


class TestSetupRestore(BackupFixture):
    def setUp(self):
        super().setUp()
        self.create()
        self.target = self.base / "target"
        self.target.mkdir()
        self.user = pwd.getpwuid(os.getuid()).pw_name
        self.messages = []
        self.job = setup.SetupRestore(self.target, self.archive, ask=Mock(side_effect=[self.user, "RESTORE"]),
                                      say=self.messages.append)
        self.job.preflight = Mock()
        self.job.show = Mock(side_effect=lambda unit, prop: "not-found" if prop == "LoadState" else "inactive")
        self.job.run = Mock(return_value="")
        self.job.enqueue = Mock()
        self.job.wait = Mock(return_value={"recovery": "before-restore.tar.gz"})
        self.cfg = {"root": str(self.target), "data_dir": str(self.target / "dashboard/data"),
                    "uid": os.getuid(), "gid": os.getgid(), "home": str(self.base)}

    def prepare_stub(self, account):
        marker = Path(self.cfg["data_dir"]) / backup.CONTROL / "setup-bootstrap.json"
        backup.atomic_json(marker, {"fresh": True})

    def test_fresh_restore_requires_confirmation_then_uses_worker(self):
        self.job.prepare_fresh = Mock(side_effect=self.prepare_stub)
        with patch.object(backup, "_host_config", return_value=self.cfg):
            self.assertTrue(self.job.execute())
        self.job.prepare_fresh.assert_called_once_with(self.user)
        self.job.enqueue.assert_called_once()
        self.assertTrue(self.job.fresh)
        self.assertFalse((Path(self.cfg["data_dir"]) / backup.CONTROL / "setup-bootstrap.json").exists())
        self.assertIn("source", "\n".join(self.messages))
        self.assertFalse(any("Passwort" in str(c) for c in self.job.ask.call_args_list))
        self.assertEqual(self.job.run.call_count, 1)  # install helper; worker enables units itself

    def test_other_python_minor_reaches_confirmation_and_worker(self):
        source = '3.11.9' if sys.version_info[:2] != (3, 11) else '3.12.1'
        def change_python(name, content):
            if name == 'manifest.json':
                value = json.loads(content); value['python'] = source
                return json.dumps(value).encode()
            return content
        self.rewrite(change_python)
        self.job.prepare_fresh = Mock(side_effect=self.prepare_stub)
        with patch.object(backup, '_host_config', return_value=self.cfg):
            self.assertTrue(self.job.execute())
        self.job.enqueue.assert_called_once()
        self.assertIn('Code und gespeicherte Paketversionen', '\n'.join(self.messages))

    def test_cancel_does_not_prepare_or_change_target(self):
        self.job.ask = Mock(side_effect=[self.user, ""])
        self.job.prepare_fresh = Mock()
        self.assertFalse(self.job.execute())
        self.job.prepare_fresh.assert_not_called()
        self.job.run.assert_not_called()
        self.job.enqueue.assert_not_called()
        self.assertFalse(list(self.target.iterdir()))

    def test_bad_archive_is_rejected_before_account_or_configuration_prompts(self):
        self.archive.write_bytes(b"not an archive")
        self.job.prepare_fresh = Mock()
        with self.assertRaises(backup.BackupError):
            self.job.execute()
        self.job.ask.assert_not_called()
        self.job.prepare_fresh.assert_not_called()
        self.job.run.assert_not_called()
        self.assertFalse(list(self.target.iterdir()))

    @unittest.skipUnless(shutil.which('age') and shutil.which('age-keygen'), 'age required')
    def test_encrypted_archive_is_decrypted_before_setup_or_enqueue(self):
        identity=self.base/'recovery.agekey'
        subprocess.run(['age-keygen','-o',str(identity)],check=True,capture_output=True)
        cipher=self.base/'encrypted.tar.gz.age'
        backup.encrypt_archive(self.archive,cipher,backup.identity_recipient(identity))
        self.job.archive=cipher
        self.job.ask=Mock(side_effect=[str(identity),self.user,'RESTORE'])
        self.job.prepare_fresh=Mock(side_effect=self.prepare_stub)
        def checked_queue(cfg,path,digest):
            self.assertFalse(backup.encrypted(path))
            self.assertEqual(digest,backup.digest(self.archive))
        self.job.enqueue=Mock(side_effect=checked_queue)
        with patch.object(backup,'_host_config',return_value=self.cfg):
            self.assertTrue(self.job.execute())
        self.job.enqueue.assert_called_once()
        self.assertTrue(cipher.exists())
        self.assertTrue(identity.exists())

    def test_existing_target_keeps_account_and_does_not_write_bootstrap(self):
        self.job.show = Mock(side_effect=lambda unit, prop: {
            "LoadState": "loaded", "WorkingDirectory": str(self.target), "User": self.user,
            "ActiveState": "inactive"}[prop])
        self.job.ask = Mock(return_value="RESTORE")
        self.job.prepare_fresh = Mock()
        with patch.object(backup, "_host_config", return_value=self.cfg):
            self.assertTrue(self.job.execute())
        self.assertFalse(self.job.fresh)
        self.job.prepare_fresh.assert_not_called()
        self.assertEqual(self.job.ask.call_count, 1)

    def test_unrelated_service_installation_is_not_overwritten(self):
        self.job.show = Mock(side_effect=lambda unit, prop: {
            "LoadState": "loaded", "WorkingDirectory": "/opt/another-hblink", "User": self.user}[prop])
        with self.assertRaisesRegex(setup.SetupError, "anderen Installationsverzeichnis"):
            self.job.execute()
        self.job.run.assert_not_called()

    def test_fresh_failure_keeps_bootstrap_services_stopped(self):
        self.job.prepare_fresh = Mock(side_effect=self.prepare_stub)
        self.job.wait = Mock(side_effect=setup.SetupError("restore failed"))
        with patch.object(backup, "_host_config", return_value=self.cfg):
            with self.assertRaisesRegex(setup.SetupError, "restore failed"):
                self.job.execute()
        self.assertEqual(self.job.run.call_args.args[0], ["systemctl", "stop", *backup.UNITS])
        self.assertTrue((Path(self.cfg["data_dir"]) / backup.CONTROL / "setup-bootstrap.json").exists())

    def test_timeout_does_not_stop_an_independent_running_worker(self):
        self.job.prepare_fresh = Mock(side_effect=self.prepare_stub)
        self.job.wait = Mock(side_effect=setup.RestoreInProgress("still running"))
        with patch.object(backup, "_host_config", return_value=self.cfg):
            with self.assertRaises(setup.RestoreInProgress):
                self.job.execute()
        self.assertFalse(any(c.args[0][0] == "systemctl" for c in self.job.run.call_args_list))

    def test_real_bootstrap_installs_without_enabling_or_starting_services(self):
        for directory in ("config", "dashboard"):
            (self.target / directory).mkdir()
            shutil.copy2(REPO / directory / "config_sample.json", self.target / directory / "config_sample.json")
        with patch.object(backup, "_own_tree"), patch.object(backup, "_as_owner"):
            self.job.prepare_fresh(self.user)
        cfg = json.loads((self.target / "dashboard/config.json").read_text())
        self.assertEqual(cfg["auth"]["superadmin"], {"username": "", "password": ""})
        self.assertFalse(cfg["auth"]["public_dashboard"])
        call = self.job.run.call_args
        self.assertEqual(call.kwargs["env"]["START_SERVICES"], "0")
        self.assertEqual(call.kwargs["env"]["ENABLE_SERVICES"], "0")
        self.assertTrue((self.target / "dashboard/data/dashboard.db").exists())

    def test_queue_copies_checked_archive_and_sets_initial_setup_marker(self):
        self.job.fresh = True
        def child(cfg, args, **kwargs):
            args = list(args)
            args[3] = args[3].replace("/usr/local/lib/hblink4-backup/backup.py", str(REPO / "dashboard/backup.py"))
            subprocess.run(args, stdin=kwargs["stdin"], check=True, timeout=20)
        with patch.object(backup, "_as_owner", side_effect=child):
            setup.SetupRestore.enqueue(self.job, self.cfg, self.archive, backup.digest(self.archive))
        folder = Path(self.cfg["data_dir"]) / backup.CONTROL
        request = json.loads((folder / "restore-request.json").read_text())
        self.assertTrue(request["initial_setup"])
        self.assertEqual((folder / (request["token"] + ".tar.gz")).read_bytes(), self.archive.read_bytes())
        self.assertEqual(request["sha256"], backup.digest(self.archive))

    def test_failed_helper_is_reported_instead_of_waiting_an_hour(self):
        folder = Path(self.cfg["data_dir"]) / backup.CONTROL
        backup.atomic_json(folder / "restore-status.json", {"state": "queued"})
        self.job.show = Mock(return_value="failed")
        with self.assertRaisesRegex(setup.SetupError, "Dienst|dienst"):
            setup.SetupRestore.wait(self.job, self.cfg, timeout=1)

    def test_https_packages_follow_database_settings_and_skip_apt(self):
        stage = self.base / "https-inspection"
        backup.unpack_archive(self.archive, stage)
        with sqlite3.connect(stage / "data/dashboard.db") as db:
            db.execute("INSERT INTO settings(key,value,updated_at,updated_by) VALUES(?,?,?,?)",
                       ("https.enabled", "true", 0, "test"))
        lookup = lambda name: None if name == "certbot" else "/usr/bin/" + name
        with patch.object(shutil, "which", side_effect=lookup), patch.dict(os.environ, {"SKIP_APT": "0"}):
            self.job.system_extras(stage)
        self.assertEqual(self.job.run.call_args.args[0], ["apt-get", "install", "-y", "certbot"])
        self.job.run.reset_mock()
        with patch.object(shutil, "which", side_effect=lookup), patch.dict(os.environ, {"SKIP_APT": "1"}):
            with self.assertRaisesRegex(setup.SetupError, "certbot"):
                self.job.system_extras(stage)
        self.job.run.assert_not_called()


class TestSetupShell(unittest.TestCase):
    def script(self, replies, *, skip_services="0", restored_file=True):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "scripts").mkdir()
            shutil.copy2(REPO / "scripts/setup.sh", root / "scripts/setup.sh")
            (root / "bin").mkdir()
            for name, text in {
                "id": "#!/bin/sh\ncase \"$1\" in -u) echo 0;; -gn) echo test;; esac\nexit 0\n",
                "python3": "#!/bin/sh\ncase \"$1\" in */setup_restore.py) echo RESTORE_ROUTE; printf '%s\\n' \"$@\"; exit 0;; *) exit 2;; esac\n",
            }.items():
                p = root / "bin" / name
                p.write_text(text)
                p.chmod(0o755)
            archive = root / "backup with spaces.tar.gz"
            if restored_file:
                archive.touch()
            result = subprocess.run(["bash", root / "scripts/setup.sh"],
                                    input=replies.format(archive=archive), text=True, capture_output=True, timeout=10,
                                    env={**os.environ, "PATH": str(root / "bin") + os.pathsep + os.environ["PATH"],
                                         "SKIP_APT": "1", "SKIP_SERVICES": skip_services})
            return result

    def test_backup_choice_routes_without_normal_configuration_questions(self):
        result = self.script("j\n{archive}\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("RESTORE_ROUTE", result.stdout)
        self.assertIn("backup with spaces.tar.gz", result.stdout)
        self.assertNotIn("DMR-Server\n", result.stdout)
        self.assertNotIn("Schritt 2/10", result.stdout)

    def test_restore_requires_services_and_an_existing_archive(self):
        result = self.script("j\n", skip_services="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SKIP_SERVICES", result.stdout)
        self.assertNotIn("RESTORE_ROUTE", result.stdout)
        result = self.script("j\n{archive}\n", restored_file=False)
        self.assertIn("Backup-Datei nicht gefunden", result.stdout)
        self.assertNotIn("RESTORE_ROUTE", result.stdout)

    def test_default_choice_keeps_normal_setup_path(self):
        result = self.script("\ntest\n")
        self.assertNotIn("RESTORE_ROUTE", result.stdout)
        self.assertIn("Schritt 2/10", result.stdout)


if __name__ == "__main__":
    unittest.main()
