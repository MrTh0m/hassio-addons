"""
Tests de la surveillance de la file de messages des bornes (queue_watch),
du rattrapage des relevés tardifs et du durcissement du WebSocket.

Contexte (incidents des 10/10 et 13/10, voir CHANGELOG) : la file de messages
de transaction de la borne se bloque après une coupure du serveur pendant une
charge. Le défaut doit être signalé, le reset automatique rester un réglage
désactivé par défaut, et les données d'une session clôturée par le serveur
doivent être corrigées quand les relevés arrivent en retard.
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
import app.csms_local as _csms_local
import app.diagnostics as _diagnostics
import app.queue_watch as qw
from app.models import (Base, Charger, ChargerMode, AuthMode, Transaction,
                        ConnectorStatus, MeterValue)
from app.csms_local import LocalChargePoint, CONNECTED_CHARGERS, _last_register_wh

TEST_ENGINE = create_engine(
    "sqlite:///file:queuewatch?mode=memory&cache=shared&uri=true",
    connect_args={"check_same_thread": False},
)
TestingSession = sessionmaker(bind=TEST_ENGINE, autoflush=False, autocommit=False)


@pytest.fixture(autouse=True)
def setup_db():
    saved = (_app_db.engine, _app_db.SessionLocal, _csms_local.SessionLocal,
             _diagnostics.SessionLocal, qw.SessionLocal)
    _app_db.engine, _app_db.SessionLocal = TEST_ENGINE, TestingSession
    _csms_local.SessionLocal = TestingSession
    _diagnostics.SessionLocal = TestingSession
    qw.SessionLocal = TestingSession
    Base.metadata.create_all(TEST_ENGINE)
    for d in (qw.LAST_METER_RX, qw.METER_STATS, qw.REMOTE_START_ACCEPTED, qw.BOOT_AT,
              qw.BLOCKED, qw.LAST_AUTO_RESET):
        d.clear()
    CONNECTED_CHARGERS.pop("c1", None)
    yield
    CONNECTED_CHARGERS.pop("c1", None)
    Base.metadata.drop_all(TEST_ENGINE)
    (_app_db.engine, _app_db.SessionLocal, _csms_local.SessionLocal,
     _diagnostics.SessionLocal, qw.SessionLocal) = saved


def _run(coro):
    return asyncio.run(coro)


def _setup(status="Charging", with_txn=True, started_min_ago=30):
    db = TestingSession()
    db.add(Charger(id="c1", mode=ChargerMode.local, auth_mode=AuthMode.free))
    db.add(ConnectorStatus(charger_id="c1", connector_id=1, status=status))
    if with_txn:
        start = datetime.utcnow() - timedelta(minutes=started_min_ago)
        db.add(Transaction(charger_id="c1", connector_id=1, id_tag="t", meter_start=1000,
                           start_time=start, status="active"))
    db.commit()
    db.close()
    CONNECTED_CHARGERS["c1"] = object()


def test_meter_silence_detected_and_sticky():
    _setup()
    qw.SERVER_STARTED = datetime.utcnow() - timedelta(hours=1)
    qw.LAST_METER_RX[("c1", 1)] = datetime.utcnow() - timedelta(minutes=10)
    qw.evaluate()
    assert qw.blocked_for("c1")["reason"] == "meter_silence"
    # Reste signalé même quand plus rien ne charge.
    db = TestingSession()
    db.query(ConnectorStatus).update({"status": "Available"})
    db.commit(); db.close()
    qw.evaluate()
    assert qw.blocked_for("c1") is not None
    # Un relevé frais lève le blocage.
    qw.note_meter_values("c1", 1, 1, [datetime.utcnow()])
    assert qw.blocked_for("c1") is None


def test_replayed_meter_does_not_clear_block():
    _setup()
    qw.SERVER_STARTED = datetime.utcnow() - timedelta(hours=1)
    qw.evaluate()
    assert qw.blocked_for("c1") is not None
    replayed = qw.note_meter_values("c1", 1, 1, [datetime.utcnow() - timedelta(hours=2)])
    assert replayed is True
    assert qw.blocked_for("c1") is not None


def test_no_block_when_meters_flow():
    _setup()
    qw.SERVER_STARTED = datetime.utcnow() - timedelta(hours=1)
    qw.note_meter_values("c1", 1, 1, [datetime.utcnow()])
    qw.evaluate()
    assert qw.blocked_for("c1") is None


def test_start_stalled_detected_then_cleared_by_boot():
    _setup(status="Preparing", with_txn=False)
    qw.REMOTE_START_ACCEPTED[("c1", 1)] = datetime.utcnow() - timedelta(minutes=3)
    qw.evaluate()
    assert qw.blocked_for("c1")["reason"] == "start_stalled"
    qw.note_boot("c1")
    assert qw.blocked_for("c1") is None


def test_boot_time_persisted_and_uptime():
    _setup(with_txn=False, status="Available")
    when = datetime.utcnow() - timedelta(hours=5)
    qw.note_boot("c1", when, from_device=True)
    qw.BOOT_AT.clear()
    assert abs(qw.uptime_seconds("c1") - 5 * 3600) < 5


def test_auto_reset_off_by_default_and_gated():
    _setup(status="Available", with_txn=False)
    calls = []

    class FakeCP:
        async def trigger_reset(self, t):
            calls.append(t)

    CONNECTED_CHARGERS["c1"] = FakeCP()
    qw.BLOCKED["c1"] = {"charger_id": "c1", "connector_id": 1, "reason": "meter_silence",
                        "detected_at": datetime.utcnow(), "since": datetime.utcnow(), "detail": "x"}
    assert qw.auto_reset_enabled() is False
    _run(qw.tick())
    assert calls == []
    qw.set_auto_reset(True)
    qw.BLOCKED["c1"] = {"charger_id": "c1", "connector_id": 1, "reason": "meter_silence",
                        "detected_at": datetime.utcnow(), "since": datetime.utcnow(), "detail": "x"}
    _run(qw.tick())
    assert calls == ["Soft"]
    # Délai de garde : pas de second reset immédiat.
    qw.BLOCKED["c1"] = {"charger_id": "c1", "connector_id": 1, "reason": "meter_silence",
                        "detected_at": datetime.utcnow(), "since": datetime.utcnow(), "detail": "x"}
    _run(qw.tick())
    assert calls == ["Soft"]


def test_auto_reset_never_while_charging():
    _setup(status="Charging", with_txn=True)
    calls = []

    class FakeCP:
        async def trigger_reset(self, t):
            calls.append(t)

    CONNECTED_CHARGERS["c1"] = FakeCP()
    qw.set_auto_reset(True)
    qw.BLOCKED["c1"] = {"charger_id": "c1", "connector_id": 1, "reason": "meter_silence",
                        "detected_at": datetime.utcnow(), "since": datetime.utcnow(), "detail": "x"}
    qw.LAST_METER_RX[("c1", 1)] = datetime.utcnow()
    _run(qw.tick())
    assert calls == []


def test_last_register_replaces_meter_start():
    _setup(with_txn=True)
    db = TestingSession()
    txn = db.query(Transaction).first()
    db.add(MeterValue(charger_id="c1", transaction_id=txn.id, connector_id=1,
                      timestamp=datetime.utcnow(), measurand="Energy.Active.Import.Register",
                      value=1500.0, unit="Wh"))
    db.commit()
    assert _last_register_wh(db, "c1", 1, txn.id, 1000) == 1500.0
    assert _last_register_wh(db, "c1", 1, 9999, 1000) == 1000
    db.close()


def test_late_meter_values_repair_closed_session():
    """Session close par le serveur (close_reason) puis relevés tardifs :
    l'énergie et le coût sont recalculés."""
    _setup(status="Available", with_txn=False)
    start = datetime.utcnow() - timedelta(hours=2)
    db = TestingSession()
    txn = Transaction(charger_id="c1", connector_id=1, id_tag="t", meter_start=1000,
                      meter_stop=1000, start_time=start, stop_time=start + timedelta(minutes=30),
                      status="completed", close_reason="reconciled")
    db.add(txn); db.commit()
    tid = txn.id
    db.close()

    cp = LocalChargePoint("c1", connection=object())
    ts = lambda m: (start + timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:%SZ")
    mv = [{"timestamp": ts(m), "sampled_value": [
        {"value": str(1000 + m * 100), "measurand": "Energy.Active.Import.Register", "unit": "Wh"}]}
        for m in (10, 20, 29)]
    _run(cp.on_meter_values(connector_id=1, meter_value=mv, transaction_id=tid))

    db = TestingSession()
    txn = db.query(Transaction).get(tid)
    n = db.query(MeterValue).filter(MeterValue.transaction_id == tid).count()
    db.close()
    assert n == 3
    assert txn.energy_wh == pytest.approx(2900.0, abs=1.0)  # 1000 -> 3900 Wh, meter_stop=meter_start ignoré


def test_ws_adapter_send_converts_closed_socket_errors():
    from app.ws_adapter import StarletteWebSocketAdapter

    class Dead:
        async def send_text(self, m):
            raise RuntimeError('Cannot call "send" once a close message has been sent.')

    with pytest.raises(ConnectionError):
        _run(StarletteWebSocketAdapter(Dead()).send("x"))
