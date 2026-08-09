import csv
import io
import json
from typing import Optional
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status, Response
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import func

from .db import get_db
from .models import (
    Charger, ChargerMode, AuthMode, Transaction, MeterValue, User, UserRole,
    UserVehicle, UserCharger, UserPermission, ConnectorStatus,
    Vehicle, TariffPlan, TariffPeriod, ChargeCondition, ChargeConditionType, ConfigurationKey,
    AppSetting,
)
from .pricing import (
    compute_session_cost, resolve_plan_for_charger, freeze_transaction_cost, price_at,
)
from .auth import (
    verify_password, hash_password, create_access_token,
    get_current_user, require_admin, is_admin, get_user_id,
)
from .csms_local import CONNECTED_CHARGERS, PENDING_REMOTE_STARTS, SMART_CHARGING_SUPPORT


def _to_csv_response(rows: list, filename: str) -> Response:
    """Convertit une liste de dicts plats en réponse CSV téléchargeable.
    Gère le marqueur {"__dt__": iso} utilisé par _serialize_row pour les
    datetimes, et convertit les valeurs dict/list restantes (ex. payload de
    log) en JSON compact plutôt que de planter sur un type non scalaire."""
    columns: list = []
    for r in rows:
        for k in r.keys():
            if k not in columns:
                columns.append(k)
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        flat = {}
        for k, v in r.items():
            if isinstance(v, dict) and "__dt__" in v:
                flat[k] = v["__dt__"]
            elif isinstance(v, (dict, list)):
                flat[k] = json.dumps(v, ensure_ascii=False)
            elif v is None:
                flat[k] = ""
            else:
                flat[k] = v
        writer.writerow(flat)
    # BOM utf-8 : Excel/LibreOffice ouvrent sinon les accents mal encodés.
    csv_bytes = output.getvalue().encode("utf-8-sig")
    return Response(
        content=csv_bytes, media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


router = APIRouter(prefix="/api")


# --- Auth ---

DEBUG_MODE_KEY = "debug_mode"


def _get_setting(db: Session, key: str, default: str = "false") -> str:
    row = db.query(AppSetting).filter(AppSetting.key == key).first()
    return row.value if row and row.value is not None else default


def _set_setting(db: Session, key: str, value: str) -> None:
    row = db.query(AppSetting).filter(AppSetting.key == key).first()
    if row:
        row.value = value
    else:
        db.add(AppSetting(key=key, value=value))
    db.commit()


def _is_debug_mode(db: Session) -> bool:
    return _get_setting(db, DEBUG_MODE_KEY, "false") == "true"


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
    debug_mode = _is_debug_mode(db)
    uid = get_user_id(user)
    u = db.query(User).filter(User.id == uid).first() if uid else None
    if not u:
        return {"role": user.get("role", "user"), "permissions": {}, "debug_mode": debug_mode}
    perms = u.permissions
    vehicle_ids = [lnk.vehicle_id for lnk in u.vehicle_links]
    charger_ids = [lnk.charger_id for lnk in u.charger_links]
    return {
        "id": u.id,
        "username": u.username,
        "role": u.role.value,
        "vehicle_ids": vehicle_ids,
        "charger_ids": charger_ids,
        "debug_mode": debug_mode,
        "permissions": {
            "can_manage_chargers": is_admin(user) or (perms.can_manage_chargers if perms else False),
            "can_manage_tariffs": is_admin(user) or (perms.can_manage_tariffs if perms else False),
            "can_manage_vehicles": is_admin(user) or (perms.can_manage_vehicles if perms else False),
            "can_view_logs": is_admin(user) or (perms.can_view_logs if perms else False),
            "can_export_import": is_admin(user) or (perms.can_export_import if perms else False),
        },
    }


class DebugModeUpdate(BaseModel):
    enabled: bool


@router.put("/settings/debug")
def set_debug_mode(body: DebugModeUpdate, db: Session = Depends(get_db), user=Depends(require_admin)):
    """Active/désactive l'onglet de diagnostic « Base de données » (réservé admin,
    indépendamment de ce réglage : celui-ci ne fait que masquer/afficher l'onglet)."""
    _set_setting(db, DEBUG_MODE_KEY, "true" if body.enabled else "false")
    return {"debug_mode": body.enabled}


@router.get("/diagnostics/packages")
def list_installed_packages(user=Depends(require_admin)):
    """Liste les paquets Python réellement installés dans le conteneur, avec
    leur version exacte. Sert à figer requirements.txt sur les versions qui
    tournent actuellement, plutôt que de deviner. Lecture seule, ne dépend pas
    de pip (lit les métadonnées d'installation directement)."""
    import importlib.metadata as _im
    pkgs = sorted(
        (
            {"name": dist.metadata["Name"], "version": dist.version}
            for dist in _im.distributions()
            if dist.metadata.get("Name")
        ),
        key=lambda p: p["name"].lower(),
    )
    return pkgs


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
    if u.username == "admin":
        raise HTTPException(status_code=400, detail="Le mot de passe du compte admin est géré par la configuration de l'add-on Home Assistant")
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
    """Renvoie aussi les bornes désactivées (deleted_at rempli), avec le champ
    `active`, pour que l'UI les affiche grisées plutôt que de les cacher. Une
    borne désactivée se réactive automatiquement toute seule si elle se
    reconnecte physiquement (voir LocalChargePoint._get_or_create_charger)."""
    chargers = db.query(Charger).order_by(Charger.deleted_at.is_not(None)).all()
    allowed_ids = _user_charger_ids(db, user)
    if allowed_ids is not None:
        chargers = [c for c in chargers if c.id in allowed_ids]
    return [
        {
            "id": c.id, "display_name": c.display_name,
            "vendor": c.vendor, "model": c.model,
            "ocpp_version": c.ocpp_version, "mode": c.mode.value,
            "auth_mode": c.auth_mode.value if c.auth_mode else "free",
            "status": c.status, "connected": c.id in CONNECTED_CHARGERS,
            "smart_charging": SMART_CHARGING_SUPPORT.get(c.id),
            "relay_url": c.relay_url,
            "tariff_plan_id": c.tariff_plan_id,
            "active": c.deleted_at is None,
        }
        for c in chargers
    ]


def _require_active_charger(charger: "Charger"):
    """La seule action autorisée sur une borne désactivée est de renommer son
    nom d'affichage (voir set_charger_display_name). Toute autre modification
    est bloquée ; elle redevient pleinement pilotable automatiquement si elle
    se reconnecte physiquement."""
    if charger.deleted_at is not None:
        raise HTTPException(
            status_code=400,
            detail="Borne désactivée : seule la modification du nom d'affichage est possible. Reconnectez-la physiquement pour la réactiver.",
        )


@router.delete("/chargers/{charger_id}")
def delete_charger(charger_id: str, db: Session = Depends(get_db), user=Depends(require_admin)):
    """Désactive une borne (suppression LOGIQUE, réversible) sans perdre son
    historique : les transactions déjà enregistrées restent en base et
    continuent d'alimenter l'historique et les statistiques des véhicules ;
    la borne apparaît grisée dans la liste et n'est plus pilotable. Si elle se
    reconnecte physiquement (BootNotification), elle est réactivée
    automatiquement avec le même id, pas besoin d'action manuelle."""
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
        "id": charger.id, "display_name": charger.display_name,
        "vendor": charger.vendor, "model": charger.model,
        "serial": charger.serial, "ocpp_version": charger.ocpp_version,
        "mode": charger.mode.value, "relay_url": charger.relay_url,
        "auth_mode": charger.auth_mode.value if charger.auth_mode else "free",
        "status": charger.status, "connected": charger.id in CONNECTED_CHARGERS,
        "tariff_plan_id": charger.tariff_plan_id,
        "smart_charging": SMART_CHARGING_SUPPORT.get(charger.id),
        "active": charger.deleted_at is None,
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
    else:
        _require_active_charger(charger)
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
    _require_active_charger(charger)
    charger.auth_mode = update.auth_mode
    db.commit()
    return {"status": "ok"}


class DisplayNameUpdate(BaseModel):
    display_name: Optional[str] = None


@router.put("/chargers/{charger_id}/display-name")
def set_charger_display_name(
    charger_id: str, body: DisplayNameUpdate,
    db: Session = Depends(get_db), user=Depends(require_admin),
):
    """Définit un nom d'affichage lisible pour la borne (ex. 'Garage').
    N'affecte pas le chargePointId OCPP, utilisé uniquement dans l'interface."""
    charger = db.query(Charger).filter(Charger.id == charger_id).first()
    if not charger:
        raise HTTPException(status_code=404, detail="Borne inconnue")
    charger.display_name = body.display_name or None
    db.commit()
    return {"status": "ok"}


def _require_local_and_connected(charger_id: str, db: Session):
    charger = db.query(Charger).filter(Charger.id == charger_id).first()
    if not charger:
        raise HTTPException(status_code=404, detail="Borne inconnue")
    if charger.deleted_at is not None:
        raise HTTPException(status_code=400, detail="Borne désactivée : reconnectez-la physiquement pour la réactiver")
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
    # Contrôle d'accès : la borne ET le véhicule doivent être associés au user
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
        if vehicle.deleted_at is not None:
            raise HTTPException(status_code=400, detail="Véhicule désactivé : réactivez-le avant de démarrer une charge")
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
    # Contrôle d'accès : la borne ET la voiture en cours doivent être associées
    allowed_chargers = _user_charger_ids(db, user)
    if allowed_chargers is not None and charger_id not in allowed_chargers:
        raise HTTPException(status_code=403, detail="Borne non associée à votre compte")
    # Vérifier que la voiture de la transaction active est bien associée au user
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
        # Filtre aussi par charger_id + connector_id (pas seulement transaction_id) :
        # une réutilisation d'id SQLite après suppression peut sinon faire hériter
        # les MeterValues d'une toute autre session (voir freeze_transaction_cost).
        meter_values = db.query(MeterValue).filter(
            MeterValue.transaction_id == s.id,
            MeterValue.charger_id == s.charger_id,
            MeterValue.connector_id == s.connector_id,
        ).all()
        cost_info = compute_session_cost(s, meter_values, plan, ignore_meter_stop=True)
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

    charger_obj = db.query(Charger).filter(Charger.id == s.charger_id).first() if s.charger_id else None
    return {
        "id": s.id, "charger_id": s.charger_id,
        "charger_display_name": charger_obj.display_name if charger_obj else None,
        "connector_id": s.connector_id, "id_tag": s.id_tag,
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
        # Charge intentionnellement suspendue en attente d'une programmation :
        # la transaction est ouverte (câble verrouillé) mais aucun kWh ne transite.
        "deferred_until": getattr(s, "deferred_until", None),
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
    # Amorçage : pour chaque véhicule présent dans la fenêtre, on récupère en
    # base l'odomètre de la DERNIÈRE charge ANTÉRIEURE à la plus ancienne charge
    # affichée. Sans cela, la première charge de la fenêtre n'aurait jamais de
    # "km parcourus" (sa précédente étant hors limite d'affichage).
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
    if vehicle.deleted_at is not None:
        raise HTTPException(status_code=400, detail="Véhicule désactivé : réactivez-le avant de lui associer une charge")
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


@router.post("/sessions/{session_id}/recalculate")
def recalculate_session(
    session_id: int,
    db: Session = Depends(get_db), user=Depends(require_admin),
):
    """Recalcule l'énergie et le coût d'une session terminée depuis meter_start/meter_stop.
    Utile pour corriger une session corrompue par un bug de calcul antérieur."""
    s = db.query(Transaction).filter(Transaction.id == session_id).first()
    if not s:
        raise HTTPException(status_code=404, detail="Session inconnue")
    if s.status != "completed":
        raise HTTPException(status_code=400, detail="Seules les sessions terminées peuvent être recalculées")
    if s.is_external:
        raise HTTPException(status_code=400, detail="Les charges externes ne sont pas recalculables")
    if s.meter_start is None or s.meter_stop is None:
        raise HTTPException(status_code=400, detail="meter_start ou meter_stop manquant, recalcul impossible")
    # Forcer le recalcul depuis meter_start/meter_stop uniquement (sans MeterValues)
    s.energy_wh = None
    s.cost = None
    db.flush()
    result = freeze_transaction_cost(db, s)
    db.commit()
    return {
        "status": "ok",
        "energy_wh": result["energy_wh"],
        "cost": result["cost"],
    }


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
    """Permet de compléter ou corriger une session a posteriori. Les users
    peuvent modifier les sessions des véhicules qui leur sont associés."""
    s = db.query(Transaction).filter(Transaction.id == session_id).first()
    if not s:
        raise HTTPException(status_code=404, detail="Session inconnue")
    # Un user ne peut modifier que les sessions de ses véhicules associés
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


def _query_history(db: Session, user: dict, vehicle_id, charger_id, status_filter, limit: int):
    query = db.query(Transaction)
    # Filtrage par droits : un user ne voit que les sessions de ses véhicules
    allowed_vehicle_ids = _user_vehicle_ids(db, user)
    if allowed_vehicle_ids is not None:
        query = query.filter(Transaction.vehicle_id.in_(allowed_vehicle_ids))
    if vehicle_id is not None:
        query = query.filter(Transaction.vehicle_id == vehicle_id)
    if charger_id is not None:
        query = query.filter(Transaction.charger_id == charger_id)
    if status_filter is not None:
        query = query.filter(Transaction.status == status_filter)
    return query.order_by(Transaction.start_time.desc()).limit(limit).all()


@router.get("/history")
def list_history(
    vehicle_id: Optional[int] = None, charger_id: Optional[str] = None,
    status_filter: Optional[str] = None, limit: int = 200,
    db: Session = Depends(get_db), user=Depends(get_current_user),
):
    sessions = _query_history(db, user, vehicle_id, charger_id, status_filter, limit)
    return _serialize_sessions_with_km(sessions, db)


@router.get("/history/export")
def export_history(
    vehicle_id: Optional[int] = None, charger_id: Optional[str] = None,
    status_filter: Optional[str] = None, limit: int = 5000,
    db: Session = Depends(get_db), user=Depends(get_current_user),
):
    """Export CSV de l'historique, mêmes filtres et même périmètre de droits
    que /history (un user ne voit que les sessions de ses véhicules)."""
    sessions = _query_history(db, user, vehicle_id, charger_id, status_filter, limit)
    data = _serialize_sessions_with_km(sessions, db)
    return _to_csv_response(data, "historique.csv")


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
    """Renvoie aussi les véhicules désactivés (deleted_at rempli), avec le
    champ `active` pour que l'UI les affiche grisés plutôt que de les cacher
    complètement. Seule une suppression DÉFINITIVE (voir plus bas) les fait
    disparaître."""
    vehicles = db.query(Vehicle).order_by(Vehicle.deleted_at.is_not(None), Vehicle.name).all()
    allowed_ids = _user_vehicle_ids(db, user)
    if allowed_ids is not None:
        vehicles = [v for v in vehicles if v.id in allowed_ids]
    return [
        {
            "id": v.id, "name": v.name, "id_tag": v.id_tag,
            "battery_capacity_kwh": v.battery_capacity_kwh,
            "active": v.deleted_at is None,
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
        "active": vehicle.deleted_at is None,
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
    """Vérifie que l'user a le droit de gérer les véhicules."""
    if is_admin(user):
        return
    uid = get_user_id(user)
    u = db.query(User).filter(User.id == uid).first() if uid else None
    perms = u.permissions if u else None
    if not (perms and perms.can_manage_vehicles):
        raise HTTPException(status_code=403, detail="Droits insuffisants pour gérer les véhicules")


def _check_vehicle_name_unique(db: Session, name: str, exclude_id: Optional[int] = None):
    """Un libellé dupliqué (même entre un véhicule actif et un désactivé) est
    une source de confusion directe : on risque d'associer une charge au
    mauvais véhicule sans s'en rendre compte. Vérifié sans distinction de
    casse ni espaces superflus, sur TOUS les véhicules (actifs et
    désactivés) puisqu'une suppression logique reste réversible."""
    clean = name.strip()
    if not clean:
        raise HTTPException(status_code=400, detail="Le nom du véhicule ne peut pas être vide")
    q = db.query(Vehicle).filter(func.lower(Vehicle.name) == clean.lower())
    if exclude_id is not None:
        q = q.filter(Vehicle.id != exclude_id)
    existing = q.first()
    if existing:
        etat = "désactivé" if existing.deleted_at is not None else "actif"
        raise HTTPException(
            status_code=400,
            detail=f"Un véhicule nommé « {existing.name} » existe déjà ({etat}). "
                   f"Réactivez-le ou choisissez un autre nom.",
        )


@router.post("/vehicles")
def create_vehicle(body: VehicleCreate, db: Session = Depends(get_db), user=Depends(get_current_user)):
    _require_vehicle_permission(user, db)
    _check_vehicle_name_unique(db, body.name)
    if body.id_tag:
        existing = db.query(Vehicle).filter(
            Vehicle.id_tag == body.id_tag, Vehicle.deleted_at.is_(None)
        ).first()
        if existing:
            raise HTTPException(status_code=400, detail="Ce idTag est déjà associé à un autre véhicule")
    vehicle = Vehicle(name=body.name.strip(), id_tag=body.id_tag or None, battery_capacity_kwh=body.battery_capacity_kwh)
    db.add(vehicle)
    db.flush()
    # Le créateur (admin ou user) est automatiquement associé à la voiture.
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
    # Un user ne peut modifier que ses propres véhicules
    allowed_ids = _user_vehicle_ids(db, user)
    if allowed_ids is not None and vehicle_id not in allowed_ids:
        raise HTTPException(status_code=403, detail="Véhicule non associé à votre compte")
    _check_vehicle_name_unique(db, body.name, exclude_id=vehicle_id)
    if body.id_tag:
        existing = db.query(Vehicle).filter(
            Vehicle.id_tag == body.id_tag, Vehicle.id != vehicle_id, Vehicle.deleted_at.is_(None)
        ).first()
        if existing:
            raise HTTPException(status_code=400, detail="Ce idTag est déjà associé à un autre véhicule")
    vehicle.name = body.name.strip()
    vehicle.id_tag = body.id_tag or None
    vehicle.battery_capacity_kwh = body.battery_capacity_kwh
    db.commit()
    return {"status": "ok"}


@router.delete("/vehicles/{vehicle_id}")
def delete_vehicle(vehicle_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Désactive un véhicule (suppression LOGIQUE, réversible). Reste visible
    (grisé) dans la liste et dans l'historique, mais ne peut plus recevoir de
    nouvelle charge tant qu'il n'est pas réactivé."""
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


@router.post("/vehicles/{vehicle_id}/reactivate")
def reactivate_vehicle(vehicle_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Annule une désactivation. Le idTag (retiré lors de la désactivation)
    n'est pas restauré automatiquement, à resaisir si besoin."""
    _require_vehicle_permission(user, db)
    vehicle = db.query(Vehicle).filter(Vehicle.id == vehicle_id).first()
    if not vehicle:
        raise HTTPException(status_code=404, detail="Véhicule inconnu")
    allowed_ids = _user_vehicle_ids(db, user)
    if allowed_ids is not None and vehicle_id not in allowed_ids:
        raise HTTPException(status_code=403, detail="Véhicule non associé à votre compte")
    vehicle.deleted_at = None
    db.commit()
    return {"status": "ok"}


@router.delete("/vehicles/{vehicle_id}/permanent")
def hard_delete_vehicle(vehicle_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Suppression DÉFINITIVE et IRRÉVERSIBLE d'un véhicule ET de tout son
    historique de charge (sessions + MeterValues associées). Contrairement à
    la désactivation, ceci efface réellement les lignes en base. L'UI doit
    avertir explicitement l'utilisateur avant d'appeler cette route."""
    _require_vehicle_permission(user, db)
    vehicle = db.query(Vehicle).filter(Vehicle.id == vehicle_id).first()
    if not vehicle:
        raise HTTPException(status_code=404, detail="Véhicule inconnu")
    allowed_ids = _user_vehicle_ids(db, user)
    if allowed_ids is not None and vehicle_id not in allowed_ids:
        raise HTTPException(status_code=403, detail="Véhicule non associé à votre compte")
    txn_ids = [t.id for t in db.query(Transaction.id).filter(Transaction.vehicle_id == vehicle_id).all()]
    if txn_ids:
        db.query(MeterValue).filter(MeterValue.transaction_id.in_(txn_ids)).delete(synchronize_session=False)
        db.query(Transaction).filter(Transaction.id.in_(txn_ids)).delete(synchronize_session=False)
    db.query(UserVehicle).filter(UserVehicle.vehicle_id == vehicle_id).delete(synchronize_session=False)
    db.delete(vehicle)
    db.commit()
    return {"status": "ok", "deleted_sessions": len(txn_ids)}


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
    _require_active_charger(charger)
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
    # La programmation repose sur SmartCharging (limitation à 0 W pour tenir la
    # charge en pause sans clore la transaction). Si la borne a explicitement
    # déclaré ne pas le supporter, on refuse : un repli par RemoteStop/Start
    # serait trop brutal et peu fiable pour un simple report d'horaire.
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
# Sauvegarde et restauration complètes des données et de la configuration.
# Sérialisation générique par introspection des colonnes SQLAlchemy : pas de
# liste de champs à maintenir, l'export suit automatiquement le schéma.

import datetime as _dt
import enum as _enum

# Ordre d'insertion respectant les clés étrangères (parents avant enfants).
_EXPORT_ORDER = [
    TariffPlan, TariffPeriod, Vehicle, Charger, ChargeCondition,
    Transaction, MeterValue, ConfigurationKey, ConnectorStatus,
]


def _serialize_row(obj) -> dict:
    out = {}
    for col in obj.__table__.columns:
        v = getattr(obj, col.name)
        if isinstance(v, _dt.datetime):
            v = {"__dt__": v.isoformat()}
        elif isinstance(v, _enum.Enum):
            v = v.value  # Enum (y compris héritant de str) -> valeur brute
        out[col.name] = v
    return out


def _deserialize_value(col, raw):
    if isinstance(raw, dict) and "__dt__" in raw:
        return _parse_iso(raw["__dt__"])
    return raw


@router.get("/export")
def export_data(db: Session = Depends(get_db), user=Depends(require_admin)):
    """Exporte l'intégralité des données et de la configuration au format JSON
    (un tableau de lignes par table). Réimportable via POST /import. Le mot de
    passe admin (table users) n'est volontairement pas inclus."""
    from . import main as _main  # pour APP_VERSION, sans import circulaire au chargement
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
    mode: str = "merge"  # "replace" (écrase tout) ou "merge" (complète / met à jour)
    tables: dict


@router.post("/import")
def import_data(body: ImportBody, db: Session = Depends(get_db), user=Depends(require_admin)):
    """Réimporte des données exportées. Deux modes :

    - replace : vide d'abord toutes les tables concernées, puis réinsère à
      l'identique (mêmes identifiants). Restauration fidèle d'une sauvegarde.
    - merge   : insère ou met à jour ligne par ligne (par clé primaire) sans
      rien supprimer de ce qui n'est pas dans le fichier.

    La table des utilisateurs n'est jamais touchée (le mot de passe reste
    celui en place). Opération transactionnelle : tout ou rien.
    """
    mode = (body.mode or "merge").lower()
    if mode not in ("replace", "merge"):
        raise HTTPException(status_code=400, detail="Mode invalide (replace|merge)")
    tables = body.tables or {}

    try:
        if mode == "replace":
            # Suppression enfants -> parents (ordre inverse de l'insertion).
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


# ============================ DIAGNOSTIC : BASE DE DONNÉES ============================
# Navigateur générique en lecture seule, réservé admin. Réutilise _EXPORT_ORDER
# et _serialize_row (voir export ci-dessus) pour rester automatiquement à jour
# avec le schéma. Volontairement limité aux tables déjà exportées : ne couvre
# pas users/user_permissions/user_vehicles/user_chargers (mot de passe et
# associations gérés depuis Réglages -> Utilisateurs, pas ici).

_TABLE_BY_NAME = {m.__tablename__: m for m in _EXPORT_ORDER}


def _order_column(model):
    """Colonne utilisée pour trier « le plus récent en premier ». created_at
    quand la table en a une, sinon la clé primaire (fonctionne aussi pour
    Charger, dont la clé primaire est un chargePointId textuel non trié)."""
    if hasattr(model, "created_at"):
        return model.created_at.desc()
    pk = list(model.__table__.primary_key.columns)[0]
    return getattr(model, pk.name).desc()


@router.get("/db/tables")
def list_db_tables(db: Session = Depends(get_db), user=Depends(require_admin)):
    """Liste des tables consultables avec leur nombre de lignes."""
    return [
        {"name": model.__tablename__, "row_count": db.query(model).count()}
        for model in _EXPORT_ORDER
    ]


@router.get("/db/tables/{table_name}/rows")
def list_db_table_rows(
    table_name: str, page: int = 1, limit: int = 50,
    db: Session = Depends(get_db), user=Depends(require_admin),
):
    """Lignes paginées d'une table, les plus récentes en premier. Lecture
    seule : cette route ne modifie jamais rien."""
    model = _TABLE_BY_NAME.get(table_name)
    if model is None:
        raise HTTPException(status_code=404, detail="Table inconnue")
    page = max(1, page)
    limit = max(1, min(limit, 200))
    total = db.query(model).count()
    rows = (
        db.query(model)
        .order_by(_order_column(model))
        .offset((page - 1) * limit)
        .limit(limit)
        .all()
    )
    columns = [c.name for c in model.__table__.columns]
    return {
        "table": table_name, "page": page, "limit": limit, "total": total,
        "columns": columns,
        "rows": [_serialize_row(r) for r in rows],
    }


@router.get("/db/tables/{table_name}/export")
def export_db_table(table_name: str, db: Session = Depends(get_db), user=Depends(require_admin)):
    """Export CSV complet d'une table (pas de pagination : toutes les lignes)."""
    model = _TABLE_BY_NAME.get(table_name)
    if model is None:
        raise HTTPException(status_code=404, detail="Table inconnue")
    rows = db.query(model).order_by(_order_column(model)).all()
    data = [_serialize_row(r) for r in rows]
    return _to_csv_response(data, f"{table_name}.csv")


# ============================ JOURNAL OCPP (live) ============================
# Flux de diagnostic en mémoire : voir en direct les messages OCPP échangés
# avec les bornes (local et relais, les deux sens). Non persisté. Réservé
# admin (voir aussi le réglage « Mode débug » qui masque/affiche l'onglet
# côté UI, indépendamment de cette autorisation).

from . import ocpp_logs


@router.get("/logs")
def get_logs(
    charger_id: Optional[str] = None, action: Optional[str] = None,
    direction: Optional[str] = None, since_id: int = 0, limit: int = 500,
    user=Depends(require_admin),
):
    return {
        "capture_high_volume": ocpp_logs.is_capturing_high_volume(),
        "entries": ocpp_logs.get_entries(
            charger_id=charger_id, action=action, direction=direction,
            since_id=since_id, limit=limit,
        ),
    }


@router.get("/logs/chargers")
def get_logs_chargers(user=Depends(require_admin)):
    """Bornes ayant produit au moins une entrée (pour alimenter le filtre)."""
    return ocpp_logs.known_chargers()


class LogSettings(BaseModel):
    capture_high_volume: bool


@router.put("/logs/settings")
def set_logs_settings(body: LogSettings, user=Depends(require_admin)):
    """Active/désactive la capture des messages à fort volume (MeterValues)."""
    ocpp_logs.set_capture_high_volume(body.capture_high_volume)
    return {"capture_high_volume": ocpp_logs.is_capturing_high_volume()}


@router.delete("/logs")
def clear_logs(user=Depends(require_admin)):
    ocpp_logs.clear()
    return {"status": "ok"}


@router.get("/logs/export")
def export_logs(
    charger_id: Optional[str] = None, action: Optional[str] = None,
    direction: Optional[str] = None, limit: int = 2000,
    user=Depends(require_admin),
):
    """Export CSV du journal en mémoire, avec les mêmes filtres que /logs."""
    entries = ocpp_logs.get_entries(
        charger_id=charger_id, action=action, direction=direction,
        since_id=0, limit=limit,
    )
    return _to_csv_response(entries, "ocpp-logs.csv")


# ============================ DANGER ZONE ============================

@router.delete("/history/all")
def delete_all_history(db: Session = Depends(get_db), user=Depends(require_admin)):
    """Supprime DÉFINITIVEMENT toutes les transactions (y compris externes)
    ET leurs MeterValues associés. Action irréversible, réservée à l'admin.

    Les MeterValues doivent être supprimés en même temps que les transactions :
    sinon ils restent orphelins avec un transaction_id qui pointe dans le vide.
    Comme SQLite réutilise les id auto-increment quand la table transactions
    redevient vide, une future session pourrait hériter du même id et se
    retrouver avec les MeterValues (et donc l'énergie/le coût) d'une toute
    autre charge, potentiellement sur une autre borne et à une autre date.
    """
    mv_count = db.query(MeterValue).delete()
    count = db.query(Transaction).delete()
    db.commit()
    return {"status": "ok", "deleted": count, "meter_values_deleted": mv_count}


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
    # Permissions
    pdata = body.permissions or {}
    db.add(UserPermission(
        user_id=u.id,
        can_manage_chargers=bool(pdata.get("can_manage_chargers")),
        can_manage_tariffs=bool(pdata.get("can_manage_tariffs")),
        can_manage_vehicles=bool(pdata.get("can_manage_vehicles")),
        can_view_logs=bool(pdata.get("can_view_logs")),
        can_export_import=bool(pdata.get("can_export_import")),
    ))
    # Associations véhicules
    for vid in body.vehicle_ids:
        db.add(UserVehicle(user_id=u.id, vehicle_id=vid))
    # Associations bornes
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
    if u.username == "admin":
        raise HTTPException(status_code=400, detail="Le compte admin n'est pas modifiable, il voit tout par définition")
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
    if u.username == "admin":
        raise HTTPException(status_code=400, detail="Le compte admin n'est pas modifiable, il voit tout par définition")
    # Remplace les associations (supprime tout puis réinsère)
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
    """L'admin peut réinitialiser le mot de passe de n'importe quel user, sauf
    le sien : ce compte est synchronisé automatiquement depuis la config Home
    Assistant à chaque démarrage (voir db.py::_sync_admin_password), un
    changement fait ici serait donc silencieusement écrasé au redémarrage."""
    u = db.query(User).filter(User.id == user_id, User.deleted_at.is_(None)).first()
    if not u:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable")
    if u.username == "admin":
        raise HTTPException(status_code=400, detail="Le mot de passe du compte admin est géré par la configuration de l'add-on Home Assistant")
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
