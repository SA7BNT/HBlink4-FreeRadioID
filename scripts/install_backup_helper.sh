#!/bin/bash
# Install the narrowly scoped restore helper; no service is stopped/restarted.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ "$(id -u)" -ne 0 ]; then
    echo "Run: sudo ./scripts/install_backup_helper.sh" >&2
    exit 1
fi
/usr/bin/python3 - "$REPO_ROOT" <<'PY'
import json
import hashlib
import grp
import os
import pathlib
import pwd
import re
import shlex
import shutil
import subprocess
import sys

root = pathlib.Path(sys.argv[1]).resolve()
def show(unit, prop):
    return subprocess.check_output(['systemctl', 'show', unit, '-p', prop, '--value'], text=True).strip()

def fail(message):
    raise SystemExit(message)

if show('hblink4-restore.service', 'ActiveState') in ('active', 'activating', 'deactivating'):
    fail('A restore is running. Wait for it to finish before updating the helper.')

if any(c.isspace() or c in '$%' for c in str(root)):
    fail('The installation path must not contain whitespace, $ or %.')
parent = root.parent.stat()
if parent.st_uid != 0 or parent.st_mode & 0o022:
    fail('Use a root-protected parent directory, e.g. /opt/HBlink4. The restore helper must not operate in a user-writable parent.')
users = []
for unit in ('hblink4.service', 'hblink4-dash.service'):
    if show(unit, 'LoadState') != 'loaded' or pathlib.Path(show(unit, 'WorkingDirectory')).resolve() != root:
        fail(f'{unit} must be installed with WorkingDirectory={root}. Run scripts/install_services.sh first.')
    name = show(unit, 'User') or 'root'
    user = pwd.getpwuid(int(name)) if name.isdigit() else pwd.getpwnam(name)
    users.append(user)
    executable = show(unit, 'ExecStart')
    script = 'hblink4/hblink.py' if unit == 'hblink4.service' else 'dashboard/server.py'
    if str(root / 'venv/bin/python') not in executable or str(root / script) not in executable:
        fail(f'{unit} must use venv/bin/python and {script}. Run scripts/install_services.sh first.')
    if unit == 'hblink4.service' and str(root / 'config/config.json') not in executable:
        fail('hblink4.service must use config/config.json inside the installation.')
if users[0].pw_uid != users[1].pw_uid:
    fail('Both HBlink4 services must use the same account for full restore.')
user = users[0]
group = show('hblink4-dash.service', 'Group')
gid = (int(group) if group.isdigit() else grp.getgrnam(group).gr_gid) if group else user.pw_gid
if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]*[$]?', user.pw_name):
    fail('Unsupported service account name.')
python = root / 'venv/bin/python'
if not python.is_file():
    fail('The destination needs its Python environment at venv/bin/python.')
version = subprocess.check_output([str(python), '-c', 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")'], text=True).strip()
if version != f'{sys.version_info.major}.{sys.version_info.minor}':
    fail('The system Python and the HBlink4 venv must use the same major/minor version.')
data = root / 'dashboard/data'
for item in shlex.split(show('hblink4-dash.service', 'Environment')):
    if item.startswith('HBLINK4_DASH_DATA_DIR='):
        data = pathlib.Path(item.split('=', 1)[1])
data = data.resolve()
if any(c.isspace() or c in '$%\\"' for c in str(data)):
    fail('The data path must not contain whitespace, $, %, backslash or double quote.')
if not data.is_dir() or data == root or root.is_relative_to(data):
    fail('Invalid dashboard data directory.')
if not data.is_relative_to(root):
    parent = data.parent.stat()
    if parent.st_uid != 0 or parent.st_mode & 0o022:
        fail('External data needs a root-protected parent directory.')
    if data.stat().st_dev != root.parent.stat().st_dev:
        fail('Installation and external data must share a filesystem for transactional restore.')

module = root / 'dashboard/backup.py'
compile(module.read_text(), str(module), 'exec')
lib = pathlib.Path('/usr/local/lib/hblink4-backup')
for folder in (lib.parent, pathlib.Path('/usr/local/sbin')):
    folder.mkdir(parents=True, exist_ok=True)
    st = folder.stat()
    if st.st_uid != 0 or st.st_mode & 0o022:
        fail(f'{folder} must be root-owned and not group/world-writable.')
lib.mkdir(parents=True, exist_ok=True)
lib.chmod(0o755)
shutil.copyfile(module, lib / 'backup.py')
(lib / 'backup.py').chmod(0o644)
helper = pathlib.Path('/usr/local/sbin/hblink4-restore')
helper.write_text('#!/bin/sh\nexec /usr/bin/python3 -I /usr/local/lib/hblink4-backup/backup.py\n')
helper.chmod(0o755)
config = pathlib.Path('/etc/hblink4-backup.json')
config.write_text(json.dumps({'root': str(root), 'data_dir': str(data), 'uid': user.pw_uid,
                             'gid': gid, 'home': user.pw_dir,
                             'helper_sha256': hashlib.sha256(module.read_bytes()).hexdigest()}, indent=2) + '\n')
config.chmod(0o644)
for item in (lib, lib / 'backup.py', helper, config):
    os.chown(item, 0, 0)
unit_dir = pathlib.Path('/etc/systemd/system')
service = unit_dir / 'hblink4-restore.service'
service.write_text('''[Unit]
Description=HBlink4 verified backup restoration

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 -I /usr/local/lib/hblink4-backup/backup.py --worker
Nice=10
UMask=0077
TimeoutStartSec=0
StandardOutput=journal
StandardError=journal
''')
watcher = unit_dir / 'hblink4-restore.path'
watcher.write_text(f'''[Unit]
Description=Watch for confirmed HBlink4 restore requests

[Path]
PathExists={data}/.hblink4-backup/restore-request.json
Unit=hblink4-restore.service

[Install]
WantedBy=multi-user.target
''')
for unit in (service, watcher):
    os.chown(unit, 0, 0)
    unit.chmod(0o644)
subprocess.run(['systemctl', 'daemon-reload'], check=True)
subprocess.run(['systemctl', 'enable', '--now', 'hblink4-restore.path'], check=True)
print('Restore helper installed for', user.pw_name)
print('Installation:', root)
print('Dashboard data:', data)
print('No service was restarted. Admin → Backup can now restore archives.')
PY
