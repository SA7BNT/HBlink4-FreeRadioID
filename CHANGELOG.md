# Änderungen

Alle Versionen von **HBlink4-FreeRadioID** (DMR Die N8schicht), die neueste oben.
Die Nummern folgen [Semantic Versioning](https://semver.org/lang/de/): `MAJOR.MINOR.PATCH`.
Wie Versionen entstehen, steht in [docs/de/versionen.md](docs/de/versionen.md).

## [1.2.0] – 2026-11-22

**APRS für DMR Die N8schicht:** ein APRS-Server in Python, Uplink & Downlink wahlweise zu APRS-IS, CB-APRS oder eigenen
Servern, **DAPRS** (GPS-Positionen von DMR-Funkgeräten werden APRS-Positionen) und eine Live-Karte im N8schicht-Design –
auch unter bis zu drei eigenen Domains mit Let's-Encrypt-Zertifikat. Alles im Adminbereich unter **📡 APRS**.
**APRS ist nach dem Update ausgeschaltet**; es ändert sich nichts, bis du es einschaltest. Anleitung: [docs/de/aprs.md](docs/de/aprs.md).

### 🛰️ DAPRS – GPS aus dem DMR-Verkehr (DMR-Server)
- **Embedded GPS:** Funkgeräte, die ihre Position während eines Sprachrufs senden („GPS Info“-LC in den Bursts B–E),
  werden erkannt. Jeder Block wird vollständig geprüft (Hamming, Spaltenparität, Prüfsumme); fehlerhafte werden verworfen.
- **GPS-Datenrufe:** Datenkopf + Rate-½-Blöcke mit **NMEA** (`$GPRMC`/`$GNRMC`/`$GPGGA`/`$GNGGA`, auch als UTF-16-SMS
  und in IP/UDP) und **Motorola LRRP** (UDP 4001), z. B. „Digital APRS“ als Privatruf an `900999`. Rate ¾ und Rate 1
  werden nicht geraten, sondern gezählt.
- Ziele einschränkbar (*Accept GPS data sent to*), Wiederholungen derselben Station innerhalb von 10 s werden unterdrückt.
- Kostet im Hot Path nur einen Vergleich pro Burst; neue Logzeile `📍 DAPRS … position from …`; neues Ereignis
  `gps_position` an das Dashboard. Neue DMR-Einstellungen `dmr.daprs.*` (folgen dem APRS-Schalter).

### 📡 APRS-Server (Dashboard)
- APRS-IS-kompatibel: Port **14580** mit Filtern (`r/ a/ p/ b/ o/ t/ s/ d/ e/ g/ u/ m/ f/`, Ausschluss mit `-`,
  `#filter` im Betrieb) und **10152** Full Feed; Login mit Passcode, `logresp`, Keep-alive, q-Konstrukt (`qAC`, `qAR`, `qAO`),
  Schleifen- und Duplikatschutz (30 s), Flutschutz, Verbindungsgrenzen pro IP, langsame Clients werden getrennt.
- Nachrichten erreichen ihren Empfänger auch außerhalb seines Filters; Clients ohne gültigen Passcode dürfen nur empfangen.
- Ports nur nach Einschalten (*Open the APRS-IS ports*) und nur mit gesetzter **Server-ID** (Rufzeichen).

### 🌍 Uplink & Downlink – wählbar
- **APRS-IS** (Vorgabe `euro.aprs2.net:14580`), **CB-APRS** (`eu.cbaprs.net:27235`, eigenes Login und Passcode oder `-1`)
  oder **manuell**: beliebig viele eigene Server, jeder mit Login, Passcode, Downlink-Filter und Richtung
  (⇅ hoch & runter, ↑ nur hoch, ↓ nur runter).
- Gesendet wird nur über verifizierte Verbindungen; was aus einem Netz kommt, geht nie in ein anderes zurück.
- Änderungen wirken sofort: Server und Verbindungen starten von selbst neu.

### 📍 DMR → APRS
- Rufzeichen aus der ID-Liste (FreeRadioID.net/radioid.net), **SSID** (Vorgabe 7), **Symbol**, **Kommentar** mit
  Platzhaltern (`{tg}`, `{repeater}`, `{dmrid}`, `{slot}`, `{server}`), Tabelle **pro DMR-ID** (eigenes Rufzeichen, SSID,
  Symbol, Kommentar oder abschalten).
- Höchstens eine Position pro Station und Minute ins Netz (einstellbar); **Gäste** (FreeRadioID-Gastzugang) nur auf der lokalen Karte.
- **Repeater als APRS-Objekte** aus den Login-Daten (Frequenz, Ablage, Colour Code); Hotspots möglich, aber aus
  (Wohnadressen); Objekte verlassener Repeater werden gelöscht gemeldet.

### 🗺️ APRS-Karte
- Leaflet-Karte im N8-Design (folgt dem Farbprofil, dunkle Karte bei dunklen Profilen) mit Stationsliste, Suche,
  Filtern (DMR, Repeater, Clients, APRS-IS/CB-APRS, Wetter), APRS-Symbolen, Detailfenster (QTH-Locator, DMR-ID,
  Talkgroup, Hotspot/Repeater, Art der GPS-Meldung, Avatar, Wetter, Rohpakete), Spuren und Live-Paketliste per WebSocket.
- Unter `/aprs`, als Tab **🗺️ APRS** im Dashboard und unter **bis zu drei eigenen Domains** – jede mit eigenem Titel,
  Untertitel und Logo; dort steht unter `/` die Karte, das Dashboard unter `/dashboard`. Am Smartphone als Bottom-Sheet.
- Öffentlich oder nur mit Login – unabhängig vom Dashboard. Kartenstile: automatisch, dunkel, OpenStreetMap, OpenTopoMap, eigener Kachelserver.

### 🔒 Let's Encrypt für die APRS-Domains
- Domains mit *HTTPS* kommen als zusätzliche Namen in das Zertifikat des Dashboards – nur wenn sie per DNS auf diesen
  Server zeigen (sonst eine Logzeile statt eines kaputten Zertifikats). Fehlende Namen werden im Hintergrund nachgeholt
  und ohne Neustart geladen; HTTP leitet auf HTTPS derselben Domain um. Die Übersicht zeigt den Status jeder Domain.

### 💾 Speicherung
- Eigene SQLite-Datenbank `dashboard/data/aprs.db` (Stationen, Spuren, Rohpakete), Schreiben gebündelt im Hintergrund,
  **höchstens 90 Tage** (Vorgabe: Stationen/Spuren 30 Tage, Rohpakete 7 Tage), stündliche Bereinigung.

### ⚙️ Adminbereich und Installation
- Neuer Bereich **📡 APRS**: Übersicht (Ports, Server-ID mit Passcode, jede Verbindung mit Zustand und Zählern, DAPRS,
  Stationen, Zertifikatsstatus, Passcode-Rechner, Clients mit **Disconnect**), Server, Uplink & Downlink, DAPRS-Erkennung,
  DMR → APRS, Karte, Domains.
- `scripts/setup.sh` hat einen neuen Schritt **APRS** (Vorgabe aus): Server-ID, Karten-Domains, Ports, Netz; öffnet bei
  Bedarf TCP 14580/10152 in ufw. `scripts/check.sh` zeigt die APRS-Ports.

### Technik
- Neue Dateien: `hblink4/daprs.py`, `dashboard/aprs/` (`packet.py`, `filters.py`, `store.py`, `service.py`),
  `dashboard/static/aprs.html`, Leaflet 1.9.4 (`dashboard/static/vendor/leaflet/`, BSD-2) und die APRS-Symbole von
  hessu/aprs-symbols (`dashboard/static/aprs-symbols/`, Lizenzen in `COPYRIGHT.md`), `scripts/e2e_daprs.py`.
- Keine neuen Python-Abhängigkeiten. Neue Tests: `tests/test_daprs.py`, `tests/test_aprs.py`; insgesamt 323.

### Wichtig beim Update
- Nichts zu tun: APRS bleibt aus. Zum Einschalten: Adminbereich → **📡 APRS** oder `sudo ./scripts/setup.sh` erneut ausführen.
- Für APRS-Apps die Ports TCP 14580 (und 10152) in der Firewall öffnen; für eigene Karten-Domains DNS-Einträge anlegen.

## [1.1.1] – 2026-11-22

- Link-Site-Icons werden um Mitternacht neu geladen; im Admin-Linkmenü gibt es zusätzlich **Refresh site icons now**.
- Der automatische GitHub-Updatecheck läuft zu vollen Stunden (bei Intervallen über einer Stunde an den passenden vollen Stunden).

## [1.1.0] – 2026-11-22

Frei einstellbare Farben mit hellem Profil und Favicons im Link-Menü. Das Farbsystem (**N8 Theme**) ist jetzt in allen
N8-Oberflächen gleich: diesem Dashboard, der [FRN-Weboberfläche](https://github.com/dhartmann1979/adn-frn-server) und dem
[N8-Hotspot](https://github.com/dhartmann1979/N8-HotSpot).

### 🎨 Aussehen (Adminbereich → Appearance)
- **Neues Farbprofil ☀️ Daylight** – hell, für Tageslicht. Alle Bereiche (Live, Statistik, OpenBridge, Talkgroups, Dialoge,
  Adminbereich) haben dafür eine eigene helle Darstellung (`dashboard/static/n8-light.css`).
- **Jede Farbe frei wählbar:** zusätzlich zu Akzent und Hintergrund jetzt *Accent 2* (Verlauf von Knöpfen und Titel),
  die drei Leuchtfarben des Hintergrunds, *Text* und *Card tint*. „from profile“ zeigt, welche Farbe noch aus dem Profil kommt; ↺ setzt sie zurück.
- **Neuer Regler Roundness** (Ecken von Karten und Knöpfen, 0–28 px).
- **Lesbarkeitsprüfung:** warnt, wenn Text auf dem gewählten Hintergrund schlecht lesbar ist (Kontrast unter 4,5 : 1).
- Farbcodes werden auf dem Server geprüft (`dashboard/n8theme.py`); ungültige Werte werden abgelehnt statt gespeichert.
- Neue Einstellungen: `theme.accent2`, `theme.glow1`–`glow3`, `theme.text`, `theme.panel`, `theme.radius`. Bestehende Einstellungen bleiben gültig.

### 🔗 Link-Menü mit Site-Icons
- Web-Links zeigen das **Favicon der verlinkten Seite** statt des Emojis. Das Dashboard holt es beim Start und nach dem Speichern
  der Links, prüft es und speichert es in SQLite (`link_icons`, 30 Tage; fehlende werden stündlich neu versucht).
  Besucher laden das Bild vom Dashboard (`/api/link-icon/…`), nicht von der fremden Seite.
- Neuer Schalter **site icon** je Link (Vorgabe an); für `mailto:`, `/pfad` und `#tab` ausgegraut.
- Sicherheit: nur öffentliche Adressen (Schutz gegen SSRF, auch über Weiterleitungen), max. 256 KB, nur echte Bilder,
  SVG ohne Skripte und externe Verweise, Auslieferung mit strenger Content-Security-Policy.

### Technik
- Neue Dateien: `dashboard/static/n8-theme.js`, `n8-theme.css`, `n8-light.css`, `dashboard/n8theme.py`, `dashboard/siteicons.py`.
- Neue Tests: `tests/test_n8theme.py`, `tests/test_siteicons.py` (Netz gemockt); insgesamt 282.

## [1.0.0] – 2026-11-21

Erste Version mit Versionsnummer. Sie fasst alles zusammen, was diese Ausgabe gegenüber dem
originalen HBlink4 (Stand 4.8.0) mitbringt. Ab hier steht jede Änderung mit eigener Nummer in dieser Datei.

### Neu im Dashboard
- **Live-Ansicht für Desktop und Smartphone:** feste Kopfzeile mit Menü (Live, Statistics, OpenBridge,
  Talkgroups, Configuration). Am Handy ist die Kopfzeile nur noch eine Zeile plus Tabs; Live und DMR sind dort zwei Statuspunkte.
- **Statistik-Kacheln:** Verbindungen, laufende Gespräche, Gespräche und Sprechzeit heute, OpenBridge,
  FreeRadioID. Am Handy als 3×2-Raster statt sechs Zeilen.
- **Last Heard neu gebaut:** kompakte Zeilen statt breiter Tabelle. Am Desktop eine Zeile pro Eintrag,
  Talkgroup-Name direkt hinter der Nummer; am Handy zwei kurze Zeilen, nichts wird abgeschnitten.
  Es stehen die ersten 8 (Handy: 5) Einträge da, **Show all** zeigt alle. Wer gerade spricht, steht immer oben.
- **Stationen als Raster:** Repeater, Hotspots, Apps, Netzwerke, Outbound-Links und OpenBridge stehen als schmale Karten
  nebeneinander; am breiten Bildschirm stehen die Bereiche zusätzlich in zwei Spalten. Die Zeitschlitze sind kurze Zeilen
  (frei: eine Zeile; aktiv: RX/TX, Sprecher, Talkgroup mit Name, Dauer).
- **Stationsinfo zu jeder DMR-ID:** Klick auf einen Sprecher (Last Heard, Zeitschlitze, OpenBridge) öffnet die Daten von
  FreeRadioID.net (Name, Ort, Registrierung, Profil) und das letzte Gespräch auf diesem Server.
- **Gerätearten:** eigene Bereiche für 📶 Repeater, 📱 Hotspots, 📲 Apps (Z3DMR, DroidStar, BlueDV, …) und 🔗 Netzwerke.
  Unbekannte Geräte heißen „❔ Unknown device type“ und zeigen den gemeldeten Paketnamen.
- **OpenBridge-Tab** mit allen Details eines Trunks: Zustand, Gegenstelle, Talkgroups, Verkehr heute bis 30 Tage, Gespräche.
- **Talkgroup-Verzeichnis und Talkgroup-Statistik**, **Configuration-Tab** mit der Anleitung zum Verbinden.
- **Fußzeile** mit © Die N8schicht, Version, Commit und Datum.
- **Datenschutz:** IP-Adressen und Ports von Stationen, Links und Trunks sieht nur, wer als Admin eingeloggt ist –
  der Server entfernt sie vorher.

### Neu im Adminbereich
- **Aufgeräumt in Tabs:** Users, Settings, Appearance, Links, OpenBridge, Import, Sessions, Log, System; gleich große Fenster,
  am Handy als Auswahlliste.
- **Alle Einstellungen in der Datenbank** statt in Dateien, mit Schaltern, Auswahllisten und Hilfetexten; Erweitertes unter *Advanced*.
- **Appearance:** Farbprofile, eigene Farben, Logo und Favicon hochladen (mit Vorschau), Titel und Untertitel, Logo-Größe.
- **Link-Menü** mit eigenen Links, als Menü oder Leiste.
- **OpenBridge-Editor:** Trunks Schritt für Schritt einrichten, Protokoll Classic oder FreeDMR v5, Talkgroups aus der Liste wählen
  oder ohne Filter; fertiger, editierbarer Abschnitt für die `hblink.cfg` der Gegenstelle; Abschnitte aus einer `hblink.cfg`
  lassen sich einfügen.
- **Import:** Konfigurationen von HBlink3, FreeDMR, ADN und HBlink4 übernehmen (Server, Talkgroups, Trunks, Links, Blacklist).
- **Geräteerkennung:** Listen, an denen Repeater, Hotspots, Apps und Netzwerke erkannt werden, im Adminbereich änderbar.
- **Updates:** Der Server fragt jede Stunde bei GitHub nach einer neuen Version. Admins sehen einen Hinweis mit der neuen
  Versionsnummer und diesen Änderungstexten. **⬇️ Update now** installiert mit Passwort-Bestätigung und startet neu –
  nur vorwärts, nur die angezeigte Version, nur vom eingerichteten Git-Remote. Neustart ohne sudo über systemd oder mit
  einer genau begrenzten sudo-Regel.

### Neu im DMR-Server
- **FreeRadioID.net Secure Login:** Stationen melden sich mit ihrem persönlichen FreeRadioID-Passwort an und bekommen volle
  Rechte; das Standard-Passwort ist der Gastzugang mit eigenen Gast-Talkgroups.
- **OpenBridge zu FreeDMR:** FreeDMR-Protokoll v5 (DMRE, Keep-alives, Versionsabgleich, Source Quench, wechselnde Adressen).
- **OpenBridge parallel:** Jede Talkgroup läuft über alle Trunks, deren Filter sie durchlässt, gleichzeitig; beliebig viele
  Trunks ohne Filter. Gespräche können zwischen Trunks weitergereicht werden (schaltbar), mit Echo-Schutz und Hop-Zähler gegen Schleifen.
- **Apps hören OpenBridge:** Gespräche aus OpenBridge gehen mit 55 Bytes an die Stationen; Apps wie Z3DMR haben die
  kürzeren Pakete vorher verworfen.
- **Geräteart „App“** für Handy- und PC-Apps ohne Funkgerät.

### Dokumentation
- Deutsche Anleitungen mit Screenshots: Installation, Dashboard, Adminbereich, Konfiguration, Betrieb, FreeRadioID,
  Schritt-für-Schritt-Anleitungen, Versionen.
- `scripts/e2e_openbridge.py` prüft OpenBridge von Ende zu Ende auf dem eigenen Rechner.
