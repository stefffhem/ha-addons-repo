#!/usr/bin/env python3
"""
Qalcosonic E3 -> MQTT Reader für Home Assistant

Liest einen Qalcosonic E3 Wärme-/Kältezähler über einen optischen IR-Lesekopf
per M-Bus-Protokoll (EN 13757-2/-3) aus und veröffentlicht die gefundenen
Messwerte per MQTT in Home Assistant (inkl. MQTT Discovery).

Viele optische Wärmezähler-Schnittstellen (u.a. bei Geräten auf Basis des
Landis+Gyr T230/T330-Moduls, zu denen auch etliche Axioma/Qalcosonic-Modelle
gehören) sprechen über den IR-Kopf kein IEC-62056-21-Klartextprotokoll,
sondern binäres M-Bus. Vor der eigentlichen Anfrage muss die optische
Schnittstelle zusätzlich mit einer Folge von Nullbytes "geweckt" werden.

Ablauf:
  1. Serielle Verbindung bei 2400 Baud, 8 Datenbits, gerade Parität, 1 Stopbit
     öffnen (Standard-Bitrate für M-Bus).
  2. Eine konfigurierbare Anzahl Nullbytes senden, um die optische
     Schnittstelle des Zählers aufzuwecken.
  3. Eine M-Bus REQ_UD2-Kurzanfrage (Kurzrahmen) an die Zähleradresse senden.
  4. Die Antwort mit der Bibliothek "pyMeterBus" (Modul `meterbus`) einlesen
     und dekodieren.
  5. Jeden gefundenen Datensatz generisch als eigenen Sensor veröffentlichen;
     die Einheit wird anhand des erkannten Werttyps automatisch geraten und
     bestimmt die passende Home-Assistant-Geräteklasse.
"""

import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass

import serial

try:
    import meterbus
except ImportError:  # pragma: no cover
    print("pyMeterBus (Modul 'meterbus') ist nicht installiert", file=sys.stderr)
    raise

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

MBUS_ADDRESS = int(os.environ.get("MBUS_ADDRESS", "254"))
MBUS_WAKEUP_ZEROS = int(os.environ.get("MBUS_WAKEUP_ZEROS", "300"))
MBUS_BAUDRATE = int(os.environ.get("MBUS_BAUDRATE", "2400"))

DEVICE_SLUG = re.sub(r"[^a-z0-9_]+", "_", DEVICE_NAME.lower()).strip("_") or "qalcosonic_e3"

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)-8s %(message)s",
)
log = logging.getLogger("qalcosonic")


# ---------------------------------------------------------------------------
# M-Bus Auslesung über den IR-Lesekopf
# ---------------------------------------------------------------------------

class MeterReadError(Exception):
    pass


def _open_serial(port: str, baudrate: int, timeout: float, bytesize=serial.EIGHTBITS) -> serial.Serial:
    """Öffnet die serielle Verbindung mit exklusivem Zugriff (verhindert
    Konflikte mit anderen Prozessen, die denselben Port anfassen) und setzt
    die DTR-Leitung, da manche IR-Leseköpfe ihre Sendeleistung darüber
    beziehen."""

    ser = serial.Serial(
        port=port,
        baudrate=baudrate,
        bytesize=bytesize,
        parity=serial.PARITY_EVEN,
        stopbits=serial.STOPBITS_ONE,
        timeout=timeout,
        exclusive=True,
    )
    try:
        ser.dtr = True
        ser.rts = False
    except Exception as exc:  # nicht jeder Adapter unterstützt das
        log.debug("DTR/RTS konnten nicht gesetzt werden: %s", exc)
    return ser


def _build_req_ud2_frame(address: int) -> bytes:
    """Baut einen M-Bus-Kurzrahmen für eine REQ_UD2-Datenanfrage (Control=0x7B
    mit gesetztem FCB, wie in mehreren feldbewährten Implementierungen für
    optische Wärmezähler-Schnittstellen verwendet)."""

    control = 0x7B
    checksum = (control + address) % 256
    return bytes([0x10, control, address, checksum, 0x16])


