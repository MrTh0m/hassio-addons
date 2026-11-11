"""
Vérifie les entités exposées à Home Assistant par le pont MQTT (0.19.42) :
affichage en kWh sans changer ni les topics ni les unique_id, remise à zéro
des valeurs de session quand le connecteur est libre, capteur « En ligne »
et indicateurs dérivés du véhicule (consommation, coût aux 100 km, part de
batterie rechargée).
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncio
import json
import pytest

os.environ.setdefault("OCPP_DATA_DIR", "/tmp/test_ocpp")
os.environ.setdefault("OCPP_ADMIN_PASSWORD", "testpass")
os.environ.setdefault("OCPP_SECRET_KEY", "test-secret")

import app.mqtt_bridge as mqtt_bridge


@pytest.fixture(autouse=True)
def _fake_client():
    mqtt_bridge._outbox.clear()
    mqtt_bridge._wakeup = None
    mqtt_bridge._client = object()  # non None : les publications sont mises en file
    yield
    mqtt_bridge._outbox.clear()
    mqtt_bridge._client = None


def _published() -> dict:
    return {topic: payload for topic, payload, _retain in mqtt_bridge._outbox}


def test_connector_energy_is_shown_in_kwh_with_unchanged_ids():
    asyncio.run(mqtt_bridge.publish_connector_discovery("borne-1", 1, "local"))
    out = _published()
    for key in ("energy_wh", "session_energy_wh", "last_session_energy_wh"):
        cfg = json.loads(out[f"homeassistant/sensor/borne_1_connector1_{key}/config"])
        assert cfg["unit_of_measurement"] == "kWh"
        assert "1000" in cfg["value_template"]
        # Identifiants et topics historiques conservés : aucune entité recréée.
        assert cfg["unique_id"] == f"ocppserver_borne_1_connector1_{key}"
        assert cfg["state_topic"] == f"ocppserver/borne_1/connector1/{key}"
    duration = json.loads(out["homeassistant/sensor/borne_1_connector1_session_duration_min/config"])
    assert duration["device_class"] == "duration"


def test_charger_discovery_has_online_sensor_and_diagnostic_status():
    asyncio.run(mqtt_bridge.publish_discovery("borne-1", "local", "Borne maison"))
    out = _published()
    online = json.loads(out["homeassistant/binary_sensor/borne_1_online/config"])
    assert online["device_class"] == "connectivity"
    assert online["state_topic"] == "ocppserver/borne_1/online"
    status = json.loads(out["homeassistant/sensor/borne_1_status/config"])
    assert status["entity_category"] == "diagnostic"


def test_online_state():
    asyncio.run(mqtt_bridge.publish_online("borne-1", True))
    assert _published()["ocppserver/borne_1/online"] == "ON"
    asyncio.run(mqtt_bridge.publish_online("borne-1", False))
    assert _published()["ocppserver/borne_1/online"] == "OFF"


def test_connector_session_idle_resets_session_values():
    asyncio.run(mqtt_bridge.publish_connector_session_idle("borne-1", 1))
    out = _published()
    base = "ocppserver/borne_1/connector1/"
    assert out[base + "session_energy_wh"] == "0"
    assert out[base + "session_cost"] == "0"
    assert out[base + "session_duration_min"] == "0"
    assert out[base + "power_w"] == "0"
    assert out[base + "current_a"] == "0"
    # « None » = état Inconnu pour un capteur MQTT de Home Assistant.
    assert out[base + "session_start_time"] == "None"
    assert out[base + "voltage_v"] == "None"
    # Le bilan de la dernière charge n'est pas touché.
    assert base + "last_session_energy_wh" not in out


def test_vehicle_session_idle():
    asyncio.run(mqtt_bridge.publish_vehicle_session_idle(7))
    out = _published()
    assert out["ocppserver/vehicle7/is_charging"] == "OFF"
    assert out["ocppserver/vehicle7/charging_at"] == "Aucune"
    assert out["ocppserver/vehicle7/session_energy_wh"] == "0"
    assert out["ocppserver/vehicle7/session_start_time"] == "None"


def test_vehicle_discovery_and_unpublish_cover_the_same_entities():
    asyncio.run(mqtt_bridge.publish_vehicle_discovery(7, "Voiture"))
    created = {t for t in _published() if t.startswith("homeassistant/")}
    for key in ("consumption_kwh_100km", "cost_per_100km", "last_session_battery_pct"):
        assert f"homeassistant/sensor/vehicle7_{key}/config" in created
    mqtt_bridge._outbox.clear()
    asyncio.run(mqtt_bridge.unpublish_vehicle_discovery(7))
    removed = _published()
    assert set(removed) == created
    assert all(payload == "" for payload in removed.values())


def test_charger_unpublish_removes_online_sensor():
    asyncio.run(mqtt_bridge.unpublish_charger_discovery("borne-1", [0, 1]))
    out = _published()
    assert out["homeassistant/binary_sensor/borne_1_online/config"] == ""
    assert out["homeassistant/sensor/borne_1_connector1_energy_wh/config"] == ""


def test_vehicle_efficiency_nominal():
    # 18 655 Wh et 3,72 EUR pour 108 km, batterie de 44 kWh.
    out = mqtt_bridge.vehicle_efficiency(18655, 3.72, 108, 44)
    assert out["consumption_kwh_100km"] == 17.3
    assert out["cost_per_100km"] == 3.44
    assert out["last_session_battery_pct"] == 42


def test_vehicle_efficiency_unknown_when_not_computable():
    unknown = mqtt_bridge.UNKNOWN
    # Odomètre non saisi : pas de distance, donc pas d'estimation.
    out = mqtt_bridge.vehicle_efficiency(18655, 3.72, None, None)
    assert out == {"consumption_kwh_100km": unknown, "cost_per_100km": unknown,
                   "last_session_battery_pct": unknown}
    # Distance trop courte pour être significative.
    out = mqtt_bridge.vehicle_efficiency(5000, 1.0, 2, 44)
    assert out["consumption_kwh_100km"] == unknown
    assert out["cost_per_100km"] == unknown
    assert out["last_session_battery_pct"] == 11
