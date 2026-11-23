# HBlink4 Scripts

## setup.sh / setup_restore.py

`sudo ./scripts/setup.sh` starts with the choice between a new configuration and
restoring an existing full backup. The restore path validates the archive and
asks for `RESTORE` before preparing the target. On a fresh target it installs
stopped units and the isolated restore helper; after success the worker starts
and enables the cloned services. Existing installations keep their service
account and use the normal recovery backup/rollback. A failed fresh restore
leaves its bootstrap stopped and disabled.

See **[Initial setup](../docs/de/installation.md#vorhandenes-backup-beim-initial-setup-einspielen)**.
The archive should be outside the checkout. `SKIP_SERVICES=1` is incompatible
with restore; `SKIP_APT=1` requires Python/venv/git and any needed certbot already installed.

## install_services.sh

Installs the systemd unit files with your user, group and paths filled in.

```bash
sudo ./scripts/install_services.sh
```

Documented in **[SYSTEMD.md](../SYSTEMD.md)**, which is the reference for
systemd deployment.

`ENABLE_SERVICES=0 START_SERVICES=0` installs the units without enabling or
starting them. Initial setup uses these switches while preparing a fresh restore.

## e2e_openbridge.py, e2e_daprs.py

End-to-end checks against a real HBlink4 on localhost (no network needed):

```bash
python3 scripts/e2e_openbridge.py   # OpenBridge trunks, transit, echo guard
python3 scripts/e2e_daprs.py        # DAPRS: GPS in a voice call and as an NMEA data call reach the dashboard
```
