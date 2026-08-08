"""
Tests d'intégration pour app/api.py.
Utilise une base SQLite en mémoire et le client de test FastAPI.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from datetime import datetime
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ["OCPP_DATA_DIR"] = "/tmp/test_ocpp"
os.environ["OCPP_ADMIN_PASSWORD"] = "testpass"
os.environ.setdefault("OCPP_SECRET_KEY", "test-secret")

from app.models import Base, Charger, ChargerMode, Transaction, TariffPlan, TariffPeriod, Vehicle
import app.db as _app_db
from app.main import app

# Base SQLite en mémoire partagée entre les tests et l'app
TEST_ENGINE = create_engine(
    "sqlite:///file:testdb?mode=memory&cache=shared&uri=true",
    connect_args={"check_same_thread": False}
)
TestingSession = sessionmaker(bind=TEST_ENGINE, autoflush=False, autocommit=False)

# Patcher l'engine et la session de l'app
_app_db.engine = TEST_ENGINE
_app_db.SessionLocal = TestingSession

def override_get_db():
    db = TestingSession()
    try:
        yield db
    finally:
        db.close()

from app.db import get_db
app.dependency_overrides[get_db] = override_get_db


@pytest.fixture(autouse=True)
def setup_db():
    Base.metadata.create_all(TEST_ENGINE)
    from app.auth import hash_password
    from app.models import User, UserRole, UserPermission
    db = TestingSession()
    admin = User(username="admin", password_hash=hash_password("testpass"), role=UserRole.admin)
    db.add(admin)
    db.flush()
    db.add(UserPermission(user_id=admin.id))
    db.commit()
    db.close()
    yield
    Base.metadata.drop_all(TEST_ENGINE)

@pytest.fixture
def client():
    return TestClient(app)

@pytest.fixture
def token(client):
    r = client.post("/api/auth/login", data={"username": "admin", "password": "testpass"})
    assert r.status_code == 200
    return r.json()["access_token"]

@pytest.fixture
def auth(token):
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def test_login_ok(client):
    r = client.post("/api/auth/login", data={"username": "admin", "password": "testpass"})
    assert r.status_code == 200
    assert "access_token" in r.json()

def test_login_bad_password(client):
    r = client.post("/api/auth/login", data={"username": "admin", "password": "wrong"})
    assert r.status_code == 401

def test_protected_endpoint_no_token(client):
    r = client.get("/api/chargers")
    assert r.status_code == 401

def test_me(client, auth):
    r = client.get("/api/auth/me", headers=auth)
    assert r.status_code == 200
    assert r.json()["role"] == "admin"


# ---------------------------------------------------------------------------
# Chargers
# ---------------------------------------------------------------------------

def test_list_chargers_empty(client, auth):
    r = client.get("/api/chargers", headers=auth)
    assert r.status_code == 200
    assert r.json() == []

def test_charger_display_name(client, auth):
    db = TestingSession()
    db.add(Charger(id="test-id", mode=ChargerMode.local))
    db.commit()
    db.close()

    r = client.put("/api/chargers/test-id/display-name",
                   json={"display_name": "Maison"}, headers=auth)
    assert r.status_code == 200

    r = client.get("/api/chargers/test-id", headers=auth)
    assert r.json()["display_name"] == "Maison"

def test_charger_display_name_clear(client, auth):
    db = TestingSession()
    db.add(Charger(id="test-id", mode=ChargerMode.local, display_name="Maison"))
    db.commit()
    db.close()

    r = client.put("/api/chargers/test-id/display-name",
                   json={"display_name": ""}, headers=auth)
    assert r.status_code == 200
    r = client.get("/api/chargers/test-id", headers=auth)
    assert r.json()["display_name"] is None

def test_delete_charger(client, auth):
    db = TestingSession()
    db.add(Charger(id="del-id", mode=ChargerMode.local))
    db.commit()
    db.close()

    r = client.delete("/api/chargers/del-id", headers=auth)
    assert r.status_code == 200
    r = client.get("/api/chargers", headers=auth)
    assert all(c["id"] != "del-id" for c in r.json())


# ---------------------------------------------------------------------------
# Sessions / recalcul
# ---------------------------------------------------------------------------

def _make_completed_session(db, meter_start, meter_stop, energy_wh=None):
    charger = Charger(id="c1", mode=ChargerMode.local)
    db.add(charger)
    txn = Transaction(
        charger_id="c1", connector_id=1,
        meter_start=meter_start, meter_stop=meter_stop,
        start_time=datetime(2026, 8, 6, 17, 0),
        stop_time=datetime(2026, 8, 6, 17, 30),
        status="completed",
        energy_wh=energy_wh,
    )
    db.add(txn)
    db.commit()
    db.refresh(txn)
    return txn.id

def test_recalculate_session_corrige_energie(client, auth):
    """Recalcul depuis meter_start/stop corrige une energy_wh corrompue."""
    db = TestingSession()
    txn_id = _make_completed_session(db, meter_start=95972, meter_stop=98075, energy_wh=98075.0)
    db.close()

    r = client.post(f"/api/sessions/{txn_id}/recalculate", headers=auth)
    assert r.status_code == 200
    data = r.json()
    assert data["energy_wh"] == pytest.approx(2103.0)

def test_recalculate_session_active_refuse(client, auth):
    """Recalcul refusé sur une session active."""
    db = TestingSession()
    charger = Charger(id="c2", mode=ChargerMode.local)
    db.add(charger)
    txn = Transaction(
        charger_id="c2", connector_id=1,
        meter_start=98075, meter_stop=None,
        start_time=datetime(2026, 8, 7, 10, 0),
        status="active",
    )
    db.add(txn)
    db.commit()
    txn_id = txn.id
    db.close()

    r = client.post(f"/api/sessions/{txn_id}/recalculate", headers=auth)
    assert r.status_code == 400

def test_recalculate_sans_meter_stop_refuse(client, auth):
    """Recalcul refusé si meter_stop absent."""
    db = TestingSession()
    charger = Charger(id="c3", mode=ChargerMode.local)
    db.add(charger)
    txn = Transaction(
        charger_id="c3", connector_id=1,
        meter_start=98075, meter_stop=None,
        start_time=datetime(2026, 8, 7, 10, 0),
        stop_time=datetime(2026, 8, 7, 11, 0),
        status="completed",
    )
    db.add(txn)
    db.commit()
    txn_id = txn.id
    db.close()

    r = client.post(f"/api/sessions/{txn_id}/recalculate", headers=auth)
    assert r.status_code == 400

def test_recalculate_avec_tarif(client, auth):
    """Recalcul inclut le coût si un tarif est associé."""
    db = TestingSession()
    plan = TariffPlan(name="Test", is_default=True, fixed_price=0.20)
    db.add(plan)
    db.flush()
    period = TariffPeriod(
        tariff_plan_id=plan.id, name="Plein", price=0.20,
        days_of_week="0,1,2,3,4,5,6", start_time="00:00", end_time="23:59"
    )
    db.add(period)
    txn_id = _make_completed_session(db, meter_start=0, meter_stop=10000, energy_wh=98075.0)
    db.close()

    r = client.post(f"/api/sessions/{txn_id}/recalculate", headers=auth)
    assert r.status_code == 200
    data = r.json()
    assert data["energy_wh"] == pytest.approx(10000.0)
    assert data["cost"] == pytest.approx(10000 / 1000 * 0.20, rel=1e-3)


# ---------------------------------------------------------------------------
# Véhicules
# ---------------------------------------------------------------------------

def test_create_vehicle(client, auth):
    r = client.post("/api/vehicles", json={"name": "Panda", "battery_capacity_kwh": 44.0}, headers=auth)
    assert r.status_code == 200
    vid = r.json()["id"]

    r = client.get("/api/vehicles", headers=auth)
    vehicles = r.json()
    assert any(v["id"] == vid and v["name"] == "Panda" for v in vehicles)

def test_vehicle_idtag_unique(client, auth):
    client.post("/api/vehicles", json={"name": "V1", "id_tag": "TAG1"}, headers=auth)
    r = client.post("/api/vehicles", json={"name": "V2", "id_tag": "TAG1"}, headers=auth)
    assert r.status_code == 400

def test_delete_vehicle(client, auth):
    r = client.post("/api/vehicles", json={"name": "ToDelete"}, headers=auth)
    vid = r.json()["id"]
    r = client.delete(f"/api/vehicles/{vid}", headers=auth)
    assert r.status_code == 200
    r = client.get("/api/vehicles", headers=auth)
    assert all(v["id"] != vid for v in r.json())


# ---------------------------------------------------------------------------
# Tarifs
# ---------------------------------------------------------------------------

def test_create_tariff(client, auth):
    r = client.post("/api/tariffs", json={"name": "HP/HC", "is_default": True}, headers=auth)
    assert r.status_code == 200
    plan_id = r.json()["id"]

    r = client.post(f"/api/tariffs/{plan_id}/periods", json={
        "name": "HC", "price": 0.15,
        "days_of_week": "0,1,2,3,4,5,6",
        "start_time": "22:00", "end_time": "06:00"
    }, headers=auth)
    assert r.status_code == 200

    r = client.get("/api/tariffs", headers=auth)
    plans = r.json()
    plan = next(p for p in plans if p["id"] == plan_id)
    assert plan["is_default"] is True
    assert len(plan["periods"]) == 1
    assert plan["periods"][0]["price"] == 0.15

def test_only_one_default_tariff(client, auth):
    """Définir un nouveau tarif par défaut retire l'ancien."""
    r1 = client.post("/api/tariffs", json={"name": "A", "is_default": True}, headers=auth)
    r2 = client.post("/api/tariffs", json={"name": "B", "is_default": True}, headers=auth)
    id1, id2 = r1.json()["id"], r2.json()["id"]

    r = client.get("/api/tariffs", headers=auth)
    plans = {p["id"]: p for p in r.json()}
    assert plans[id1]["is_default"] is False
    assert plans[id2]["is_default"] is True


