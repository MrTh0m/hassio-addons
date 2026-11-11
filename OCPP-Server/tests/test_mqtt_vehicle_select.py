"""
Tests du select MQTT « véhicule » d'un connecteur et des cumuls mensuels d'un
véhicule (0.19.43) :
- options et valeur par défaut du select, choix respecté (y compris « Aucun ») ;
- la charge démarrée depuis Home Assistant est rattachée au véhicule choisi ;
- pendant une session, le select affiche le véhicule de la session ;
- cumuls du mois : seules les charges terminées du mois civil en cours comptent.
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

import asyncio
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from test_api import setup_db, TestingSession  # noqa: F401
import app.mqtt_bridge as mqtt_bridge
from app.csms_local import PENDING_REMOTE_STARTS
from app.models import Charger, ChargerMode, ConnectorStatus, Transaction, Vehicle, VehicleCharger


@pytest.fixture(autouse=True)
def _clean_bridge():
    def reset():
        mqtt_bridge._outbox.clear()
        mqtt_bridge.SELECTED_VEHICLE.clear()
        mqtt_bridge._select_defaulted.clear()
        PENDING_REMOTE_STARTS.clear()
    reset()
    mqtt_bridge._wakeup = None
    mqtt_bridge._client = object()
    yield
    reset()
    mqtt_bridge._client = None


def _published() -> dict:
    return {topic: payload for topic, payload, _retain in mqtt_bridge._outbox}


def _setup(vehicles=("Zoe",), link=True, mode=ChargerMode.local):
    db = TestingSession()
    db.add(Charger(id="cp1", mode=mode, display_name="Maison"))
    db.add(ConnectorStatus(charger_id="cp1", connector_id=1, status="Available"))
    ids = []
    for name in vehicles:
        v = Vehicle(name=name)
        db.add(v)
        db.flush()
        ids.append(v.id)
        if link:
            db.add(VehicleCharger(vehicle_id=v.id, charger_id="cp1"))
    db.commit()
    db.close()
    return ids


def test_single_vehicle_is_preselected():
    (vid,) = _setup()
    asyncio.run(mqtt_bridge.publish_vehicle_select("cp1", 1))
    out = _published()
    cfg = json.loads(out["homeassistant/select/cp1_connector1_vehicle/config"])
    assert cfg["options"] == ["Aucun", "Zoe"]
    assert cfg["command_topic"] == "ocppserver/cp1/connector1/vehicle/set"
    assert out["ocppserver/cp1/connector1/vehicle/state"] == "Zoe"
    assert mqtt_bridge.SELECTED_VEHICLE[("cp1", 1)] == vid


def test_default_is_last_vehicle_used_on_connector():
    a, b = _setup(vehicles=("Panda", "Zoe"))
    db = TestingSession()
    db.add(Transaction(charger_id="cp1", connector_id=1, vehicle_id=b, meter_start=0,
                       start_time=datetime(2026, 11, 1, 10), status="completed"))
    db.commit()
    db.close()
    asyncio.run(mqtt_bridge.publish_vehicle_select("cp1", 1))
    assert _published()["ocppserver/cp1/connector1/vehicle/state"] == "Zoe"


def test_choice_and_none_are_respected():
    a, b = _setup(vehicles=("Panda", "Zoe"))
    asyncio.run(mqtt_bridge._handle_vehicle_select("cp1", 1, "Panda"))
    assert mqtt_bridge.SELECTED_VEHICLE[("cp1", 1)] == a
    assert _published()["ocppserver/cp1/connector1/vehicle/state"] == "Panda"
    asyncio.run(mqtt_bridge._handle_vehicle_select("cp1", 1, "Aucun"))
    assert ("cp1", 1) not in mqtt_bridge.SELECTED_VEHICLE
    # Une republication ne doit pas remettre un véhicule par défaut.
    asyncio.run(mqtt_bridge.publish_vehicle_select("cp1", 1))
    assert _published()["ocppserver/cp1/connector1/vehicle/state"] == "Aucun"


def test_unlinked_vehicles_are_offered_when_charger_has_no_link():
    _setup(vehicles=("Panda", "Zoe"), link=False)
    asyncio.run(mqtt_bridge.publish_vehicle_select("cp1", 1))
    cfg = json.loads(_published()["homeassistant/select/cp1_connector1_vehicle/config"])
    assert cfg["options"] == ["Aucun", "Panda", "Zoe"]


def test_start_identity_attaches_selected_vehicle():
    (vid,) = _setup()
    assert mqtt_bridge.start_identity("cp1", 1, "MQTT") == "MQTT"  # rien de choisi
    assert ("cp1", 1) not in PENDING_REMOTE_STARTS
    asyncio.run(mqtt_bridge._handle_vehicle_select("cp1", 1, "Zoe"))
    assert mqtt_bridge.start_identity("cp1", 1, "MQTT") == f"REMOTE-{vid}"
    assert PENDING_REMOTE_STARTS[("cp1", 1)] == vid


def test_select_shows_session_vehicle_and_ignores_changes_during_session():
    a, b = _setup(vehicles=("Panda", "Zoe"))
    asyncio.run(mqtt_bridge._handle_vehicle_select("cp1", 1, "Panda"))
    db = TestingSession()
    db.add(Transaction(charger_id="cp1", connector_id=1, vehicle_id=b, meter_start=0,
                       start_time=datetime.utcnow(), status="active"))
    db.commit()
    db.close()
    asyncio.run(mqtt_bridge._handle_vehicle_select("cp1", 1, "Aucun"))
    assert _published()["ocppserver/cp1/connector1/vehicle/state"] == "Zoe"
    assert mqtt_bridge.SELECTED_VEHICLE[("cp1", 1)] == a  # choix conservé pour la suite


def test_relay_charger_has_no_select():
    _setup(mode=ChargerMode.relay)
    asyncio.run(mqtt_bridge.publish_vehicle_select("cp1", 1))
    assert _published()["homeassistant/select/cp1_connector1_vehicle/config"] == ""


def test_month_totals():
    (vid,) = _setup()
    db = TestingSession()
    def txn(start, energy, cost, odo, status="completed"):
        db.add(Transaction(charger_id="cp1", connector_id=1, vehicle_id=vid, meter_start=0,
                           start_time=start, energy_wh=energy, cost=cost, odometer_km=odo, status=status))
    txn(datetime(2026, 10, 20, 12), 10000, 2.0, 3000)
    txn(datetime(2026, 11, 3, 12), 18655, 3.72, 3108)
    txn(datetime(2026, 11, 9, 12), 5000, 1.0, 3200)
    txn(datetime(2026, 11, 10, 12), 999, 9.0, 3300, status="active")
    db.commit()
    out = mqtt_bridge.vehicle_month_totals(db, vid, ZoneInfo("Europe/Paris"), now=datetime(2026, 11, 11, 18))
    db.close()
    assert out == {"month_charge_count": 2, "month_energy_kwh": 23.66, "month_cost": 4.72, "month_km": 200.0}


def test_month_boundary_follows_display_timezone():
    (vid,) = _setup()
    db = TestingSession()
    # 31/10 23:30 UTC = 1er novembre 00:30 à Paris : compte pour novembre.
    db.add(Transaction(charger_id="cp1", connector_id=1, vehicle_id=vid, meter_start=0,
                       start_time=datetime(2026, 10, 31, 23, 30), energy_wh=1000, cost=0.2, status="completed"))
    db.commit()
    out = mqtt_bridge.vehicle_month_totals(db, vid, ZoneInfo("Europe/Paris"), now=datetime(2026, 11, 11, 18))
    db.close()
    assert out["month_charge_count"] == 1
