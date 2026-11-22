# HBlink4 Scripts

## install_services.sh

Installs the systemd unit files with your user, group and paths filled in.

```bash
sudo ./scripts/install_services.sh
```

Documented in **[SYSTEMD.md](../SYSTEMD.md)**, which is the reference for
systemd deployment.

## e2e_openbridge.py, e2e_daprs.py

End-to-end checks against a real HBlink4 on localhost (no network needed):

```bash
python3 scripts/e2e_openbridge.py   # OpenBridge trunks, transit, echo guard
python3 scripts/e2e_daprs.py        # DAPRS: GPS in a voice call and as an NMEA data call reach the dashboard
```
