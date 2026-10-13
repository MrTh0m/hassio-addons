"""Surveillance de la file de messages de transaction des bornes.

Contexte (incidents des 10/10 et 13/10, voir CHANGELOG) : après une coupure du
serveur pendant une charge (sauvegarde nocturne du Pi, plus de réponse pendant
10 à 15 minutes), la Schneider cesse d'envoyer tous ses messages liés aux
transactions (MeterValues, StartTransaction, StopTransaction) alors que les
Heartbeat et les StatusNotification continuent de passer. Rien ne le signale :
l'énergie reste figée, la charge suivante ne démarre pas ("prête" côté
serveur, rien côté borne). Le seul déblocage connu est un redémarrage logiciel
(Reset Soft), après lequel la borne rejoue toute sa file d'un coup.

Ce module repère ces symptômes sans rien demander à la borne :

- ``meter_silence`` : connecteur en ``Charging`` avec une transaction active,
  mais plus aucun MeterValues reçu depuis un délai nettement supérieur à
  l'intervalle de relevé configuré sur la borne.
- ``start_stalled`` : un RemoteStartTransaction a été accepté, mais aucun
  StartTransaction n'est arrivé après un délai raisonnable alors que le
  connecteur est resté en ``Preparing``.

Un blocage détecté est conservé jusqu'à preuve du contraire (BootNotification,
StartTransaction, StopTransaction ou MeterValues récent), y compris une fois la
charge terminée : la file reste bloquée même quand plus rien ne charge, et
c'est justement la prochaine charge qui échouerait.

Le reset automatique est un réglage administrateur (désactivé par défaut) et
n'est tenté que lorsqu'aucun connecteur de la borne n'est en pleine charge.

Tout est en mémoire (rien n'est persisté, sauf l'heure de démarrage de la borne
dans ``app_settings`` pour que l'uptime survive à un redémarrage du serveur).
"""

import logging
from datetime import datetime, timedelta

from .db import SessionLocal
from .models import AppSetting, Charger, ChargerMode, ConfigurationKey, ConnectorStatus, Transaction

logger = logging.getLogger("queue-watch")

# Réglages (AppSetting).
AUTO_RESET_KEY = "auto_reset_blocked_queue"
BOOT_KEY_PREFIX = "charger_boot_at:"

# Valeurs par défaut des seuils.
DEFAULT_METER_INTERVAL_S = 30
METER_SILENCE_FACTOR = 4          # seuil = 4 x intervalle de relevé...
METER_SILENCE_MIN_S = 120         # ...avec un plancher de 2 minutes
START_STALL_S = 60                # RemoteStart accepté sans StartTransaction
FRESH_METER_S = 90                # un relevé plus récent que ça prouve que la file circule
AUTO_RESET_COOLDOWN_S = 30 * 60   # jamais plus d'un reset automatique / 30 min / borne

# Statuts où un redémarrage de la borne ne coupe aucune charge en cours.
SAFE_RESET_STATUSES = {"Available", "Preparing", "Finishing", "SuspendedEV", "Unavailable"}

REASON_LABELS = {
    "meter_silence": "Plus aucun relevé de compteur reçu pendant une charge",
    "start_stalled": "Démarrage de charge accepté mais jamais confirmé par la borne",
}

SERVER_STARTED = datetime.utcnow()

# --- État en mémoire ---
LAST_METER_RX: dict[tuple, datetime] = {}      # (borne, connecteur) -> dernière réception
METER_STATS: dict[tuple, dict] = {}            # (borne, connecteur) -> compteurs de relevés
REMOTE_START_ACCEPTED: dict[tuple, datetime] = {}
BOOT_AT: dict[str, datetime] = {}              # borne -> dernier démarrage connu
BLOCKED: dict[str, dict] = {}                  # borne -> blocage constaté
LAST_AUTO_RESET: dict[str, datetime] = {}      # borne -> dernier reset automatique


def _utcnow() -> datetime:
    return datetime.utcnow()


# --------------------------------------------------------------------------
# Hooks appelés par csms_local (aucun ne doit jamais lever d'exception)
# --------------------------------------------------------------------------

