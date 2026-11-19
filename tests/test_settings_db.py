"""
Shared settings database (hblink4/settings_db.py) and the installer helper.

SQLite is the main configuration of dashboard AND DMR server. The config files
only seed it on the first start; whichever program starts first seeds, nothing
is ever overwritten by seeding, and the DMR server reads its values back.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from hblink4.settings_db import (DMR_FIELDS, Field, SettingsDB, SettingsError, get_path,
                                 resolve_db_path, validate)

REPO = Path(__file__).resolve().parent.parent
TG = Field("x", "s", "Talkgroups", "tglist", [8])
JS = Field("y", "s", "Patterns", "json", [])


class TestValidation(unittest.TestCase):
    def test_tglist(self):
        self.assertEqual(validate(TG, "262, 2620 ,262"), [262, 2620])
        self.assertIsNone(validate(TG, "all"))
        self.assertEqual(validate(TG, "none"), [])
        self.assertEqual(validate(TG, [91, "92"]), [91, 92])
        for bad in ("abc", "0", "99999999"):
            with self.assertRaises(SettingsError):
                validate(TG, bad)

    def test_json(self):
        self.assertEqual(validate(JS, '[{"name": "a"}]'), [{"name": "a"}])
        for bad in ('{"a": 1}', "[1, 2]", "[{"):
            with self.assertRaises(SettingsError):
                validate(JS, bad)


class TestSeeding(unittest.TestCase):
    def test_first_start_seeds_then_db_wins(self):
        db = SettingsDB.open(Path(tempfile.mkdtemp()) / "s.db")
        file_cfg = {"global": {"port_ipv4": 62040},
                    "repeater_configurations": {"default": {"passphrase": "fromfile", "slot1_talkgroups": [262]}},
                    "freeradioid": {"enabled": False}}
        self.assertGreater(db.seed(DMR_FIELDS, file_cfg, "config/config.json"), 10)
        cfg = {}
        db.apply(DMR_FIELDS, cfg)
        self.assertEqual(get_path(cfg, "global.port_ipv4"), 62040)
        self.assertFalse(get_path(cfg, "freeradioid.enabled"))
        self.assertEqual(get_path(cfg, "freeradioid.guest_talkgroups"), [26501, 26502, 26503])   # default

        db.write("dmr.repeater_configurations.default.passphrase", "fromadmin", "boss")
        edited = {"repeater_configurations": {"default": {"passphrase": "edited-file"}}}
        self.assertEqual(db.seed(DMR_FIELDS, edited, "config/config.json"), 0)   # never overwrites
        cfg2 = {}
        db.apply(DMR_FIELDS, cfg2)
        self.assertEqual(get_path(cfg2, "repeater_configurations.default.passphrase"), "fromadmin")

    def test_db_path(self):
        self.assertEqual(resolve_db_path(Path("/opt/x")), Path("/opt/x/dashboard/data/dashboard.db"))
        self.assertEqual(resolve_db_path(Path("/opt/x"), "/var/lib/n8.db"), Path("/var/lib/n8.db"))


class TestHBlinkLoadsFromDatabase(unittest.TestCase):
    def test_load_config_overlays_db(self):
        from hblink4 import hblink as hb
        tmp = Path(tempfile.mkdtemp())
        dbp = tmp / "s.db"
        cfg_path = tmp / "config.json"
        cfg_path.write_text(json.dumps({"global": {"settings_db": str(dbp), "port_ipv4": 62031},
                                        "repeater_configurations": {"default": {"passphrase": "first"}}}))
        saved_db, saved_cfg = hb._SETTINGS_DB, hb.CONFIG
        try:
            hb._SETTINGS_DB = None
            hb.load_config(str(cfg_path))
            self.assertEqual(hb.CONFIG["repeater_configurations"]["default"]["passphrase"], "first")
            SettingsDB.open(dbp).write("dmr.repeater_configurations.default.passphrase", "admin-set", "boss")
            hb._SETTINGS_DB = None
            hb.load_config(str(cfg_path))
            self.assertEqual(hb.CONFIG["repeater_configurations"]["default"]["passphrase"], "admin-set")
            self.assertTrue(hb.CONFIG["freeradioid"]["enabled"])           # FreeRadioID on by default
        finally:
            hb._SETTINGS_DB, hb.CONFIG = saved_db, saved_cfg


class TestSetupHelper(unittest.TestCase):
    def test_write_and_current(self):
        repo = Path(tempfile.mkdtemp())
        for rel in ("config/config_sample.json", "dashboard/config_sample.json"):
            (repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (repo / rel).write_text((REPO / rel).read_text())
        answers = {"server_name": "dmr.die-n8schicht.de", "dmr_port": "62031", "ts1": "262, 2620",
                   "ts2": "all", "frid": True, "passphrase": "passw0rd", "guest_tgs": "26501",
                   "dashboard_title": "DMR Die N8schicht", "server_description": "N8schicht DMR Network",
                   "web_port": "8080", "public": True, "admin_user": "daniel", "admin_hash": "scrypt$x",
                   "https": False, "domain": "", "email": ""}
        (repo / "a.json").write_text(json.dumps(answers))
        helper = str(REPO / "scripts" / "setup_apply.py")
        r = subprocess.run([sys.executable, helper, "write", str(repo), str(repo / "a.json")],
                           capture_output=True, text=True, env={"PYTHONPATH": str(REPO)})
        self.assertEqual(r.returncode, 0, r.stderr)
        dash = json.loads((repo / "dashboard" / "config.json").read_text())
        self.assertEqual(dash["auth"]["superadmin"], {"username": "daniel", "password": "", "password_hash": "scrypt$x"})
        dmr = json.loads((repo / "config" / "config.json").read_text())
        self.assertIsNone(dmr["repeater_configurations"]["default"]["slot2_talkgroups"])
        db = SettingsDB.open(repo / "dashboard" / "data" / "dashboard.db").stored()
        self.assertEqual(db["dmr.repeater_configurations.default.slot1_talkgroups"], [262, 2620])
        r = subprocess.run([sys.executable, helper, "current", str(repo)], capture_output=True, text=True,
                           env={"PYTHONPATH": str(REPO)})
        cur = json.loads(r.stdout)
        self.assertEqual((cur["ts1"], cur["ts2"], cur["admin_user"]), ("262, 2620", "all", "daniel"))


if __name__ == '__main__':
    unittest.main()
