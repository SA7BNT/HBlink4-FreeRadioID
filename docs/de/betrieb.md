# 🩺 Betrieb & Fehlersuche

[← Übersicht](../../readme.md) · [Installation](installation.md) · [Dashboard](dashboard.md) · [Adminbereich](adminbereich.md) · [Konfiguration](konfiguration.md) · [FreeRadioID](freeradioid.md) · **Betrieb & Fehlersuche**

---

## Die beiden Dienste

| Dienst | Was | Log |
|---|---|---|
| `hblink4` | DMR-Server (UDP 62031) | `journalctl -u hblink4` und `logs/hblink.log` |
| `hblink4-dash` | Dashboard und Adminbereich (8080, mit HTTPS 80/443) | `journalctl -u hblink4-dash` |

```bash
sudo systemctl status  hblink4 hblink4-dash
sudo systemctl restart hblink4 hblink4-dash
sudo journalctl -u hblink4 -u hblink4-dash -f          # live mitlesen
```

> 💡 **Immer mit `sudo`.** Ohne `sudo` fragt systemd (polkit) nach einem Passwort und wartet. Das sieht dann so aus, als würde der Neustart „hängen“.
> Soll der Dienst-Benutzer die Dienste ohne Passwort neu starten dürfen, hilft eine sudoers-Regel:
>
> ```bash
> echo 'hblink ALL=(root) NOPASSWD: /usr/bin/systemctl restart hblink4, /usr/bin/systemctl restart hblink4-dash, /usr/bin/systemctl restart hblink4 hblink4-dash' \
>   | sudo tee /etc/sudoers.d/hblink4 && sudo chmod 440 /etc/sudoers.d/hblink4 && sudo visudo -c
> ```
> (`hblink` durch deinen Dienst-Benutzer ersetzen.)

Aus dem Browser geht es auch: **Adminbereich → System → Restart dashboard / Restart DMR server**.

**Zum Testen ohne systemd** gibt es `./run_all.sh`. Es startet beides im Vordergrund und beendet es mit Strg+C. Es sollte nur laufen, wenn die Dienste gestoppt sind, und immer als Dienst-Benutzer, nicht als root.

## Schnell-Check: `scripts/check.sh`

```bash
sudo /opt/HBlink4/scripts/check.sh
```

Das Skript prüft der Reihe nach:
- die Dienste und ob sie in einer Neustart-Schleife hängen
- Dateirechte, den Socket `/tmp/hblink4.sock` und die Ports (aus dem Adminbereich bzw. den Config-Dateien)
- ob HBlink4 und das Dashboard als Dienst-Benutzer starten

Zu jedem Problem steht die passende Lösung mit **→** dahinter. Am Ende steht `No problems found.` oder die Zahl der Probleme.

## Typische Fehler

| Meldung / Symptom | Ursache | Lösung |
|---|---|---|
| `attempt to write a readonly database` | Dateien gehören root, weil etwas mit `sudo` gestartet wurde | `sudo chown -R hblink:hblink /opt/HBlink4` und Dienste neu starten |
| Dashboard zeigt **DMR Server** rot | Socket `/tmp/hblink4.sock` gehört dem falschen Benutzer, oder HBlink4 läuft nicht | `sudo rm -f /tmp/hblink4.sock && sudo systemctl restart hblink4-dash hblink4` |
| `Address already in use` (Port 62031) | Ein anderer DMR-Server (z. B. ADN/FreeDMR-Proxy) belegt den Port | anderen Port im Adminbereich wählen bzw. den anderen Dienst stoppen |
| HTTPS startet nicht | Domain zeigt nicht auf den Server, Port 80 zu oder belegt | Fehler im Log lesen; das Dashboard läuft so lange per HTTP weiter |
| `hblink4-dash` startet in einer Schleife | fehlende Python-Module | `sudo -u hblink ./venv/bin/pip install -r requirements.txt -r requirements-dashboard.txt` |
| Station kommt nicht rein (MSTNAK) | falsches Passwort, ID auf der Blacklist oder FreeRadioID.net nicht erreichbar | `journalctl -u hblink4` zeigt den Grund; zum Test das Gast-Passwort nehmen |
| Keine Avatare | ID nicht bei FreeRadioID.net registriert, Avatare abgeschaltet oder Liste noch nicht geladen | Adminbereich → System zeigt die Zahl der gespeicherten Avatare; **⟳ Refresh now** unter Callsign database |
| Superadmin-Passwort vergessen | – | neuen Hash erzeugen (`./venv/bin/python -m dashboard.auth hash-password`), in `dashboard/config.json` eintragen, Dashboard neu starten; oder `sudo ./scripts/setup.sh` |

## Sicherung

Alles Wichtige steckt in drei Dateien:

```bash
cd /opt/HBlink4
sudo -u hblink sqlite3 dashboard/data/dashboard.db ".backup '/tmp/dashboard-$(date +%F).db'"
sudo tar czf ~/hblink4-backup-$(date +%F).tgz config/config.json dashboard/config.json /tmp/dashboard-$(date +%F).db
```

`.backup` sichert die Datenbank auch im laufenden Betrieb sauber; dafür braucht es das Paket `sqlite3`. **Zurückspielen:** Dienste stoppen, die drei Dateien an ihren Platz kopieren, die Rechte für den Dienst-Benutzer setzen und die Dienste starten.

## Umziehen auf einen neuen Server

1. Auf dem neuen Server [installieren](installation.md) mit `setup.sh`.
2. Dienste stoppen und die drei Dateien aus der Sicherung einspielen.
3. `sudo chown -R hblink:hblink /opt/HBlink4 && sudo systemctl start hblink4 hblink4-dash`.
4. Den DNS-Eintrag umstellen. Das HTTPS-Zertifikat holt das Dashboard auf dem neuen Server selbst.

---

[← Zurück zur Übersicht](../../readme.md)
