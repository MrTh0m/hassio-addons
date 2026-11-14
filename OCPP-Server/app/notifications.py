"""
Notifications par utilisateur (0.19.45).

Un événement (début ou fin de charge, charge interrompue, problème de borne,
charge non autorisée) est envoyé :

- en notification push Web (PWA, y compris application fermée) sur chaque
  appareil où l'utilisateur les a activées ;
- par un service notify de Home Assistant choisi par l'utilisateur (par
  exemple notify.mobile_app_pixel), via l'API Supervisor ;
- en événement MQTT sur l'entité « Événements » de la borne, pour les
  automatisations Home Assistant (indépendamment des réglages utilisateur).

Chaque utilisateur choisit ses catégories. Il ne reçoit que ce qui concerne
ses véhicules et ses bornes (un administrateur reçoit tout).

L'envoi se fait toujours en tâche de fond : un service push ou Home Assistant
lent ou injoignable ne doit jamais retarder la réponse à un message OCPP.
"""
import asyncio
import json
import logging
import os
import urllib.error
import urllib.request
from datetime import datetime

from . import db as _db
from .models import (
    AppSetting, Charger, NotificationSetting, PushSubscription, User, UserCharger,
    UserRole, UserVehicle, Vehicle,
)

logger = logging.getLogger("notifications")

# --------------------------------------------------------------------------
# Catégories et événements
# --------------------------------------------------------------------------

CATEGORIES = {
    "charge": "Début et fin de charge",
    "interrupted": "Charge interrompue",
    "charger": "Problème de borne",
    "unauthorized": "Charge non autorisée",
}
DEFAULT_CATEGORIES = tuple(CATEGORIES)

# événement -> catégorie
EVENTS = {
    "charge_started": "charge",
    "charge_ended": "charge",
    "charge_interrupted": "interrupted",
    "charger_offline": "charger",
    "charger_online": "charger",
    "queue_blocked": "charger",
    "connector_fault": "charger",
    "unauthorized_charge": "unauthorized",
    "test": None,
}

# Raisons d'arrêt OCPP 1.6 qui signifient une charge interrompue (et non une
# fin normale : véhicule débranché, arrêt demandé, batterie pleine...).
INTERRUPT_REASONS = {"EmergencyStop", "PowerLoss", "Reboot", "HardReset", "SoftReset", "Other"}
STOP_REASON_LABELS = {
    "EmergencyStop": "arrêt d'urgence",
    "PowerLoss": "coupure de courant",
    "Reboot": "redémarrage de la borne",
    "HardReset": "redémarrage de la borne",
    "SoftReset": "redémarrage de la borne",
    "Other": "arrêt de la borne",
    "EVDisconnected": "véhicule vu comme débranché",
}

# Délai avant de signaler une borne hors ligne : les reconnexions en rafale
# (coupure Wi-Fi brève, redémarrage du serveur) ne doivent pas notifier.
OFFLINE_NOTIFY_DELAY_S = 300
OFFLINE_NOTIFIED: set[str] = set()
_offline_tasks: dict[str, asyncio.Task] = {}

# Débuts de charge déjà notifiés (une reprise après SuspendedEV ne renotifie pas).
STARTED_NOTIFIED: set[int] = set()

SEND_TIMEOUT_S = 10

# --------------------------------------------------------------------------
# Réglages utilisateur
# --------------------------------------------------------------------------


def get_settings(db, user_id: int) -> NotificationSetting:
    row = db.query(NotificationSetting).filter(NotificationSetting.user_id == user_id).first()
    if row is None:
        row = NotificationSetting(user_id=user_id, categories=",".join(DEFAULT_CATEGORIES))
        db.add(row)
        db.flush()
    return row


def categories_of(row: NotificationSetting | None) -> set[str]:
    if row is None:
        return set(DEFAULT_CATEGORIES)
    return {c for c in (row.categories or "").split(",") if c in CATEGORIES}


# --------------------------------------------------------------------------
# Clés VAPID (push Web)
# --------------------------------------------------------------------------

VAPID_PRIVATE_KEY = "vapid_private_pem"
VAPID_PUBLIC_KEY = "vapid_public_key"
# « sub » des jetons VAPID : adresse de contact au format mailto, exigée par
# les services push (Apple notamment) et vérifiée par py_vapid (une URL n'est
# acceptée que réduite à son hôte, sans chemin).
VAPID_SUB = "mailto:ocpp-server@users.noreply.github.com"


