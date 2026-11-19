# 🧾 Konfiguration

[← Übersicht](../../readme.md) · [Installation](installation.md) · [Dashboard](dashboard.md) · [Adminbereich](adminbereich.md) · **Konfiguration** · [FreeRadioID](freeradioid.md) · [Betrieb & Fehlersuche](betrieb.md)

---

## Grundprinzip: Dateien für den Start, SQLite für alles andere

```
 config/config.json  ─┐   1. Start: Werte übernehmen       ┌─▶ HBlink4 (DMR-Server)
                      ├──────────────────▶ dashboard.db ────┤
 dashboard/config.json┘   danach: nur noch Adminbereich    └─▶ Dashboard
```

1. **Beim ersten Start** übernehmen HBlink4 und das Dashboard die Werte aus den Config-Dateien in die SQLite-Datenbank `dashboard/data/dashboard.db`.
2. **Ab dann gilt die Datenbank.** Spätere Änderungen in den Dateien haben bei diesen Einstellungen keine Wirkung mehr. Du änderst sie im [Adminbereich](adminbereich.md).
3. **Neue Einstellungen** aus einem Update kommen beim nächsten Start mit ihrer Vorgabe dazu. Vorhandene Werte werden nie überschrieben.
4. **Zurück zur Datei:** Mit **↺ config.json** im Adminbereich holst du einen Abschnitt wieder aus der Datei.

Fällt die Datenbank aus (fehlende Rechte, kaputte Datei), läuft HBlink4 mit den Werten aus der Datei weiter und meldet das im Log.

## Was in den Dateien bleibt

Das erstellt `scripts/setup.sh`; die Vorlagen sind `config/config_sample.json` und `dashboard/config_sample.json`.

### `config/config.json` (HBlink4)

```json
{
  "global": {
    "bind_ipv4": "0.0.0.0",  "port_ipv4": 62031,
    "bind_ipv6": "::",       "port_ipv6": 62032,  "disable_ipv6": true,
    "settings_db": "dashboard/data/dashboard.db",
    "logging": { "file": "logs/hblink.log", "console_level": "INFO",
                 "file_level": "INFO", "retention_days": 14 }
  },
  "dashboard": { "enabled": true, "transport": "unix", "unix_socket": "/tmp/hblink4.sock" },
  "repeater_configurations": {
    "default": { "passphrase": "passw0rd",
                 "slot1_talkgroups": [262, 2620, 26500],
                 "slot2_talkgroups": [262, 2620, 26500] }
  },
  "freeradioid": { "enabled": true, "guest_talkgroups": [26501, 26502, 26503] }
}
```

| Schlüssel | Bleibt in der Datei? | Bedeutung |
|---|---|---|
| `global.bind_*`, `port_ipv6`, `disable_ipv6` | ✅ immer | Adresse, auf der HBlink4 lauscht |
| `global.settings_db` | ✅ immer | Pfad zur gemeinsamen Datenbank, relativ zum Repo |
| `global.logging` | ✅ immer | Logdatei und Loglevel |
| `dashboard` | ✅ immer | Verbindung zum Dashboard; muss zu `event_receiver` passen |
| `global.port_ipv4` | ⏱ erster Start | DMR-Port, danach Adminbereich |
| `repeater_configurations.default` | ⏱ erster Start | Standard-Passwort und Talkgroups |
| `freeradioid` | ⏱ erster Start | Secure Login und Gast-Talkgroups |

### `dashboard/config.json` (Dashboard)

| Schlüssel | Bleibt in der Datei? | Bedeutung |
|---|---|---|
| `auth.superadmin` | ✅ immer | Benutzername und `password_hash` des Superadmins (Hash erzeugen: `./venv/bin/python -m dashboard.auth hash-password`) |
| `web.bind` | ✅ immer | Adresse des Webservers |
| `event_receiver` | ✅ immer | Verbindung von HBlink4; muss zu `dashboard` passen |
| `user_database.user_agent` | ✅ immer | Kontaktadresse für die Abfragen bei FreeRadioID/radioid.net |
| `web.port`, `https`, `dashboard_title`, `server_name`, `server_description` | ⏱ erster Start | danach Adminbereich |

## Alle Einstellungen in SQLite

### Dashboard

