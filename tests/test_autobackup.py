"""Real age/rclone I/O plus scheduler, confidentiality and admin access tests."""
import asyncio
import configparser
import copy
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from dashboard import backup, autobackup as auto
from test_backup import BackupFixture

TOOLS = all(shutil.which(name) for name in ("age", "age-keygen", "rclone"))


def sftp_target():
    return {"id": "a" * 32, "type": "sftp", "label": "SFTP test", "enabled": True,
            "path": "/backups", "host": "example.invalid", "port": 22, "user": "uploader",
            "password": "private-sftp-password", "key_file": "", "known_hosts": "example.invalid ssh-ed25519 AAAAtest"}


def drive_target():
    return {"id": "b" * 32, "type": "drive", "label": "Drive test", "enabled": True,
            "path": "HBlink4Backups", "client_id": "own-client.example", "client_secret": "private-client-secret",
            "token": json.dumps({"access_token": "private-access-token", "refresh_token": "private-refresh-token", "token_type": "Bearer"}),
            "folder_id": ""}


class TestSchedule(unittest.TestCase):
    def test_timezone_dst_and_new_activation(self):
        cfg = {**copy.deepcopy(auto.DEFAULT), "enabled": True, "timezone": "Europe/Stockholm", "at": "03:00"}
        before = datetime(2026, 11, 23, 1, 59, tzinfo=timezone.utc)
        after = datetime(2026, 11, 23, 2, 1, tzinfo=timezone.utc)
        self.assertIn("2026-11-22", auto.slot_for(cfg, before))
        self.assertIn("2026-11-23", auto.slot_for(cfg, after))
        cfg["enabled_since"] = after.timestamp()
        self.assertIsNone(auto.slot_for(cfg, after))
        self.assertEqual(auto.next_due(cfg, after), datetime(2026, 11, 24, 2, tzinfo=timezone.utc).timestamp())
        cfg.update(at="02:30", enabled_since=0)
        spring = datetime(2026, 3, 29, 1, 40, tzinfo=timezone.utc)
        self.assertIn("2026-03-29", auto.slot_for(cfg, spring))
        autumn1 = datetime(2026, 10, 25, 0, 40, tzinfo=timezone.utc)
        autumn2 = datetime(2026, 10, 25, 1, 40, tzinfo=timezone.utc)
        self.assertEqual(auto.slot_for(cfg, autumn1), auto.slot_for(cfg, autumn2))

    def test_weekly_and_retention_buckets(self):
        cfg = {**copy.deepcopy(auto.DEFAULT), "frequency": "weekly", "weekday": 0, "at": "03:00"}
        now = datetime(2026, 11, 25, 12, tzinfo=timezone.utc)
        self.assertIn("2026-11-23", auto.slot_for(cfg, now))
        cfg.update(keep_daily=2, keep_weekly=2)
        entries = [{"name": str(i), "created": (now - timedelta(days=i)).timestamp()} for i in range(15)]
        keep = auto.keep_names(entries, cfg)
        self.assertIn("0", keep)
        self.assertIn("1", keep)
        self.assertIn("3", keep)  # newest snapshot of previous ISO week
        self.assertNotIn("14", keep)

    def test_only_sftp_and_drive_and_key_export_gate(self):
        for kind in ("github", "ftp", "local", "s3"):
            with self.subTest(kind=kind), self.assertRaises(auto.AutoError):
                auto.validate({"targets": [{"type": kind}]}, auto.DEFAULT)
        with self.assertRaises(auto.AutoError):
            auto.validate({"enabled": True, "targets": [sftp_target()]}, auto.DEFAULT)
        previous = {**copy.deepcopy(auto.DEFAULT), "key_exported": True, "targets": [sftp_target()]}
        cfg = auto.validate({"enabled": True, "targets": [{**sftp_target(), "password": ""}]}, previous)
        self.assertEqual(cfg["targets"][0]["password"], "private-sftp-password")
        self.assertTrue(cfg["enabled"])
        bad = sftp_target()
        bad["known_hosts"] = ""
        with self.assertRaises(auto.AutoError):
            auto.validate({"targets": [bad]}, auto.DEFAULT)

    def test_provider_errors_do_not_echo_tokens(self):
        failed = Mock(returncode=1, stderr=b'invalid_grant private-refresh-token', stdout=b'')
        with patch.object(subprocess, "run", return_value=failed):
            with self.assertRaisesRegex(auto.AutoError, "abgelaufen") as result:
                auto._tool(["rclone", "anything"])
        self.assertNotIn("private-refresh-token", str(result.exception))


