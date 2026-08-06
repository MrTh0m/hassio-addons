from typing import Optional
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .db import get_db
from .models import (
    Charger, ChargerMode, AuthMode, Transaction, MeterValue, User, UserRole,
    UserVehicle, UserCharger, UserPermission, ConnectorStatus,
    Vehicle, TariffPlan, TariffPeriod, ChargeCondition, ChargeConditionType, ConfigurationKey,
)
from .pricing import (
    compute_session_cost, resolve_plan_for_charger, freeze_transaction_cost, price_at,
)
from .auth import (
    verify_password, hash_password, create_access_token,
    get_current_user, require_admin, is_admin, get_user_id,
)
from .csms_local import CONNECTED_CHARGERS, PENDING_REMOTE_STARTS, SMART_CHARGING_SUPPORT

router = APIRouter(prefix="/api")


# --- Auth ---

@router.post("/auth/login")
def login(form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = db.query(User).filter(
        User.username == form.username, User.deleted_at.is_(None)
    ).first()
    if not user or not verify_password(form.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Identifiants invalides")
    token = create_access_token(user.id, user.username, user.role.value)
    return {"access_token": token, "token_type": "bearer"}


@router.get("/auth/me")
def get_me(db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Retourne le profil complet du user connecté (rôle + permissions)."""
    uid = get_user_id(user)
    u = db.query(User).filter(User.id == uid).first() if uid else None
    if not u:
        return {"role": user.get("role", "user"), "permissions": {}}
    perms = u.permissions
    vehicle_ids = [lnk.vehicle_id for lnk in u.vehicle_links]
    charger_ids = [lnk.charger_id for lnk in u.charger_links]
    return {
        "id": u.id,
        "username": u.username,
        "role": u.role.value,
        "vehicle_ids": vehicle_ids,
        "charger_ids": charger_ids,
        "permissions": {
            "can_manage_chargers": is_admin(user) or (perms.can_manage_chargers if perms else False),
            "can_manage_tariffs": is_admin(user) or (perms.can_manage_tariffs if perms else False),
            "can_manage_vehicles": is_admin(user) or (perms.can_manage_vehicles if perms else False),
            "can_view_logs": is_admin(user) or (perms.can_view_logs if perms else False),
            "can_export_import": is_admin(user) or (perms.can_export_import if perms else False),
        },
    }


class ChangePasswordBody(BaseModel):
    current_password: str
    new_password: str


@router.post("/auth/change-password")
def change_password(
    body: ChangePasswordBody,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Permet à n'importe quel user de changer son propre mot de passe."""
    uid = get_user_id(user)
    u = db.query(User).filter(User.id == uid).first() if uid else None
    if not u:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable")
    if not verify_password(body.current_password, u.password_hash):
        raise HTTPException(status_code=400, detail="Mot de passe actuel incorrect")
    if len(body.new_password) < 6:
        raise HTTPException(status_code=400, detail="Le nouveau mot de passe doit faire au moins 6 caractères")
    u.password_hash = hash_password(body.new_password)
    db.commit()
    return {"status": "ok"}


# --- Chargers ---

class ChargerModeUpdate(BaseModel):
    mode: ChargerMode
    relay_url: Optional[str] = None


def _user_charger_ids(db: Session, user: dict) -> Optional[list]:
    """Retourne la liste des charger_ids accessibles à l'user, ou None si admin."""
    if is_admin(user):
        return None
    uid = get_user_id(user)
    if uid is None:
        return []
    links = db.query(UserCharger).filter(UserCharger.user_id == uid).all()
    return [lnk.charger_id for lnk in links]


def _user_vehicle_ids(db: Session, user: dict) -> Optional[list]:
    """Retourne la liste des vehicle_ids accessibles à l'user, ou None si admin."""
    if is_admin(user):
        return None
    uid = get_user_id(user)
    if uid is None:
        return []
    links = db.query(UserVehicle).filter(UserVehicle.user_id == uid).all()
    return [lnk.vehicle_id for lnk in links]


@router.get("/chargers")
def list_chargers(db: Session = Depends(get_db), user=Depends(get_current_user)):
    chargers = db.query(Charger).filter(Charger.deleted_at.is_(None)).all()
    allowed_ids = _user_charger_ids(db, user)
    if allowed_ids is not None:
        chargers = [c for c in chargers if c.id in allowed_ids]
    return [
        {
            "id": c.id, "vendor": c.vendor, "model": c.model,
            "ocpp_version": c.ocpp_version, "mode": c.mode.value,
            "auth_mode": c.auth_mode.value if c.auth_mode else "free",
            "status": c.status, "connected": c.id in CONNECTED_CHARGERS,
            "smart_charging": SMART_CHARGING_SUPPORT.get(c.id),
            "relay_url": c.relay_url,
            "tariff_plan_id": c.tariff_plan_id,
        }
        for c in chargers
    ]


@router.delete("/chargers/{charger_id}")
def delete_charger(charger_id: str, db: Session = Depends(get_db), user=Depends(require_admin)):
    charger = db.query(Charger).filter(Charger.id == charger_id).first()
    if not charger:
        raise HTTPException(status_code=404, detail="Borne inconnue")
    charger.deleted_at = datetime.utcnow()
    db.query(ChargeCondition).filter(ChargeCondition.charger_id == charger_id).delete()
    db.commit()
    CONNECTED_CHARGERS.pop(charger_id, None)
    return {"status": "ok"}


@router.get("/chargers/{charger_id}")
def get_charger(charger_id: str, db: Session = Depends(get_db), user=Depends(get_current_user)):
    charger = db.query(Charger).filter(Charger.id == charger_id).first()
    if not charger:
        raise HTTPException(status_code=404, detail="Borne inconnue")
    return {
        "id": charger.id, "vendor": charger.vendor, "model": charger.model,
        "serial": charger.serial, "ocpp_version": charger.ocpp_version,
        "mode": charger.mode.value, "relay_url": charger.relay_url,
        "auth_mode": charger.auth_mode.value if charger.auth_mode else "free",
        "status": charger.status, "connected": charger.id in CONNECTED_CHARGERS,
        "tariff_plan_id": charger.tariff_plan_id,
        "smart_charging": SMART_CHARGING_SUPPORT.get(charger.id),
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


class AuthModeUpdate(BaseModel):
    auth_mode: AuthMode


@router.put("/chargers/{charger_id}/auth-mode")
def set_charger_auth_mode(
    charger_id: str, update: AuthModeUpdate,
    db: Session = Depends(get_db), user=Depends(require_admin),
):
    charger = db.query(Charger).filter(Charger.id == charger_id).first()
    if not charger:
        raise HTTPException(status_code=404, detail="Borne inconnue")
    charger.auth_mode = update.auth_mode
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
    vehicle_id: Optional[int] = None
    id_tag: Optional[str] = None


class StopChargeRequest(BaseModel):
    transaction_id: int


@router.post("/chargers/{charger_id}/connectors/{connector_id}/start")
async def start_charge(
    charger_id: str, connector_id: int, body: StartChargeRequest,
    db: Session = Depends(get_db), user=Depends(get_current_user),
):
    allowed_chargers = _user_charger_ids(db, user)
    if allowed_chargers is not None and charger_id not in allowed_chargers:
        raise HTTPException(status_code=403, detail="Borne non associée à votre compte")
    allowed_vehicles = _user_vehicle_ids(db, user)
    if allowed_vehicles is not None and body.vehicle_id is not None and body.vehicle_id not in allowed_vehicles:
        raise HTTPException(status_code=403, detail="Véhicule non associé à votre compte")

    cp = _require_local_and_connected(charger_id, db)

    id_tag = body.id_tag
    vehicle = None
    if body.vehicle_id is not None:
        vehicle = db.query(Vehicle).filter(Vehicle.id == body.vehicle_id).first()
        if not vehicle:
            raise HTTPException(status_code=404, detail="Véhicule inconnu")
        if not id_tag:
            id_tag = vehicle.id_tag or f"REMOTE-{vehicle.id}"
    if not id_tag:
        id_tag = "WEBADMIN"

    if vehicle is not None:
        PENDING_REMOTE_STARTS[(charger_id, connector_id)] = vehicle.id

    result = await cp.trigger_remote_start(connector_id, id_tag)
    return {"status": result.status}


@router.post("/chargers/{charger_id}/connectors/{connector_id}/stop")
async def stop_charge(
    charger_id: str, connector_id: int, body: StopChargeRequest,
    db: Session = Depends(get_db), user=Depends(get_current_user),
):
    allowed_chargers = _user_charger_ids(db, user)
    if allowed_chargers is not None and charger_id not in allowed_chargers:
        raise HTTPException(status_code=403, detail="Borne non associée à votre compte")
    allowed_vehicles = _user_vehicle_ids(db, user)
    if allowed_vehicles is not None:
        txn = db.query(Transaction).filter(
            Transaction.id == body.transaction_id,
            Transaction.status == "active",
        ).first()
        if txn and txn.vehicle_id not in allowed_vehicles:
            raise HTTPException(status_code=403, detail="Véhicule non associé à votre compte")

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


# --- Métriques et historique ---

def _session_power_max_w(db: Session, s: Transaction) -> Optional[float]:
    if s.id is None:
        return None
    rows = db.query(MeterValue.value).filter(
        MeterValue.transaction_id == s.id,
        MeterValue.measurand == "Power.Active.Import",
    ).all()
    values = [r[0] for r in rows if r[0] is not None]
    return round(max(values), 1) if values else None


def _serialize_session(s: Transaction, db: Session, prev_odometer: Optional[float] = None) -> dict:
    if s.is_external:
        cost, energy_wh, tariff_plan_name = s.cost, s.energy_wh or 0.0, s.tariff_plan_name
    elif s.status == "completed":
        if s.cost is None and s.energy_wh is None:
            freeze_transaction_cost(db, s)
            db.commit()
        cost, energy_wh, tariff_plan_name = s.cost, s.energy_wh or 0.0, s.tariff_plan_name
    else:
        charger = db.query(Charger).filter(Charger.id == s.charger_id).first()
        plan = resolve_plan_for_charger(db, charger)
        meter_values = db.query(MeterValue).filter(MeterValue.transaction_id == s.id).all()
        cost_info = compute_session_cost(s, meter_values, plan)
        cost, energy_wh = cost_info["cost"], cost_info["energy_wh"]
        tariff_plan_name = plan.name if plan else None

    vehicle = db.query(Vehicle).filter(Vehicle.id == s.vehicle_id).first() if s.vehicle_id else None

    duration_min = None
    if s.start_time:
        end = s.stop_time or datetime.utcnow()
        duration_min = round((end - s.start_time).total_seconds() / 60, 1)

    power_max_w = None if s.is_external else _session_power_max_w(db, s)

    battery_recharge_percent = None
    battery_percent_end_est = None
    if vehicle and vehicle.battery_capacity_kwh:
        battery_recharge_percent = round((energy_wh / 1000.0) / vehicle.battery_capacity_kwh * 100, 1)
        if s.battery_percent_start is not None:
            est = s.battery_percent_start + battery_recharge_percent
            battery_percent_end_est = round(min(est, 100.0), 1)

    km_since_last = None
    kwh_per_100km = None
    if s.odometer_km is not None and prev_odometer is not None:
        delta = s.odometer_km - prev_odometer
        if delta > 0:
            km_since_last = round(delta, 1)
            if energy_wh:
                kwh_per_100km = round((energy_wh / 1000.0) / delta * 100, 2)

    return {
        "id": s.id, "charger_id": s.charger_id, "connector_id": s.connector_id, "id_tag": s.id_tag,
        "vehicle_id": s.vehicle_id, "vehicle_name": vehicle.name if vehicle else None,
        "meter_start": s.meter_start, "meter_stop": s.meter_stop,
        "start_time": s.start_time.isoformat() if s.start_time else None,
        "stop_time": s.stop_time.isoformat() if s.stop_time else None,
        "status": s.status, "duration_min": duration_min,
        "energy_wh": energy_wh, "cost": cost,
        "tariff_plan_name": tariff_plan_name,
        "odometer_km": s.odometer_km,
        "battery_percent_start": s.battery_percent_start,
        "battery_percent_end": s.battery_percent_end,
        "is_external": bool(s.is_external),
        "location_label": s.location_label,
        "power_max_w": power_max_w,
        "battery_recharge_percent": battery_recharge_percent,
        "battery_percent_end_est": battery_percent_end_est,
        "km_since_last": km_since_last,
        "kwh_per_100km": kwh_per_100km,
        "deferred_until": getattr(s, "deferred_until", None),
    }


def _serialize_sessions_with_km(sessions, db: Session) -> list[dict]:
    ordered = sorted(
        [s for s in sessions if s.start_time],
        key=lambda s: s.start_time,
    )
    last_odo: dict[int, float] = {}
    seen_first: set[int] = set()
    for s in ordered:
        if not s.vehicle_id or s.vehicle_id in seen_first:
            continue
        seen_first.add(s.vehicle_id)
        prior = (
            db.query(Transaction)
            .filter(
                Transaction.vehicle_id == s.vehicle_id,
                Transaction.odometer_km.isnot(None),
                Transaction.start_time.isnot(None),
                Transaction.start_time < s.start_time,
            )
            .order_by(Transaction.start_time.desc())
            .first()
        )
        if prior and prior.odometer_km is not None:
            last_odo[s.vehicle_id] = prior.odometer_km

    prev_by_id: dict[int, Optional[float]] = {}
    for s in ordered:
        prev = last_odo.get(s.vehicle_id) if s.vehicle_id else None
        prev_by_id[s.id] = prev
        if s.vehicle_id and s.odometer_km is not None:
            last_odo[s.vehicle_id] = s.odometer_km
    return [_serialize_session(s, db, prev_by_id.get(s.id)) for s in sessions]


class SessionUpdate(BaseModel):
    vehicle_id: Optional[int] = None
    odometer_km: Optional[float] = None
    battery_percent_start: Optional[float] = None
    battery_percent_end: Optional[float] = None
    energy_kwh: Optional[float] = None
    cost: Optional[float] = None
    location_label: Optional[str] = None
    start_time: Optional[str] = None
    stop_time: Optional[str] = None


class ExternalChargeCreate(BaseModel):
    vehicle_id: int
    energy_kwh: float
    cost: Optional[float] = None
    location_label: Optional[str] = None
    start_time: Optional[str] = None
    stop_time: Optional[str] = None
    odometer_km: Optional[float] = None
    battery_percent_start: Optional[float] = None
    battery_percent_end: Optional[float] = None


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", ""))
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Date invalide : {value}")


@router.post("/external-charges")
def create_external_charge(
    body: ExternalChargeCreate,
    db: Session = Depends(get_db), user=Depends(get_current_user),
):
    vehicle = db.query(Vehicle).filter(Vehicle.id == body.vehicle_id).first()
    if not vehicle:
        raise HTTPException(status_code=404, detail="Véhicule inconnu")
    start = _parse_iso(body.start_time) or datetime.utcnow()
    stop = _parse_iso(body.stop_time)
    txn = Transaction(
        charger_id=None, connector_id=None, vehicle_id=vehicle.id,
        start_time=start, stop_time=stop, status="completed",
        is_external=True, location_label=body.location_label,
        energy_wh=(body.energy_kwh or 0.0) * 1000.0,
        cost=body.cost,
        tariff_plan_name=body.location_label or "Externe",
        odometer_km=body.odometer_km,
        battery_percent_start=body.battery_percent_start,
        battery_percent_end=body.battery_percent_end,
    )
    db.add(txn)
    db.commit()
    db.refresh(txn)
    return {"id": txn.id}


@router.delete("/sessions/{session_id}")
def delete_session(session_id: int, db: Session = Depends(get_db), user=Depends(require_admin)):
    s = db.query(Transaction).filter(Transaction.id == session_id).first()
    if not s:
        raise HTTPException(status_code=404, detail="Session inconnue")
    db.query(MeterValue).filter(MeterValue.transaction_id == session_id).update(
        {MeterValue.transaction_id: None}
    )
    db.delete(s)
    db.commit()
    return {"status": "ok"}


@router.put("/sessions/{session_id}")
def update_session(
    session_id: int, body: SessionUpdate,
    db: Session = Depends(get_db), user=Depends(get_current_user),
):
    """Permet de compléter ou corriger une session a posteriori. Les users
    peuvent modifier les sessions des véhicules qui leur sont associés."""
    s = db.query(Transaction).filter(Transaction.id == session_id).first()
    if not s:
        raise HTTPException(status_code=404, detail="Session inconnue")
    allowed_vehicles = _user_vehicle_ids(db, user)
    if allowed_vehicles is not None and s.vehicle_id not in allowed_vehicles:
        raise HTTPException(status_code=403, detail="Session non accessible à votre compte")
    data = body.model_dump(exclude_unset=True)
    if "vehicle_id" in data:
        vehicle_id = data["vehicle_id"]
        if vehicle_id is not None:
            vehicle = db.query(Vehicle).filter(Vehicle.id == vehicle_id).first()
            if not vehicle:
                raise HTTPException(status_code=404, detail="Véhicule inconnu")
        s.vehicle_id = vehicle_id
    if "odometer_km" in data:
        s.odometer_km = data["odometer_km"]
    if "battery_percent_start" in data:
        s.battery_percent_start = data["battery_percent_start"]
    if "battery_percent_end" in data:
        s.battery_percent_end = data["battery_percent_end"]
    if s.is_external:
        if "energy_kwh" in data and data["energy_kwh"] is not None:
            s.energy_wh = data["energy_kwh"] * 1000.0
        if "cost" in data:
            s.cost = data["cost"]
        if "location_label" in data:
            s.location_label = data["location_label"]
            s.tariff_plan_name = data["location_label"] or "Externe"
        if "start_time" in data and data["start_time"]:
            s.start_time = _parse_iso(data["start_time"])
        if "stop_time" in data:
            s.stop_time = _parse_iso(data["stop_time"])
    db.commit()
    return {"status": "ok"}


@router.get("/chargers/{charger_id}/sessions")
def list_sessions(
    charger_id: str, connector_id: Optional[int] = None,
    db: Session = Depends(get_db), user=Depends(get_current_user),
):
    query = db.query(Transaction).filter(Transaction.charger_id == charger_id)
    if connector_id is not None:
        query = query.filter(Transaction.connector_id == connector_id)
    sessions = query.order_by(Transaction.start_time.desc()).all()
    return _serialize_sessions_with_km(sessions, db)


@router.get("/history")
def list_history(
    vehicle_id: Optional[int] = None, charger_id: Optional[str] = None,
    status_filter: Optional[str] = None, limit: int = 200,
    db: Session = Depends(get_db), user=Depends(get_current_user),
):
    query = db.query(Transaction)
    allowed_vehicle_ids = _user_vehicle_ids(db, user)
    if allowed_vehicle_ids is not None:
        query = query.filter(Transaction.vehicle_id.in_(allowed_vehicle_ids))
    if vehicle_id is not None:
        query = query.filter(Transaction.vehicle_id == vehicle_id)
    if charger_id is not None:
        query = query.filter(Transaction.charger_id == charger_id)
    if status_filter is not None:
        query = query.filter(Transaction.status == status_filter)
    sessions = query.order_by(Transaction.start_time.desc()).limit(limit).all()
    return _serialize_sessions_with_km(sessions, db)


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


# --- Véhicules ---

class VehicleCreate(BaseModel):
    name: str
    id_tag: Optional[str] = None
    battery_capacity_kwh: Optional[float] = None


@router.get("/vehicles")
def list_vehicles(db: Session = Depends(get_db), user=Depends(get_current_user)):
    vehicles = db.query(Vehicle).filter(Vehicle.deleted_at.is_(None)).order_by(Vehicle.name).all()
    allowed_ids = _user_vehicle_ids(db, user)
    if allowed_ids is not None:
        vehicles = [v for v in vehicles if v.id in allowed_ids]
    return [
        {
            "id": v.id, "name": v.name, "id_tag": v.id_tag,
            "battery_capacity_kwh": v.battery_capacity_kwh,
        }
        for v in vehicles
    ]


@router.get("/vehicles/{vehicle_id}/stats")
def vehicle_stats(vehicle_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    vehicle = db.query(Vehicle).filter(Vehicle.id == vehicle_id).first()
    if not vehicle:
        raise HTTPException(status_code=404, detail="Véhicule inconnu")
    sessions = db.query(Transaction).filter(
        Transaction.vehicle_id == vehicle_id
    ).order_by(Transaction.start_time.desc()).all()
    serialized = _serialize_sessions_with_km(sessions, db)

    completed = [s for s in serialized if s["status"] == "completed"]
    total_energy_wh = sum(s["energy_wh"] or 0 for s in completed)
    total_cost = sum(s["cost"] or 0 for s in completed if s["cost"] is not None)
    total_km = sum(s["km_since_last"] or 0 for s in serialized if s["km_since_last"])
    odos = [s["odometer_km"] for s in serialized if s["odometer_km"] is not None]
    avg_consumption = round(total_energy_wh / 1000.0 / total_km * 100, 2) if total_km else None

    return {
        "id": vehicle.id, "name": vehicle.name, "id_tag": vehicle.id_tag,
        "battery_capacity_kwh": vehicle.battery_capacity_kwh,
        "stats": {
            "charge_count": len(completed),
            "external_count": sum(1 for s in completed if s["is_external"]),
            "total_energy_kwh": round(total_energy_wh / 1000.0, 2),
            "total_cost": round(total_cost, 2),
            "total_km": round(total_km, 1) if total_km else 0,
            "avg_kwh_per_100km": avg_consumption,
            "last_odometer_km": max(odos) if odos else None,
        },
        "sessions": serialized,
    }


def _require_vehicle_permission(user: dict, db: Session):
    if is_admin(user):
        return
    uid = get_user_id(user)
    u = db.query(User).filter(User.id == uid).first() if uid else None
    perms = u.permissions if u else None
    if not (perms and perms.can_manage_vehicles):
        raise HTTPException(status_code=403, detail="Droits insuffisants pour gérer les véhicules")


@router.post("/vehicles")
def create_vehicle(body: VehicleCreate, db: Session = Depends(get_db), user=Depends(get_current_user)):
    _require_vehicle_permission(user, db)
    if body.id_tag:
        existing = db.query(Vehicle).filter(
            Vehicle.id_tag == body.id_tag, Vehicle.deleted_at.is_(None)
        ).first()
        if existing:
            raise HTTPException(status_code=400, detail="Ce idTag est déjà associé à un autre véhicule")
    vehicle = Vehicle(name=body.name, id_tag=body.id_tag or None, battery_capacity_kwh=body.battery_capacity_kwh)
    db.add(vehicle)
    db.flush()
    uid = get_user_id(user)
    if uid:
        db.add(UserVehicle(user_id=uid, vehicle_id=vehicle.id))
    db.commit()
    db.refresh(vehicle)
    return {"id": vehicle.id}


@router.put("/vehicles/{vehicle_id}")
def update_vehicle(
    vehicle_id: int, body: VehicleCreate,
    db: Session = Depends(get_db), user=Depends(get_current_user),
):
    _require_vehicle_permission(user, db)
    vehicle = db.query(Vehicle).filter(Vehicle.id == vehicle_id).first()
    if not vehicle:
        raise HTTPException(status_code=404, detail="Véhicule inconnu")
    allowed_ids = _user_vehicle_ids(db, user)
    if allowed_ids is not None and vehicle_id not in allowed_ids:
        raise HTTPException(status_code=403, detail="Véhicule non associé à votre compte")
    if body.id_tag:
        existing = db.query(Vehicle).filter(
            Vehicle.id_tag == body.id_tag, Vehicle.id != vehicle_id, Vehicle.deleted_at.is_(None)
        ).first()
        if existing:
            raise HTTPException(status_code=400, detail="Ce idTag est déjà associé à un autre véhicule")
    vehicle.name = body.name
    vehicle.id_tag = body.id_tag or None
    vehicle.battery_capacity_kwh = body.battery_capacity_kwh
    db.commit()
    return {"status": "ok"}


@router.delete("/vehicles/{vehicle_id}")
def delete_vehicle(vehicle_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    _require_vehicle_permission(user, db)
    vehicle = db.query(Vehicle).filter(Vehicle.id == vehicle_id).first()
    if not vehicle:
        raise HTTPException(status_code=404, detail="Véhicule inconnu")
    allowed_ids = _user_vehicle_ids(db, user)
    if allowed_ids is not None and vehicle_id not in allowed_ids:
        raise HTTPException(status_code=403, detail="Véhicule non associé à votre compte")
    vehicle.deleted_at = datetime.utcnow()
    vehicle.id_tag = None
    db.commit()
    return {"status": "ok"}


# --- Tarifs ---

class TariffPlanCreate(BaseModel):
    name: str
    is_default: bool = False
    fixed_price: Optional[float] = None
    subscribed_power_kva: Optional[float] = None


class TariffPeriodCreate(BaseModel):
    name: str
    price: float
    days_of_week: str = "0,1,2,3,4,5,6"
    start_time: str
    end_time: str


def _serialize_plan(p: TariffPlan) -> dict:
    return {
        "id": p.id, "name": p.name, "is_default": p.is_default, "fixed_price": p.fixed_price,
        "subscribed_power_kva": p.subscribed_power_kva,
        "periods": [
            {
                "id": period.id, "name": period.name, "price": period.price,
                "days_of_week": period.days_of_week,
                "start_time": period.start_time, "end_time": period.end_time,
            }
            for period in p.periods
        ],
    }


@router.get("/tariffs")
def list_tariffs(db: Session = Depends(get_db), user=Depends(get_current_user)):
    plans = db.query(TariffPlan).order_by(TariffPlan.name).all()
    return [_serialize_plan(p) for p in plans]


@router.post("/tariffs")
def create_tariff(body: TariffPlanCreate, db: Session = Depends(get_db), user=Depends(require_admin)):
    if body.is_default:
        db.query(TariffPlan).update({TariffPlan.is_default: False})
    plan = TariffPlan(
        name=body.name, is_default=body.is_default, fixed_price=body.fixed_price,
        subscribed_power_kva=body.subscribed_power_kva,
    )
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return {"id": plan.id}


@router.put("/tariffs/{plan_id}")
def update_tariff(
    plan_id: int, body: TariffPlanCreate,
    db: Session = Depends(get_db), user=Depends(require_admin),
):
    plan = db.query(TariffPlan).filter(TariffPlan.id == plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="Tarif inconnu")
    if body.is_default:
        db.query(TariffPlan).update({TariffPlan.is_default: False})
    plan.name = body.name
    plan.is_default = body.is_default
    plan.fixed_price = body.fixed_price
    plan.subscribed_power_kva = body.subscribed_power_kva
    db.commit()
    return {"status": "ok"}


@router.post("/tariffs/{plan_id}/set-default")
def set_default_tariff(plan_id: int, db: Session = Depends(get_db), user=Depends(require_admin)):
    plan = db.query(TariffPlan).filter(TariffPlan.id == plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="Tarif inconnu")
    db.query(TariffPlan).update({TariffPlan.is_default: False})
    plan.is_default = True
    db.commit()
    return {"status": "ok"}


@router.delete("/tariffs/{plan_id}")
def delete_tariff(plan_id: int, db: Session = Depends(get_db), user=Depends(require_admin)):
    plan = db.query(TariffPlan).filter(TariffPlan.id == plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="Tarif inconnu")
    db.delete(plan)
    db.commit()
    return {"status": "ok"}


@router.post("/tariffs/{plan_id}/periods")
def add_period(
    plan_id: int, body: TariffPeriodCreate,
    db: Session = Depends(get_db), user=Depends(require_admin),
):
    plan = db.query(TariffPlan).filter(TariffPlan.id == plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="Tarif inconnu")
    period = TariffPeriod(
        tariff_plan_id=plan_id, name=body.name, price=body.price,
        days_of_week=body.days_of_week, start_time=body.start_time, end_time=body.end_time,
    )
    db.add(period)
    db.commit()
    db.refresh(period)
    return {"id": period.id}


@router.put("/tariffs/{plan_id}/periods/{period_id}")
def update_period(
    plan_id: int, period_id: int, body: TariffPeriodCreate,
    db: Session = Depends(get_db), user=Depends(require_admin),
):
    period = db.query(TariffPeriod).filter(
        TariffPeriod.id == period_id, TariffPeriod.tariff_plan_id == plan_id
    ).first()
    if not period:
        raise HTTPException(status_code=404, detail="Période inconnue")
    period.name = body.name
    period.price = body.price
    period.days_of_week = body.days_of_week
    period.start_time = body.start_time
    period.end_time = body.end_time
    db.commit()
    return {"status": "ok"}


@router.delete("/tariffs/{plan_id}/periods/{period_id}")
def delete_period(
    plan_id: int, period_id: int,
    db: Session = Depends(get_db), user=Depends(require_admin),
):
    period = db.query(TariffPeriod).filter(
        TariffPeriod.id == period_id, TariffPeriod.tariff_plan_id == plan_id
    ).first()
    if not period:
        raise HTTPException(status_code=404, detail="Période inconnue")
    db.delete(period)
    db.commit()
    return {"status": "ok"}


class ChargerTariffUpdate(BaseModel):
    tariff_plan_id: Optional[int] = None


@router.put("/chargers/{charger_id}/tariff")
def set_charger_tariff(
    charger_id: str, body: ChargerTariffUpdate,
    db: Session = Depends(get_db), user=Depends(require_admin),
):
    charger = db.query(Charger).filter(Charger.id == charger_id).first()
    if not charger:
        raise HTTPException(status_code=404, detail="Borne inconnue")
    charger.tariff_plan_id = body.tariff_plan_id
    db.commit()
    return {"status": "ok"}


@router.get("/chargers/{charger_id}/stats")
def charger_stats(charger_id: str, db: Session = Depends(get_db), user=Depends(get_current_user)):
    charger = db.query(Charger).filter(Charger.id == charger_id).first()
    if not charger:
        raise HTTPException(status_code=404, detail="Borne inconnue")
    sessions = db.query(Transaction).filter(
        Transaction.charger_id == charger_id
    ).order_by(Transaction.start_time.desc()).all()
    serialized = _serialize_sessions_with_km(sessions, db)
    completed = [s for s in serialized if s["status"] == "completed"]
    total_energy_wh = sum(s["energy_wh"] or 0 for s in completed)
    total_cost = sum(s["cost"] or 0 for s in completed if s["cost"] is not None)
    powers = [s["power_max_w"] for s in serialized if s["power_max_w"]]
    return {
        "id": charger.id, "vendor": charger.vendor, "model": charger.model,
        "stats": {
            "charge_count": len(completed),
            "total_energy_kwh": round(total_energy_wh / 1000.0, 2),
            "total_cost": round(total_cost, 2),
            "power_max_w": round(max(powers), 1) if powers else None,
        },
        "sessions": serialized,
    }


@router.get("/occupancy")
def power_occupancy(db: Session = Depends(get_db), user=Depends(get_current_user)):
    active = db.query(Transaction).filter(Transaction.status == "active").all()
    per_plan: dict[int, dict] = {}

    def _bucket(plan):
        pid = plan.id if plan else 0
        if pid not in per_plan:
            per_plan[pid] = {
                "plan_id": plan.id if plan else None,
                "plan_name": plan.name if plan else "(sans abonnement)",
                "subscribed_power_kva": plan.subscribed_power_kva if plan else None,
                "delivered_power_w": 0.0,
                "active_sessions": 0,
            }
        return per_plan[pid]

    for s in active:
        charger = db.query(Charger).filter(Charger.id == s.charger_id).first() if s.charger_id else None
        plan = resolve_plan_for_charger(db, charger) if charger else None
        last_power = db.query(MeterValue).filter(
            MeterValue.transaction_id == s.id,
            MeterValue.measurand == "Power.Active.Import",
        ).order_by(MeterValue.timestamp.desc()).first()
        power_w = last_power.value if last_power else 0.0
        b = _bucket(plan)
        b["delivered_power_w"] += power_w
        b["active_sessions"] += 1

    result = []
    for b in per_plan.values():
        sub_w = (b["subscribed_power_kva"] or 0) * 1000.0
        ratio = round(b["delivered_power_w"] / sub_w * 100, 1) if sub_w > 0 else None
        result.append({
            **b,
            "delivered_power_w": round(b["delivered_power_w"], 1),
            "occupancy_percent": ratio,
        })
    return result


# --- Conditions de charge ---

class ChargeConditionCreate(BaseModel):
    connector_id: Optional[int] = None
    type: ChargeConditionType
    time_value: Optional[str] = None
    enabled: bool = True


def _serialize_condition(c: ChargeCondition) -> dict:
    return {
        "id": c.id, "charger_id": c.charger_id, "connector_id": c.connector_id,
        "type": c.type.value, "time_value": c.time_value, "enabled": bool(c.enabled),
    }


@router.get("/chargers/{charger_id}/conditions")
def list_conditions(charger_id: str, db: Session = Depends(get_db), user=Depends(get_current_user)):
    conds = db.query(ChargeCondition).filter(
        ChargeCondition.charger_id == charger_id
    ).order_by(ChargeCondition.id).all()
    return [_serialize_condition(c) for c in conds]


@router.post("/chargers/{charger_id}/conditions")
def create_condition(
    charger_id: str, body: ChargeConditionCreate,
    db: Session = Depends(get_db), user=Depends(require_admin),
):
    charger = db.query(Charger).filter(
        Charger.id == charger_id, Charger.deleted_at.is_(None)
    ).first()
    if not charger:
        raise HTTPException(status_code=404, detail="Borne inconnue")
    if SMART_CHARGING_SUPPORT.get(charger_id) is False:
        raise HTTPException(
            status_code=409,
            detail="Cette borne ne supporte pas SmartCharging : la programmation de charge n'est pas disponible.",
        )
    if body.type in (ChargeConditionType.start_after, ChargeConditionType.ready_by) and not body.time_value:
        raise HTTPException(status_code=400, detail="Une heure est requise pour ce type de condition")
    cond = ChargeCondition(
        charger_id=charger_id, connector_id=body.connector_id,
        type=body.type, time_value=body.time_value, enabled=body.enabled,
    )
    db.add(cond)
    db.commit()
    db.refresh(cond)
    return {"id": cond.id}


@router.put("/chargers/{charger_id}/conditions/{condition_id}")
def update_condition(
    charger_id: str, condition_id: int, body: ChargeConditionCreate,
    db: Session = Depends(get_db), user=Depends(require_admin),
):
    cond = db.query(ChargeCondition).filter(
        ChargeCondition.id == condition_id, ChargeCondition.charger_id == charger_id
    ).first()
    if not cond:
        raise HTTPException(status_code=404, detail="Condition inconnue")
    cond.connector_id = body.connector_id
    cond.type = body.type
    cond.time_value = body.time_value
    cond.enabled = body.enabled
    db.commit()
    return {"status": "ok"}


@router.delete("/chargers/{charger_id}/conditions/{condition_id}")
def delete_condition(
    charger_id: str, condition_id: int,
    db: Session = Depends(get_db), user=Depends(require_admin),
):
    cond = db.query(ChargeCondition).filter(
        ChargeCondition.id == condition_id, ChargeCondition.charger_id == charger_id
    ).first()
    if not cond:
        raise HTTPException(status_code=404, detail="Condition inconnue")
    db.delete(cond)
    db.commit()
    return {"status": "ok"}


# ============================ EXPORT / IMPORT ============================

import datetime as _dt
import enum as _enum

_EXPORT_ORDER = [
    TariffPlan, TariffPeriod, Vehicle, Charger, ChargeCondition,
    Transaction, MeterValue, ConfigurationKey, ConnectorStatus,
]
_TABLE_BY_NAME = {m.__tablename__: m for m in _EXPORT_ORDER}


def _serialize_row(obj) -> dict:
    out = {}
    for col in obj.__table__.columns:
        v = getattr(obj, col.name)
        if isinstance(v, _dt.datetime):
            v = {"__dt__": v.isoformat()}
        elif isinstance(v, _enum.Enum):
            v = v.value
        out[col.name] = v
    return out


def _deserialize_value(col, raw):
    if isinstance(raw, dict) and "__dt__" in raw:
        return _parse_iso(raw["__dt__"])
    return raw


@router.get("/export")
def export_data(db: Session = Depends(get_db), user=Depends(require_admin)):
    from . import main as _main
    payload = {"tables": {}}
    for model in _EXPORT_ORDER:
        rows = db.query(model).all()
        payload["tables"][model.__tablename__] = [_serialize_row(r) for r in rows]
    payload["meta"] = {
        "version": getattr(_main, "APP_VERSION", None),
        "exported_at": _dt.datetime.utcnow().isoformat(),
    }
    return payload


class ImportBody(BaseModel):
    mode: str = "merge"
    tables: dict


@router.post("/import")
def import_data(body: ImportBody, db: Session = Depends(get_db), user=Depends(require_admin)):
    mode = (body.mode or "merge").lower()
    if mode not in ("replace", "merge"):
        raise HTTPException(status_code=400, detail="Mode invalide (replace|merge)")
    tables = body.tables or {}

    try:
        if mode == "replace":
            for model in reversed(_EXPORT_ORDER):
                if model.__tablename__ in tables:
                    db.query(model).delete()
            db.flush()

        for model in _EXPORT_ORDER:
            rows = tables.get(model.__tablename__)
            if not rows:
                continue
            cols = {c.name: c for c in model.__table__.columns}
            pk_names = [c.name for c in model.__table__.primary_key.columns]
            for raw in rows:
                data = {k: _deserialize_value(cols[k], v) for k, v in raw.items() if k in cols}
                obj = None
                if mode == "merge" and len(pk_names) == 1:
                    pk = data.get(pk_names[0])
                    if pk is not None:
                        obj = db.get(model, pk)
                if obj is None:
                    db.add(model(**data))
                else:
                    for k, v in data.items():
                        if k not in pk_names:
                            setattr(obj, k, v)
            db.flush()
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"Import échoué : {exc}")

    return {"status": "ok", "mode": mode}


# ============================ JOURNAL OCPP (live) ============================

from . import ocpp_logs


@router.get("/logs")
def get_logs(
    charger_id: Optional[str] = None, action: Optional[str] = None,
    direction: Optional[str] = None, since_id: int = 0, limit: int = 500,
    user=Depends(get_current_user),
):
    return {
        "capture_high_volume": ocpp_logs.is_capturing_high_volume(),
        "entries": ocpp_logs.get_entries(
            charger_id=charger_id, action=action, direction=direction,
            since_id=since_id, limit=limit,
        ),
    }


@router.get("/logs/chargers")
def get_logs_chargers(user=Depends(get_current_user)):
    return ocpp_logs.known_chargers()


class LogSettings(BaseModel):
    capture_high_volume: bool


@router.put("/logs/settings")
def set_logs_settings(body: LogSettings, user=Depends(require_admin)):
    ocpp_logs.set_capture_high_volume(body.capture_high_volume)
    return {"capture_high_volume": ocpp_logs.is_capturing_high_volume()}


@router.delete("/logs")
def clear_logs(user=Depends(require_admin)):
    ocpp_logs.clear()
    return {"status": "ok"}


# ============================ DANGER ZONE ============================

@router.delete("/history/all")
def delete_all_history(db: Session = Depends(get_db), user=Depends(require_admin)):
    """Supprime DEFINITVEMENT toutes les transactions. Action irréversible."""
    count = db.query(Transaction).delete()
    db.commit()
    return {"status": "ok", "deleted": count}


# ============================ GESTION DES UTILISATEURS ============================


def _serialize_user(u: User) -> dict:
    perms = u.permissions
    return {
        "id": u.id,
        "username": u.username,
        "role": u.role.value,
        "vehicle_ids": [lnk.vehicle_id for lnk in u.vehicle_links],
        "charger_ids": [lnk.charger_id for lnk in u.charger_links],
        "permissions": {
            "can_manage_chargers": perms.can_manage_chargers if perms else False,
            "can_manage_tariffs": perms.can_manage_tariffs if perms else False,
            "can_manage_vehicles": perms.can_manage_vehicles if perms else False,
            "can_view_logs": perms.can_view_logs if perms else False,
            "can_export_import": perms.can_export_import if perms else False,
        },
    }


class UserCreate(BaseModel):
    username: str
    password: str
    role: str = "user"
    vehicle_ids: list[int] = []
    charger_ids: list[str] = []
    permissions: dict = {}


class UserPermissionUpdate(BaseModel):
    can_manage_chargers: bool = False
    can_manage_tariffs: bool = False
    can_manage_vehicles: bool = False
    can_view_logs: bool = False
    can_export_import: bool = False


class UserAssocUpdate(BaseModel):
    vehicle_ids: list[int] = []
    charger_ids: list[str] = []


class AdminPasswordReset(BaseModel):
    new_password: str


@router.get("/users")
def list_users(db: Session = Depends(get_db), user=Depends(require_admin)):
    users = db.query(User).filter(User.deleted_at.is_(None)).order_by(User.username).all()
    return [_serialize_user(u) for u in users]


@router.post("/users")
def create_user(body: UserCreate, db: Session = Depends(get_db), user=Depends(require_admin)):
    if len(body.password) < 6:
        raise HTTPException(status_code=400, detail="Le mot de passe doit faire au moins 6 caractères")
    existing = db.query(User).filter(User.username == body.username, User.deleted_at.is_(None)).first()
    if existing:
        raise HTTPException(status_code=400, detail="Ce nom d'utilisateur est déjà pris")
    try:
        role = UserRole(body.role)
    except ValueError:
        raise HTTPException(status_code=400, detail="Rôle invalide (admin|user)")
    u = User(username=body.username, password_hash=hash_password(body.password), role=role)
    db.add(u)
    db.flush()
    pdata = body.permissions or {}
    db.add(UserPermission(
        user_id=u.id,
        can_manage_chargers=bool(pdata.get("can_manage_chargers")),
        can_manage_tariffs=bool(pdata.get("can_manage_tariffs")),
        can_manage_vehicles=bool(pdata.get("can_manage_vehicles")),
        can_view_logs=bool(pdata.get("can_view_logs")),
        can_export_import=bool(pdata.get("can_export_import")),
    ))
    for vid in body.vehicle_ids:
        db.add(UserVehicle(user_id=u.id, vehicle_id=vid))
    for cid in body.charger_ids:
        db.add(UserCharger(user_id=u.id, charger_id=cid))
    db.commit()
    db.refresh(u)
    return _serialize_user(u)


@router.put("/users/{user_id}/permissions")
def update_user_permissions(
    user_id: int, body: UserPermissionUpdate,
    db: Session = Depends(get_db), user=Depends(require_admin),
):
    u = db.query(User).filter(User.id == user_id, User.deleted_at.is_(None)).first()
    if not u:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable")
    perms = u.permissions
    if not perms:
        perms = UserPermission(user_id=u.id)
        db.add(perms)
    perms.can_manage_chargers = body.can_manage_chargers
    perms.can_manage_tariffs = body.can_manage_tariffs
    perms.can_manage_vehicles = body.can_manage_vehicles
    perms.can_view_logs = body.can_view_logs
    perms.can_export_import = body.can_export_import
    db.commit()
    return _serialize_user(u)


@router.put("/users/{user_id}/associations")
def update_user_associations(
    user_id: int, body: UserAssocUpdate,
    db: Session = Depends(get_db), user=Depends(require_admin),
):
    u = db.query(User).filter(User.id == user_id, User.deleted_at.is_(None)).first()
    if not u:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable")
    db.query(UserVehicle).filter(UserVehicle.user_id == user_id).delete()
    db.query(UserCharger).filter(UserCharger.user_id == user_id).delete()
    for vid in body.vehicle_ids:
        db.add(UserVehicle(user_id=user_id, vehicle_id=vid))
    for cid in body.charger_ids:
        db.add(UserCharger(user_id=user_id, charger_id=cid))
    db.commit()
    db.refresh(u)
    return _serialize_user(u)


@router.post("/users/{user_id}/reset-password")
def admin_reset_password(
    user_id: int, body: AdminPasswordReset,
    db: Session = Depends(get_db), user=Depends(require_admin),
):
    u = db.query(User).filter(User.id == user_id, User.deleted_at.is_(None)).first()
    if not u:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable")
    if len(body.new_password) < 6:
        raise HTTPException(status_code=400, detail="Le mot de passe doit faire au moins 6 caractères")
    u.password_hash = hash_password(body.new_password)
    db.commit()
    return {"status": "ok"}


@router.delete("/users/{user_id}")
def delete_user(user_id: int, db: Session = Depends(get_db), user=Depends(require_admin)):
    u = db.query(User).filter(User.id == user_id, User.deleted_at.is_(None)).first()
    if not u:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable")
    if u.role == UserRole.admin and u.username == "admin":
        raise HTTPException(status_code=400, detail="Le compte admin principal ne peut pas être supprimé")
    u.deleted_at = datetime.utcnow()
    db.commit()
    return {"status": "ok"}
