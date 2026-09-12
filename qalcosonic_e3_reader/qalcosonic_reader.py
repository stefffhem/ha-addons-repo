#!/usr/bin/env python3
"""
Qalcosonic E3 -> MQTT Reader für Home Assistant

Liest einen Qalcosonic E3 Wärme-/Kältezähler über einen optischen IR-Lesekopf
(serielle Schnittstelle) nach dem Protokoll IEC 62056-21 (früher IEC 61107 /
IEC 1107), Auslesemodus C, aus und veröffentlicht die gefundenen Messwerte
per MQTT in Home Assistant (inkl. MQTT Discovery, d.h. die Sensoren tauchen
automatisch in Home Assistant auf, ohne dass man sie manuell anlegen muss).

Das Protokoll ist herstellerunabhängig: Der Zähler antwortet auf eine
Weckanfrage mit einem Kennungstelegramm und schickt danach einen Datenblock
aus Zeilen der Form:

    CODE(WERT*EINHEIT)

z.B. "6.8(00123.45*MWh)" oder "9.4(032.5*C)". Da der genaue Zeichensatz an
Codes je nach Zählertyp/Firmware leicht variieren kann, wertet dieses Skript
JEDE Zeile generisch aus: Der Code wird zum Sensor-Namen, die Einheit
bestimmt automatisch die passende Home-Assistant-Geräteklasse (Energie,
Volumen, Temperatur, Leistung, Betriebsstunden, ...).
"""

import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field

import serial

try:
    import paho.mqtt.client as mqtt
except ImportError:  # pragma: no cover
    print("paho-mqtt ist nicht installiert", file=sys.stderr)
    raise


# ---------------------------------------------------------------------------
# Konfiguration aus Umgebungsvariablen (von run.sh gesetzt)
# ---------------------------------------------------------------------------

SERIAL_PORT = os.environ.get("SERIAL_PORT", "/dev/ttyUSB0")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "300"))
MQTT_HOST = os.environ.get("MQTT_HOST", "core-mosquitto")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("MQTT_USER", "")
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD", "")
TOPIC_PREFIX = os.environ.get("MQTT_TOPIC_PREFIX", "qalcosonic_e3")
DEVICE_NAME = os.environ.get("DEVICE_NAME", "Wärmezähler")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "info").upper()

DEVICE_SLUG = re.sub(r"[^a-z0-9_]+", "_", DEVICE_NAME.lower()).strip("_") or "qalcosonic_e3"

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)-8s %(message)s",
)
log = logging.getLogger("qalcosonic")


# ---------------------------------------------------------------------------
# IEC 62056-21 Auslesung (Modus C) über den IR-Lesekopf
# ---------------------------------------------------------------------------

# Baudraten-Kennziffer laut Norm -> tatsächliche Baudrate
BAUD_ID_MAP = {
    "0": 300,
    "1": 600,
    "2": 1200,
    "3": 2400,
    "4": 4800,
    "5": 9600,
    "6": 19200,
}

# Erkennt Datenzeilen wie "6.8(00123.45*MWh)" oder "9.21(12345678)"
LINE_RE = re.compile(r"^([0-9A-Za-z.\-:*]+)\(([^)]*)\)")
# Erkennt "WERT*EINHEIT" innerhalb der Klammer, mehrere per '&' getrennt möglich
VALUE_RE = re.compile(r"^([+-]?[0-9]+(?:\.[0-9]+)?)(?:\*(.+))?$")


class MeterReadError(Exception):
    pass


