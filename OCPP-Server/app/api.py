from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .db import get_db
from .models import (
    Charger, ChargerMode, Transaction, MeterValue, User, ConnectorStatus,
    Vehicle, TariffPlan, TariffPeriod,
)
from .pricing import compute_session_cost, resolve_plan_for_charger, freeze_transaction_cost
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
        "tariff_plan_id": charger.tariff_plan_id,
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

def _serialize_session(s: Transaction, db: Session) -> dict:
    if s.status == "completed":
        if s.cost is None and s.energy_wh is None:
            # Ancienne session (créée avant l'introduction du gel de coût) :
            # on la fige maintenant, une seule fois, pour ne plus jamais la
            # recalculer à partir d'un tarif qui aura pu changer depuis.
            freeze_transaction_cost(db, s)
            db.commit()
        cost, energy_wh, tariff_plan_name = s.cost, s.energy_wh or 0.0, s.tariff_plan_name
    else:
        # Session encore active : calcul en direct, forcément amené à changer
        # au fil de la charge, donc jamais figé.
        charger = db.query(Charger).filter(Charger.id == s.charger_id).first()
        plan = resolve_plan_for_charger(db, charger)
        meter_values = db.query(MeterValue).filter(MeterValue.transaction_id == s.id).all()
        cost_info = compute_session_cost(s, meter_values, plan)
        cost, energy_wh = cost_info["cost"], cost_info["energy_wh"]
        tariff_plan_name = plan.name if plan else None

    vehicle = db.query(Vehicle).filter(Vehicle.id == s.vehicle_id).first() if s.vehicle_id else None
    return {
        "id": s.id, "charger_id": s.charger_id, "connector_id": s.connector_id, "id_tag": s.id_tag,
        "vehicle_id": s.vehicle_id, "vehicle_name": vehicle.name if vehicle else None,
        "meter_start": s.meter_start, "meter_stop": s.meter_stop,
        "start_time": s.start_time.isoformat() if s.start_time else None,
        "stop_time": s.stop_time.isoformat() if s.stop_time else None,
        "status": s.status,
        "energy_wh": energy_wh, "cost": cost,
        "tariff_plan_name": tariff_plan_name,
    }


@router.get("/chargers/{charger_id}/sessions")
def list_sessions(
    charger_id: str, connector_id: Optional[int] = None,
    db: Session = Depends(get_db), user=Depends(get_current_user),
):
    query = db.query(Transaction).filter(Transaction.charger_id == charger_id)
    if connector_id is not None:
        query = query.filter(Transaction.connector_id == connector_id)
    sessions = query.order_by(Transaction.start_time.desc()).all()
    return [_serialize_session(s, db) for s in sessions]


@router.get("/history")
def list_history(
    vehicle_id: Optional[int] = None, charger_id: Optional[str] = None, limit: int = 200,
    db: Session = Depends(get_db), user=Depends(get_current_user),
):
    query = db.query(Transaction)
    if vehicle_id is not None:
        query = query.filter(Transaction.vehicle_id == vehicle_id)
    if charger_id is not None:
        query = query.filter(Transaction.charger_id == charger_id)
    sessions = query.order_by(Transaction.start_time.desc()).limit(limit).all()
    return [_serialize_session(s, db) for s in sessions]


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
    vehicles = db.query(Vehicle).order_by(Vehicle.name).all()
    return [
        {
            "id": v.id, "name": v.name, "id_tag": v.id_tag,
            "battery_capacity_kwh": v.battery_capacity_kwh,
        }
        for v in vehicles
    ]


@router.post("/vehicles")
def create_vehicle(body: VehicleCreate, db: Session = Depends(get_db), user=Depends(require_admin)):
    if body.id_tag:
        existing = db.query(Vehicle).filter(Vehicle.id_tag == body.id_tag).first()
        if existing:
            raise HTTPException(status_code=400, detail="Ce idTag est déjà associé à un autre véhicule")
    vehicle = Vehicle(name=body.name, id_tag=body.id_tag or None, battery_capacity_kwh=body.battery_capacity_kwh)
    db.add(vehicle)
    db.commit()
    db.refresh(vehicle)
    return {"id": vehicle.id}


@router.put("/vehicles/{vehicle_id}")
def update_vehicle(
    vehicle_id: int, body: VehicleCreate,
    db: Session = Depends(get_db), user=Depends(require_admin),
):
    vehicle = db.query(Vehicle).filter(Vehicle.id == vehicle_id).first()
    if not vehicle:
        raise HTTPException(status_code=404, detail="Véhicule inconnu")
    if body.id_tag:
        existing = db.query(Vehicle).filter(Vehicle.id_tag == body.id_tag, Vehicle.id != vehicle_id).first()
        if existing:
            raise HTTPException(status_code=400, detail="Ce idTag est déjà associé à un autre véhicule")
    vehicle.name = body.name
    vehicle.id_tag = body.id_tag or None
    vehicle.battery_capacity_kwh = body.battery_capacity_kwh
    db.commit()
    return {"status": "ok"}


@router.delete("/vehicles/{vehicle_id}")
def delete_vehicle(vehicle_id: int, db: Session = Depends(get_db), user=Depends(require_admin)):
    vehicle = db.query(Vehicle).filter(Vehicle.id == vehicle_id).first()
    if not vehicle:
        raise HTTPException(status_code=404, detail="Véhicule inconnu")
    db.delete(vehicle)
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
    """Marque cet abonnement comme celui utilisé par défaut (pour les bornes
    sans tarif explicitement assigné), sans avoir à renvoyer tous ses champs."""
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
    # Les sessions déjà terminées ont leur coût figé indépendamment des
    # périodes (voir freeze_transaction_cost) : supprimer une période ne
    # modifie jamais rétroactivement un coût déjà calculé.
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
