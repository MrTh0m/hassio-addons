"""
Tests de l'endpoint POST /api/server/restart (redémarrage de l'add-on via
l'API Supervisor de Home Assistant). L'appel réel au Supervisor est remplacé
par un faux pour ne rien redémarrer pendant les tests.
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from datetime import datetime

# Réutilise la base de test et les fixtures de test_api.py
from test_api import setup_db, client, token, auth, TestingSession  # noqa: F401
from app.models import Charger, ChargerMode, Transaction
import app.api as api_module


def _fake_restart(monkeypatch):
    calls = []

    async def fake(tok, delay=1.5):
        calls.append(tok)

    monkeypatch.setattr(api_module, "_supervisor_restart_later", fake)
    return calls


def test_restart_requires_admin(client):
    r = client.post("/api/server/restart", json={})
    assert r.status_code == 401


def test_restart_unavailable_without_supervisor(client, auth, monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    calls = _fake_restart(monkeypatch)
    r = client.post("/api/server/restart", json={}, headers=auth)
    assert r.status_code == 503
    assert calls == []


def test_restart_ok_without_active_charge(client, auth, monkeypatch):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "sv-token")
    calls = _fake_restart(monkeypatch)
    r = client.post("/api/server/restart", json={}, headers=auth)
    assert r.status_code == 200
    assert r.json() == {"status": "restarting", "active_transactions": 0}
    assert calls == ["sv-token"]


def test_restart_asks_confirmation_when_charging(client, auth, monkeypatch):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "sv-token")
    calls = _fake_restart(monkeypatch)
    db = TestingSession()
    db.add(Charger(id="cp-restart", mode=ChargerMode.local))
    db.add(Transaction(charger_id="cp-restart", connector_id=1, id_tag="t",
                       meter_start=0, start_time=datetime.utcnow(), status="active"))
    db.commit(); db.close()

    r = client.post("/api/server/restart", json={}, headers=auth)
    assert r.status_code == 200
    assert r.json() == {"status": "confirm_required", "active_transactions": 1}
    assert calls == []

    r = client.post("/api/server/restart", json={"force": True}, headers=auth)
    assert r.json() == {"status": "restarting", "active_transactions": 1}
    assert calls == ["sv-token"]