class EncryptionFixture(BackupFixture):
    def setUp(self):
        super().setUp()
        self.identity = self.base / "recovery.agekey"
        subprocess.run(["age-keygen", "-o", str(self.identity)], check=True, capture_output=True)
        self.host = {"root": str(self.root), "data_dir": str(self.data), "identity": str(self.identity), "instance": "c" * 32}
        self.vault = auto.Vault(self.root, self.data, self.host)

    def config(self):
        return auto.validate({"targets": [sftp_target(), drive_target()]}, {**copy.deepcopy(auto.DEFAULT), "key_exported": True})


@unittest.skipUnless(TOOLS, "age, age-keygen and rclone are required for encrypted backup integration tests")
class TestEncryption(EncryptionFixture):
    def test_archive_roundtrip_wrong_key_tampering_and_key_exclusion(self):
        self.create()
        encrypted = self.base / "backup.tar.gz.age"
        backup.encrypt_archive(self.archive, encrypted, backup.identity_recipient(self.identity))
        self.assertTrue(backup.encrypted(encrypted))
        clear = self.base / "decrypted.tar.gz"
        backup.plain_archive(encrypted, clear, self.identity)
        self.assertEqual(backup.digest(clear), backup.digest(self.archive))
        manifest = backup.unpack_archive(clear, self.base / "restored")
        self.assertFalse(any("recovery.agekey" in name for name in manifest["files"]))
        wrong = self.base / "wrong.agekey"
        subprocess.run(["age-keygen", "-o", str(wrong)], check=True, capture_output=True)
        with self.assertRaises(backup.BackupError):
            backup.plain_archive(encrypted, self.base / "wrong.tar.gz", wrong)
        changed = bytearray(encrypted.read_bytes()); changed[-1] ^= 1; encrypted.write_bytes(changed)
        with self.assertRaises(backup.BackupError):
            backup.plain_archive(encrypted, self.base / "bad.tar.gz", self.identity)
        self.assertFalse((self.base / "bad.tar.gz").exists())

    def test_credential_confidentiality_masks_and_unlock_on_another_server(self):
        cfg = self.config()
        self.vault.write(cfg)
        cipher = self.vault.path.read_bytes()
        for secret in (b'private-sftp-password', b'private-client-secret', b'private-refresh-token'):
            self.assertNotIn(secret, cipher)
            self.assertNotIn(secret.decode(), json.dumps(self.vault.public()))
        self.assertEqual(self.vault.read(), cfg)
        newer = self.base / "new-host.agekey"
        subprocess.run(["age-keygen", "-o", str(newer)], check=True, capture_output=True)
        other = auto.Vault(self.root, self.data, {**self.host, "identity": str(newer)})
        with self.assertRaises(auto.AutoError):
            other.read()
        other.recover(self.identity)
        restored = other.read()
        self.assertFalse(restored["enabled"])
        self.assertFalse(restored["key_exported"])
        self.assertEqual(restored["targets"][0]["password"], "private-sftp-password")
        other.reset()
        self.assertEqual(other.read()["targets"], [])
        self.assertTrue(list(self.control.glob("locked-autobackup-*.age")))

    def test_sftp_host_pinning_and_oauth_refresh_are_preserved(self):
        cfg = self.config()
        self.vault.write(cfg)
        work = self.base / "sftp-config"
        with auto.Remote(self.vault, cfg["targets"][0], cfg, work) as remote:
            parser = configparser.RawConfigParser(); parser.read(remote.ini)
            self.assertEqual(parser["target"]["known_hosts_file"], str(work / "known_hosts"))
            self.assertNotEqual(parser["target"]["pass"], "private-sftp-password")
            self.assertEqual((work / "known_hosts").read_text().strip(), sftp_target()["known_hosts"])
        self.assertFalse((work / "rclone.conf").exists())
        with auto.Remote(self.vault, cfg["targets"][1], cfg, self.base / "drive-config") as remote:
            parser = configparser.RawConfigParser(); parser.read(remote.ini)
            refreshed = json.dumps({"access_token": "new-access", "refresh_token": "new-refresh"})
            parser["target"]["token"] = refreshed
            with open(remote.ini, "w") as out:
                parser.write(out)
        self.assertEqual(self.vault.read()["targets"][1]["token"], refreshed)

    def test_real_rclone_copy_and_readback_with_local_backend(self):
        cfg = self.config()
        source = self.base / "send"; source.mkdir()
        payload = source / "test.age"; payload.write_bytes(os.urandom(65536))
        sink = self.base / "remote"
        with auto.Remote(self.vault, cfg["targets"][0], cfg, self.base / "runtime") as remote:
            # Only the test substitutes the transport; production validation
            # accepts SFTP/Drive exclusively. Exercise the real rclone CLI.
            remote.ini.write_text('[target]\ntype = local\n')
            remote.base = 'target:' + str(sink)
            remote.upload(payload)
            self.assertEqual((sink / payload.name).read_bytes(), payload.read_bytes())
            (sink / payload.name).write_bytes(b'bad data')
            with self.assertRaises(auto.AutoError):
                remote.call('check', source, remote.base, '--one-way', '--download')

    def test_auto_job_keeps_copy_and_never_prunes_after_upload_failure(self):
        cfg = self.config()
        self.vault.write(cfg)
        async def create(path):
            return await asyncio.to_thread(backup.create_archive, self.root, self.data, path)
        service = auto.AutoBackup(self.root, self.data, create, asyncio.Lock(), lambda: None, self.host)
        with patch.object(service, '_upload', side_effect=auto.AutoError('upload failed')), patch.object(service, '_prune') as prune:
            result = asyncio.run(service.run('daily:test'))
        self.assertFalse(result['ok'])
        prune.assert_not_called()
        archives = list(self.control.glob('auto-*.age'))
        self.assertEqual(len(archives), 1)
        backup.plain_archive(archives[0], self.base / 'surviving.tar.gz', self.identity)
        self.assertEqual(service.state()['schedule_attempts'], 1)
        with patch.object(service, '_upload'), patch.object(service, '_prune'):
            self.assertTrue(asyncio.run(service.run('daily:test'))['ok'])
        self.assertEqual(service.state()['schedule_attempts'], 2)
        self.assertEqual(service.state()['schedule_state'], 'success')

    def test_complete_job_checks_remote_and_prunes_only_verified_catalog(self):
        cfg = self.config(); cfg['targets'] = cfg['targets'][:1]
        cfg.update(keep_daily=2, keep_weekly=0)
        self.vault.write(cfg)
        sink = self.base / 'remote-history'
        sink.mkdir()
        class LocalRemote(auto.Remote):
            def __enter__(remote):
                super().__enter__()
                remote.ini.write_text('[target]\ntype = local\n')
                remote.base = 'target:' + str(sink)
                return remote
        catalog = {}
        for days in range(1,6):
            stamp = (datetime.now(timezone.utc)-timedelta(days=days)).strftime('%Y%m%dT%H%M%SZ')
            name = f'auto-{stamp}-{days:032x}.tar.gz.age'
            (self.control/name).write_bytes(b'old encrypted snapshot')
            (sink/name).write_bytes(b'old encrypted snapshot')
            catalog[name]={'name':name,'created':time.time()-86400*days,'local':True,'targets':['a'*32]}
        unknown='auto-20000101T000000Z-'+'f'*32+'.tar.gz.age'
        (sink/unknown).write_bytes(b'untracked, must survive')
        async def create(path):
            return await asyncio.to_thread(backup.create_archive,self.root,self.data,path)
        service=auto.AutoBackup(self.root,self.data,create,asyncio.Lock(),lambda:None,self.host)
        service.state_write({'status':'idle','catalog':catalog,'history':[]})
        with patch.object(auto,'Remote',LocalRemote):
            result=asyncio.run(service.run())
        self.assertTrue(result['ok'],result['state']['error'])
        self.assertEqual(len(service.state()['catalog']),2)
        self.assertTrue((sink/unknown).exists())
        latest=service.state()['last_name']
        self.assertEqual((sink/latest).read_bytes(),(self.control/latest).read_bytes())
        self.assertTrue(backup.encrypted(sink/latest))

    def test_scheduler_does_not_repeat_success_and_caps_failed_retries(self):
        cfg = self.config(); cfg['enabled'] = True
        service = auto.AutoBackup(self.root,self.data,None,asyncio.Lock(),lambda:None,self.host)
        for initial, expected in (({}, 1),
                                  ({'schedule_slot':'daily:test','schedule_state':'success'}, 0),
                                  ({'schedule_slot':'daily:test','schedule_state':'failed','schedule_attempts':3}, 0),
                                  ({'schedule_slot':'daily:test','schedule_state':'failed','schedule_attempts':1,
                                    'schedule_last_attempt':time.time()-1000}, 1)):
            with self.subTest(initial=initial):
                service.state_write({'status':'idle','history':[],'catalog':{},**initial})
                calls=[]; ticks=[]
                async def run(slot):
                    calls.append(slot)
                    service.state_write({'status':'success','schedule_slot':slot,'schedule_state':'success'})
                async def sleep(seconds):
                    ticks.append(seconds)
                    if len(ticks)==3:
                        raise asyncio.CancelledError()
                with patch.object(service.vault,'read',return_value=cfg), \
                     patch.object(service,'run',side_effect=run), patch.object(auto,'slot_for',return_value='daily:test'), \
                     patch.object(asyncio,'sleep',side_effect=sleep):
                    with self.assertRaises(asyncio.CancelledError):
                        asyncio.run(service.loop())
                self.assertEqual(len(calls),expected)