def vapid_keys(db) -> tuple[str, str]:
    """Clés VAPID du serveur (PEM privée, clé publique applicationServerKey en
    base64url), générées une fois puis conservées en base. Les changer
    invaliderait tous les abonnements push existants."""
    priv = db.query(AppSetting).filter(AppSetting.key == VAPID_PRIVATE_KEY).first()
    pub = db.query(AppSetting).filter(AppSetting.key == VAPID_PUBLIC_KEY).first()
    if priv and pub and priv.value and pub.value:
        return priv.value, pub.value
    from cryptography.hazmat.primitives import serialization
    from py_vapid import Vapid02, b64urlencode

    v = Vapid02()
    v.generate_keys()
    pem = v.private_pem().decode()
    raw = v.public_key.public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    public = b64urlencode(raw)
    for key, value in ((VAPID_PRIVATE_KEY, pem), (VAPID_PUBLIC_KEY, public)):
        row = db.query(AppSetting).filter(AppSetting.key == key).first()
        if row:
            row.value = value
        else:
            db.add(AppSetting(key=key, value=value))
    db.commit()
    logger.info("Clés VAPID générées pour les notifications push")
    return pem, public


# --------------------------------------------------------------------------
# Destinataires
# --------------------------------------------------------------------------


def recipients(db, event: str, charger_id: str | None, vehicle_id: int | None) -> list[User]:
    """Utilisateurs concernés par un événement, avant filtrage par leurs
    catégories : un administrateur reçoit tout ; un utilisateur reçoit les
    événements de charge de ses véhicules (ou de ses bornes quand la charge
    n'a pas de véhicule), et les événements de borne de ses bornes."""
    users = db.query(User).filter(User.deleted_at.is_(None)).all()
    out = []
    category = EVENTS.get(event)
    for u in users:
        if u.role == UserRole.admin:
            out.append(u)
            continue
        my_chargers = {l.charger_id for l in db.query(UserCharger).filter(UserCharger.user_id == u.id).all()}
        my_vehicles = {l.vehicle_id for l in db.query(UserVehicle).filter(UserVehicle.user_id == u.id).all()}
        if category in ("charge", "interrupted"):
            if vehicle_id is not None:
                if vehicle_id in my_vehicles:
                    out.append(u)
            elif charger_id in my_chargers:
                out.append(u)
        elif charger_id in my_chargers:
            out.append(u)
    return out


# --------------------------------------------------------------------------
# Textes
# --------------------------------------------------------------------------


def _fmt_duration(minutes) -> str | None:
    if minutes is None:
        return None
    m = int(round(minutes))
    return f"{m // 60} h {m % 60:02d}" if m >= 60 else f"{m} min"


def _fmt_num(v, digits) -> str:
    return f"{v:.{digits}f}".replace(".", ",")


def build_message(event: str, ctx: dict) -> tuple[str, str]:
    charger = ctx.get("charger_name") or "la borne"
    vehicle = ctx.get("vehicle_name")
    who = vehicle or charger
    parts = []
    if ctx.get("energy_wh") is not None:
        parts.append(f"{_fmt_num(ctx['energy_wh'] / 1000, 1)} kWh")
    if ctx.get("cost") is not None:
        parts.append(f"{_fmt_num(ctx['cost'], 2)} €")
    d = _fmt_duration(ctx.get("duration_min"))
    if d:
        parts.append(d)
    bilan = ", ".join(parts)
    where = f" sur {charger}" if vehicle else ""

    if event == "charge_started":
        return "Charge démarrée", f"{who} : la charge a démarré{where}."
    if event == "charge_ended":
        return "Charge terminée", f"{who} : {bilan or 'charge terminée'}{where}."
    if event == "charge_interrupted":
        why = ctx.get("reason_label") or "arrêt inattendu"
        tail = f" ({bilan})" if bilan else ""
        return "Charge interrompue", f"{who} : {why}{where}{tail}."
    if event == "charger_offline":
        return "Borne hors ligne", f"{charger} ne répond plus au serveur depuis {OFFLINE_NOTIFY_DELAY_S // 60} min."
    if event == "charger_online":
        return "Borne de nouveau en ligne", f"{charger} est reconnectée au serveur."
    if event == "queue_blocked":
        return "Borne bloquée", f"{charger} : file de messages bloquée ({ctx.get('detail') or 'aucun relevé'}). Un redémarrage de la borne peut être nécessaire."
    if event == "connector_fault":
        return "Défaut de la borne", f"{charger} : {ctx.get('detail') or 'erreur signalée'}."
    if event == "unauthorized_charge":
        return "Charge non autorisée", f"{charger} : charge démarrée avec l'identifiant {ctx.get('id_tag') or 'inconnu'}, non autorisé par le serveur."
    if event == "test":
        return "Notification de test", "Les notifications OCPP Server fonctionnent sur ce canal."
    return "OCPP Server", event


