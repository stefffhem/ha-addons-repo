# Qalcosonic E3 Wärmezähler → MQTT (Home Assistant Add-on)

Dieses Add-on liest einen **Qalcosonic E3** Wärme-/Kältezähler über einen
**optischen IR-Lesekopf** aus (Protokoll IEC 62056‑21 / IEC 1107, Modus C –
das Standardprotokoll, das die meisten Wärme- und Energiezähler über die
optische Schnittstelle sprechen) und veröffentlicht die Messwerte per MQTT.
Dank MQTT Discovery erscheinen die Sensoren automatisch in Home Assistant.

## Installation

1. Kopiere den kompletten Ordner `qalcosonic_e3_reader` nach
   `/addons/local/qalcosonic_e3_reader` auf deinem Home-Assistant-System
   (z. B. per Samba-Share, SSH oder dem "Studio Code Server"-Add-on).
2. In Home Assistant: **Einstellungen → Add-ons → Add-on Store → oben rechts
   „⋮" → Repositories neu laden** (oder das Add-on-Store einfach neu öffnen).
   Unter „Lokale Add-ons" sollte jetzt **„Qalcosonic E3 Wärmezähler"**
   auftauchen.
3. Add-on installieren, danach **Konfiguration** öffnen und anpassen (siehe
   unten), dann **Starten**.

## Konfiguration

| Option | Beschreibung | Standard |
|---|---|---|
| `serial_port` | Gerätepfad des IR-Lesekopfs | `/dev/ttyUSB0` |
| `poll_interval` | Abfrageintervall in Sekunden | `300` |
| `mqtt_host` | MQTT-Broker-Host | `core-mosquitto` |
| `mqtt_port` | MQTT-Broker-Port | `1883` |
| `mqtt_user` | MQTT-Benutzername | `Steffen` |
| `mqtt_password` | MQTT-Passwort | *(leer)* |
| `mqtt_topic_prefix` | Präfix für MQTT-Topics | `qalcosonic_e3` |
| `device_name` | Anzeigename in Home Assistant | `Wärmezähler` |
| `log_level` | Log-Ausführlichkeit | `info` |

Deine Angaben sind bereits als Standardwerte in der `config.yaml` hinterlegt
(Broker `core-mosquitto`, Port `1883`, Benutzer `Steffen`) – trage nur noch
dein MQTT-Passwort und ggf. den richtigen `serial_port` ein.

### Den richtigen `serial_port` finden

Unter **Einstellungen → System → Hardware → Alle Hardware anzeigen** siehst
du, unter welchem Pfad der IR-Lesekopf eingehängt ist (meist
`/dev/ttyUSB0`, `/dev/ttyUSB1` oder ein `/dev/serial/by-id/...`-Pfad, der
sich nach einem Neustart nicht ändert – empfehlenswert, falls mehrere
USB-Geräte angeschlossen sind).

## Wie die Auslesung funktioniert

1. Das Add-on sendet über den IR-Kopf eine Weckanfrage (`/?!`) bei 300 Baud.
2. Der Zähler antwortet mit einer Kennung und einem Baudraten-Vorschlag.
3. Das Add-on bestätigt (ACK) und schaltet auf die vom Zähler vorgeschlagene
   Baudrate (üblicherweise 9600 Baud) um.
4. Der Zähler sendet daraufhin einen Datenblock mit Zeilen der Form
   `CODE(WERT*EINHEIT)`, z. B. `6.8(00123.45*MWh)` für die Energie oder
   `6.26(00045.12*m3)` für das Volumen.
5. Jede erkannte Zeile wird automatisch als eigener Sensor in Home Assistant
   angelegt – die Einheit entscheidet automatisch über die passende
   Geräteklasse (Energie, Wasser/Volumen, Temperatur, Leistung, ...).

Da die genauen Codes je nach Firmware-Version des E3 leicht variieren
können, wertet das Add-on **jede** im Telegramm gefundene Zeile aus – du
musst also nicht raten, welche Werte dein Zähler konkret liefert, sondern
siehst nach dem ersten erfolgreichen Auslesevorgang alle verfügbaren
Sensoren in Home Assistant (unter dem Gerät „Wärmezähler").

## MQTT-Topics

- `qalcosonic_e3/status` – `online` / `offline` (Verfügbarkeit)
- `qalcosonic_e3/state` – JSON mit allen aktuell ausgelesenen Werten
- `homeassistant/sensor/<id>/config` – MQTT-Discovery-Konfiguration je Sensor

## Fehlersuche

- **„Keine gültige Antwort vom Zähler erhalten"**: Lesekopf sitzt nicht
  exakt auf dem optischen Fenster des Zählers, oder das Zählerdisplay muss
  vorher per Knopfdruck aktiviert werden (bei manchen E3-Varianten nötig,
  damit die optische Schnittstelle für ein paar Sekunden aktiv ist).
- **Serielle Schnittstelle nicht erreichbar**: `serial_port` falsch, oder
  dem Add-on fehlt der Gerätezugriff – prüfe, ob das USB-Gerät unter
  „Hardware" überhaupt sichtbar ist.
- **Zähler antwortet, aber keine Sensoren erscheinen**: Log-Level auf
  `debug` stellen und im Add-on-Log die empfangenen Rohzeilen prüfen.
- Der Zähler ist batteriebetrieben – ein sehr kurzes Abfrageintervall
  (unter ca. 60–300 s) verkürzt die Batterielebensdauer unnötig.
