# Änderungen

Alle Versionen von **HBlink4-FreeRadioID** (DMR Die N8schicht), die neueste oben.
Die Nummern folgen [Semantic Versioning](https://semver.org/lang/de/): `MAJOR.MINOR.PATCH`.
Wie Versionen entstehen, steht in [docs/de/versionen.md](docs/de/versionen.md).

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
