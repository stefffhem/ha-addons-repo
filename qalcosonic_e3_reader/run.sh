#!/usr/bin/with-contenv bashio
# ==============================================================================
# Startet den Qalcosonic E3 MQTT-Reader
# ==============================================================================

export SERIAL_PORT
export POLL_INTERVAL
export MQTT_HOST
export MQTT_PORT
export MQTT_USER
export MQTT_PASSWORD
export MQTT_TOPIC_PREFIX
export DEVICE_NAME
export LOG_LEVEL

SERIAL_PORT=$(bashio::config 'serial_port')
POLL_INTERVAL=$(bashio::config 'poll_interval')
MQTT_HOST=$(bashio::config 'mqtt_host')
MQTT_PORT=$(bashio::config 'mqtt_port')
MQTT_USER=$(bashio::config 'mqtt_user')
MQTT_PASSWORD=$(bashio::config 'mqtt_password')
MQTT_TOPIC_PREFIX=$(bashio::config 'mqtt_topic_prefix')
DEVICE_NAME=$(bashio::config 'device_name')
LOG_LEVEL=$(bashio::config 'log_level')

bashio::log.info "Serieller Port: ${SERIAL_PORT}"
bashio::log.info "MQTT-Broker:    ${MQTT_HOST}:${MQTT_PORT}"
bashio::log.info "Abfrageintervall: ${POLL_INTERVAL}s"
bashio::log.info "Starte Qalcosonic E3 Reader ..."

exec python3 /qalcosonic_reader.py
