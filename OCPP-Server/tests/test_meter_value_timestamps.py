"""
Tests des relevés rejoués (incident du 09/10).

1. Après un reboot, la borne rejoue sa file de relevés ; le serveur les datait
   de l'heure de réception au lieu de leur `timestamp`.
2. Conséquence : une session de 228 Wh affichait 456 Wh. L'arrêt rejoué
   (323618 Wh, daté 08:30) précédait dans le temps les relevés de la même
   session (323390 -> 323618 Wh) reçus ensuite, et la somme des paliers
   croissants recomptait deux fois la même énergie.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("OCPP_DATA_DIR", "/tmp/test_ocpp")
os.environ.setdefault("OCPP_ADMIN_PASSWORD", "testpass")
os.environ.setdefault("OCPP_SECRET_KEY", "test-secret")

import app.db as _app_db
import app.csms_local as _csms_local
from app.models import Base, Charger, ChargerMode, Transaction, MeterValue
from app.csms_local import LocalChargePoint
from app.pricing import compute_session_cost

TEST_ENGINE = create_engine(
    "sqlite:///file:mvtimestamps?mode=memory&cache=shared&uri=true",
    connect_args={"check_same_thread": False},
)
TestingSession = sessionmaker(bind=TEST_ENGINE, autoflush=False, autocommit=False)


@pytest.fixture(autouse=True)
def setup_db():
    orig_db_engine, orig_db_session = _app_db.engine, _app_db.SessionLocal
    orig_csms_session = _csms_local.SessionLocal
    _app_db.engine, _app_db.SessionLocal = TEST_ENGINE, TestingSession
    _csms_local.SessionLocal = TestingSession
    Base.metadata.create_all(TEST_ENGINE)
    yield
    Base.metadata.drop_all(TEST_ENGINE)
    _app_db.engine, _app_db.SessionLocal = orig_db_engine, orig_db_session
    _csms_local.SessionLocal = orig_csms_session


def _run(coro):
    return asyncio.run(coro)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + ".000Z"


def _frame(value, ts):
    return [{
        "timestamp": _iso(ts),
        "sampled_value": [
            {"value": str(value), "measurand": "Energy.Active.Import.Register", "unit": "Wh"},
        ],
    }]


def _charger(db, cid="c1"):
    db.add(Charger(id=cid, mode=ChargerMode.local))
    db.commit()


def _stored(measurand="Energy.Active.Import.Register"):
    db = TestingSession()
    rows = db.query(MeterValue).filter(MeterValue.measurand == measurand).order_by(MeterValue.id).all()
    db.close()
    return rows


# --- horodatage stocké ----------------------------------------------------

def test_replayed_reading_without_active_session_keeps_charger_date():
    db = TestingSession(); _charger(db); db.close()
    cp = LocalChargePoint("c1", connection=object())
    replayed = datetime.utcnow() - timedelta(days=5)
    _run(cp.on_meter_values(connector_id=1, meter_value=_frame(323128, replayed), transaction_id=31))
    rows = _stored()
    assert len(rows) == 1
    assert rows[0].timestamp == replayed.replace(microsecond=0)


def test_live_reading_on_active_session_keeps_charger_date():
    db = TestingSession(); _charger(db)
    start = datetime.utcnow() - timedelta(minutes=30)
    txn = Transaction(charger_id="c1", connector_id=1, meter_start=1000, status="active", start_time=start)
    db.add(txn); db.commit(); db.refresh(txn); txn_id = txn.id; db.close()
    cp = LocalChargePoint("c1", connection=object())
    when = datetime.utcnow() - timedelta(minutes=5)
    _run(cp.on_meter_values(connector_id=1, meter_value=_frame(1500, when), transaction_id=txn_id))
    rows = _stored()
    assert rows[0].transaction_id == txn_id
    assert rows[0].timestamp == when.replace(microsecond=0)


def test_reading_dated_before_session_start_falls_back_to_reception_time():
    """Horloge de borne déréglée pendant une session réelle : le relevé doit
    rester dans la fenêtre de la session pour compter dans l'énergie."""
    db = TestingSession(); _charger(db)
    start = datetime.utcnow() - timedelta(minutes=10)
    txn = Transaction(charger_id="c1", connector_id=1, meter_start=1000, status="active", start_time=start)
    db.add(txn); db.commit(); db.refresh(txn); txn_id = txn.id; db.close()
    cp = LocalChargePoint("c1", connection=object())
    before = datetime.utcnow()
    _run(cp.on_meter_values(connector_id=1, meter_value=_frame(1200, datetime(2026, 10, 3, 22, 13)), transaction_id=txn_id))
    rows = _stored()
    assert rows[0].transaction_id == txn_id
    assert before - timedelta(seconds=1) <= rows[0].timestamp <= datetime.utcnow() + timedelta(seconds=1)


