"""
Tests de la suppression DÉFINITIVE d'un véhicule (DELETE /api/vehicles/{id}/permanent) :
- une session faite sur une borne locale encore présente dans l'app est conservée
  (vehicle_id détaché, nom du véhicule gardé en snapshot) ;
- une charge externe, ou une session dont la borne a déjà été supprimée
  définitivement, est réellement effacée avec le véhicule.
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from datetime import datetime

from test_api import setup_db, client, token, auth, TestingSession  # noqa: F401
from app.models import Charger, ChargerMode, Transaction, Vehicle, MeterValue


def _make_vehicle(db, name="Panda"):
    v = Vehicle(name=name)
    db.add(v)
    db.commit()
    db.refresh(v)
    return v.id


def test_session_on_existing_charger_is_kept(client, auth):
    db = TestingSession()
    db.add(Charger(id="cp-kept", mode=ChargerMode.local))
    db.commit()
    vid = _make_vehicle(db, "Panda")
    txn = Transaction(charger_id="cp-kept", connector_id=1, vehicle_id=vid,
                       meter_start=0, start_time=datetime.utcnow(), status="completed")
    db.add(txn)
    db.commit()
    txn_id = txn.id
    db.close()

    r = client.delete(f"/api/vehicles/{vid}/permanent", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["kept_sessions"] == 1
    assert body["deleted_sessions"] == 0

    db = TestingSession()
    kept = db.query(Transaction).filter(Transaction.id == txn_id).first()
    assert kept is not None  # pas supprimée
    assert kept.charger_id == "cp-kept"  # la borne garde son historique
    assert kept.vehicle_id is None  # détachée du véhicule supprimé
    assert kept.vehicle_display_name_snapshot == "Panda"
    db.close()

    # Le véhicule lui-même a bien disparu
    r = client.get("/api/vehicles", headers=auth)
    assert all(v["id"] != vid for v in r.json())


def test_external_charge_is_deleted_with_vehicle(client, auth):
    db = TestingSession()
    vid = _make_vehicle(db, "Zoe")
    txn = Transaction(charger_id=None, is_external=True, vehicle_id=vid,
                       meter_start=0, start_time=datetime.utcnow(), status="completed")
    db.add(txn)
    db.commit()
    txn_id = txn.id
    db.close()

    r = client.delete(f"/api/vehicles/{vid}/permanent", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["kept_sessions"] == 0
    assert body["deleted_sessions"] == 1

    db = TestingSession()
    assert db.query(Transaction).filter(Transaction.id == txn_id).first() is None
    db.close()


def test_session_on_already_hard_deleted_charger_is_deleted_with_vehicle(client, auth):
    """Une borne déjà supprimée définitivement (charger_id NULL, nom en
    snapshot) n'est plus « présente dans l'app » : l'exception ne joue pas,
    la session part avec le véhicule."""
    db = TestingSession()
    vid = _make_vehicle(db, "508")
    txn = Transaction(charger_id=None, charger_display_name_snapshot="Ancienne borne",
                       vehicle_id=vid, meter_start=0, start_time=datetime.utcnow(),
                       status="completed")
    db.add(txn)
    db.commit()
    txn_id = txn.id
    db.close()

    r = client.delete(f"/api/vehicles/{vid}/permanent", headers=auth)
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "deleted_sessions": 1, "kept_sessions": 0}

    db = TestingSession()
    assert db.query(Transaction).filter(Transaction.id == txn_id).first() is None
    db.close()


def test_meter_values_deleted_only_for_removed_sessions(client, auth):
    db = TestingSession()
    db.add(Charger(id="cp-mv", mode=ChargerMode.local))
    db.commit()
    vid = _make_vehicle(db, "C3")
    kept_txn = Transaction(charger_id="cp-mv", connector_id=1, vehicle_id=vid,
                            meter_start=0, start_time=datetime.utcnow(), status="completed")
    ext_txn = Transaction(charger_id=None, is_external=True, vehicle_id=vid,
                           meter_start=0, start_time=datetime.utcnow(), status="completed")
    db.add_all([kept_txn, ext_txn])
    db.commit()
    kept_id, ext_id = kept_txn.id, ext_txn.id
    db.add(MeterValue(charger_id="cp-mv", transaction_id=kept_id, measurand="Energy.Active.Import.Register", value="1"))
    db.add(MeterValue(charger_id="cp-mv", transaction_id=ext_id, measurand="Energy.Active.Import.Register", value="1"))
    db.commit()
    db.close()

    r = client.delete(f"/api/vehicles/{vid}/permanent", headers=auth)
    assert r.status_code == 200

    db = TestingSession()
    assert db.query(MeterValue).filter(MeterValue.transaction_id == kept_id).count() == 1  # conservé
    assert db.query(MeterValue).filter(MeterValue.transaction_id == ext_id).count() == 0  # effacé
    db.close()


def test_charger_history_shows_deleted_vehicle_name(client, auth):
    """La fiche borne continue d'afficher le nom du véhicule supprimé, marqué
    comme tel, via _serialize_session."""
    db = TestingSession()
    db.add(Charger(id="cp-hist", mode=ChargerMode.local))
    db.commit()
    vid = _make_vehicle(db, "Twingo")
    db.add(Transaction(charger_id="cp-hist", connector_id=1, vehicle_id=vid,
                        meter_start=0, meter_stop=1000, start_time=datetime.utcnow(),
                        stop_time=datetime.utcnow(), status="completed"))
    db.commit()
    db.close()

    client.delete(f"/api/vehicles/{vid}/permanent", headers=auth)

    r = client.get("/api/chargers/cp-hist/stats", headers=auth)
    assert r.status_code == 200
    sessions = r.json()["sessions"]
    assert len(sessions) == 1
    assert sessions[0]["vehicle_name"] == "Twingo"
    assert sessions[0]["vehicle_deleted"] is True