def note_meter_values(charger_id: str, connector_id: int, transaction_id, timestamps: list) -> bool:
    """Enregistre la réception d'un MeterValues. ``timestamps`` : horodatages
    annoncés par la borne (datetime UTC naïfs). Renvoie True si le message est
    un relevé rejoué (daté de bien avant sa réception) : il ne prouve pas que
    la file circule à nouveau."""
    now = _utcnow()
    key = (charger_id, connector_id)
    freshest = max(timestamps) if timestamps else now
    replayed = (now - freshest).total_seconds() > FRESH_METER_S
    LAST_METER_RX[key] = now
    stats = METER_STATS.setdefault(key, {"count": 0, "replayed": 0, "first_rx": now, "transaction_id": None})
    if stats.get("transaction_id") != transaction_id:
        # Nouvelle transaction : on repart de zéro pour ce connecteur.
        stats.update({"count": 0, "replayed": 0, "first_rx": now, "transaction_id": transaction_id})
    stats["count"] += 1
    if replayed:
        stats["replayed"] += 1
    stats["last_rx"] = now
    if timestamps:
        stats["last_ts"] = freshest
    if not replayed:
        _clear_blocked(charger_id, "relevé de compteur récent reçu")
    return replayed


def note_remote_start(charger_id: str, connector_id: int, accepted: bool) -> None:
    key = (charger_id, connector_id)
    if accepted:
        REMOTE_START_ACCEPTED[key] = _utcnow()
    else:
        REMOTE_START_ACCEPTED.pop(key, None)


def note_start_transaction(charger_id: str, connector_id: int) -> None:
    REMOTE_START_ACCEPTED.pop((charger_id, connector_id), None)
    _clear_blocked(charger_id, "StartTransaction reçu")


def note_stop_transaction(charger_id: str) -> None:
    # Un StopTransaction rejoué après un reset arrive juste après la boot, qui
    # a déjà levé le blocage ; un StopTransaction normal prouve aussi que la
    # file circule.
    _clear_blocked(charger_id, "StopTransaction reçu")


def note_boot(charger_id: str, boot_at: datetime | None = None, from_device: bool = False) -> None:
    """La borne vient de démarrer. ``from_device`` : l'heure vient de la borne
    elle-même (SecurityEventNotification/StartupOfTheDevice), donc fiable ; sinon
    c'est l'heure de réception du BootNotification, qu'on n'écrase pas si un
    démarrage plus précis vient d'être enregistré."""
    now = _utcnow()
    when = min(boot_at, now) if boot_at else now
    previous = BOOT_AT.get(charger_id)
    if from_device or previous is None or (now - previous).total_seconds() > 300:
        BOOT_AT[charger_id] = when
        _persist_boot(charger_id, when)
    if not from_device:
        _clear_blocked(charger_id, "borne redémarrée")
        for key in [k for k in REMOTE_START_ACCEPTED if k[0] == charger_id]:
            REMOTE_START_ACCEPTED.pop(key, None)


def _persist_boot(charger_id: str, when: datetime) -> None:
    db = SessionLocal()
    try:
        key = BOOT_KEY_PREFIX + charger_id
        row = db.query(AppSetting).filter(AppSetting.key == key).first()
        value = when.isoformat()
        if row:
            row.value = value
        else:
            db.add(AppSetting(key=key, value=value))
        db.commit()
    except Exception:
        logger.debug("Enregistrement de l'heure de démarrage de %s impossible", charger_id, exc_info=True)
    finally:
        db.close()


def boot_at(charger_id: str) -> datetime | None:
    """Heure du dernier démarrage connu de la borne (mémoire, sinon base)."""
    if charger_id in BOOT_AT:
        return BOOT_AT[charger_id]
    db = SessionLocal()
    try:
        row = db.query(AppSetting).filter(AppSetting.key == BOOT_KEY_PREFIX + charger_id).first()
        if row and row.value:
            try:
                BOOT_AT[charger_id] = datetime.fromisoformat(row.value)
                return BOOT_AT[charger_id]
            except ValueError:
                return None
    except Exception:
        logger.debug("Lecture de l'heure de démarrage de %s impossible", charger_id, exc_info=True)
    finally:
        db.close()
    return None


