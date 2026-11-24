# Automatische verschlüsselte Backups

[← Backup und Klonen](backup.md) · [Initial Setup](installation.md)

**Admin → Backup → AutoBackup** erstellt vollständige HBlink4-Sicherungen täglich
oder wöchentlich. Jede Sicherung bleibt verschlüsselt auf dem Server und wird an
alle aktivierten **SFTP- und Google-Drive-Ziele** übertragen. GitHub ist kein Ziel.
Die Funktion ist dem Superadmin vorbehalten und anfangs deaktiviert.

## Einmal vorbereiten

Im aktuellen HBlink4-Verzeichnis auf dem Linux/systemd-Server ausführen:

```bash
sudo ./scripts/install_autobackup.sh
```

Das Skript installiert bei Bedarf die Debian-Pakete `age` und `rclone`, aktualisiert
den Wiederherstellungshelfer und erstellt eine servereigene Installationskennung
sowie einen privaten age-Schlüssel. Vorhandene Schlüssel bleiben erhalten.
Es startet keine HBlink4-Dienste neu und verbindet sich mit keinem Backup-Ziel.
Bei `SKIP_APT=1` müssen die Werkzeuge bereits installiert sein. Die Voraussetzungen
des [Wiederherstellungshelfers](backup.md#zielserver-vorbereiten) gelten ebenfalls.

1. **Admin → Backup** über HTTPS öffnen. Alternativ einen lokalen SSH-Tunnel zum
   Dashboard verwenden, beispielsweise `ssh -L 18080:127.0.0.1:8080 freepbx` und
   `http://127.0.0.1:18080` öffnen; `8080` durch den tatsächlichen Dashboard-Port ersetzen.
2. Superadmin-Passwort eingeben und **Wiederherstellungsschlüssel herunterladen**.
   Die `.agekey`-Datei getrennt von diesem Server sicher aufbewahren. Ohne diesen
   Schlüssel lässt sich ein verschlüsseltes Backup auf einem anderen Server nicht öffnen.
3. SFTP oder Google Drive hinzufügen, Angaben speichern und **Gespeichertes Ziel testen**
   auswählen. Der Test schreibt, liest, vergleicht und löscht eine zufällige kleine Prüfdatei.
4. **Verschlüsseltes Backup jetzt ausführen** auswählen. Nach Abschluss **Status aktualisieren**
   anklicken und den Erfolg für den vollständigen Lauf kontrollieren.
5. Rhythmus, Uhrzeit und Zeitzone wählen, **AutoBackup aktivieren** und speichern.

Der Zeitplan läuft im Dashboard-Dienst, auch wenn kein Browser geöffnet ist.
Für AutoBackup sind keine eingehenden Firewall-Ports nötig; der Server baut die
Verbindungen zum SFTP-Server beziehungsweise zu Google selbst auf.

## SFTP

Benötigt werden Host, Port (normalerweise 22), Benutzer, Zielordner und entweder
Passwort oder der absolute Pfad zu einer SSH-Schlüsseldatei auf dem HBlink4-Server.
Die Datei muss für den Dashboard-Dienstbenutzer lesbar sein. Eine Passphrase für
verschlüsselte SSH-Schlüsseldateien wird in diesem Dialog nicht unterstützt.
Einen unverschlüsselten, ausschließlich für Backups vorgesehenen
SSH-Schlüssel mit eingeschränktem Zielkonto verwenden oder Passwortanmeldung wählen.

**Geprüfter SSH-Hostschlüssel** erwartet die OpenSSH-`known_hosts`-Zeile für genau
diesen Host und Port. Den Fingerabdruck unabhängig beim Serverbetreiber prüfen.
Ein ungeprüftes `ssh-keyscan` allein bestätigt die Identität nicht. Ohne passende
Zeile oder bei geändertem Schlüssel bricht rclone die Verbindung ab.
Siehe [rclone: Host key validation](https://rclone.org/sftp/#host-key-validation).

Der Zielordner muss beschreibbar sein. Darunter legt HBlink4 einen Unterordner
`hblink4-<Installationskennung>` an. Eine externe SSH-Schlüsseldatei gehört nicht
zum HBlink4-Backup und muss auf einem neuen Server separat bereitgestellt werden.

## Google Drive

Drive benötigt einen **eigenen Google OAuth-Client**, Client ID, Client Secret und
ein Token-JSON mit `refresh_token`. Der gemeinsame rclone-Client ist für diese
Einrichtung nicht vorgesehen. Die Anleitung zur Anlage und Freigabe des Clients
steht unter [rclone: eigener Client](https://rclone.org/drive/#making-your-own-client-id).

Auf einem vertrauenswürdigen Computer mit Browser `rclone config` ausführen:
einen neuen Drive-Zugang mit dem eigenen OAuth-Client und Scope **`drive.file`**
einrichten, bei Google anmelden und den Zugriff erlauben. Das vollständige
`token = {...}`-JSON aus der dortigen privaten rclone-Konfigurationsdatei als
**OAuth Token-JSON** eintragen, ohne den Präfix `token =`. Client Secret und Token
nicht als Shell-Argument oder in öffentliche Logs kopieren. Weitere Möglichkeiten:
[rclone: Remote Setup](https://rclone.org/remote_setup/).

`drive.file` beschränkt den Zugriff auf von dieser Anwendung angelegte oder dafür
freigegebene Dateien. **Folder ID** am besten leer lassen, damit rclone den
Zielordner selbst anlegt. Ein fremder, im Browser angelegter Ordner ist mit diesem
Scope nicht automatisch zugänglich. Dateien bleiben in der Drive-Weboberfläche sichtbar.
Bei OAuth-Apps im Testmodus können Freigaben auslaufen; bei `invalid_grant` erneut
autorisieren und das neue Token speichern. Automatisch erneuerte Tokens werden
von HBlink4 wieder verschlüsselt gespeichert.

## Zeitplan und Aufbewahrung

| Einstellung | Verhalten |
|---|---|
| Rhythmus | Täglich oder an einem gewählten Wochentag |
| Zeitzone | IANA-Name, etwa `Europe/Stockholm` oder `Europe/Berlin`; berücksichtigt Sommerzeit |
| Nachholen | Nach einem Neustart höchstens den letzten fälligen Termin seit Aktivierung nachholen |
| Wiederholung | Höchstens drei Versuche pro Termin, nach Fehlern 15 und dann 30 Minuten Abstand |
| Tägliche Stände | Jeweils neuester Stand der letzten belegten Sicherungstage; Standard 7 |
| Wöchentliche Stände | Zusätzlich neuester Stand je ISO-Woche; Standard 4 |
| Übertragung | Ein Upload gleichzeitig, standardmäßig höchstens 2 MiB/s |

Ein Tages- und ein Wochenstand können dieselbe Datei sein. Es wird erst bereinigt,
wenn der neue Stand **auf allen aktivierten Zielen hochgeladen und durch erneutes
Lesen vollständig verglichen** wurde. Gelöscht werden ausschließlich von dieser
Installation katalogisierte AutoBackup-Dateien. Fremde Dateien und manuelle
Backups bleiben erhalten. Änderungen am Zielordner verschieben alte Sicherungen
nicht; dort verbleibende Dateien gegebenenfalls selbst verwalten.

Bei Fehlern bleibt die bereits verschlüsselte lokale Sicherung herunterladbar;
es findet keine Bereinigung statt. Wiederholte Fehler können deshalb zusätzlichen
Speicher belegen. Für Erstellung und Prüfung wird außerdem temporärer Platz für
das Archiv, seine entpackte Prüfung und eine entschlüsselte Vergleichskopie benötigt.
Während Updates, Sicherungen oder Wiederherstellungen wird kein zweiter Lauf gestartet.
Status, letzter Erfolg und die letzten 100 Ergebnisse werden gespeichert; das Menü
zeigt die letzten fünf. Der manuelle Knopf startet auch bei deaktiviertem Zeitplan.

## Verschlüsselung und Wiederherstellung

AutoBackup-Dateien heißen `auto-<UTC-Datum>-<Kennung>.tar.gz.age`. Der Inhalt wird
mit age verschlüsselt. Dateinamen, Zeitpunkt und Größe bleiben beim Zielanbieter
sichtbar. Das Archiv wird vor dem Upload vollständig geprüft, entschlüsselt und
mit dem ursprünglichen Archiv verglichen. Während der Erstellung liegen private
temporäre Klartextdateien auf dem Server; anschließend werden sie entfernt.

Die Zugangsdaten liegen in `dashboard/data/autobackup-config.age` verschlüsselt.
Der Lauf erzeugt eine nur für das Dienstkonto lesbare temporäre rclone-Konfiguration
und entfernt sie danach. Die API liefert gespeicherte Passwörter und Tokens nicht
zurück; leere Felder behalten bestehende Werte. Der private Wiederherstellungsschlüssel
liegt unter `/var/lib/hblink4-secrets/autobackup.agekey`, außerhalb von Programm und
Dashboard-Daten. `/etc/hblink4-autobackup.json` enthält nur Pfade und Installationskennung.
Der Dienst kann den Schlüssel lesen; die Verschlüsselung schützt keine bereits
kompromittierte laufende Installation.

**Bestehender Zielserver:** Im Menü **Backup** die `.tar.gz.age`-Datei und den separat
aufbewahrten Wiederherstellungsschlüssel auswählen, hochladen und prüfen. Auf demselben
Server kann dessen installierter Schlüssel automatisch verwendet werden. Danach gilt
der normale Ablauf mit Passwort, `RESTORE`, Recovery-Backup und Rücknahme bei Fehlern.
Zum Entschlüsseln muss das Paket `age` auf dem Ziel installiert sein.

**Initial Setup:** `sudo ./scripts/setup.sh` starten, Wiederherstellung wählen und
die verschlüsselte Datei angeben. Das Setup fragt den lokalen Pfad zur `.agekey`-Datei
ab und prüft das entschlüsselte Archiv vor Änderungen am Ziel. Mit `SKIP_APT=1` muss
`age` bereits installiert sein. Es gelten die Größenlimits und die
[Python-Kompatibilitätsprüfung](backup.md#zielserver-vorbereiten); Quell- und Ziel-Unterversion dürfen abweichen.

**AutoBackup auf dem Klon:** `sudo ./scripts/install_autobackup.sh` ausführen.
Der neue Server erhält einen eigenen Schlüssel und Zielunterordner. Übernommene
Zugangsdaten sind zunächst gesperrt. Im Backup-Menü mit dem ursprünglichen Schlüssel
und dem aktuellen Superadmin-Passwort entsperren oder mit `RESET` neu einrichten.
Beim Entsperren werden die Zugangsdaten mit dem neuen Schlüssel verschlüsselt und
der Zeitplan deaktiviert. Den neuen Schlüssel exportieren, Ziele testen und erst
dann wieder aktivieren. Die ursprüngliche verschlüsselte Konfiguration bleibt beim
Zurücksetzen als private Recovery-Datei erhalten; vorhandene Archive werden nicht gelöscht.

Manuelle `.tar.gz`-Backups und Recovery-Backups vor einer Wiederherstellung bleiben
unverschlüsselt. Auch sie enthalten sensible Daten und müssen geschützt aufbewahrt werden.

## Prüfung und Grenzen

Tests führen echte age-Verschlüsselung/Entschlüsselung und rclone-Übertragung samt
Rücklesen auf einem lokalen Testziel aus. Sie prüfen unter anderem beschädigte
Archive, falsche Schlüssel, Zugriff nur als Superadmin, verschlüsselte Zugangsdaten,
Aufbewahrung und den Wiederherstellungseinstieg beim Setup. Öffentliche SFTP-/Drive-Konten
und ein vollständiger systemd-Klon auf einem zweiten Linux-Server sind damit nicht
ersetzt. Vor produktiver Nutzung den Zieltest und eine Wiederherstellungsprobe mit
den eigenen Zugangsdaten durchführen.