def read_meter(port: str, timeout: float = 8.0) -> dict:
    """Führt eine vollständige IEC 62056-21 Auslesung durch und gibt ein
    Dict {code: (value, unit)} mit allen gefundenen Messwerten zurück."""

    ser = serial.Serial(
        port=port,
        baudrate=300,
        bytesize=serial.SEVENBITS,
        parity=serial.PARITY_EVEN,
        stopbits=serial.STOPBITS_ONE,
        timeout=timeout,
    )

    try:
        ser.reset_input_buffer()
        ser.reset_output_buffer()

        # 1. Weckanfrage (Request Message) senden
        log.debug("Sende Weckanfrage /?!")
        ser.write(b"/?!\r\n")
        ser.flush()

        # 2. Kennungstelegramm empfangen: /XXXZyyyyyyyyyy<CR><LF>
        identification = ser.readline()
        log.debug("Kennung empfangen: %r", identification)
        if not identification.startswith(b"/"):
            raise MeterReadError(
                f"Keine gültige Antwort vom Zähler erhalten (bekommen: {identification!r}). "
                "Ist der Lesekopf richtig auf dem optischen Sensor des Zählers platziert?"
            )

        # Baudraten-Kennziffer ist das 5. Zeichen (Index 4), z.B. "/AXM5xxxxxx"
        baud_id = chr(identification[4]) if len(identification) > 4 else "0"
        new_baud = BAUD_ID_MAP.get(baud_id, 300)
        log.debug("Zähler bietet Baudrate-ID '%s' -> %d Baud an", baud_id, new_baud)

        # 3. ACK senden: <ACK>0<Z>0<CR><LF> -> wählt Modus C (Datenübertragung, Standard)
        ack = bytes([0x06]) + b"0" + baud_id.encode() + b"0\r\n"
        ser.write(ack)
        ser.flush()

        # Kurze Pause laut Norm (max. 1500ms Umschaltzeit), dann Baudrate umstellen
        time.sleep(0.3)
        if new_baud != 300:
            ser.baudrate = new_baud

        # 4. Datenblock lesen, bis Endezeile "!" kommt oder Timeout erreicht ist
        raw_lines = []
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = ser.readline()
            if not line:
                break
            raw_lines.append(line)
            if line.strip() == b"!":
                break

        if not raw_lines:
            raise MeterReadError("Zähler hat keine Daten gesendet (Timeout).")

        return _parse_data_block(raw_lines)

    finally:
        ser.close()


def _parse_data_block(raw_lines) -> dict:
    """Parst die rohen Telegrammzeilen generisch in {code: (value, unit)}."""

    results = {}
    for raw in raw_lines:
        try:
            text = raw.decode("ascii", errors="replace").strip()
        except Exception:
            continue

        if not text or text in ("!", "\x02", "\x03"):
            continue

        match = LINE_RE.match(text)
        if not match:
            continue

        code, content = match.group(1), match.group(2)
        if not content:
            continue

        # Mehrere Werte können innerhalb einer Zeile per '&' getrennt sein,
        # z.B. Vorlauf- und Rücklauftemperatur in derselben Zeile.
        parts = content.split("&")
        for idx, part in enumerate(parts):
            value_match = VALUE_RE.match(part.strip())
            key = code if len(parts) == 1 else f"{code}_{idx + 1}"
            if value_match:
                value_str, unit = value_match.group(1), value_match.group(2)
                try:
                    value = float(value_str)
                    if value.is_integer():
                        value = int(value)
                except ValueError:
                    value = value_str
                results[key] = (value, unit or "")
            else:
                # Nicht-numerischer Inhalt (z.B. Datum, Seriennummer, Text)
                results[key] = (part.strip(), "")

    log.info("Zähler ausgelesen: %d Datenpunkte gefunden", len(results))
    return results


# ---------------------------------------------------------------------------
# Zuordnung Einheit -> Home Assistant Geräteklasse / Icon
# ---------------------------------------------------------------------------

@dataclass
class UnitInfo:
    device_class: str = None
    state_class: str = "measurement"
    icon: str = "mdi:gauge"


UNIT_MAP = {
    "wh": UnitInfo("energy", "total_increasing", "mdi:lightning-bolt"),
    "kwh": UnitInfo("energy", "total_increasing", "mdi:lightning-bolt"),
    "mwh": UnitInfo("energy", "total_increasing", "mdi:lightning-bolt"),
    "gj": UnitInfo("energy", "total_increasing", "mdi:fire"),
    "m3": UnitInfo("water", "total_increasing", "mdi:water"),
    "l": UnitInfo("water", "total_increasing", "mdi:water"),
    "m3ph": UnitInfo(None, "measurement", "mdi:water-pump"),
    "m3/h": UnitInfo(None, "measurement", "mdi:water-pump"),
    "c": UnitInfo("temperature", "measurement", "mdi:thermometer"),
    "°c": UnitInfo("temperature", "measurement", "mdi:thermometer"),
    "kw": UnitInfo("power", "measurement", "mdi:flash"),
    "w": UnitInfo("power", "measurement", "mdi:flash"),
    "h": UnitInfo("duration", "total_increasing", "mdi:clock-outline"),
    "bar": UnitInfo("pressure", "measurement", "mdi:gauge"),
}


def unit_info(unit: str) -> UnitInfo:
    return UNIT_MAP.get((unit or "").lower(), UnitInfo(None, "measurement", "mdi:counter"))


