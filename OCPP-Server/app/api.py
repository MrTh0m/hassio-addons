from typing import Optional
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .db import get_db
from .models import (
    Charger, ChargerMode, AuthMode, Transaction, MeterValue, User, ConnectorStatus,
    Vehicle, TariffPlan, TariffPeriod, ChargeCondition, ChargeConditionType,
)
from .pricing import (
    compute_session_cost, resolve_plan_for_charger, freeze_transaction_cost, price_at,
)
from .auth import verify_password, create_access_token, get_current_user, require_admin
from .csms_local import CONNECTED_CHARGERS, PENDING_REMOTE_STARTS

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
    # Les bornes supprimées (deleted_at) sont masquées partout dans l'appli.
    chargers = db.query(Charger).filter(Charger.deleted_at.is_(None)).all()
    return [
        {
            "id": c.id, "vendor": c.vendor, "model": c.model,
            "ocpp_version": c.ocpp_version, "mode": c.mode.value,
            "auth_mode": c.auth_mode.value if c.auth_mode else "free",
            "status": c.status, "connected": c.id in CONNECTED_CHARGERS,
        }
        for c in chargers
    ]


@router.delete("/chargers/{charger_id}")
def delete_charger(charger_id: str, db: Session = Depends(get_db), user=Depends(require_admin)):
    """Retire une borne du CSMS (remplacement, panne...) sans perdre son
    historique : suppression LOGIQUE (deleted_at). Les transactions déjà
    enregistrées restent en base et continuent d'alimenter l'historique et les
    statistiques des véhicules ; la borne disparaît simplement des listes et
    n'est plus pilotée. Si elle se reconnecte, elle sera redécouverte comme
    une nouvelle borne (ou tu peux la restaurer via l'API)."""
    charger = db.query(Charger).filter(Charger.id == charger_id).first()
    if not charger:
        raise HTTPException(status_code=404, detail="Borne inconnue")
    charger.deleted_at = datetime.utcnow()
    # On coupe aussi ses conditions de charge et son statut de connecteurs
    # pour qu'elle ne soit plus pilotée ni affichée comme disponible.
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
    """Définit la politique de déclenchement de la charge pour cette borne :
    'free' (sans autorisation, démarrage automatique au branchement) ou
    'authorized' (badge connu ou bouton requis). N'a de sens qu'en mode local."""
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
    cp = _require_local_and_connected(charger_id, db)

    id_tag = body.id_tag
    vehicle = None
    if body.vehicle_id is not None:
        vehicle = db.query(Vehicle).filter(Vehicle.id == body.vehicle_id).first()
        if not vehicle:
            raise HTTPException(status_code=404, detail="Véhicule inconnu")
        # idTag du véhicule s'il en a un, sinon un tag distant dédié (accepté
        # même en mode 'authorized', cf. RESERVED_TAGS / préfixe REMOTE-).
        if not id_tag:
            id_tag = vehicle.id_tag or f"REMOTE-{vehicle.id}"
    if not id_tag:
        id_tag = "WEBADMIN"

    # Mémorise le véhicule visé pour rattacher la future session, même sans idTag
    # (le StartTransaction qui suivra ne le porterait pas sinon).
    if vehicle is not None:
        PENDING_REMOTE_STARTS[(charger_id, connector_id)] = vehicle.id

    result = await cp.trigger_remote_start(connector_id, id_tag)
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

def _session_power_max_w(db: Session, s: Transaction) -> Optional[float]:
    """Puissance maximale relevée pendant la session (Power.Active.Import),
    ou None si aucun relevé de puissance n'est disponible (ex. charge externe,
    ou borne qui ne remonte que l'énergie)."""
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
        # Charge externe : énergie et coût saisis à la main, jamais recalculés.
        cost, energy_wh, tariff_plan_name = s.cost, s.energy_wh or 0.0, s.tariff_plan_name
    elif s.status == "completed":
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

    duration_min = None
    if s.start_time:
        end = s.stop_time or datetime.utcnow()
        duration_min = round((end - s.start_time).total_seconds() / 60, 1)

    power_max_w = None if s.is_external else _session_power_max_w(db, s)

    # --- Indicateurs dérivés ------------------------------------------------
    # % de recharge : énergie injectée rapportée à la capacité batterie.
    battery_recharge_percent = None
    battery_percent_end_est = None
    if vehicle and vehicle.battery_capacity_kwh:
        battery_recharge_percent = round((energy_wh / 1000.0) / vehicle.battery_capacity_kwh * 100, 1)
        if s.battery_percent_start is not None:
            est = s.battery_percent_start + battery_recharge_percent
            battery_percent_end_est = round(min(est, 100.0), 1)

    # km depuis la charge précédente (même véhicule) et kWh/100km.
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
    }


