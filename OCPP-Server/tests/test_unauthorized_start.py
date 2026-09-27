"""
Tests pour le suivi des sessions non autorisées et la liste d'autorisation
locale (VehicleCharger / SendLocalList).

Contexte (incident du 26/09, voir CHANGELOG) : après un incident de file OCPP
bloquée, une borne Schneider a démarré une charge locale avec son idTag par
défaut ("freeCharge") après un Blocked du serveur. Avant ce correctif,
on_start_transaction ne créait alors AUCUNE Transaction : la charge tournait
(courant réel compris) sans qu'aucune trace ne le signale nulle part, ni
dans l'historique, ni dans le bandeau de santé. Ces tests verrouillent le
nouveau comportement : la session est désormais toujours créée et suivie
(avec unauthorized=True), et signalée dans le bandeau de santé.

Vérifie aussi _authorized_id_tags_for_charger, qui construit la liste
poussée sur la borne via SendLocalList (voir sync_local_auth_list) à partir
des véhicules associés (VehicleCharger).
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncio
from datetime import datetime
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("OCPP_DATA_DIR", "/tmp/test_ocpp")
os.environ.setdefault("OCPP_ADMIN_PASSWORD", "testpass")
os.environ.setdefault("OCPP_SECRET_KEY", "test-secret")

import app.db as _app_db
import app.csms_local as _csms_local
import app.diagnostics as _diagnostics
from app.models import Base, Charger, ChargerMode, AuthMode, Transaction, Vehicle, VehicleCharger, ConnectorStatus
from app.csms_local import LocalChargePoint, RESERVED_TAGS, _tag_is_authorized, _authorized_id_tags_for_charger

TEST_ENGINE = create_engine(
    "sqlite:///file:unauthorizedstart?mode=memory&cache=shared&uri=true",
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


def SessionLocal():
    return TestingSession()


def _run(coro):
    return asyncio.run(coro)


def _make_charger(charger_id="c1", auth_mode=AuthMode.authorized):
    db = SessionLocal()
    db.add(Charger(id=charger_id, mode=ChargerMode.local, auth_mode=auth_mode))
    db.commit()
    db.close()


def test_unknown_tag_still_creates_and_flags_transaction():
    """L'idTag par défaut de la borne ("freeCharge"), non reconnu, doit
    quand même créer une transaction suivie (unauthorized=True) avec un
    transaction_id réel, jamais 0 : c'est ce qui permet aux MeterValues/
    StopTransaction qui suivront de s'y rattacher au lieu de rester
    orphelins."""
    _make_charger()
    cp = LocalChargePoint("c1", connection=object())
    resp = _run(cp.on_start_transaction(connector_id=1, id_tag="freeCharge", meter_start=1000))

    assert resp.id_tag_info["status"] == "Blocked"
    assert resp.transaction_id != 0

    db = SessionLocal()
    txn = db.query(Transaction).filter(Transaction.id == resp.transaction_id).first()
    db.close()
    assert txn is not None
    assert txn.unauthorized is True
    assert txn.status == "active"
    assert txn.id_tag == "freeCharge"


def test_known_vehicle_tag_creates_authorized_transaction():
    _make_charger()
    db = SessionLocal()
    db.add(Vehicle(name="Zoé", id_tag="ZOE-1"))
    db.commit()
    db.close()

    cp = LocalChargePoint("c1", connection=object())
    resp = _run(cp.on_start_transaction(connector_id=1, id_tag="ZOE-1", meter_start=1000))

    assert resp.id_tag_info["status"] == "Accepted"
    db = SessionLocal()
    txn = db.query(Transaction).filter(Transaction.id == resp.transaction_id).first()
    db.close()
    assert txn.unauthorized is False


def test_free_auth_mode_always_accepts():
    """auth_mode=free (l'autre cas déjà existant) doit rester inchangé :
    tout idTag est accepté, y compris un idTag inconnu."""
    _make_charger(auth_mode=AuthMode.free)
    cp = LocalChargePoint("c1", connection=object())
    resp = _run(cp.on_start_transaction(connector_id=1, id_tag="whatever", meter_start=1000))
    assert resp.id_tag_info["status"] == "Accepted"
    db = SessionLocal()
    txn = db.query(Transaction).filter(Transaction.id == resp.transaction_id).first()
    db.close()
    assert txn.unauthorized is False


def test_unauthorized_session_surfaces_in_health_badge():
    _make_charger()
    cp = LocalChargePoint("c1", connection=object())
    _run(cp.on_start_transaction(connector_id=1, id_tag="freeCharge", meter_start=1000))

    badges = {b["key"]: b for b in _diagnostics.compute_health()}
    badge = badges["sessions_non_autorisees"]
    assert badge["severity"] == "warn"
    assert badge["count"] == 1
    assert badge["items"][0]["id_tag"] == "freeCharge"

    # Le badge générique (connecteur en Charging sans AUCUNE transaction) ne
    # doit pas se déclencher en plus : la transaction existe bel et bien.
    db = SessionLocal()
    db.add(ConnectorStatus(charger_id="c1", connector_id=1, status="Charging"))
    db.commit()
    db.close()
    badges = {b["key"]: b for b in _diagnostics.compute_health()}
    assert badges["charge_active_non_suivie"]["count"] == 0


def test_authorized_id_tags_includes_associated_vehicles_and_reserved_tags():
    _make_charger()
    db = SessionLocal()
    v1 = Vehicle(name="Zoé", id_tag="ZOE-1")
    v2 = Vehicle(name="ID.3", id_tag="ID3-1")
    db.add_all([v1, v2])
    db.flush()
    db.add(VehicleCharger(vehicle_id=v1.id, charger_id="c1"))
    # v2 n'est PAS associée à c1 : ne doit pas apparaître dans sa liste.
    db.commit()

    tags = _authorized_id_tags_for_charger(db, "c1")
    db.close()

    assert "ZOE-1" in tags
    assert "ID3-1" not in tags
    assert RESERVED_TAGS.issubset(set(tags))


def test_deactivated_vehicle_excluded_from_authorized_tags():
    _make_charger()
    db = SessionLocal()
    v = Vehicle(name="Zoé", id_tag="ZOE-1", deleted_at=datetime.utcnow())
    db.add(v)
    db.flush()
    db.add(VehicleCharger(vehicle_id=v.id, charger_id="c1"))
    db.commit()

    tags = _authorized_id_tags_for_charger(db, "c1")
    db.close()
    assert "ZOE-1" not in tags


def test_sync_local_auth_list_noop_when_charger_not_connected():
    """Une borne non connectée (CONNECTED_CHARGERS vide en test) ne doit
    jamais faire planter la synchronisation : elle sera resynchronisée à sa
    prochaine reconnexion (voir on_boot_notification)."""
    _make_charger()
    result = _run(_csms_local.sync_local_auth_list("c1"))
    assert result is None