# ---------------------------------------------------------------------------
# MQTT: Verbindung, Discovery, Publish
# ---------------------------------------------------------------------------

class MqttPublisher:
    def __init__(self):
        self.client = mqtt.Client(client_id=f"ha-{DEVICE_SLUG}-addon")
        if MQTT_USER:
            self.client.username_pw_set(MQTT_USER, MQTT_PASSWORD)

        self.availability_topic = f"{TOPIC_PREFIX}/status"
        self.client.will_set(self.availability_topic, "offline", retain=True)

        self._discovered = set()
        self._connected = False

        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self._connected = True
            log.info("Mit MQTT-Broker %s:%s verbunden", MQTT_HOST, MQTT_PORT)
            client.publish(self.availability_topic, "online", retain=True)
        else:
            log.error("MQTT-Verbindung fehlgeschlagen, rc=%s", rc)

    def _on_disconnect(self, client, userdata, rc):
        self._connected = False
        log.warning("MQTT-Verbindung getrennt (rc=%s)", rc)

    def connect(self):
        log.info("Verbinde mit MQTT-Broker %s:%s ...", MQTT_HOST, MQTT_PORT)
        self.client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
        self.client.loop_start()

        deadline = time.time() + 15
        while not self._connected and time.time() < deadline:
            time.sleep(0.2)
        if not self._connected:
            log.warning("Noch keine MQTT-Bestätigung erhalten, fahre trotzdem fort")

    def _device_block(self):
        return {
            "identifiers": [DEVICE_SLUG],
            "name": DEVICE_NAME,
            "manufacturer": "Axioma Metering",
            "model": "Qalcosonic E3",
        }

    def ensure_discovery(self, code: str, unit: str):
        if code in self._discovered:
            return
        info = unit_info(unit)
        object_id = f"{DEVICE_SLUG}_{re.sub(r'[^a-z0-9_]+', '_', code.lower())}"
        config_topic = f"homeassistant/sensor/{object_id}/config"

        payload = {
            "name": f"{DEVICE_NAME} {code}",
            "unique_id": object_id,
            "state_topic": f"{TOPIC_PREFIX}/state",
            "availability_topic": self.availability_topic,
            "value_template": f"{{{{ value_json['{code}'] }}}}",
            "unit_of_measurement": unit or None,
            "icon": info.icon,
            "device": self._device_block(),
        }
        if info.device_class:
            payload["device_class"] = info.device_class
        if info.state_class:
            payload["state_class"] = info.state_class

        # None-Werte entfernen (MQTT Discovery mag keine null-Felder für Strings)
        payload = {k: v for k, v in payload.items() if v is not None}

        self.client.publish(config_topic, json.dumps(payload), retain=True)
        self._discovered.add(code)
        log.debug("Discovery veröffentlicht für %s", code)

    def publish_state(self, values: dict):
        state = {code: value for code, (value, _unit) in values.items()}
        self.client.publish(f"{TOPIC_PREFIX}/state", json.dumps(state), retain=True)

        for code, (_value, unit) in values.items():
            self.ensure_discovery(code, unit)

    def close(self):
        try:
            self.client.publish(self.availability_topic, "offline", retain=True)
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Hauptschleife
# ---------------------------------------------------------------------------

def main():
    log.info("Qalcosonic E3 Reader gestartet (Port=%s, Intervall=%ss)", SERIAL_PORT, POLL_INTERVAL)

    publisher = MqttPublisher()
    publisher.connect()

    try:
        while True:
            start = time.time()
            try:
                values = read_meter(SERIAL_PORT)
                if values:
                    publisher.publish_state(values)
                    log.info("Werte veröffentlicht: %s", {k: v[0] for k, v in values.items()})
                else:
                    log.warning("Keine auswertbaren Daten im Telegramm gefunden")
            except MeterReadError as exc:
                log.error("Auslesefehler: %s", exc)
            except serial.SerialException as exc:
                log.error("Serielle Schnittstelle nicht erreichbar (%s): %s", SERIAL_PORT, exc)
            except Exception:
                log.exception("Unerwarteter Fehler beim Auslesen")

            elapsed = time.time() - start
            sleep_time = max(5, POLL_INTERVAL - elapsed)
            time.sleep(sleep_time)
    except KeyboardInterrupt:
        pass
    finally:
        publisher.close()


if __name__ == "__main__":
    main()
