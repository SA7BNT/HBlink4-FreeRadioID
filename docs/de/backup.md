# Backup und Klonen

[← Adminbereich](adminbereich.md) · [Installation](installation.md) · [Betrieb](betrieb.md)

**Admin → Backup** erstellt eine übertragbare Sicherung der HBlink4-Installation.
Erstellen, Herunterladen, Prüfen und Wiederherstellen sind dem **Superadmin** vorbehalten,
weil das Archiv sämtliche Zugangsdaten und privaten Schlüssel enthalten kann.

Auf einem neuen Server kann die Sicherung außerdem direkt über **`sudo ./scripts/setup.sh` →
Vorhandenes HBlink4-Backup wiederherstellen** eingespielt werden. Dabei übernimmt das Setup
die Vorbereitung von Python-Umgebung, Diensten und Diensthelfer; ein vorheriger Dashboard-Login
ist nicht erforderlich. Siehe [Wiederherstellung beim Initial Setup](installation.md#vorhandenes-backup-beim-initial-setup-einspielen).

## Was gesichert wird

| Bestandteil | Verhalten |
|---|---|
| Programm und eigene Dateien | Gesamtes Installationsverzeichnis einschließlich lokaler Änderungen, Konfigurationen, Bilder und projektinterner Logs |
| Dashboard-Daten | Gesamtes Datenverzeichnis, Benutzer, Passwort-Hashes, Rechte, Einstellungen, Talkgroups, OpenBridge-Konfiguration, Statistiken, APRS-Daten und Caches |
| SQLite | Konsistente Kopien über die SQLite-Backup-API; bereits bestätigte Änderungen in WAL-Dateien sind enthalten |
| Zertifikate | Zertifikate im Datenverzeichnis und explizit konfigurierte TLS-Dateien; interne Zertifikatslinks werden als reguläre Dateien kopiert |
| Versionen | `VERSION`, Git-Bundle des aktuellen Commits und veröffentlichte Python-Paketversionen; Git-Zugangsdaten und Hooks werden nicht kopiert |
| systemd | Effektive Quell-Dienstdateien unter `metadata/services.json` als Referenz; auf dem Ziel gelten dessen bereits eingerichtete Dienste und Dienstkonten |

Die Sicherung bildet **HBlink4**, nicht das komplette Betriebssystem ab. Firewall, DNS,
IP-Adressen des Servers, FreePBX, systemweite Journale, Router-Portfreigaben und andere Dienste
werden nicht verändert. Laufende Funkverbindungen werden nach dem Neustart neu aufgebaut.
Virtuelle Python-Umgebungen, `.git`-Arbeitsdaten, Werkzeug-Caches, Sockets und vorhandene
Backup-Archive werden nicht rekursiv mitgesichert. Stattdessen werden Git-Commit und Paketversionen separat abgelegt.
Andere externe Symlinks oder lokale/editierbare Python-Abhängigkeiten führen zu einer klaren
Fehlermeldung, statt eine unvollständige Sicherung als vollständig auszugeben.

**Manuelle Archive sind nicht verschlüsselt.** [AutoBackup](autobackup.md) ergänzt
tägliche/wöchentliche, verschlüsselte Sicherungen nach SFTP oder Google Drive.
Manuelle Archive sicher herunterladen, geschützt aufbewahren und nicht
öffentlich teilen oder in Git einchecken. Nur vertrauenswürdige eigene Backups wiederherstellen:
Sie enthalten ausführbaren HBlink4-Programmcode. SHA-256 erkennt Beschädigungen, bestätigt aber
nicht die Herkunft einer Datei.

## Backup erstellen

1. Als Superadmin **Admin → Backup** öffnen.
2. **Backup erstellen und herunterladen** auswählen. Das Archiv wird zunächst auf dem Server
   erstellt; danach beginnt der Download. Der Funkbetrieb kann während der Sicherung weiterlaufen.
3. Den Download außerhalb des Servers sicher aufbewahren. Vorhandene Archive lassen sich im
   selben Menü erneut herunterladen, einschließlich automatischer Sicherungen vor einer Wiederherstellung.

Die Archive liegen in `dashboard/data/.hblink4-backup/`, bei einem abweichenden
`HBLINK4_DASH_DATA_DIR` entsprechend dort. Das Backup-Verzeichnis wird nicht öffentlich ausgeliefert.
Neben jedem gespeicherten Archiv steht **Löschen**. Nach Bestätigung wird ausschließlich
die lokale Datei entfernt; Kopien auf SFTP oder Google Drive bleiben erhalten. Das gilt
auch für verschlüsselte AutoBackups und Sicherungen vor einer Wiederherstellung.
Während einer laufenden Sicherung, Wiederherstellung oder Aktualisierung ist Löschen gesperrt.
Manuelle Archive bleiben bis zum Löschen erhalten; AutoBackup hat zusätzlich eine eigene
Aufbewahrung. Auf ausreichend freien Speicher achten.
Grenzen: **2 GiB komprimiert, 8 GiB entpackt, 100.000 Dateien**.

## Zielserver vorbereiten

Auf dem Ziel muss eine funktionierende HBlink4-Installation mit dieser Backup-Funktion
und den Diensten `hblink4.service` und `hblink4-dash.service` vorhanden sein.
Beide Dienste benötigen dasselbe Dienstkonto, dasselbe Installationsverzeichnis als
`WorkingDirectory` und die virtuelle Umgebung unter `venv/bin/python`.
Die System-Python-Version und beide HBlink4-Umgebungen müssen in **Haupt-/Nebenversion**
übereinstimmen, beispielsweise Python 3.11 auf Quelle und Ziel.

Im HBlink4-Verzeichnis des Zielservers einmal ausführen:

```bash
sudo ./scripts/install_backup_helper.sh
```

Der Installer prüft die Umgebung, installiert den Helfer rootgeschützt unter
`/usr/local/lib/hblink4-backup/` und `/usr/local/sbin/hblink4-restore` und erzeugt
`/etc/hblink4-backup.json`. Eine systemd-Path-Unit beobachtet bestätigte Wiederherstellungsaufträge
und startet den unabhängigen Root-Helfer. Das Dashboard benötigt keine sudo-Regel und behält
`NoNewPrivileges=true`. Die beiden HBlink4-Dienste werden beim Installieren nicht neu gestartet.

Der übergeordnete Installationsordner muss root gehören und darf nicht gruppen-/weltbeschreibbar
sein, beispielsweise `/opt` für `/opt/HBlink4`. Ein externes Datenverzeichnis braucht ebenfalls
einen rootgeschützten Elternordner und muss auf demselben Dateisystem liegen. Diese Bedingungen
verhindern, dass Verzeichnisse während einer privilegierten Wiederherstellung umgeleitet werden.
Nach Änderungen an `dashboard/backup.py` den Helfer erneut installieren; das Menü erkennt einen veralteten Helfer.

Für den Neuaufbau der Python-Umgebung benötigt der Zielserver Zugang zum konfigurierten
Python-Paketindex und die zur Plattform passenden Pakete/Build-Voraussetzungen. Bestehende
systemd-Einstellungen und Dienstkonten bleiben auf dem Ziel erhalten. Die Quell-Dienste werden
als Referenz gesichert, aber aus einem hochgeladenen Archiv niemals als root ausgeführt.

## Wiederherstellen

1. Auf dem **Zielserver** als dessen bisheriger Superadmin **Admin → Backup** öffnen.
2. Archiv auswählen, bei `.tar.gz.age` zusätzlich den separaten Wiederherstellungsschlüssel
   angeben und **Datei hochladen und prüfen** anklicken. Dateipfade, Dateitypen,
   Prüfsummen, Größen, Konfigurationen, Datenbanken und Python-Version werden geprüft.
3. Version, Datum und Dateianzahl in der Vorschau kontrollieren. Das aktuelle Passwort des
   **Zielservers** eingeben, `RESTORE` eintragen und die Wiederherstellung starten.
4. Der unabhängige Diensthelfer stoppt beide Dienste und erzeugt zunächst ein Backup des
   Zielzustands. Dann ersetzt er Programm und Daten, passt installationslokale Pfade an,
   baut die Python-Umgebung neu auf und startet die Dienste. Das kann mehrere Minuten dauern.
5. Seite neu öffnen und mit dem **Konto aus dem Quell-Backup** anmelden. Alle importierten
   Sitzungen wurden ungültig gemacht. Einstellungen, Talkgroups, Benutzer und APRS-Daten prüfen.

Ein Klon übernimmt auch Rufzeichen, Passwörter und OpenBridge-Identitäten des Quellservers.
Vor gleichzeitigem Betrieb beider Installationen deren Verbindungsidentitäten entsprechend anpassen.
Die Übertragung macht keine DNS-Änderung; die im Backup enthaltenen Dashboard-Domains müssen
für den Zielserver erreichbar sein. Andere Dienstkonten und Installationspfade auf dem Ziel
werden berücksichtigt; lokale Dateipfade in Konfiguration und Einstellungen werden angepasst.

## Fehler und Wiederherstellung des Zielzustands

Bei Fehlern während des Einspielens, beim Aufbau der Python-Umgebung oder beim Dienststart
spielt der Helfer den bisherigen Programm-, Daten- und Python-Umgebungszustand zurück und
startet die alten Dienste. Die Meldung steht nach der Anmeldung unter **Backup**.
Das zusätzliche Archiv `before-restore-…tar.gz` bleibt dort herunterladbar.

```bash
sudo journalctl -u hblink4-restore.service --no-pager -n 100
sudo systemctl status hblink4.service hblink4-dash.service --no-pager
```

Wenn auch die automatische Rücknahme scheitert, bleibt die bisherige Installation im
rootgeschützten Verzeichnis `hblink4-recovery-<Token>` neben dem Installationsverzeichnis erhalten.
Nach Stromausfall oder gewaltsamem Abbruch muss anhand dieses Verzeichnisses und des
automatischen Wiederherstellungsarchivs manuell geprüft werden; dafür gibt es keine Garantie
einer automatischen Rücknahme. Vor einer erneuten Wiederherstellung zuerst den Fehler beheben.

## Validierung der Funktion

Die automatisierten Tests prüfen echte Archiv-/SQLite-Rundläufe, WAL-Inhalte, Kontenübernahme,
Pfadanpassung, ungültige Archive, Berechtigungen, Upload-Veränderungen und die Rücknahme von
Programm, Daten und Python-Umgebung nach simulierten Dienstfehlern. systemd und die
Paketinstallation werden in diesen Tests ersetzt; eine produktive Wiederherstellung auf
einem zweiten Linux-Server muss vor dem ersten Migrationseinsatz zusätzlich erprobt werden.
