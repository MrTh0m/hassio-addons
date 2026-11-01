"""
Tests des horodatages OCPP rejoués (incident du 03/10).

Contexte : après un redémarrage, la borne a rejoué sa file de messages, dont
un StopTransaction vieux de 5 jours. Le serveur enregistrait l'heure de
réception au lieu du `timestamp` du message, ce qui faussait dates et durées
(transaction 23 : arrêtée le 27/09, affichée terminée le 03/10).

Vérifie aussi le handler SecurityEventNotification (action hors profil 1.6
standard, auparavant rejetée en NotImplemented à chaque démarrage).
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncio
import json
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
from app.models import Base, Charger, ChargerMode, AuthMode, Transaction
from app.csms_local import LocalChargePoint, _ocpp_ts

TEST_ENGINE = create_engine(
    "sqlite:///file:replayedts?mode=memory&cache=shared&uri=true",
    connect_args={"check_same_thread": False},
)
TestingSession = sessionmaker(bind=TEST_ENGINE, autoflush=False, autocommit=False)


@pytest.fixture(autouse=True)
def setup_db():
    orig_db_engine, orig_db_session = _app_db.engine, _app_db.SessionLocal
    orig_csms_session = _csms_local.SessionLocal
    orig_diag_session = _diagnostics.SessionLocal
    _app_db.engine, _app_db.SessionLocal = TEST_ENGINE, TestingSession
    _csms_local.SessionLocal = TestingSession
    _diagnostics.SessionLocal = TestingSession
    Base.metadata.create_all(TEST_ENGINE)
    yield
    Base.metadata.drop_all(TEST_ENGINE)
    _app_db.engine, _app_db.SessionLocal = orig_db_engine, orig_db_session
    _csms_local.SessionLocal = orig_csms_session
    _diagnostics.SessionLocal = orig_diag_session


def _run(coro):
    return asyncio.run(coro)


def _make_charger(charger_id="c1"):
    db = TestingSession()
    db.add(Charger(id=charger_id, mode=ChargerMode.local, auth_mode=AuthMode.free))
    db.commit()
    db.close()


def _iso(dt: datetime, ms=True) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + (".000Z" if ms else "Z")


# --- _ocpp_ts -------------------------------------------------------------

def test_ocpp_ts_parses_z_suffix_to_naive_utc():
    assert _ocpp_ts("2026-09-27T09:08:26.000Z") == datetime(2026, 9, 27, 9, 8, 26)
    assert _ocpp_ts("2026-09-27T09:08:26Z") == datetime(2026, 9, 27, 9, 8, 26)


def test_ocpp_ts_converts_offsets_to_utc():
    assert _ocpp_ts("2026-09-27T11:08:26+02:00") == datetime(2026, 9, 27, 9, 8, 26)


def test_ocpp_ts_naive_value_is_treated_as_utc():
    assert _ocpp_ts("2026-09-27T09:08:26") == datetime(2026, 9, 27, 9, 8, 26)


@pytest.mark.parametrize("bad", [None, "", "pas une date", 12345, "2026-13-45T99:99:99Z"])
def test_ocpp_ts_falls_back_to_now_when_missing_or_unreadable(bad):
    before = datetime.utcnow()
    got = _ocpp_ts(bad)
    assert before <= got <= datetime.utcnow()


def test_ocpp_ts_never_returns_a_future_date():
    future = datetime.utcnow() + timedelta(days=2)
    got = _ocpp_ts(_iso(future))
    assert got <= datetime.utcnow()


# --- StartTransaction / StopTransaction -----------------------------------

def test_replayed_start_and_stop_keep_the_charger_dates():
    _make_charger()
    cp = LocalChargePoint("c1", connection=object())
    start = datetime.utcnow() - timedelta(hours=3)
    stop = start + timedelta(minutes=48)

    resp = _run(cp.on_start_transaction(
        connector_id=1, id_tag="REMOTE-5", meter_start=1000, timestamp=_iso(start)))
    _run(cp.on_stop_transaction(
        transaction_id=resp.transaction_id, meter_stop=1000, timestamp=_iso(stop), reason="PowerLoss"))

    db = TestingSession()
    txn = db.query(Transaction).filter(Transaction.id == resp.transaction_id).first()
    db.close()
    assert txn.start_time == start.replace(microsecond=0)
    assert txn.stop_time == stop.replace(microsecond=0)
    assert txn.status == "completed"


def test_start_without_timestamp_keeps_working():
    """Les appels sans timestamp (tests existants, bornes peu rigoureuses)
    retombent sur l'heure de réception."""
    _make_charger()
    cp = LocalChargePoint("c1", connection=object())
    before = datetime.utcnow()
    resp = _run(cp.on_start_transaction(connector_id=1, id_tag="x", meter_start=0))
    db = TestingSession()
    txn = db.query(Transaction).filter(Transaction.id == resp.transaction_id).first()
    db.close()
    assert before - timedelta(seconds=1) <= txn.start_time <= datetime.utcnow() + timedelta(seconds=1)


