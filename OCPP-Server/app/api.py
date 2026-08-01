from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .db import get_db
from .models import Charger, ChargerMode, Transaction, MeterValue, User, ConnectorStatus
from .auth import verify_password, create_access_token, get_current_user, require_admin
from .csms_local import CONNECTED_CHARGERS

router = APIRouter(prefix="/api")


# --- Auth ---

@router.post("/auth/login")
def login(form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == form.username).first()
    if not user or not verify_password(form.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Identifiants invalides")
    token = create_access_token(user.username, user.role.value)
    return {"access_token": token, "token_type": "bearer"}


# --- Chargers ---

class ChargerModeUpdate(BaseModel):
    mode: ChargerMode
    relay_url: Optional[str] = None


@router.get("/chargers")
def list_chargers(db: Session = Depends(get_db), user=Depends(get_current_user)):
    chargers = db.query(Charger).all()
    return [
        {
            "id": c.id, "vendor": c.vendor, "model": c.model,
            "ocpp_version": c.ocpp_version, "mode": c.mode.value,
            "status": c.status, "connected": c.id in CONNECTED_CHARGERS,
        }
        for c in chargers
    ]


@router.get("/chargers/{charger_id}")
def get_charger(charger_id: str, db: Session = Depends(get_db), user=Depends(get_current_user)):
    charger = db.query(Charger).filter(Charger.id == charger_id).first()
    if not charger:
        raise HTTPException(status_code=404, detail="Borne inconnue")
    return {
        "id": charger.id, "vendor": charger.vendor, "model": charger.model,
        "serial": charger.serial, "ocpp_version": charger.ocpp_version,
        "mode": charger.mode.value, "relay_url": charger.relay_url,
        "status": charger.status, "connected": charger.id in CONNECTED_CHARGERS,
    }


@router.put("/chargers/{charger_id}/mode")
def set_charger_mode(
    charger_id: str, update: ChargerModeUpdate,
    db: Session = Depends(get_db), user=Depends(require_admin),
):
    charger = db.query(Charger).filter(Charger.id == charger_id).first()
    if not charger:
        charger = Charger(id=charger_id)
        db.add(charger)
    if update.mode == ChargerMode.relay and not update.relay_url:
        raise HTTPException(status_code=400, detail="relay_url requis en mode relais")
    charger.mode = update.mode
    charger.relay_url = update.relay_url if update.mode == ChargerMode.relay else None
    db.commit()
    return {"status": "ok"}


def _require_local_and_connected(charger_id: str, db: Session):
    charger = db.query(Charger).filter(Charger.id == charger_id).first()
    if not charger:
        raise HTTPException(status_code=404, detail="Borne inconnue")
    if charger.mode != ChargerMode.local:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Action indisponible : cette borne est en mode relais, "
                   "seul le serveur officiel peut la piloter.",
        )
    cp = CONNECTED_CHARGERS.get(charger_id)
    if not cp:
        raise HTTPException(status_code=503, detail="Borne non connectée")
    return cp


# --- Contrôle (mode local uniquement) ---

class StartChargeRequest(BaseModel):
    id_tag: str


class StopChargeRequest(BaseModel):
    transaction_id: int


@router.post("/chargers/{charger_id}/connectors/{connector_id}/start")
async def start_charge(
    charger_id: str, connector_id: int, body: StartChargeRequest,
    db: Session = Depends(get_db), user=Depends(get_current_user),
):
    cp = _require_local_and_connected(charger_id, db)
    result = await cp.trigger_remote_start(connector_id, body.id_tag)
    return {"status": result.status}


@router.post("/chargers/{charger_id}/connectors/{connector_id}/stop")
async def stop_charge(
    charger_id: str, connector_id: int, body: StopChargeRequest,
    db: Session = Depends(get_db), user=Depends(get_current_user),
):
    cp = _require_local_and_connected(charger_id, db)
    result = await cp.trigger_remote_stop(body.transaction_id)
    return {"status": result.status}


# --- Configuration (mode local uniquement) ---

class ConfigValueUpdate(BaseModel):
    value: str


@router.get("/chargers/{charger_id}/config")
async def get_configuration(charger_id: str, db: Session = Depends(get_db), user=Depends(get_current_user)):
    charger = db.query(Charger).filter(Charger.id == charger_id).first()
    if not charger:
        raise HTTPException(status_code=404, detail="Borne inconnue")
    if charger.mode != ChargerMode.local:
        raise HTTPException(status_code=409, detail="Indisponible en mode relais")
    cp = CONNECTED_CHARGERS.get(charger_id)
    if cp:
        keys = await cp.fetch_configuration()
        return keys
    return [
        {"key": k.key, "value": k.value, "readonly": k.readonly}
        for k in charger.config_keys
    ]


@router.put("/chargers/{charger_id}/config/{key}")
async def set_configuration(
    charger_id: str, key: str, body: ConfigValueUpdate,
    db: Session = Depends(get_db), user=Depends(require_admin),
):
    cp = _require_local_and_connected(charger_id, db)
    status_result = await cp.push_configuration(key, body.value)
    return {"status": status_result}


@router.get("/chargers/{charger_id}/connectors")
def list_connector_statuses(charger_id: str, db: Session = Depends(get_db), user=Depends(get_current_user)):
    entries = db.query(ConnectorStatus).filter(ConnectorStatus.charger_id == charger_id).order_by(
        ConnectorStatus.connector_id
    ).all()
    return [
        {
            "connector_id": e.connector_id, "status": e.status,
            "error_code": e.error_code,
            "updated_at": e.updated_at.isoformat() if e.updated_at else None,
        }
        for e in entries
    ]


# --- Métriques et historique (disponibles quel que soit le mode) ---

@router.get("/chargers/{charger_id}/sessions")
def list_sessions(
    charger_id: str, connector_id: Optional[int] = None,
    db: Session = Depends(get_db), user=Depends(get_current_user),
):
    query = db.query(Transaction).filter(Transaction.charger_id == charger_id)
    if connector_id is not None:
        query = query.filter(Transaction.connector_id == connector_id)
    sessions = query.order_by(Transaction.start_time.desc()).all()
    return [
        {
            "id": s.id, "connector_id": s.connector_id, "id_tag": s.id_tag,
            "meter_start": s.meter_start, "meter_stop": s.meter_stop,
            "start_time": s.start_time.isoformat() if s.start_time else None,
            "stop_time": s.stop_time.isoformat() if s.stop_time else None,
            "status": s.status,
        }
        for s in sessions
    ]


@router.get("/chargers/{charger_id}/metervalues")
def list_meter_values(
    charger_id: str, limit: int = 500, connector_id: Optional[int] = None,
    db: Session = Depends(get_db), user=Depends(get_current_user),
):
    query = db.query(MeterValue).filter(MeterValue.charger_id == charger_id)
    if connector_id is not None:
        query = query.filter(MeterValue.connector_id == connector_id)
    values = query.order_by(MeterValue.timestamp.desc()).limit(limit).all()
    return [
        {
            "timestamp": v.timestamp.isoformat(), "measurand": v.measurand,
            "value": v.value, "unit": v.unit, "connector_id": v.connector_id,
        }
        for v in values
    ]