def read_meter(port: str, timeout: float = 8.0, retries: int = 2) -> dict:
    """Führt eine vollständige M-Bus-Auslesung durch und gibt ein
    Dict {code: (value, unit)} mit allen gefundenen Messwerten zurück."""

    last_error = None
    for attempt in range(1, retries + 2):
        try:
            return _read_meter_once(port, timeout)
        except (serial.SerialException, MeterReadError) as exc:
            last_error = exc
            log.warning("Ausleseversuch %d fehlgeschlagen: %s", attempt, exc)
            time.sleep(1.0)
    raise last_error


def _read_meter_once(port: str, timeout: float) -> dict:
    ser = _open_serial(port, MBUS_BAUDRATE, timeout)

    try:
        # Kurze Settle-Zeit nach dem Öffnen des Ports (manche USB-Seriell-Chips
        # verlieren sonst die ersten gesendeten Bytes).
        time.sleep(0.3)
        ser.reset_input_buffer()
        ser.reset_output_buffer()

        log.debug(
            "Sende %d Wakeup-Nullbytes bei %d Baud, um die optische "
            "Schnittstelle zu wecken",
            MBUS_WAKEUP_ZEROS,
            MBUS_BAUDRATE,
        )
        ser.write(b"\x00" * MBUS_WAKEUP_ZEROS)
        ser.flush()
        time.sleep(0.5)

        frame = _build_req_ud2_frame(MBUS_ADDRESS)
        log.debug("Sende REQ_UD2-Anfrage: %s", frame.hex())
        ser.reset_input_buffer()
        ser.write(frame)
        ser.flush()

        # Viele einfache IR-Leseköpfe spiegeln die eigene Sendung hardwareseitig
        # auf die Empfangsleitung zurück (TX->RX-Kopplung zur Baudraten-
        # erkennung). Die echte Zähler-Antwort folgt dann als zusätzlicher,
        # separater Frame direkt danach. Deshalb wird nach einem erkannten
        # Echo einfach weitergelesen, statt sofort aufzugeben.
        raw_bytes = None
        for read_attempt in range(1, 4):
            try:
                candidate = meterbus.recv_frame(ser)
            except serial.SerialException as exc:
                raise serial.SerialException(f"Fehler beim Lesen der Antwort: {exc}") from exc
            except Exception as exc:
                if read_attempt == 1:
                    raise MeterReadError(f"Keine gültige M-Bus-Antwort erhalten: {exc}") from exc
                break

            if not candidate:
                break

            candidate_bytes = candidate if isinstance(candidate, (bytes, bytearray)) else bytes(candidate)
            log.info(
                "Empfangener Frame (Versuch %d, %d Bytes): %s",
                read_attempt, len(candidate_bytes), candidate_bytes.hex(),
            )

            if candidate_bytes == frame:
                log.debug("Frame %d ist ein Echo der eigenen Anfrage, lese weiter", read_attempt)
                continue

            raw_bytes = candidate_bytes
            break

        if not raw_bytes:
            raise MeterReadError(
                "Zähler hat auf die M-Bus-Anfrage nicht mit echten Daten geantwortet "
                "(nur Echo der eigenen Anfrage oder keine Antwort). Ist der Lesekopf "
                "richtig auf dem optischen Sensor des Zählers platziert? Manche Zähler "
                "müssen zusätzlich per Tastendruck aktiviert werden, oder brauchen mehr "
                "Wakeup-Nullbytes (Option mbus_wakeup_zeros erhöhen)."
            )

        try:
            telegram = meterbus.load(raw_bytes)
        except Exception as exc:
            raise MeterReadError(
                f"Antwort konnte nicht als M-Bus-Telegramm dekodiert werden: {exc}"
            ) from exc

        try:
            body_json = telegram.body.to_JSON()
        except Exception as exc:
            raise MeterReadError(
                f"Telegramm enthielt keine auswertbaren Nutzdaten (Typ: {type(telegram).__name__}): {exc}"
            ) from exc

        log.info("Rohes M-Bus-Telegramm (JSON): %s", body_json)

        try:
            parsed = json.loads(body_json)
        except (TypeError, ValueError) as exc:
            raise MeterReadError(f"Antwort war kein gültiges JSON: {exc}") from exc

        return _extract_values(parsed)

    finally:
        ser.close()