# ---------------------------------------------------------------------------
# Historique
# ---------------------------------------------------------------------------

def test_history_empty(client, auth):
    r = client.get("/api/history", headers=auth)
    assert r.status_code == 200
    assert r.json() == []

def test_history_contient_charger_display_name(client, auth):
    """La sérialisation des sessions inclut charger_display_name."""
    db = TestingSession()
    charger = Charger(id="named-id", mode=ChargerMode.local, display_name="Maison")
    db.add(charger)
    txn = Transaction(
        charger_id="named-id", connector_id=1,
        meter_start=0, meter_stop=5000,
        start_time=datetime(2026, 8, 7, 10, 0),
        stop_time=datetime(2026, 8, 7, 11, 0),
        status="completed",
    )
    db.add(txn)
    db.commit()
    db.close()

    r = client.get("/api/history", headers=auth)
    assert r.status_code == 200
    sessions = r.json()
    assert len(sessions) == 1
    assert sessions[0]["charger_display_name"] == "Maison"

def test_session_active_ignore_meter_stop(client, auth):
    """Session active : energy_wh calculé sans meter_stop parasite."""
    db = TestingSession()
    charger = Charger(id="c-active", mode=ChargerMode.local)
    db.add(charger)
    txn = Transaction(
        charger_id="c-active", connector_id=1,
        meter_start=98075, meter_stop=196155,  # meter_stop parasite
        start_time=datetime(2026, 8, 7, 10, 0),
        status="active",
    )
    db.add(txn)
    db.commit()
    db.close()

    r = client.get("/api/chargers/c-active/sessions", headers=auth)
    assert r.status_code == 200
    sessions = r.json()
    assert len(sessions) == 1
    # Sans MeterValues et avec ignore_meter_stop → 0.0
    assert sessions[0]["energy_wh"] == 0.0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
