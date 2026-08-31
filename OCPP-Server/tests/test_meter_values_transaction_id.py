"""
Tests pour la corrélation MeterValues <-> transaction active par transactionId.

Contexte (incident réel, voir CHANGELOG) : une borne Schneider a rejoué un
backlog de relevés bufferisés (transactionId=0, datés de plusieurs jours dans
le passé) EN PLEIN MILIEU d'une vraie session (transactionId=9 côté borne).
Avant ce correctif, on_meter_values associait tout relevé reçu à la
transaction active du connecteur sans vérifier le transactionId annoncé par
la borne, donc les relevés rejoués écrasaient régulièrement la progression
réelle de l'énergie de la vraie session.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncio
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

TEST_ENGINE = create_engine(
    "sqlite:///file:metervalues?mode=memory&cache=shared&uri=true",
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


def SessionLocal():
    return TestingSession()


def _run(coro):
    return asyncio.run(coro)


def _energy_frame(value, unit="Wh", ts="2026-08-12T15:44:00.000Z"):
    return [{
        "timestamp": ts,
        "sampled_value": [
            {"value": str(value), "measurand": "Energy.Active.Import.Register", "unit": unit},
        ],
    }]


def _setup_charger_and_active_txn(charger_id="c1", connector_id=1, txn_id_hint=None, meter_start=132546):
    db = SessionLocal()
    db.add(Charger(id=charger_id, mode=ChargerMode.local))
    db.commit()
    txn = Transaction(
        charger_id=charger_id, connector_id=connector_id,
        meter_start=meter_start, status="active",
    )
    db.add(txn)
    db.commit()
    db.refresh(txn)
    txn_id = txn.id
    db.close()
    return txn_id


def test_matching_transaction_id_gets_associated():
    txn_id = _setup_charger_and_active_txn()
    cp = LocalChargePoint("c1", connection=object())
    _run(cp.on_meter_values(connector_id=1, meter_value=_energy_frame(132589), transaction_id=txn_id))
    db = SessionLocal()
    rows = db.query(MeterValue).filter(MeterValue.charger_id == "c1").all()
    db.close()
    assert len(rows) == 1
    assert rows[0].transaction_id == txn_id
    assert rows[0].value == 132589


def test_mismatched_transaction_id_not_associated():
    txn_id = _setup_charger_and_active_txn()
    cp = LocalChargePoint("c1", connection=object())
    # transactionId=0 ne correspond pas à notre transaction active : le
    # relevé (rejoué depuis un backlog) ne doit PAS lui être rattaché.
    _run(cp.on_meter_values(connector_id=1, meter_value=_energy_frame(132546), transaction_id=0))
    db = SessionLocal()
    rows = db.query(MeterValue).filter(MeterValue.charger_id == "c1").all()
    db.close()
    assert len(rows) == 1
    assert rows[0].transaction_id is None  # stocké, mais détaché de la session


def test_missing_transaction_id_falls_back_to_active_session():
    # Compatibilité : une borne qui n'envoie pas transactionId du tout
    # (certains simulateurs) doit toujours pouvoir alimenter la session active.
    txn_id = _setup_charger_and_active_txn()
    cp = LocalChargePoint("c1", connection=object())
    _run(cp.on_meter_values(connector_id=1, meter_value=_energy_frame(132600), transaction_id=None))
    db = SessionLocal()
    rows = db.query(MeterValue).filter(MeterValue.charger_id == "c1").all()
    db.close()
    assert len(rows) == 1
    assert rows[0].transaction_id == txn_id


def test_replayed_backlog_does_not_corrupt_real_session_energy():
    """Reproduction directe de l'incident : un relevé rejoué (mauvais
    transactionId, valeur proche de meter_start) intercalé avec de vrais
    relevés ne doit pas faire retomber l'énergie de la session à 0."""
    txn_id = _setup_charger_and_active_txn(meter_start=132546)
    cp = LocalChargePoint("c1", connection=object())

    # Vrai relevé n°1 (transactionId correct)
    _run(cp.on_meter_values(connector_id=1, meter_value=_energy_frame(132546), transaction_id=txn_id))
    # Relevé rejoué (backlog, mauvais transactionId) intercalé
    _run(cp.on_meter_values(connector_id=1, meter_value=_energy_frame(132546), transaction_id=0))
    # Vrai relevé n°2 : la charge progresse réellement
    _run(cp.on_meter_values(connector_id=1, meter_value=_energy_frame(132589), transaction_id=txn_id))
    # Un autre relevé rejoué, après le vrai progrès : ne doit pas l'écraser
    _run(cp.on_meter_values(connector_id=1, meter_value=_energy_frame(132546), transaction_id=0))

    db = SessionLocal()
    real_rows = db.query(MeterValue).filter(MeterValue.transaction_id == txn_id).all()
    orphan_rows = db.query(MeterValue).filter(MeterValue.transaction_id.is_(None)).all()
    db.close()
    assert len(real_rows) == 2
    assert len(orphan_rows) == 2
    assert max(r.value for r in real_rows) == 132589  # la vraie progression est intacte
