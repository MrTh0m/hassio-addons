"""
Tests des agrégats mensuels : fiche véhicule, fiche borne, résumé d'accueil.
Mois civils dans le fuseau d'affichage (Europe/Paris par défaut), mois vides
comblés jusqu'au mois courant, km depuis la charge précédente.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import datetime
from zoneinfo import ZoneInfo

from test_api import (  # noqa: F401  (fixtures et harnais partagés)
    client, token, auth, setup_db, TestingSession,
)
from app.models import Charger, ChargerMode, Transaction, Vehicle
from app.api import _monthly_from_rows

PARIS = ZoneInfo("Europe/Paris")


def _row(start, energy_wh=1000.0, cost=0.2, minutes=60.0, km=None, external=False):
    return {"start": start, "energy_wh": energy_wh, "cost": cost,
            "charging_min": minutes, "km": km, "external": external}


def test_empty_rows_give_no_months():
    assert _monthly_from_rows([], PARIS) == []


def test_months_are_filled_and_summed():
    rows = [
        _row(datetime(2026, 1, 10, 12), energy_wh=10000, cost=2.0, minutes=120, km=100),
        _row(datetime(2026, 1, 20, 12), energy_wh=5000, cost=1.0, minutes=60, km=50),
        _row(datetime(2026, 3, 5, 12), energy_wh=8000, cost=1.6, minutes=90),
    ]
    months = _monthly_from_rows(rows, PARIS)
    keys = [m["month"] for m in months]
    assert keys[:3] == ["2026-01", "2026-02", "2026-03"]
    jan, feb, mar = months[0], months[1], months[2]
    assert jan["charge_count"] == 2 and jan["energy_kwh"] == 15.0 and jan["cost"] == 3.0
    assert jan["charging_min"] == 180.0 and jan["km"] == 150.0
    assert jan["kwh_per_100km"] == 10.0  # 15 kWh pour 150 km
    assert feb["charge_count"] == 0 and feb["energy_kwh"] == 0 and feb["kwh_per_100km"] is None
    assert mar["km"] == 0 and mar["kwh_per_100km"] is None
    # la liste s'étend jusqu'au mois courant
    now = datetime.utcnow()
    assert keys[-1] == f"{now.year:04d}-{now.month:02d}"


def test_month_boundary_uses_display_timezone():
    # 31/01 23:30 UTC = 01/02 00:30 à Paris : appartient à février.
    months = _monthly_from_rows([_row(datetime(2026, 1, 31, 23, 30))], PARIS)
    assert months[0]["month"] == "2026-02"
    months_utc = _monthly_from_rows([_row(datetime(2026, 1, 31, 23, 30))], ZoneInfo("UTC"))
    assert months_utc[0]["month"] == "2026-01"


def test_external_count_and_unknown_values():
    rows = [_row(datetime(2026, 5, 2, 10), energy_wh=None, cost=None, minutes=None, external=True)]
    m = _monthly_from_rows(rows, PARIS)[0]
    assert m["charge_count"] == 1 and m["external_count"] == 1
    assert m["energy_kwh"] == 0 and m["cost"] == 0 and m["charging_min"] == 0


def _seed(db):
    db.add(Charger(id="c1", mode=ChargerMode.local, display_name="Maison"))
    v = Vehicle(name="Zoe")
    db.add(v)
    db.flush()
    def tx(start, energy, cost, odo, secs=3600.0):
        db.add(Transaction(
            charger_id="c1", connector_id=1, vehicle_id=v.id, status="completed",
            start_time=start, stop_time=start.replace(hour=start.hour + 1),
            energy_wh=energy, cost=cost, odometer_km=odo, charging_seconds=secs,
            meter_start=0.0, meter_stop=energy,
        ))
    tx(datetime(2026, 1, 10, 8), 10000.0, 2.0, 1000.0)
    tx(datetime(2026, 1, 25, 8), 6000.0, 1.2, 1100.0, secs=7200.0)
    tx(datetime(2026, 2, 12, 8), 9000.0, 1.8, 1250.0)
    db.commit()
    return v.id


def test_vehicle_stats_contains_monthly(client, auth):
    db = TestingSession()
    vid = _seed(db)
    db.close()
    data = client.get(f"/api/vehicles/{vid}/stats", headers=auth).json()
    by = {m["month"]: m for m in data["monthly"]}
    assert by["2026-01"]["charge_count"] == 2
    assert by["2026-01"]["energy_kwh"] == 16.0
    assert by["2026-01"]["km"] == 100.0       # 1100 - 1000 ; la 1re charge n'a pas de précédente
    assert by["2026-01"]["charging_min"] == 180.0
    assert by["2026-02"]["km"] == 150.0       # 1250 - 1100
    assert by["2026-02"]["cost"] == 1.8


def test_charger_stats_contains_monthly(client, auth):
    db = TestingSession()
    _seed(db)
    db.close()
    data = client.get("/api/chargers/c1/stats", headers=auth).json()
    assert [m["month"] for m in data["monthly"]][:2] == ["2026-01", "2026-02"]
    assert sum(m["charge_count"] for m in data["monthly"]) == 3


def test_summary_endpoint(client, auth):
    db = TestingSession()
    _seed(db)
    db.close()
    r = client.get("/api/summary", headers=auth)
    assert r.status_code == 200
    by = {m["month"]: m for m in r.json()["monthly"]}
    assert by["2026-01"]["energy_kwh"] == 16.0 and by["2026-01"]["km"] == 100.0
    assert by["2026-02"]["km"] == 150.0


def test_summary_requires_auth(client):
    assert client.get("/api/summary").status_code == 401


def test_summary_empty_database(client, auth):
    assert client.get("/api/summary", headers=auth).json()["monthly"] == []
