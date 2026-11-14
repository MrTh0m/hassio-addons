"""
Tests des notifications par utilisateur (0.19.45) : destinataires selon les
droits, catégories, canaux push et Home Assistant, événement MQTT, et
branchement sur les gestionnaires OCPP (début, fin, interruption, non
autorisée, défaut, borne hors ligne).
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncio
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("OCPP_DATA_DIR", "/tmp/test_ocpp")
os.environ.setdefault("OCPP_ADMIN_PASSWORD", "testpass")
os.environ.setdefault("OCPP_SECRET_KEY", "test-secret")

import app.db as _app_db
import app.csms_local as _csms
import app.notifications as notif
import app.mqtt_bridge as _mqtt
from app.models import (
    Base, Charger, ChargerMode, AuthMode, Transaction, Vehicle, User, UserRole,
    UserVehicle, UserCharger, NotificationSetting, PushSubscription, ConnectorStatus,
)
from app.csms_local import LocalChargePoint

TEST_ENGINE = create_engine(
    "sqlite:///file:notiftests?mode=memory&cache=shared&uri=true",
    connect_args={"check_same_thread": False},
)
TestingSession = sessionmaker(bind=TEST_ENGINE, autoflush=False, autocommit=False)


class Sent:
    def __init__(self):
        self.push, self.ha, self.mqtt = [], [], []


@pytest.fixture
def sent(monkeypatch):
    monkeypatch.setattr(_app_db, "SessionLocal", TestingSession)
    monkeypatch.setattr(_csms, "SessionLocal", TestingSession)
    s = Sent()

    def fake_push(info, payload, pem):
        if info["endpoint"].endswith("/gone"):
            raise notif.PushGone()
        s.push.append((info["endpoint"], payload))

    def fake_ha(service, title, message, tag):
        s.ha.append((service, title, message))

    async def fake_mqtt(charger_id, event, attrs):
        s.mqtt.append((charger_id, event, attrs))

    monkeypatch.setattr(notif, "_send_push", fake_push)
    monkeypatch.setattr(notif, "_send_ha", fake_ha)
    monkeypatch.setattr(_mqtt, "publish_charger_event", fake_mqtt)
    notif.STARTED_NOTIFIED.clear()
    notif.OFFLINE_NOTIFIED.clear()
    _csms.START_RETRIES.clear()
    _csms.PENDING_REMOTE_STARTS.clear()
    Base.metadata.create_all(TEST_ENGINE)
    yield s
    Base.metadata.drop_all(TEST_ENGINE)


def _world():
    """Deux bornes, deux véhicules ; admin, Sandie (Zoé + borne Maison),
    Paul (véhicule ID.3 seulement). Chacun a un appareil push et un service HA."""
    db = TestingSession()
    db.add_all([
        Charger(id="maison", display_name="Maison", mode=ChargerMode.local, auth_mode=AuthMode.authorized),
        Charger(id="garage", display_name="Garage", mode=ChargerMode.local, auth_mode=AuthMode.authorized),
    ])
    zoe, id3 = Vehicle(name="Zoé"), Vehicle(name="ID.3")
    admin = User(username="admin", password_hash="x", role=UserRole.admin)
    sandie = User(username="sandie", password_hash="x", role=UserRole.user)
    paul = User(username="paul", password_hash="x", role=UserRole.user)
    db.add_all([zoe, id3, admin, sandie, paul])
    db.flush()
    db.add_all([
        UserVehicle(user_id=sandie.id, vehicle_id=zoe.id),
        UserCharger(user_id=sandie.id, charger_id="maison"),
        UserVehicle(user_id=paul.id, vehicle_id=id3.id),
    ])
    for u in (admin, sandie, paul):
        db.add(PushSubscription(user_id=u.id, endpoint=f"https://push.example/{u.username}", p256dh="k", auth="a"))
        db.add(NotificationSetting(user_id=u.id, categories="charge,interrupted,charger,unauthorized",
                                   ha_service=f"mobile_app_{u.username}"))
    db.commit()
    ids = {"zoe": zoe.id, "id3": id3.id, "admin": admin.id, "sandie": sandie.id, "paul": paul.id}
    db.close()
    return ids


def _run(coro):
    async def wrapper():
        r = await coro
        await notif.drain()
        return r
    return asyncio.run(wrapper())


def _push_users(s):
    return sorted(e.rsplit("/", 1)[1] for e, _ in s.push)


def test_charge_event_goes_to_vehicle_owner_and_admin(sent):
    ids = _world()

    async def go():
        notif.notify("charge_ended", "maison", 1, ids["zoe"], energy_wh=23400, cost=4.85, duration_min=192)
    _run(go())
    assert _push_users(sent) == ["admin", "sandie"]
    assert sorted(s for s, _, _ in sent.ha) == ["mobile_app_admin", "mobile_app_sandie"]
    title, msg = sent.ha[0][1], sent.ha[0][2]
    assert title == "Charge terminée"
    assert "Zoé" in msg and "23,4 kWh" in msg and "4,85 €" in msg and "3 h 12" in msg and "Maison" in msg
    assert sent.mqtt and sent.mqtt[0][1] == "charge_ended"
    assert sent.mqtt[0][2]["energy_kwh"] == 23.4


def test_charger_event_goes_to_charger_users_only(sent):
    _world()

    async def go():
        notif.notify("charger_offline", "maison")
        notif.notify("charger_offline", "garage")
    _run(go())
    # Maison : admin + sandie ; Garage : admin seulement. Paul n'a aucune borne.
    assert _push_users(sent) == ["admin", "admin", "sandie"]


def test_categories_filter_per_user(sent):
    ids = _world()
    db = TestingSession()
    st = db.query(NotificationSetting).filter(NotificationSetting.user_id == ids["sandie"]).first()
    st.categories = "charger"
    st.ha_service = None
    db.commit()
    db.close()

    async def go():
        notif.notify("charge_started", "maison", 1, ids["zoe"])
    _run(go())
    assert _push_users(sent) == ["admin"]


def test_expired_push_subscription_is_deleted(sent):
    ids = _world()
    db = TestingSession()
    db.add(PushSubscription(user_id=ids["admin"], endpoint="https://push.example/gone", p256dh="k", auth="a"))
    db.commit()
    db.close()

    async def go():
        notif.notify("charger_online", "garage")
    _run(go())
    db = TestingSession()
    assert db.query(PushSubscription).filter(PushSubscription.endpoint.like("%/gone")).count() == 0
    assert db.query(PushSubscription).filter(PushSubscription.user_id == ids["admin"]).first().last_success_at is not None
    db.close()


def test_failing_channel_does_not_break_others(sent, monkeypatch):
    _world()

    def boom(*a, **k):
        raise RuntimeError("HA injoignable")
    monkeypatch.setattr(notif, "_send_ha", boom)

    async def go():
        return await notif._dispatch("charger_offline", "garage", None, None, None, {})
    r = asyncio.run(go())
    assert r["push"] == 1 and r["ha_failed"] == 1 and r["errors"]


def test_test_notification_only_to_caller_and_ignores_categories(sent):
    ids = _world()
    db = TestingSession()
    db.query(NotificationSetting).filter(NotificationSetting.user_id == ids["paul"]).first().categories = ""
    db.commit()
    db.close()
    r = asyncio.run(notif._dispatch("test", None, None, None, ids["paul"], {}))
    assert r["push"] == 1 and r["ha"] == 1
    assert _push_users(sent) == ["paul"]
    assert sent.mqtt == []


def test_vapid_keys_generated_once(sent):
    db = TestingSession()
    pem1, pub1 = notif.vapid_keys(db)
    pem2, pub2 = notif.vapid_keys(db)
    db.close()
    assert pem1 == pem2 and pub1 == pub2
    assert "PRIVATE KEY" in pem1
    assert len(pub1) >= 86  # point P-256 non compressé (65 octets) en base64url


# --- Branchement OCPP -------------------------------------------------------

def _iso(dt):
    return dt.replace(microsecond=0).isoformat() + "Z"


def _events(s):
    return [e for _, e, _ in s.mqtt]


def test_ocpp_flow_started_once_then_ended(sent):
    ids = _world()

    async def go():
        cp = LocalChargePoint("maison", connection=object())
        _csms.PENDING_REMOTE_STARTS[("maison", 1)] = ids["zoe"]
        r = await cp.on_start_transaction(connector_id=1, id_tag=f"REMOTE-{ids['zoe']}", meter_start=1000,
                                          timestamp=_iso(datetime.utcnow() - timedelta(hours=1)))
        await cp.on_status_notification(connector_id=1, status="Charging", error_code="NoError")
        await cp.on_status_notification(connector_id=1, status="SuspendedEV", error_code="NoError")
        await cp.on_status_notification(connector_id=1, status="Charging", error_code="NoError")
        await cp.on_stop_transaction(transaction_id=r.transaction_id, meter_stop=6000,
                                     timestamp=_iso(datetime.utcnow()), reason="EVDisconnected")
        # Arrêt rejoué une seconde fois (file de la borne) : pas de doublon.
        await cp.on_stop_transaction(transaction_id=r.transaction_id, meter_stop=6000,
                                     timestamp=_iso(datetime.utcnow()), reason="EVDisconnected")
    _run(go())
    assert _events(sent) == ["charge_started", "charge_ended"]


def test_ocpp_power_loss_is_interrupted(sent):
    ids = _world()

    async def go():
        cp = LocalChargePoint("maison", connection=object())
        _csms.PENDING_REMOTE_STARTS[("maison", 1)] = ids["zoe"]
        r = await cp.on_start_transaction(connector_id=1, id_tag=f"REMOTE-{ids['zoe']}", meter_start=1000,
                                          timestamp=_iso(datetime.utcnow() - timedelta(minutes=20)))
        await cp.on_stop_transaction(transaction_id=r.transaction_id, meter_stop=3000,
                                     timestamp=_iso(datetime.utcnow()), reason="PowerLoss")
    _run(go())
    assert _events(sent) == ["charge_interrupted"]
    assert "coupure de courant" in sent.ha[0][2]


def test_ocpp_aborted_start_is_silent_until_outcome(sent, monkeypatch):
    """Démarrage avorté puis relancé : ni fin ni interruption pour la
    tentative fusionnée."""
    ids = _world()
    monkeypatch.setattr(_csms, "START_RETRY_DELAY_S", 0)

    async def fake_remote_start(self, connector_id, id_tag):
        from types import SimpleNamespace
        return SimpleNamespace(status="Accepted")
    monkeypatch.setattr(LocalChargePoint, "trigger_remote_start", fake_remote_start)

    async def go():
        cp = LocalChargePoint("maison", connection=object())
        t0 = datetime.utcnow() - timedelta(seconds=10)
        _csms.PENDING_REMOTE_STARTS[("maison", 1)] = ids["zoe"]
        r = await cp.on_start_transaction(connector_id=1, id_tag=f"REMOTE-{ids['zoe']}", meter_start=1000, timestamp=_iso(t0))
        await cp.on_status_notification(connector_id=1, status="Available", error_code="NoError")
        await cp.on_status_notification(connector_id=1, status="Preparing", error_code="NoError")
        await cp.on_stop_transaction(transaction_id=r.transaction_id, meter_stop=1000,
                                     timestamp=_iso(t0 + timedelta(seconds=6)), reason="EVDisconnected")
        await asyncio.sleep(0.2)
        r2 = await cp.on_start_transaction(connector_id=1, id_tag=f"REMOTE-{ids['zoe']}", meter_start=1000,
                                           timestamp=_iso(datetime.utcnow()))
        await cp.on_status_notification(connector_id=1, status="Charging", error_code="NoError")
        return r2
    _run(go())
    assert _events(sent) == ["charge_started"]


def test_ocpp_unauthorized_and_fault(sent):
    _world()

    async def go():
        cp = LocalChargePoint("maison", connection=object())
        await cp.on_start_transaction(connector_id=1, id_tag="freeCharge", meter_start=1000,
                                      timestamp=_iso(datetime.utcnow()))
        await cp.on_status_notification(connector_id=1, status="Faulted", error_code="GroundFailure")
        await cp.on_status_notification(connector_id=1, status="Faulted", error_code="GroundFailure")  # répété : pas de doublon
    _run(go())
    assert _events(sent) == ["unauthorized_charge", "connector_fault"]
    faults = [a for _, e, a in sent.mqtt if e == "connector_fault"]
    assert "GroundFailure" in faults[0]["message"]


def test_offline_after_delay_then_online(sent, monkeypatch):
    _world()
    monkeypatch.setattr(notif, "OFFLINE_NOTIFY_DELAY_S", 0)
    _csms.CONNECTED_CHARGERS.pop("maison", None)

    async def go():
        notif.charger_disconnected("maison")
        await asyncio.sleep(0.05)
        await notif.drain()
        notif.charger_connected("maison")
    _run(go())
    assert _events(sent) == ["charger_offline", "charger_online"]


def test_brief_disconnect_does_not_notify(sent, monkeypatch):
    _world()
    monkeypatch.setattr(notif, "OFFLINE_NOTIFY_DELAY_S", 0.2)

    async def go():
        notif.charger_disconnected("maison")
        await asyncio.sleep(0.05)
        notif.charger_connected("maison")
        await asyncio.sleep(0.3)
    _run(go())
    assert sent.mqtt == []


def test_home_assistant_request_format(monkeypatch):
    """Appel au service notify via l'API Supervisor, et lecture de la liste
    des services notify."""
    import io, json as _json
    calls = []

    class Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=None):
        calls.append((req.get_method(), req.full_url, dict(req.header_items()), req.data))
        if req.full_url.endswith("/services"):
            return Resp(_json.dumps([{"domain": "light", "services": {"turn_on": {}}},
                                     {"domain": "notify", "services": {"mobile_app_pixel": {}, "send_message": {}, "persistent_notification": {}}}]).encode())
        return Resp(b"[]")

    monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")
    monkeypatch.setattr(notif.urllib.request, "urlopen", fake_urlopen)
    notif._send_ha("notify.mobile_app_pixel", "Charge terminée", "Zoé : 23,4 kWh", "ocpp-charge_ended-maison-1")
    method, url, headers, data = calls[0]
    assert method == "POST" and url == "http://supervisor/core/api/services/notify/mobile_app_pixel"
    assert headers["Authorization"] == "Bearer tok"
    body = _json.loads(data)
    assert body["title"] == "Charge terminée" and body["data"]["tag"] == "ocpp-charge_ended-maison-1"
    assert notif.list_ha_notify_services() == ["mobile_app_pixel", "persistent_notification"]


def test_home_assistant_unavailable_without_token(monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    assert notif.supervisor_available() is False
    with pytest.raises(RuntimeError):
        notif._send_ha("mobile_app_pixel", "t", "m", "tag")