def uptime_seconds(charger_id: str) -> int | None:
    boot = boot_at(charger_id)
    if boot is None:
        return None
    return max(0, int((_utcnow() - boot).total_seconds()))


def _clear_blocked(charger_id: str, why: str) -> None:
    if BLOCKED.pop(charger_id, None) is not None:
        logger.info("Borne %s : file de messages à nouveau fluide (%s)", charger_id, why)


# --------------------------------------------------------------------------
# Évaluation
# --------------------------------------------------------------------------

def _meter_interval_s(db, charger_id: str) -> int:
    row = db.query(ConfigurationKey).filter(
        ConfigurationKey.charger_id == charger_id,
        ConfigurationKey.key == "MeterValueSampleInterval",
    ).first()
    try:
        value = int(float(row.value)) if row and row.value is not None else DEFAULT_METER_INTERVAL_S
    except (TypeError, ValueError):
        value = DEFAULT_METER_INTERVAL_S
    return value if value > 0 else DEFAULT_METER_INTERVAL_S


def evaluate(now: datetime | None = None) -> list[dict]:
    """Passe en revue les bornes locales connectées, met à jour BLOCKED et
    renvoie les constats de ce passage (un par borne en blocage)."""
    from .csms_local import CONNECTED_CHARGERS

    now = now or _utcnow()
    findings: dict[str, dict] = {}
    db = SessionLocal()
    try:
        rows = db.query(ConnectorStatus, Charger).join(
            Charger, Charger.id == ConnectorStatus.charger_id
        ).filter(
            ConnectorStatus.connector_id != 0,
            Charger.deleted_at.is_(None),
            Charger.mode == ChargerMode.local,
        ).all()
        for cs, charger in rows:
            if charger.id not in CONNECTED_CHARGERS:
                continue
            key = (charger.id, cs.connector_id)
            active = db.query(Transaction).filter(
                Transaction.charger_id == charger.id,
                Transaction.connector_id == cs.connector_id,
                Transaction.status == "active",
            ).order_by(Transaction.id.desc()).first()

            # 1. Charge en cours sans plus aucun relevé de compteur.
            if cs.status == "Charging" and active is not None:
                threshold = max(METER_SILENCE_MIN_S, METER_SILENCE_FACTOR * _meter_interval_s(db, charger.id))
                reference = max(
                    LAST_METER_RX.get(key, datetime.min),
                    active.charging_since or datetime.min,
                    active.start_time or datetime.min,
                    SERVER_STARTED,
                )
                silent_s = (now - reference).total_seconds()
                if silent_s >= threshold:
                    findings[charger.id] = {
                        "charger_id": charger.id, "connector_id": cs.connector_id,
                        "reason": "meter_silence", "since": reference,
                        "detail": f"aucun MeterValues depuis {int(silent_s // 60)} min {int(silent_s % 60)} s (seuil {threshold} s)",
                    }
                    continue

            # 2. Démarrage à distance accepté, jamais confirmé.
            accepted_at = REMOTE_START_ACCEPTED.get(key)
            if (accepted_at is not None and cs.status == "Preparing" and active is None
                    and (now - accepted_at).total_seconds() >= START_STALL_S):
                findings[charger.id] = {
                    "charger_id": charger.id, "connector_id": cs.connector_id,
                    "reason": "start_stalled", "since": accepted_at,
                    "detail": f"RemoteStartTransaction accepté il y a {int((now - accepted_at).total_seconds())} s, aucun StartTransaction reçu",
                }
    finally:
        db.close()

    for cid, finding in findings.items():
        existing = BLOCKED.get(cid)
        if existing is None:
            logger.warning("Borne %s connecteur %s : file de messages de transaction probablement bloquée (%s)",
                           cid, finding["connector_id"], finding["detail"])
            BLOCKED[cid] = {**finding, "detected_at": now}
        else:
            existing.update({"detail": finding["detail"], "connector_id": finding["connector_id"]})
    # Les blocages précédemment constatés restent (voir docstring du module) :
    # seul un signe de vie les lève, voir _clear_blocked.
    return list(BLOCKED.values())


