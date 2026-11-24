"""Real foreign-Python archives and offline target venv/package checks."""
import json
import os
import shutil
import subprocess
import sys
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from dashboard import backup
from test_backup import BackupFixture

REPO = Path(__file__).resolve().parent.parent


class TestPythonCompatibility(BackupFixture):
    def wheel(self, requires_python='>=3.9'):
        folder = self.base / 'index'
        folder.mkdir(exist_ok=True)
        files = {
            'restore_compat_demo.py': b'VALUE = 42\n',
            'restore_compat_demo-1.0.dist-info/METADATA':
                f'Metadata-Version: 2.1\nName: restore-compat-demo\nVersion: 1.0\nRequires-Python: {requires_python}\n'.encode(),
            'restore_compat_demo-1.0.dist-info/WHEEL': b'Wheel-Version: 1.0\nGenerator: HBlink4-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n',
        }
        files['restore_compat_demo-1.0.dist-info/RECORD'] = ''.join(name + ',,\n' for name in files).encode()
        path = folder / 'restore_compat_demo-1.0-py3-none-any.whl'
        with zipfile.ZipFile(path, 'w') as archive:
            for name, content in files.items():
                archive.writestr(name, content)
        return path

    def source_archive(self, executable, wheel):
        source = self.base / ('source-python-' + Path(executable).name)
        subprocess.run([executable, '-m', 'venv', str(source)], check=True, capture_output=True, timeout=120)
        python = source / 'bin/python'
        site = Path(subprocess.check_output([python, '-c', 'import site; print(site.getsitepackages()[0])'], text=True).strip())
        # Install only this test's own small wheel, without a package index.
        with zipfile.ZipFile(wheel) as archive:
            for name in archive.namelist():
                target = site / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(name))
        code = 'import sys; sys.path.insert(0,sys.argv[1]); from dashboard import backup; backup.create_archive(sys.argv[2],sys.argv[3],sys.argv[4])'
        subprocess.run([python, '-c', code, str(REPO), str(self.root), str(self.data), str(self.archive)],
                       check=True, capture_output=True, timeout=120)
        return self.unpack()

    def owner(self, cfg, args, **kwargs):
        # Tests already run as an ordinary user; production drops to the
        # configured service account. All pip traffic stays in the local index.
        env = {**os.environ, 'PIP_CONFIG_FILE': '/dev/null', 'PIP_NO_INDEX': '1',
               'PIP_FIND_LINKS': str(self.base / 'index'), 'PYTHONNOUSERSITE': '1'}
        return backup.command(args, env=env, **kwargs)

    def cfg(self):
        return {'uid': os.getuid(), 'gid': os.getgid(), 'home': str(self.base)}

    def test_real_archives_from_311_and_314_prepare_for_target_python(self):
        sources = [shutil.which(name) for name in ('python3.11', 'python3.14')]
        sources = [name for name in sources if name and subprocess.check_output(
            [name, '-c', 'import sys; print("%s.%s"%sys.version_info[:2])'], text=True).strip()
            != '%s.%s' % sys.version_info[:2]]
        if not sources:
            self.skipTest('A second Python minor is required')
        wheel = self.wheel()
        for executable in sources:
            with self.subTest(source=executable):
                stage, manifest = self.source_archive(executable, wheel)
                self.assertTrue(backup.python_migration(manifest))
                prepared = self.control / ('restore-python-' + Path(executable).name)
                with patch.object(backup, '_as_owner', side_effect=self.owner):
                    backup.prepare_python_packages(self.cfg(), stage, prepared)
                    self.assertFalse((prepared / 'probe').exists())
                    self.assertTrue(list((prepared / 'wheels').glob('*.whl')))
                    # Recreate the final environment at its final path and
                    # install the prepared wheels with the network disabled.
                    final = self.base / ('final-' + Path(executable).name)
                    subprocess.run([sys.executable, '-m', 'venv', str(final)], check=True, capture_output=True, timeout=120)
                    requirements = self.base / ('target-requirements-' + Path(executable).name + '.txt')
                    requirements.write_text(backup.migration_requirements((stage / 'metadata/requirements-frozen.txt').read_text()))
                    self.owner(self.cfg(), [str(final / 'bin/python'), '-m', 'pip', 'install', '--no-index',
                                          '--find-links', str(prepared / 'wheels'), '-r', str(requirements)], timeout=120)
                    self.assertEqual(subprocess.check_output([final / 'bin/python', '-c',
                        'import restore_compat_demo; print(restore_compat_demo.VALUE)'], text=True).strip(), '42')
                    backup.cleanup_prepared_python(self.cfg(), prepared)
                self.assertFalse(prepared.exists())

    def test_real_incompatible_python_requirement_fails_preflight(self):
        executable = shutil.which('python3.11')
        if not executable or sys.version_info[:2] <= (3, 11):
            self.skipTest('Python 3.11 and a newer target required')
        wheel = self.wheel('>=3.9,<3.12')
        stage, manifest = self.source_archive(executable, wheel)
        prepared = self.control / 'restore-python-incompatible'
        with patch.object(backup, '_as_owner', side_effect=self.owner):
            try:
                with self.assertRaisesRegex(backup.BackupError, 'Paketprüfung für Ziel-Python'):
                    backup.prepare_python_packages(self.cfg(), stage, prepared)
            finally:
                backup.cleanup_prepared_python(self.cfg(), prepared)
        self.assertFalse(prepared.exists())
        self.assertTrue((self.root / 'venv/excluded').exists())

    def test_invalid_source_syntax_is_rejected_before_package_execution(self):
        self.create()
        stage, _ = self.unpack()
        (stage / 'app/hblink4/hblink.py').write_text('if\n')
        with patch.object(backup, '_as_owner') as owner:
            with self.assertRaisesRegex(backup.BackupError, 'hblink4/hblink.py.*nicht kompatibel'):
                backup.prepare_python_packages(self.cfg(), stage, self.control / 'restore-python-syntax')
        owner.assert_not_called()

    def test_migration_preserves_application_pins_and_uses_target_bootstrap_tools(self):
        source = 'pip==23.0.1\nsetuptools==66.1.1\nwheel==0.38.4\nPillow==11.0.0\nfastapi==0.115.0\n'
        self.assertEqual(backup.migration_requirements(source), 'Pillow==11.0.0\nfastapi==0.115.0\n')
        for invalid in ('--extra-index-url https://example.invalid', 'pkg @ file:///tmp/pkg', '-r /etc/passwd'):
            with self.subTest(invalid=invalid), self.assertRaises(backup.BackupError):
                backup.migration_requirements(invalid)