def test_stop_never_before_start_for_legacy_transactions():
    """Une transaction créée avant ce correctif porte l'heure de réception
    comme début ; un StopTransaction rejoué plus ancien ne doit pas donner
    une durée négative."""
    _make_charger()
    started = datetime.utcnow() - timedelta(minutes=5)
    db = TestingSession()
    t = Transaction(charger_id="c1", connector_id=1, id_tag="x", meter_start=0,
                    start_time=started, status="active")
    db.add(t)
    db.commit()
    tid = t.id
    db.close()

    cp = LocalChargePoint("c1", connection=object())
    old_stop = _iso(started - timedelta(days=5))
    _run(cp.on_stop_transaction(transaction_id=tid, meter_stop=0, timestamp=old_stop))

    db = TestingSession()
    txn = db.query(Transaction).filter(Transaction.id == tid).first()
    db.close()
    assert txn.stop_time == started


def test_charging_seconds_never_negative_with_replayed_stop():
    _make_charger()
    now = datetime.utcnow()
    db = TestingSession()
    t = Transaction(charger_id="c1", connector_id=1, id_tag="x", meter_start=0,
                    start_time=now - timedelta(hours=1), status="active",
                    charging_seconds=10.0, charging_since=now)
    db.add(t)
    db.commit()
    tid = t.id
    db.close()

    cp = LocalChargePoint("c1", connection=object())
    # Arrêt réel 30 min AVANT charging_since (posé à la réception).
    _run(cp.on_stop_transaction(
        transaction_id=tid, meter_stop=0, timestamp=_iso(now - timedelta(minutes=30))))

    db = TestingSession()
    txn = db.query(Transaction).filter(Transaction.id == tid).first()
    db.close()
    assert txn.charging_seconds == 10.0
    assert txn.charging_since is None


# --- SecurityEventNotification --------------------------------------------

def test_security_event_notification_is_acknowledged_over_the_wire():
    """Test de bout en bout par la vraie route de la bibliothèque : la borne
    reçoit un CallResult (message type 3) à la place du CallError
    NotImplemented d'avant."""
    sent = []

    class FakeConn:
        async def send(self, msg):
            sent.append(msg)

    cp = LocalChargePoint("c1", connection=FakeConn())
    call = json.dumps([2, "uid-1", "SecurityEventNotification", {
        "type": "StartupOfTheDevice",
        "timestamp": "2026-10-02T22:39:49.000Z",
        "techInfo": "The Charge Point has booted",
    }])
    _run(cp.route_message(call))

    assert len(sent) == 1
    reply = json.loads(sent[0])
    assert reply[0] == 3, reply          # CallResult, pas 4 (CallError)
    assert reply[1] == "uid-1"
    assert reply[2] == {}