def blocked_for(charger_id: str) -> dict | None:
    entry = BLOCKED.get(charger_id)
    if entry is None:
        return None
    return {
        "reason": entry["reason"],
        "label": REASON_LABELS.get(entry["reason"], entry["reason"]),
        "connector_id": entry["connector_id"],
        "detail": entry.get("detail"),
        "since": entry.get("detected_at").isoformat() + "Z" if entry.get("detected_at") else None,
    }


# --------------------------------------------------------------------------
# Reset automatique (réglage administrateur)
# --------------------------------------------------------------------------

def auto_reset_enabled(db=None) -> bool:
    own = db is None
    db = db or SessionLocal()
    try:
        row = db.query(AppSetting).filter(AppSetting.key == AUTO_RESET_KEY).first()
        return bool(row and row.value == "true")
    finally:
        if own:
            db.close()


def set_auto_reset(enabled: bool) -> None:
    db = SessionLocal()
    try:
        row = db.query(AppSetting).filter(AppSetting.key == AUTO_RESET_KEY).first()
        value = "true" if enabled else "false"
        if row:
            row.value = value
        else:
            db.add(AppSetting(key=AUTO_RESET_KEY, value=value))
        db.commit()
    finally:
        db.close()


def _charge_in_progress(charger_id: str) -> bool:
    db = SessionLocal()
    try:
        rows = db.query(ConnectorStatus.status).filter(
            ConnectorStatus.charger_id == charger_id, ConnectorStatus.connector_id != 0
        ).all()
        return any(r[0] not in SAFE_RESET_STATUSES for r in rows)
    finally:
        db.close()


async def tick() -> None:
    """Appelé chaque minute par le planificateur : met à jour les constats puis,
    si le réglage est activé, redémarre la borne bloquée quand aucune charge
    n'est en cours (jamais plus d'une fois par AUTO_RESET_COOLDOWN_S)."""
    from .csms_local import CONNECTED_CHARGERS
    from . import ocpp_logs
    from .sse import sse_notify

    evaluate()
    if not BLOCKED or not auto_reset_enabled():
        return
    now = _utcnow()
    for cid, entry in list(BLOCKED.items()):
        cp = CONNECTED_CHARGERS.get(cid)
        if cp is None:
            continue
        last = LAST_AUTO_RESET.get(cid)
        if last is not None and (now - last).total_seconds() < AUTO_RESET_COOLDOWN_S:
            continue
        if _charge_in_progress(cid):
            continue
        LAST_AUTO_RESET[cid] = now
        logger.warning("Borne %s : redémarrage logiciel automatique pour débloquer sa file (%s)", cid, entry["reason"])
        ocpp_logs.record(cid, "out", "Reset.auto", summary=f"reset automatique ({entry['reason']})",
                         payload={"reason": entry["reason"], "detail": entry.get("detail")})
        try:
            await cp.trigger_reset("Soft")
        except Exception:
            logger.warning("Redémarrage automatique de la borne %s échoué", cid, exc_info=True)
        sse_notify("queue_auto_reset", {"charger_id": cid, "reason": entry["reason"]})


def snapshot() -> dict:
    """État complet pour le dump de diagnostic."""
    return {
        "generated_at": _utcnow().isoformat() + "Z",
        "server_started": SERVER_STARTED.isoformat() + "Z",
        "auto_reset_enabled": auto_reset_enabled(),
        "blocked": {cid: blocked_for(cid) for cid in BLOCKED},
        "boot_at": {cid: (d.isoformat() + "Z") for cid, d in BOOT_AT.items()},
        "meter_stats": {
            f"{cid}/{conn}": {
                **{k: (v.isoformat() + "Z" if isinstance(v, datetime) else v) for k, v in stats.items()},
                "last_rx_age_s": int((_utcnow() - LAST_METER_RX[(cid, conn)]).total_seconds()) if (cid, conn) in LAST_METER_RX else None,
            }
            for (cid, conn), stats in METER_STATS.items()
        },
    }