def test_reading_without_timestamp_uses_reception_time():
    db = TestingSession(); _charger(db); db.close()
    cp = LocalChargePoint("c1", connection=object())
    before = datetime.utcnow()
    _run(cp.on_meter_values(connector_id=1, transaction_id=None, meter_value=[{
        "sampled_value": [{"value": "10", "measurand": "Energy.Active.Import.Register", "unit": "Wh"}],
    }]))
    rows = _stored()
    assert before - timedelta(seconds=1) <= rows[0].timestamp <= datetime.utcnow() + timedelta(seconds=1)


# --- calcul d'énergie -----------------------------------------------------

def _txn(meter_start, meter_stop, start, stop):
    return SimpleNamespace(meter_start=meter_start, meter_stop=meter_stop,
                           start_time=start, stop_time=stop, status="completed")


def _mv(value, ts):
    return SimpleNamespace(measurand="Energy.Active.Import.Register", value=value, unit="Wh", timestamp=ts)


def test_out_of_order_readings_are_not_counted_twice():
    """Cas réel du 09/10 (tx 34) : 228 Wh, pas 456."""
    t0 = datetime(2026, 10, 9, 6, 30, 15)
    txn = _txn(323390, 323618, t0, t0)
    mvs = [
        _mv(323390, datetime(2026, 10, 9, 6, 33, 42)),
        _mv(323412, datetime(2026, 10, 9, 6, 33, 48)),
        _mv(323515, datetime(2026, 10, 9, 6, 33, 53)),
        _mv(323618, datetime(2026, 10, 9, 6, 33, 58)),
    ]
    assert compute_session_cost(txn, mvs, None)["energy_wh"] == 228


def test_cost_not_doubled_either():
    t0 = datetime(2026, 10, 9, 6, 30, 15)
    txn = _txn(323390, 323618, t0, t0)
    mvs = [_mv(323390, t0 + timedelta(minutes=3)), _mv(323618, t0 + timedelta(minutes=4))]
    plan = SimpleNamespace(
        name="x", is_default=True, periods=[],
        base_price=0.2, price=0.2,
    )
    from app import pricing
    orig = pricing.price_at
    pricing.price_at = lambda plan, when: 0.2
    try:
        res = compute_session_cost(txn, mvs, plan)
    finally:
        pricing.price_at = orig
    assert res["energy_wh"] == 228
    assert res["cost"] == round(0.228 * 0.2, 4)


def test_normal_increasing_session_unchanged():
    t0 = datetime(2026, 10, 9, 8, 0, 0)
    txn = _txn(1000, 1900, t0, t0 + timedelta(hours=1))
    mvs = [_mv(1300, t0 + timedelta(minutes=20)), _mv(1600, t0 + timedelta(minutes=40))]
    assert compute_session_cost(txn, mvs, None)["energy_wh"] == 900


def test_fallback_meter_stop_equal_to_start_still_counts_measured_energy():
    """Régression 0.19.30 : meter_stop replié sur meter_start."""
    t0 = datetime(2026, 10, 9, 8, 0, 0)
    txn = _txn(1000, 1000, t0, t0 + timedelta(hours=2))
    mvs = [_mv(1500, t0 + timedelta(minutes=30)), _mv(2500, t0 + timedelta(minutes=90))]
    assert compute_session_cost(txn, mvs, None)["energy_wh"] == 1500