def _extract_values(parsed: dict) -> dict:
    """Wandelt die von pyMeterBus gelieferte Datensatzliste generisch in
    {code: (value, unit)} um."""

    results = {}
    records = parsed.get("records", []) if isinstance(parsed, dict) else []

    for idx, record in enumerate(records):
        if not isinstance(record, dict):
            continue
        rtype = str(record.get("type", f"record_{idx}"))
        value = record.get("value")
        if value is None:
            continue

        base_code = re.sub(r"[^a-z0-9_]+", "_", rtype.lower()).strip("_") or f"record_{idx}"
        code = base_code
        suffix = 2
        while code in results:
            code = f"{base_code}_{suffix}"
            suffix += 1

        results[code] = (value, _guess_unit(rtype))

    log.info("Zähler ausgelesen: %d Datenpunkte gefunden", len(results))
    return results


# Grobe Zuordnung von Schlüsselwörtern im pyMeterBus-Werttyp (z.B.
# "VIFUnit.ENERGY_WH") zu einer für Home Assistant sinnvollen Einheit.
# Kann anhand der echten Roh-JSON-Ausgabe (siehe Log) bei Bedarf präzisiert
# werden.
_TYPE_TO_UNIT = [
    ("ENERGY_WH", "Wh"),
    ("ENERGY_KWH", "kWh"),
    ("ENERGY_MWH", "MWh"),
    ("ENERGY_MJ", "MJ"),
    ("ENERGY_GJ", "GJ"),
    ("ENERGY_J", "J"),
    ("ENERGY", "kWh"),
    ("VOLUME_FLOW", "m³/h"),
    ("VOLUME", "m³"),
    ("FLOW_TEMPERATURE", "°C"),
    ("RETURN_TEMPERATURE", "°C"),
    ("TEMPERATURE_DIFFERENCE", "K"),
    ("TEMPERATURE", "°C"),
    ("POWER_KW", "kW"),
    ("POWER_W", "W"),
    ("POWER", "W"),
    ("ACTUALITY_DURATION", "h"),
    ("OPERATING_TIME", "h"),
    ("ON_TIME", "h"),
    ("DURATION", "h"),
    ("PRESSURE", "bar"),
    ("BATTERY", "d"),
]


def _guess_unit(rtype: str) -> str:
    upper = rtype.upper()
    for keyword, unit in _TYPE_TO_UNIT:
        if keyword in upper:
            return unit
    return ""


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
    "j": UnitInfo("energy", "total_increasing", "mdi:lightning-bolt"),
    "mj": UnitInfo("energy", "total_increasing", "mdi:lightning-bolt"),
    "gj": UnitInfo("energy", "total_increasing", "mdi:fire"),
    "m3": UnitInfo("water", "total_increasing", "mdi:water"),
    "m³": UnitInfo("water", "total_increasing", "mdi:water"),
    "l": UnitInfo("water", "total_increasing", "mdi:water"),
    "m3ph": UnitInfo(None, "measurement", "mdi:water-pump"),
    "m3/h": UnitInfo(None, "measurement", "mdi:water-pump"),
    "m³/h": UnitInfo(None, "measurement", "mdi:water-pump"),
    "c": UnitInfo("temperature", "measurement", "mdi:thermometer"),
    "°c": UnitInfo("temperature", "measurement", "mdi:thermometer"),
    "k": UnitInfo("temperature", "measurement", "mdi:thermometer"),
    "kw": UnitInfo("power", "measurement", "mdi:flash"),
    "w": UnitInfo("power", "measurement", "mdi:flash"),
    "h": UnitInfo("duration", "total_increasing", "mdi:clock-outline"),
    "d": UnitInfo(None, "measurement", "mdi:battery-clock"),
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
    log.info(
        "Qalcosonic E3 Reader gestartet (Port=%s, Intervall=%ss, M-Bus-Adresse=%s, "
        "Baudrate=%s, Wakeup-Nullbytes=%s)",
        SERIAL_PORT, POLL_INTERVAL, MBUS_ADDRESS, MBUS_BAUDRATE, MBUS_WAKEUP_ZEROS,
    )

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