@unittest.skipUnless(TOOLS, "age/rclone are needed")
class TestAutoApi(EncryptionFixture):
    def setUp(self):
        super().setUp()
        from dashboard import server
        from fastapi.testclient import TestClient
        self.server = server
        self.saved = (server.state.auth, server.state._data_dir, server._version.REPO_ROOT, server._auto_cache)
        server.state.auth, server.state._data_dir, server._version.REPO_ROOT = self.auth, self.data, self.root
        self.manager = auto.AutoBackup(self.root, self.data, server._create_backup_snapshot, server._backup_lock, server._backup_idle, self.host)
        server._auto_cache = ((str(self.root),str(self.data)), self.manager)
        self.client = TestClient(server.app)
        self.addCleanup(self.client.close)
        self.addCleanup(self.restore_server)
        self.headers = {'X-Dashboard':'1'}

    def restore_server(self):
        self.server.state.auth, self.server.state._data_dir, self.server._version.REPO_ROOT, self.server._auto_cache = self.saved

    def login(self):
        result=self.client.post('/api/auth/login',json={'username':'source','password':'source-secret'},headers=self.headers)
        self.assertEqual(result.status_code,200)

    def test_api_requires_superadmin_and_masks_saved_secrets(self):
        for method,path in [('GET','/api/admin/autobackup'),('POST','/api/admin/autobackup/config'),
                            ('POST','/api/admin/autobackup/key'),('POST','/api/admin/autobackup/run'),
                            ('POST','/api/admin/autobackup/test/'+ 'a'*32),('POST','/api/admin/backup/recovery-key')]:
            self.assertEqual(self.client.request(method,path,json={} if method=='POST' else None,headers=self.headers).status_code,401,path)
        self.login()
        self.assertEqual(self.client.post('/api/admin/autobackup/config',json={'targets':[sftp_target()]},headers=self.headers).status_code,200)
        public=self.client.get('/api/admin/autobackup').text
        self.assertNotIn('private-sftp-password',public)
        bad=self.client.post('/api/admin/autobackup/key',json={'password':'wrong'},headers=self.headers)
        self.assertEqual(bad.status_code,400)
        key=self.client.post('/api/admin/autobackup/key',json={'password':'source-secret'},headers=self.headers)
        self.assertEqual(key.status_code,200)
        self.assertEqual(key.content,self.identity.read_bytes())
        self.assertEqual(key.headers['cache-control'],'no-store')

    def test_encrypted_upload_uses_one_time_key_and_never_deletes_host_key(self):
        self.login(); self.create()
        cipher=self.base/'input.age'
        backup.encrypt_archive(self.archive,cipher,backup.identity_recipient(self.identity))
        key=self.client.post('/api/admin/backup/recovery-key',content=self.identity.read_bytes(),headers=self.headers)
        self.assertEqual(key.status_code,200,key.text)
        token=key.json()['token']
        result=self.client.post('/api/admin/backup/inspect',content=cipher.read_bytes(),headers={**self.headers,'X-Backup-Key':token})
        self.assertEqual(result.status_code,200,result.text)
        self.assertFalse((self.control/('recovery-'+token+'.key')).exists())
        result=self.client.post('/api/admin/backup/inspect',content=cipher.read_bytes(),headers={**self.headers,'X-Backup-Key':'not-a-key-token'})
        self.assertEqual(result.status_code,200,result.text)
        self.assertTrue(self.identity.exists())

    def test_remote_http_is_blocked_for_credentials_and_key_uploads(self):
        self.login()
        from starlette.requests import Request
        scope={'type':'http','scheme':'http','server':('example.org',80),'path':'/', 'headers':[], 'client':('198.51.100.2',1234),'query_string':b''}
        with self.assertRaises(self.server._HTTPError):
            self.server._secure_backup_transport(Request(scope))
        scope['scheme']='https'
        self.server._secure_backup_transport(Request(scope))

    def test_regular_admin_cannot_access_any_auto_backup_route(self):
        self.auth.create_user(self.actor,'ordinary','password1','admin')
        self.client.post('/api/auth/login',json={'username':'ordinary','password':'password1'},headers=self.headers)
        routes=[('GET','/api/admin/autobackup'),('POST','/api/admin/autobackup/config'),
                ('POST','/api/admin/autobackup/key'),('POST','/api/admin/autobackup/run'),
                ('POST','/api/admin/autobackup/test/'+'a'*32),('POST','/api/admin/autobackup/unlock'),
                ('POST','/api/admin/autobackup/reset'),('POST','/api/admin/backup/recovery-key')]
        for method,path in routes:
            self.assertEqual(self.client.request(method,path,json={} if method=='POST' else None,
                                                headers=self.headers).status_code,403,path)

    def test_unlock_requires_password_and_one_time_key_then_disables_schedule(self):
        self.login()
        self.vault.write(self.config())
        new_key=self.base/'new-server.agekey'
        subprocess.run(['age-keygen','-o',str(new_key)],check=True,capture_output=True)
        self.manager.vault._host={**self.host,'identity':str(new_key)}
        self.assertTrue(self.client.get('/api/admin/autobackup').json()['locked'])
        uploaded=self.client.post('/api/admin/backup/recovery-key',content=self.identity.read_bytes(),headers=self.headers).json()
        body={'password':'wrong','key_token':uploaded['token']}
        self.assertEqual(self.client.post('/api/admin/autobackup/unlock',json=body,headers=self.headers).status_code,400)
        body['password']='source-secret'
        response=self.client.post('/api/admin/autobackup/unlock',json=body,headers=self.headers)
        self.assertEqual(response.status_code,200,response.text)
        status=self.client.get('/api/admin/autobackup').json()
        self.assertFalse(status['locked'])
        self.assertFalse(status['config']['enabled'])
        self.assertFalse(status['config']['key_exported'])
        self.assertEqual(self.client.post('/api/admin/autobackup/unlock',json=body,headers=self.headers).status_code,400)

    def test_manual_start_reserves_job_before_a_second_request_can_queue_it(self):
        from starlette.requests import Request
        self.vault.write(self.config())
        request=Request({'type':'http','scheme':'http','server':('testclient',80),'path':'/',
                         'headers':[],'client':('testclient',1234),'query_string':b''})
        async def scenario():
            hold=asyncio.Event()
            async def running():
                await hold.wait()
            with patch.object(self.server,'_require_backup_owner',return_value=self.actor), \
                 patch.object(self.manager,'run',side_effect=running):
                first=await self.server.admin_autobackup_run(request)
                self.assertTrue(first['queued'])
                try:
                    with self.assertRaises(self.server._HTTPError) as error:
                        await self.server.admin_autobackup_run(request)
                    self.assertEqual(error.exception.status,409)
                finally:
                    self.manager.task.cancel()
                    try:
                        await self.manager.task
                    except asyncio.CancelledError:
                        pass
        asyncio.run(scenario())
