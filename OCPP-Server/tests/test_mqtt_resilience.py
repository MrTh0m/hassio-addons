"""
Vérifie qu'une panne MQTT ne peut jamais remonter jusqu'à l'appelant.

Contexte (incident réel, voir CHANGELOG) : un _client.publish() non protégé,
appelé depuis csms_local.py::on_status_notification, a laissé une exception
MQTT (broker temporairement indisponible) se propager jusque dans le
gestionnaire de messages OCPP lui-même, ce qui a fait planter la boucle de
traitement et déconnecté la borne physique en boucle. _safe_publish() doit
absorber ce genre de panne sans jamais la laisser fuiter.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncio
import pytest

os.environ.setdefault("OCPP_DATA_DIR", "/tmp/test_ocpp")
os.environ.setdefault("OCPP_ADMIN_PASSWORD", "testpass")
os.environ.setdefault("OCPP_SECRET_KEY", "test-secret")

import app.mqtt_bridge as mqtt_bridge


class _FlakyClient:
    """Simule un broker MQTT qui échoue (timeout, déconnexion, etc.)."""
    async def publish(self, topic, payload, retain=True):
        raise Exception("Operation timed out")  # même famille que aiomqtt.exceptions.MqttError


def test_safe_publish_never_raises_on_broker_failure():
    mqtt_bridge._client = _FlakyClient()
    try:
        # Ne doit lever aucune exception, même si le broker échoue.
        asyncio.run(mqtt_bridge._safe_publish("some/topic", "payload"))
    finally:
        mqtt_bridge._client = None


def test_safe_publish_noop_without_client():
    mqtt_bridge._client = None
    # Ne doit pas planter non plus si le pont MQTT n'est pas connecté du tout.
    asyncio.run(mqtt_bridge._safe_publish("some/topic", "payload"))


def test_publish_connector_discovery_survives_broker_failure():
    # Cas exact de l'incident : publish_connector_discovery (appelée depuis
    # on_status_notification) ne doit jamais lever, même si CHAQUE appel
    # interne au broker échoue.
    mqtt_bridge._client = _FlakyClient()
    try:
        asyncio.run(mqtt_bridge.publish_connector_discovery("charger-1", 1, "local", "Borne test"))
    finally:
        mqtt_bridge._client = None


def test_publish_state_survives_broker_failure():
    mqtt_bridge._client = _FlakyClient()
    try:
        asyncio.run(mqtt_bridge.publish_connector_state("charger-1", 1, power_w=100, status="Charging"))
    finally:
        mqtt_bridge._client = None


def test_publish_vehicle_state_survives_broker_failure():
    mqtt_bridge._client = _FlakyClient()
    try:
        asyncio.run(mqtt_bridge.publish_vehicle_state(1, is_charging="ON"))
    finally:
        mqtt_bridge._client = None
