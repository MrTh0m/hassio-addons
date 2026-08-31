"""
Tests unitaires pour le suivi des clés de configuration en attente de
redémarrage (PENDING_REBOOT_KEYS).

Contexte : un ChangeConfiguration OCPP peut répondre RebootRequired plutôt
qu'Accepted (ex. Cst_BackendUrl sur le simulateur MicroOcpp) : la valeur est
bien stockée côté borne mais son comportement actif (ex. la connexion
WebSocket vers l'ancienne URL) n'est appliqué qu'après un redémarrage. Ces
tests vérifient que ce cas est distingué d'Accepted/Rejected/NotSupported et
que le drapeau est bien levé au redémarrage suivant (BootNotification).

N'utilise pas de vraie connexion WebSocket : ChargePoint.call est remplacé
par une réponse pré-fabriquée, comme le fait déjà le reste de la suite pour
isoler la logique métier du transport.
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

from ocpp.v16 import call_result
from ocpp.v16.enums import ConfigurationStatus

import app.db as _app_db
import app.csms_local as _csms_local
from app.models import Base, ConfigurationKey
from app.csms_local import LocalChargePoint, PENDING_REBOOT_KEYS

# Moteur dédié à ce fichier, indépendant de celui que test_api.py installe le
# temps de sa propre session. csms_local.py fait `from .db import
# SessionLocal` à l'import : cette liaison est capturée une fois pour toutes
# et ne suit PAS un `app.db.SessionLocal = ...` fait après coup par un autre
# fichier de test. On patche donc explicitement `app.csms_local.SessionLocal`
# (en plus de `app.db`), et on restaure les deux après coup, pour être
# robuste à l'ordre d'exécution des fichiers de test.
TEST_ENGINE = create_engine(
    "sqlite:///file:rebootpending?mode=memory&cache=shared&uri=true",
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
    PENDING_REBOOT_KEYS.clear()
    yield
    PENDING_REBOOT_KEYS.clear()
    Base.metadata.drop_all(TEST_ENGINE)
    _app_db.engine, _app_db.SessionLocal = orig_db_engine, orig_db_session
    _csms_local.SessionLocal = orig_csms_session


def SessionLocal():
    return TestingSession()


def _make_cp(charger_id="test-charger-reboot"):
    return LocalChargePoint(charger_id, connection=object())


def _stub_call(cp, response):
    """Remplace ChargePoint.call par une réponse pré-fabriquée, sans passer
    par une vraie connexion WebSocket."""
    async def _fake_call(payload, **kwargs):
        return response
    cp.call = _fake_call


def _run(coro):
    return asyncio.run(coro)


def test_reboot_required_marks_key_pending():
    cp = _make_cp()
    _stub_call(cp, call_result.ChangeConfiguration(status=ConfigurationStatus.reboot_required))
    status = _run(cp.push_configuration("Cst_BackendUrl", "ws://192.168.0.41:8000/ocpp"))
    assert status == ConfigurationStatus.reboot_required
    assert "Cst_BackendUrl" in PENDING_REBOOT_KEYS.get(cp.id, set())


def test_accepted_clears_pending_flag():
    cp = _make_cp()
    _stub_call(cp, call_result.ChangeConfiguration(status=ConfigurationStatus.reboot_required))
    _run(cp.push_configuration("Cst_BackendUrl", "ws://192.168.0.41:8000/ocpp"))
    assert "Cst_BackendUrl" in PENDING_REBOOT_KEYS.get(cp.id, set())

    _stub_call(cp, call_result.ChangeConfiguration(status=ConfigurationStatus.accepted))
    status = _run(cp.push_configuration("Cst_BackendUrl", "ws://192.168.0.42:8000/ocpp"))
    assert status == ConfigurationStatus.accepted
    assert "Cst_BackendUrl" not in PENDING_REBOOT_KEYS.get(cp.id, set())


def test_rejected_does_not_mark_pending_and_does_not_update_cache():
    cp = _make_cp()
    db = SessionLocal()
    db.add(ConfigurationKey(charger_id=cp.id, key="HeartbeatInterval", value="300", readonly=False))
    db.commit()
    db.close()

    _stub_call(cp, call_result.ChangeConfiguration(status=ConfigurationStatus.rejected))
    status = _run(cp.push_configuration("HeartbeatInterval", "60"))
    assert status == ConfigurationStatus.rejected
    assert "HeartbeatInterval" not in PENDING_REBOOT_KEYS.get(cp.id, set())

    db = SessionLocal()
    entry = db.query(ConfigurationKey).filter_by(charger_id=cp.id, key="HeartbeatInterval").first()
    assert entry.value == "300"  # inchangé : la borne a refusé
    db.close()


def test_reboot_required_updates_local_cache_too():
    """RebootRequired doit quand même mettre à jour le cache local : la borne a
    bien stocké la valeur (confirmé par son propre GetConfiguration côté
    MicroOcpp), seul le comportement actif attend le redémarrage."""
    cp = _make_cp()
    db = SessionLocal()
    db.add(ConfigurationKey(charger_id=cp.id, key="Cst_BackendUrl", value="ws://old", readonly=False))
    db.commit()
    db.close()

    _stub_call(cp, call_result.ChangeConfiguration(status=ConfigurationStatus.reboot_required))
    _run(cp.push_configuration("Cst_BackendUrl", "ws://new"))

    db = SessionLocal()
    entry = db.query(ConfigurationKey).filter_by(charger_id=cp.id, key="Cst_BackendUrl").first()
    assert entry.value == "ws://new"
    db.close()


def test_boot_notification_clears_pending_reboot():
    cp = _make_cp()
    PENDING_REBOOT_KEYS[cp.id] = {"Cst_BackendUrl"}
    _run(cp.on_boot_notification(charge_point_vendor="Vendor", charge_point_model="Model"))
    assert cp.id not in PENDING_REBOOT_KEYS


def test_boot_notification_does_not_affect_other_chargers_pending_keys():
    cp = _make_cp("test-charger-reboot")
    other_id = "test-charger-other"
    PENDING_REBOOT_KEYS[cp.id] = {"Cst_BackendUrl"}
    PENDING_REBOOT_KEYS[other_id] = {"HeartbeatInterval"}
    _run(cp.on_boot_notification(charge_point_vendor="Vendor", charge_point_model="Model"))
    assert cp.id not in PENDING_REBOOT_KEYS
    assert other_id in PENDING_REBOOT_KEYS