# --------------------------------------------------------------------------
# Canaux
# --------------------------------------------------------------------------


class PushGone(Exception):
    """Abonnement push expiré ou révoqué : à supprimer."""


def _send_push(subscription: dict, payload: str, private_pem: str) -> None:
    from py_vapid import Vapid02
    from pywebpush import webpush, WebPushException

    # webpush() n'accepte la clé en texte qu'au format DER base64url : on lui
    # passe l'objet clé déjà chargé depuis le PEM conservé en base.
    vapid = Vapid02.from_pem(private_pem.encode())
    try:
        webpush(
            subscription_info=subscription,
            data=payload,
            vapid_private_key=vapid,
            vapid_claims={"sub": VAPID_SUB},
            ttl=3600,
            timeout=SEND_TIMEOUT_S,
        )
    except WebPushException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status in (404, 410):
            raise PushGone() from exc
        raise


def supervisor_available() -> bool:
    return bool(os.environ.get("SUPERVISOR_TOKEN"))


def _supervisor_request(method: str, path: str, body: dict | None = None):
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        raise RuntimeError("API Home Assistant indisponible (SUPERVISOR_TOKEN absent)")
    req = urllib.request.Request(
        f"http://supervisor/core/api{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=SEND_TIMEOUT_S) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else None


def _send_ha(service: str, title: str, message: str, tag: str) -> None:
    service = service.removeprefix("notify.")
    _supervisor_request("POST", f"/services/notify/{service}", {
        "title": title,
        "message": message,
        "data": {"tag": tag, "group": "ocpp-server"},
    })


def list_ha_notify_services() -> list[str]:
    """Services notify déclarés dans Home Assistant (ex. mobile_app_pixel)."""
    data = _supervisor_request("GET", "/services") or []
    for domain in data:
        if domain.get("domain") == "notify":
            return sorted(s for s in (domain.get("services") or {}) if s not in ("send_message",))
    return []


# --------------------------------------------------------------------------
# Envoi
# --------------------------------------------------------------------------

_pending: set[asyncio.Task] = set()


def notify(event: str, charger_id: str | None = None, connector_id: int | None = None,
           vehicle_id: int | None = None, only_user_id: int | None = None, **ctx) -> asyncio.Task | None:
    """Programme l'envoi d'un événement à ses destinataires. Ne bloque jamais
    et ne lève jamais : à appeler depuis les gestionnaires OCPP."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    task = loop.create_task(_dispatch(event, charger_id, connector_id, vehicle_id, only_user_id, ctx))
    _pending.add(task)
    task.add_done_callback(_pending.discard)
    return task


async def drain():
    """Attend les envois en cours (tests)."""
    while _pending:
        await asyncio.gather(*list(_pending), return_exceptions=True)


def _context(db, charger_id, vehicle_id, ctx) -> dict:
    out = dict(ctx)
    if charger_id and "charger_name" not in out:
        ch = db.query(Charger).filter(Charger.id == charger_id).first()
        out["charger_name"] = (ch.display_name if ch else None) or charger_id
    if vehicle_id and "vehicle_name" not in out:
        v = db.query(Vehicle).filter(Vehicle.id == vehicle_id).first()
        out["vehicle_name"] = v.name if v else None
    return out


async def _dispatch(event, charger_id, connector_id, vehicle_id, only_user_id, ctx) -> dict:
    """Envoie l'événement ; renvoie le bilan par canal (utilisé par le test)."""
    result = {"push": 0, "push_failed": 0, "ha": 0, "ha_failed": 0, "mqtt": False, "errors": []}
    db = _db.SessionLocal()
    try:
        full = _context(db, charger_id, vehicle_id, ctx)
        title, body = build_message(event, full)
        tag = f"ocpp-{event}-{charger_id or ''}-{connector_id or ''}"
        category = EVENTS.get(event)
        if only_user_id is not None:
            users = db.query(User).filter(User.id == only_user_id).all()
        else:
            users = recipients(db, event, charger_id, vehicle_id)

        push_jobs, ha_jobs = [], []
        private_pem = None
        for u in users:
            st = db.query(NotificationSetting).filter(NotificationSetting.user_id == u.id).first()
            if category is not None and category not in categories_of(st):
                continue
            if st is None or st.push_enabled is not False:
                subs = db.query(PushSubscription).filter(PushSubscription.user_id == u.id).all()
                if subs and private_pem is None:
                    private_pem, _ = vapid_keys(db)
                for s in subs:
                    push_jobs.append((s.id, {"endpoint": s.endpoint, "keys": {"p256dh": s.p256dh, "auth": s.auth}}))
            if st is not None and st.ha_service:
                ha_jobs.append(st.ha_service)
        db.commit()
    finally:
        db.close()

    payload = json.dumps({"title": title, "body": body, "tag": tag, "event": event,
                          "charger_id": charger_id, "connector_id": connector_id})

    gone, ok_ids = [], []
    for sub_id, info in push_jobs:
        try:
            await asyncio.to_thread(_send_push, info, payload, private_pem)
            result["push"] += 1
            ok_ids.append(sub_id)
        except PushGone:
            gone.append(sub_id)
            result["push_failed"] += 1
        except Exception as exc:
            result["push_failed"] += 1
            result["errors"].append(f"push : {exc}")
            logger.warning("Notification push %s non envoyée : %s", event, exc)

    for service in dict.fromkeys(ha_jobs):
        try:
            await asyncio.to_thread(_send_ha, service, title, body, tag)
            result["ha"] += 1
        except Exception as exc:
            result["ha_failed"] += 1
            result["errors"].append(f"Home Assistant ({service}) : {exc}")
            logger.warning("Notification Home Assistant %s via %s non envoyée : %s", event, service, exc)

    if gone or ok_ids:
        db = _db.SessionLocal()
        try:
            if gone:
                db.query(PushSubscription).filter(PushSubscription.id.in_(gone)).delete(synchronize_session=False)
                logger.info("%d abonnement(s) push expiré(s) supprimé(s)", len(gone))
            if ok_ids:
                db.query(PushSubscription).filter(PushSubscription.id.in_(ok_ids)).update(
                    {PushSubscription.last_success_at: datetime.utcnow()}, synchronize_session=False)
            db.commit()
        finally:
            db.close()

    if charger_id and event != "test":
        try:
            from . import mqtt_bridge
            await mqtt_bridge.publish_charger_event(charger_id, event, {
                "title": title, "message": body, "connector_id": connector_id,
                "vehicle_id": vehicle_id, "vehicle": full.get("vehicle_name"),
                "energy_kwh": round(full["energy_wh"] / 1000, 3) if full.get("energy_wh") is not None else None,
                "cost": full.get("cost"), "duration_min": full.get("duration_min"),
            })
            result["mqtt"] = True
        except Exception:
            logger.debug("Événement MQTT %s non publié", event, exc_info=True)
    return result


# --------------------------------------------------------------------------
# Aides pour les points d'appel (csms_local, main, queue_watch)
# --------------------------------------------------------------------------


def notify_session_end(txn, reason: str | None = None, interrupted: bool = False, reason_label: str | None = None):
    """Fin de session : « terminée » ou « interrompue » selon la raison."""
    if txn is None or txn.is_external:
        return
    STARTED_NOTIFIED.discard(txn.id)
    duration = None
    if txn.start_time and txn.stop_time:
        duration = round((txn.stop_time - txn.start_time).total_seconds() / 60, 1)
    if reason in INTERRUPT_REASONS:
        interrupted = True
    event = "charge_interrupted" if interrupted else "charge_ended"
    notify(event, txn.charger_id, txn.connector_id, txn.vehicle_id,
           energy_wh=txn.energy_wh, cost=txn.cost, duration_min=duration,
           reason_label=reason_label or STOP_REASON_LABELS.get(reason or "", None))


def notify_charge_started(txn):
    if txn is None or txn.unauthorized or txn.id in STARTED_NOTIFIED:
        return
    STARTED_NOTIFIED.add(txn.id)
    notify("charge_started", txn.charger_id, txn.connector_id, txn.vehicle_id)


def charger_disconnected(charger_id: str):
    """Programme l'alerte « borne hors ligne » si la borne ne revient pas."""
    old = _offline_tasks.pop(charger_id, None)
    if old:
        old.cancel()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _offline_tasks[charger_id] = loop.create_task(_offline_after_delay(charger_id))


async def _offline_after_delay(charger_id: str):
    try:
        await asyncio.sleep(OFFLINE_NOTIFY_DELAY_S)
        from .csms_local import CONNECTED_CHARGERS
        if charger_id in CONNECTED_CHARGERS or charger_id in OFFLINE_NOTIFIED:
            return
        OFFLINE_NOTIFIED.add(charger_id)
        notify("charger_offline", charger_id)
    except asyncio.CancelledError:
        pass
    finally:
        if _offline_tasks.get(charger_id) is asyncio.current_task():
            _offline_tasks.pop(charger_id, None)


def charger_connected(charger_id: str):
    old = _offline_tasks.pop(charger_id, None)
    if old:
        old.cancel()
    if charger_id in OFFLINE_NOTIFIED:
        OFFLINE_NOTIFIED.discard(charger_id)
        notify("charger_online", charger_id)
