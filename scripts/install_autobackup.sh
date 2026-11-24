#!/bin/bash
# Prepare age/rclone and a host-local private identity. No cloud connection.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[ "$(id -u)" -eq 0 ] || { echo "Run: sudo ./scripts/install_autobackup.sh" >&2; exit 1; }
if ! command -v age >/dev/null || ! command -v age-keygen >/dev/null || ! command -v rclone >/dev/null; then
    if [ "${SKIP_APT:-0}" = 1 ] || ! command -v apt-get >/dev/null; then
        echo "Install age and rclone first, or run without SKIP_APT=1." >&2
        exit 1
    fi
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y age rclone
fi
"$REPO_ROOT/scripts/install_backup_helper.sh"
/usr/bin/python3 - "$REPO_ROOT" <<'PY'
import json, os, pathlib, subprocess, sys, uuid
root = pathlib.Path(sys.argv[1]).resolve()
restore = json.loads(pathlib.Path('/etc/hblink4-backup.json').read_text())
if restore['root'] != str(root):
    raise SystemExit('The restore helper belongs to another installation.')
config = pathlib.Path('/etc/hblink4-autobackup.json')
if config.is_symlink():
    raise SystemExit('The AutoBackup configuration must not be a symlink.')
old = json.loads(config.read_text()) if config.exists() else {}
if old and old.get('root') != str(root):
    raise SystemExit('AutoBackup is already assigned to another installation.')
secret_dir = pathlib.Path('/var/lib/hblink4-secrets')
secret_dir.mkdir(exist_ok=True, mode=0o750)
if secret_dir.is_symlink():
    raise SystemExit('The secrets directory must not be a symlink.')
os.chown(secret_dir, 0, restore['gid'])
secret_dir.chmod(0o750)
identity = secret_dir / 'autobackup.agekey'
if identity.is_symlink():
    raise SystemExit('The private identity must not be a symlink.')
if not identity.exists():
    subprocess.run(['age-keygen', '--output', str(identity)], check=True, stdout=subprocess.DEVNULL)
subprocess.run(['age-keygen', '-y', str(identity)], check=True, stdout=subprocess.DEVNULL)
os.chown(identity, restore['uid'], restore['gid'])
identity.chmod(0o600)
value = {'root': str(root), 'data_dir': restore['data_dir'], 'identity': str(identity),
         'instance': old.get('instance') or uuid.uuid4().hex}
config.write_text(json.dumps(value, indent=2) + '\n')
os.chown(config, 0, 0)
config.chmod(0o644)
print('AutoBackup prepared. No destination was contacted and scheduling is off until enabled in Admin → Backup.')
print('Export the recovery key there and keep it outside this server.')
PY