def _serialize_sessions_with_km(sessions, db: Session) -> list[dict]:
    """Sérialise une liste de sessions en calculant, par véhicule, les km
    parcourus depuis la charge précédente (nécessite de connaître l'odomètre
    de la charge antérieure du même véhicule, dans l'ordre chronologique)."""
    # Odomètre précédent connu par véhicule, en parcourant du plus ancien au
    # plus récent, puis on rétablit l'ordre d'entrée.
    ordered = sorted(
        [s for s in sessions if s.start_time],
        key=lambda s: s.start_time,
    )
    last_odo: dict[int, float] = {}
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
    # Champs éditables uniquement pour une charge externe saisie à la main.
    energy_kwh: Optional[float] = None
    cost: Optional[float] = None
    location_label: Optional[str] = None
    start_time: Optional[str] = None
    stop_time: Optional[str] = None


class ExternalChargeCreate(BaseModel):
    """Charge réalisée ailleurs (borne tierce), saisie manuellement pour garder
    une continuité de suivi du véhicule (coût total, kWh, km, %)."""
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
    """Enregistre une charge effectuée sur une borne tierce (hors CSMS). Elle
    apparaît dans l'historique et les statistiques du véhicule au même titre
    qu'une charge locale, mais marquée « externe » (énergie et coût figés tels
    que saisis, aucune borne associée)."""
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
    """Supprime une session (utile surtout pour corriger une charge externe
    saisie par erreur). Une session OCPP réelle peut aussi être supprimée, mais
    ses MeterValues associées sont alors détachées (transaction_id remis à NULL)."""
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
    """Permet de compléter ou corriger une session a posteriori : associer un
    véhicule (même après coup, si le badge n'a pas été présenté ou pour une
    charge démarrée depuis Home Assistant), et renseigner kilométrage /
    niveaux de batterie, qu'aucun capteur ne fournit. Seuls les champs
    effectivement envoyés sont modifiés (ex. assigner un véhicule pendant une
    charge en cours n'efface pas un km déjà renseigné par ailleurs)."""
    s = db.query(Transaction).filter(Transaction.id == session_id).first()
    if not s:
        raise HTTPException(status_code=404, detail="Session inconnue")
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
    # Champs réservés aux charges externes (énergie/coût saisis à la main).
    # Sur une vraie session OCPP, l'énergie et le coût sont issus des
    # MeterValues / du gel de tarif : on ne les laisse pas réécrire.
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
    return [
        {
            "id": v.id, "name": v.name, "id_tag": v.id_tag,
            "battery_capacity_kwh": v.battery_capacity_kwh,
        }
        for v in vehicles
    ]


@router.get("/vehicles/{vehicle_id}/stats")
def vehicle_stats(vehicle_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Fiche synthétique d'un véhicule : nombre de charges, énergie et coût
    cumulés, km parcourus, conso moyenne, et historique complet de ses charges
    (locales comme externes)."""
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


@router.post("/vehicles")
def create_vehicle(body: VehicleCreate, db: Session = Depends(get_db), user=Depends(require_admin)):
    if body.id_tag:
        existing = db.query(Vehicle).filter(
            Vehicle.id_tag == body.id_tag, Vehicle.deleted_at.is_(None)
        ).first()
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
def delete_vehicle(vehicle_id: int, db: Session = Depends(get_db), user=Depends(require_admin)):
    """Retire un véhicule (suppression LOGIQUE) : ses charges passées restent
    en base pour ne pas fausser l'historique. Son idTag est libéré (remis à
    NULL) pour pouvoir être réattribué à un autre véhicule."""
    vehicle = db.query(Vehicle).filter(Vehicle.id == vehicle_id).first()
    if not vehicle:
        raise HTTPException(status_code=404, detail="Véhicule inconnu")
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


# --- Fiche borne (statistiques) ---

@router.get("/chargers/{charger_id}/stats")
def charger_stats(charger_id: str, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Fiche synthétique d'une borne : nombre de charges délivrées, énergie et
    coût cumulés, puissance max observée, et son historique de charges."""
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


# --- Taux d'occupation de l'alimentation électrique ---

@router.get("/occupancy")
def power_occupancy(db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Taux d'occupation de l'abonnement électrique à l'instant T : puissance
    délivrée cumulée (somme des Power.Active.Import les plus récents de chaque
    charge en cours) rapportée à la puissance souscrite de l'abonnement.

    La puissance souscrite est en kVA ; on l'assimile à des kW (facteur de
    puissance ≈ 1 sur une recharge VE), c'est une approximation raisonnable
    pour un indicateur de charge.
    """
    # Charges actuellement actives.
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
        # Dernier relevé de puissance de cette session.
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


# --- Conditions de charge (programmation) ---

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
    """Ajoute une condition de charge à une borne. Plusieurs conditions se
    combinent en ET (toutes doivent autoriser). Voir scheduler.py pour leur
    évaluation. Ces conditions n'ont de sens qu'en mode local."""
    charger = db.query(Charger).filter(
        Charger.id == charger_id, Charger.deleted_at.is_(None)
    ).first()
    if not charger:
        raise HTTPException(status_code=404, detail="Borne inconnue")
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