| Abschnitt | Schlüssel | Vorgabe |
|---|---|---|
| 🏷️ General | `dashboard_title`, `server_name`, `server_description`, `logo`, `favicon` | DMR Die N8schicht, dmr.die-n8schicht.de, N8schicht DMR Network, N8schicht-Logo |
| 📢 Announcement | `announcement.enabled` / `.text`, `network_info.enabled` / `.button_text` / `.url` | aus |
| 🔐 Access | `auth.public_dashboard`, `auth.session_hours`, `auth.remember_days` | öffentlich, 12 h, 30 Tage |
| 🛡️ FreeRadioID data | `freeradioid.avatars`, `freeradioid.profile_lookup`, `user_database.freeradioid.enabled` | alles an |
| 📘 Callsign database | `user_database.source_url`, `user_database.filter.countries`, `user_database.refresh.time_of_day` | **FreeRadioID.net**, alle Länder, 03:17 |
| 📈 Talkgroup statistics | `tg_stats.enabled`, `tg_stats.public`, `tg_stats.retention_days`, `tg_stats.names` | an, öffentlich, 30 Tage, leer |
| 🛠️ Configuration tab | `connect.enabled`, `connect.host`, `connect.show_password`, `connect.note` | an, Servername, an, leer |
| 🔒 HTTPS & web 🔄 | `https.enabled` / `.domain` / `.email` / `.staging` / `.port` / `.http_port`, `web.port` | aus, 443, 80, 8080 |

### DMR-Server

| Abschnitt | Schlüssel (`dmr.` + Pfad in `config/config.json`) | Vorgabe |
|---|---|---|
| 🛰️ DMR server | `global.port_ipv4` 🔄, `global.max_missed`, `global.timeout_duration`, `global.stream_timeout`, `global.stream_hang_time`, `global.reconnect_report_threshold` | 62031, 3, 30 s, 2,0 s, 10,0 s, 3600 s |
| 🔑 Access | `repeater_configurations.default.passphrase`, `.slot1_talkgroups`, `.slot2_talkgroups`, `.trust`, `.default_unit_calls` | passw0rd, aus der Datei, aus, aus |
| 🛡️ FreeRadioID login | `freeradioid.enabled`, `freeradioid.guest_talkgroups`, `freeradioid.timeout` | **an**, 26501–26503, 3,0 s |
| 🧩 Advanced | `repeater_configurations.patterns`, `blacklist.patterns`, `outbound_connections` 🔄, `openbridge_connections` 🔄 | leer |

🔄 = wirkt erst nach einem Neustart (Knopf im Adminbereich).

**Talkgroup-Listen:** `262, 2620` = nur diese Talkgroups · `all` = alle (`null` in JSON) · `none` = keine (`[]`).

**trust:** Ist `trust` an, gilt die Talkgroup-Liste, die das Gerät selbst sendet (RPTO, z. B. `TS1=1,2;TS2=262`). Ist es aus, gelten nur die Talkgroups von oben.

## Patterns: eigene Regeln pro Repeater

Patterns geben bestimmten IDs oder Rufzeichen ein eigenes Passwort und eigene Talkgroups. Das erste passende Pattern gilt; passt keins, gilt `default`. Eintragen im Adminbereich unter *Patterns, links & trunks*:

```json
[
  {
    "name": "Eigene Repeater",
    "description": "Repeater der N8schicht, privates Passwort, ohne FreeRadioID",
    "match": { "ids": [262123, 262124] },
    "config": {
      "passphrase": "geheim-und-lang",
      "freeradioid": false,
      "trust": true,
      "slot1_talkgroups": null,
      "slot2_talkgroups": [262, 2620, 26500]
    }
  },
  {
    "name": "Hotspots DL",
    "match": { "id_ranges": [[2620000, 2629999], [262000000, 262999999]] },
    "config": { "passphrase": "passw0rd", "slot1_talkgroups": [], "slot2_talkgroups": [262, 26500] }
  }
]
```

`match` kennt `ids`, `id_ranges` und `callsigns` (mit `*`, z. B. `"DB0*"`).

**Blacklist**: gleiche Form, statt `config` steht ein `reason`:

```json
[ { "name": "Gesperrt", "match": { "ids": [1234567] }, "reason": "Missbrauch" } ]
```

Mehr Beispiele, auch für `outbound_connections` und `openbridge_connections`, stehen in [`config/config_advanced_sample.json`](../../config/config_advanced_sample.json) und in der [Original-Doku](../configuration.md).

## Sichern und zurücksetzen

- **Sichern:** `dashboard/data/dashboard.db` enthält alle Einstellungen, Benutzer, das Protokoll und die Talkgroup-Statistik (Tabelle `tg_calls`). Diese Datei und die beiden `config.json` reichen als Backup, siehe [Betrieb](betrieb.md#sicherung).
- **Alles zurück auf die Dateien:** Dienste stoppen, `dashboard.db` umbenennen und neu starten. Achtung: Dann sind auch alle Benutzer weg, nur der Superadmin bleibt.

---

Weiter: [🛡️ FreeRadioID](freeradioid.md)
