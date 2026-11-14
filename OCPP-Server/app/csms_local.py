import asyncio
import logging
from datetime import datetime, time as dtime, timedelta, timezone
from dataclasses import dataclass

from ocpp.routing import on
from ocpp.v16 import ChargePoint as ChargePoint16
from ocpp.v16 import call, call_result
from ocpp.v16.enums import (
    Action, RegistrationStatus, AuthorizationStatus, ConfigurationStatus,
    ChargingProfilePurposeType, ChargingProfileKindType, ChargingRateUnitType,
    ClearChargingProfileStatus, ResetType, UpdateType,
)

from .db import SessionLocal
from .models import Charger, ChargerMode, Transaction, MeterValue, ConfigurationKey, ConnectorStatus, Vehicle, VehicleCharger, AppSetting
from .pricing import freeze_transaction_cost, compute_session_cost, resolve_plan_for_charger
from . import mqtt_bridge
from . import ocpp_logs
from . import queue_watch
from .sse import sse_notify

logger = logging.getLogger("csms_local")

CONNECTED_CHARGERS: dict[str, "LocalChargePoint"] = {}
PENDING_REMOTE_STARTS: dict[tuple[str, int], int] = {}
AUTO_START_ATTEMPTED: set[tuple[str, int]] = set()
RESERVED_TAGS = {"WEBADMIN", "MQTT", "SCHED"}
SMART_CHARGING_SUPPORT: dict[str, bool] = {}
# Clés de configuration en attente d'un redémarrage de la borne pour être
# effectivement appliquées (la borne a répondu RebootRequired à un
# ChangeConfiguration). Purement en mémoire, remis à zéro pour une borne
# dès qu'un nouveau BootNotification est reçu (elle vient de redémarrer,
# pour quelque raison que ce soit) ou dès que le serveur redémarre. La date
# est conservée (pas juste le nom de la clé) pour pouvoir signaler dans le
# bandeau de santé une clé en attente depuis anormalement longtemps.
PENDING_REBOOT_KEYS: dict[str, dict[str, datetime]] = {}

# Statuts de connecteur considérés comme "occupés" (véhicule branché, quelle
# que soit l'étape) pour le pilotage automatique de la luminosité. Mêmes
# valeurs que celles utilisées par le planificateur (scheduler.py) pour
# décider si un connecteur est "plugged".
OCCUPIED_CONNECTOR_STATUSES = ("Preparing", "Charging", "SuspendedEV", "SuspendedEVSE", "Finishing")

# --- Relance automatique d'un démarrage avorté ---
# Observé sur la Schneider (dump du 14/11 : 10 cas en un mois, toujours après
# un long repos du véhicule) : quelques secondes après le StartTransaction, la
# borne voit le véhicule comme débranché (connecteur Available puis Preparing
# 2 s plus tard) et clôt la transaction avec reason EVDisconnected, à 0 Wh. La
# tentative lancée juste après fonctionne. Le serveur relance donc lui-même
# la charge, avec le même idTag et le même véhicule, et fusionne la tentative
# avortée dans la suivante pour qu'elles ne forment qu'une seule charge.
ABORTED_START_MAX_S = 30        # durée max d'une tentative avortée (dates de la borne)
ABORTED_START_MAX_WH = 20.0     # énergie max d'une tentative avortée
START_RETRY_MAX = 3             # relances au plus pour une même charge, par défaut
START_RETRY_MAX_LIMIT = 5       # valeur maximale acceptée par le réglage
# Réglage administrateur (Réglages, Avancé) : nombre de relances, 0 = aucune.
START_RETRY_SETTING_KEY = "start_retry_max"
START_RETRY_DELAY_S = 3         # pause avant de relancer
START_RETRY_WAIT_PREPARING_S = 20   # attente du retour du connecteur sur Preparing
START_RETRY_WAIT_START_S = 30   # attente du StartTransaction après la relance
START_RETRY_TTL_S = 120         # au-delà, une nouvelle charge n'est plus fusionnée
# Tentative avortée en attente de fusion, par connecteur :
# {"txn_id": id de la transaction avortée, "at": date de son arrêt}.
START_RETRIES: dict[tuple[str, int], dict] = {}

# --- Borne réglée avec StopTransactionOnEVSideDisconnect = false ---
# La borne garde alors sa transaction ouverte quand le véhicule est vu comme
# débranché. Le serveur ne clôt donc plus la session dès Available/Preparing :
# il attend EV_DISCONNECT_GRACE_S, et si le connecteur est toujours libre il
# demande l'arrêt à la borne (RemoteStopTransaction) puis, sans arrêt reçu
# après EV_DISCONNECT_STOP_WAIT_S, clôt la session lui-même.
EV_DISCONNECT_GRACE_S = 30
EV_DISCONNECT_STOP_WAIT_S = 15
EV_DISCONNECT_PENDING: set[tuple[str, int]] = set()


def start_retry_max(db=None) -> int:
    """Nombre de relances automatiques après un démarrage avorté (réglage
    administrateur, START_RETRY_MAX par défaut). 0 désactive la relance ; une
    relance faite à la main dans les START_RETRY_TTL_S reste fusionnée."""
    own = db is None
    db = db or SessionLocal()
    try:
        row = db.query(AppSetting).filter(AppSetting.key == START_RETRY_SETTING_KEY).first()
        try:
            value = int(row.value) if row and row.value is not None else START_RETRY_MAX
        except ValueError:
            value = START_RETRY_MAX
        return max(0, min(START_RETRY_MAX_LIMIT, value))
    finally:
        if own:
            db.close()


def set_start_retry_max(value: int) -> int:
    value = max(0, min(START_RETRY_MAX_LIMIT, int(value)))
    db = SessionLocal()
    try:
        row = db.query(AppSetting).filter(AppSetting.key == START_RETRY_SETTING_KEY).first()
        if row:
            row.value = str(value)
        else:
            db.add(AppSetting(key=START_RETRY_SETTING_KEY, value=str(value)))
        db.commit()
    finally:
        db.close()
    return value


def _is_aborted_start(txn, reason, now: datetime | None = None) -> bool:
    """Vrai si cette transaction, qui vient d'être arrêtée par la borne, est
    un démarrage avorté (voir ABORTED_START_MAX_S) : arrêt EVDisconnected,
    quelques secondes après le début, sans énergie. Un arrêt rejoué longtemps
    après (file de la borne) n'en est pas un : il n'y a plus rien à relancer."""
    if txn is None or reason != "EVDisconnected":
        return False
    if txn.unauthorized or txn.is_external:
        return False
    if not txn.start_time or not txn.stop_time:
        return False
    if (txn.stop_time - txn.start_time).total_seconds() > ABORTED_START_MAX_S:
        return False
    if txn.meter_start is not None and txn.meter_stop is not None \
            and (txn.meter_stop - txn.meter_start) > ABORTED_START_MAX_WH:
        return False
    now = now or datetime.utcnow()
    return (now - txn.stop_time).total_seconds() <= START_RETRY_TTL_S


def _absorb_aborted_start(db, txn, prev) -> bool:
    """Fusionne la tentative avortée ``prev`` dans la transaction ``txn`` qui
    la relance : ``txn`` reprend le début, le relevé de départ, le véhicule et
    les saisies de ``prev``, ses relevés lui sont rattachés, puis ``prev`` est
    supprimée. L'historique, les statistiques et Home Assistant ne voient
    ainsi qu'une seule charge."""
    if prev is None or txn is None or prev.id == txn.id:
        return False
    if prev.start_time and (txn.start_time is None or prev.start_time < txn.start_time):
        txn.start_time = prev.start_time
    if prev.meter_start is not None and (txn.meter_start is None or prev.meter_start < txn.meter_start):
        txn.meter_start = prev.meter_start
    txn.start_attempts = (prev.start_attempts or 1) + (txn.start_attempts or 1)
    if txn.vehicle_id is None and prev.vehicle_id is not None:
        txn.vehicle_id = prev.vehicle_id
    for field in ("odometer_km", "battery_percent_start"):
        if getattr(txn, field) is None and getattr(prev, field) is not None:
            setattr(txn, field, getattr(prev, field))
    db.query(MeterValue).filter(MeterValue.transaction_id == prev.id).update(
        {MeterValue.transaction_id: txn.id}, synchronize_session=False)
    db.delete(prev)
    return True


def _ev_disconnect_keeps_transaction(db, charger_id: str) -> bool:
    """Vrai si la borne est réglée avec StopTransactionOnEVSideDisconnect =
    false (elle garde sa transaction quand le véhicule est vu débranché)."""
    row = db.query(ConfigurationKey).filter(
        ConfigurationKey.charger_id == charger_id,
        ConfigurationKey.key == "StopTransactionOnEVSideDisconnect",
    ).first()
    return row is not None and (row.value or "").strip().lower() == "false"


def _connector_snapshot(charger_id: str, connector_id: int) -> tuple[str | None, int | None]:
    """Statut du connecteur et id de sa transaction active (None si aucune)."""
    db = SessionLocal()
    try:
        row = db.query(ConnectorStatus).filter(
            ConnectorStatus.charger_id == charger_id,
            ConnectorStatus.connector_id == connector_id,
        ).first()
        txn = db.query(Transaction).filter(
            Transaction.charger_id == charger_id,
            Transaction.connector_id == connector_id,
            Transaction.status == "active",
        ).order_by(Transaction.id.desc()).first()
        return (row.status if row else None), (txn.id if txn else None)
    finally:
        db.close()


def _charger_occupied(db, charger_id: str) -> bool:
    """Vrai si au moins un connecteur (hors pseudo-connecteur 0) de cette
    borne est actuellement occupé. Pris en agrégat sur toute la borne, car
    LightIntensity est une clé OCPP unique pour toute la borne, pas par
    connecteur (voir discussion multi-connecteurs) : sur une borne à
    plusieurs connecteurs, un seul connecteur occupé suffit à considérer la
    borne "en charge" pour ce réglage."""
    rows = db.query(ConnectorStatus.status).filter(
        ConnectorStatus.charger_id == charger_id,
        ConnectorStatus.connector_id != 0,
    ).all()
    return any(r[0] in OCCUPIED_CONNECTOR_STATUSES for r in rows)


def _in_time_window(start: str | None, end: str | None, now: datetime) -> bool:
    """Vrai si l'heure de `now` tombe dans la fenêtre [start, end) au format
    "HH:MM". Gère le passage de minuit (ex. 22:00–06:00). Retourne False si
    start/end sont absents ou mal formés, plutôt que de planter."""
    if not start or not end:
        return False
    try:
        sh, sm = (int(x) for x in start.split(":"))
        eh, em = (int(x) for x in end.split(":"))
        s, e = dtime(sh, sm), dtime(eh, em)
    except (ValueError, AttributeError):
        return False
    t = now.time()
    if s <= e:
        return s <= t < e
    return t >= s or t < e


def compute_light_target(mode, occupied: bool, fixed_value, auto_charge_value,
                         auto_free_value, night_enabled, night_reduction,
                         night_start, night_end, zero_supported, now: datetime):
    """Calcule la valeur LightIntensity (%) que la borne devrait avoir en ce
    moment d'après les réglages de pilotage automatique. Fonction pure (pas
    d'accès base/réseau) pour rester testable sans borne réelle, sur le même
    principe que should_charge_now dans scheduler.py.

    La réduction nocturne est indépendante du mode fixe/auto (voir échange
    avec Thomas) : en mode "fixed", elle s'applique sur la valeur fixe elle-
    même ; en mode "auto", par-dessus la valeur "en charge"/"libre" courante.

    En mode fixe, tant qu'une valeur fixe est configurée, la fonction renvoie
    toujours quelque chose (la valeur pleine hors fenêtre nocturne, réduite
    pendant), même si night_enabled est désactivé (dans ce cas la réduction ne
    s'applique simplement jamais, mais la valeur pleine est quand même
    renvoyée). Ça permet à apply_light_intensity() de se resynchroniser tout
    seul si la valeur réellement appliquée sur la borne a dérivé (ex. remise
    à la valeur pleine juste après la fin de la fenêtre nocturne), sans jamais
    solliciter la borne inutilement puisque apply_light_intensity() ne pousse
    que si la cible diffère de la valeur déjà en cache.

    Retourne None uniquement quand rien n'est calculable : mode "fixed" sans
    aucune valeur fixe connue, ou mode "auto" sans les deux valeurs
    configurées."""
    if mode == "auto":
        if auto_charge_value is None or auto_free_value is None:
            return None
        base = auto_charge_value if occupied else auto_free_value
    else:
        if fixed_value is None:
            return None
        base = fixed_value
    if night_enabled and night_reduction and _in_time_window(night_start, night_end, now):
        floor = 0 if zero_supported else 1
        return max(floor, base - night_reduction)
    return base


def _auth_mode_value(charger) -> str:
    mode = getattr(charger, "auth_mode", None) if charger is not None else None
    return mode.value if mode is not None else "free"


def _tag_is_authorized(db, charger, id_tag) -> bool:
    if _auth_mode_value(charger) == "free":
        return True
    if not id_tag:
        return False
    if id_tag in RESERVED_TAGS or id_tag.startswith("REMOTE-"):
        return True
    return db.query(Vehicle).filter(Vehicle.id_tag == id_tag).first() is not None


def _authorized_id_tags_for_charger(db, charger_id: str) -> list[str]:
    """Tags à pousser dans la liste d'autorisation locale (SendLocalList) de
    cette borne : les idTag des véhicules actifs qui lui sont associés
    (VehicleCharger), plus les tags internes que le serveur utilise lui-même
    (RESERVED_TAGS). Les tags "REMOTE-*" ne sont volontairement pas inclus :
    ils ne sont jamais présentés localement à la borne, uniquement envoyés par
    le serveur via RemoteStartTransaction, déjà acceptés à ce titre."""
    rows = db.query(Vehicle).join(
        VehicleCharger, VehicleCharger.vehicle_id == Vehicle.id
    ).filter(
        VehicleCharger.charger_id == charger_id,
        Vehicle.deleted_at.is_(None),
        Vehicle.id_tag.isnot(None),
    ).all()
    tags = {v.id_tag for v in rows}
    tags.update(RESERVED_TAGS)
    return sorted(tags)


def now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _ocpp_ts(value) -> datetime:
    """Convertit un horodatage OCPP (ISO 8601, en général suffixé par Z) en
    datetime UTC naïf, comme tout le reste de la base. Utilisé pour les
    champs `timestamp` de StartTransaction/StopTransaction : une borne qui
    rejoue sa file de messages après une coupure (observé le 03/10 : un
    StopTransaction envoyé 5 jours après l'arrêt réel) donne ainsi la vraie
    date de l'événement, pas l'heure de réception.

    Retombe sur l'heure actuelle si la valeur est absente ou illisible, et
    borne le résultat à l'heure de réception pour qu'une horloge de borne en
    avance ne crée jamais de date future."""
    now = datetime.utcnow()
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return now
    elif isinstance(value, datetime):
        parsed = value
    else:
        return now
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return min(parsed, now)


@dataclass
class _EmptyResponse:
    """Réponse vide ({}), pour les actions hors du profil OCPP 1.6 standard
    pour lesquelles la bibliothèque n'a pas de classe call_result."""


def _last_register_wh(db, charger_id: str, connector_id: int, transaction_id: int, fallback):
    """Dernier relevé d'énergie reçu pour cette transaction (à défaut
    ``fallback``, en général meter_start). Sert de meter_stop provisoire quand
    le serveur clôt lui-même une transaction faute de StopTransaction : mieux
    que meter_stop = meter_start, qui effaçait toute l'énergie mesurée."""
    last_reg = db.query(MeterValue).filter(
        MeterValue.transaction_id == transaction_id,
        MeterValue.charger_id == charger_id,
        MeterValue.connector_id == connector_id,
        MeterValue.measurand == "Energy.Active.Import.Register",
    ).order_by(MeterValue.timestamp.desc()).first()
    return last_reg.value if last_reg is not None else fallback


# Délai de grâce entre le passage d'un connecteur sur "Finishing" et la
# clôture côté serveur d'une transaction toujours "active". En temps normal la
# borne envoie son StopTransaction quelques secondes avant/après Finishing
# (observé : 2 à 5 s). Observé le 04/10 sur la Schneider : un arrêt à distance
# demandé pendant un état SuspendedEV fait passer le connecteur en Finishing
# (RemoteStopTransaction accepté) mais AUCUN StopTransaction n'est envoyé, la
# transaction restait donc "active" indéfiniment (Fin vide dans l'historique,
# puissance du dernier relevé comptée dans l'occupation de l'abonnement).
FINISHING_STOP_GRACE_S = 45


async def finalize_orphan_transaction(charger_id: str, connector_id: int, stop_time: datetime | None = None,
                                      expected_statuses: tuple = ("Finishing",), reason: str = "finishing",
                                      only_txn_id: int | None = None) -> bool:
    """Clôt côté serveur la transaction active d'un connecteur resté sur
    "Finishing" sans StopTransaction reçu. En OCPP 1.6, "Finishing" signifie
    que la session de charge est terminée, donc la transaction ne peut plus
    être légitimement active.

    Sert aussi (expected_statuses, reason, only_txn_id) à clore la session
    d'un connecteur resté libre quand la borne garde sa transaction ouverte
    (voir EV_DISCONNECT_GRACE_S).

    Ne fait rien (retourne False) si le connecteur n'est plus sur Finishing ou
    s'il n'y a pas de transaction active. meter_stop est le dernier relevé
    d'énergie reçu pour cette transaction (à défaut meter_start). Si la borne
    envoie finalement son StopTransaction plus tard, on_stop_transaction
    retrouve la transaction par son id (même déjà terminée) et remplace
    meter_stop/stop_time par les valeurs réelles de la borne."""
    db = SessionLocal()
    duration_min = None
    last_energy_wh = None
    last_cost = None
    last_vehicle_id = None
    txn_id = None
    try:
        row = db.query(ConnectorStatus).filter(
            ConnectorStatus.charger_id == charger_id,
            ConnectorStatus.connector_id == connector_id,
        ).first()
        if row is None or row.status not in expected_statuses:
            return False
        txn = db.query(Transaction).filter(
            Transaction.charger_id == charger_id,
            Transaction.connector_id == connector_id,
            Transaction.status == "active",
        ).order_by(Transaction.id.desc()).first()
        if txn is None or (only_txn_id is not None and txn.id != only_txn_id):
            return False

        end = stop_time or datetime.utcnow()
        if txn.start_time and end < txn.start_time:
            end = txn.start_time

        if txn.meter_stop is None:
            txn.meter_stop = _last_register_wh(db, charger_id, connector_id, txn.id, txn.meter_start)

        txn.stop_time = end
        txn.status = "completed"
        txn.close_reason = reason
        txn.deferred_until = None
        if txn.charging_since:
            txn.charging_seconds = (txn.charging_seconds or 0.0) + max(0.0, (end - txn.charging_since).total_seconds())
            txn.charging_since = None
        if txn.start_time:
            duration_min = round((end - txn.start_time).total_seconds() / 60, 1)
        db.flush()
        freeze_transaction_cost(db, txn)
        db.commit()
        txn_id = txn.id
        last_energy_wh = txn.energy_wh
        last_cost = txn.cost
        last_vehicle_id = txn.vehicle_id
    finally:
        db.close()

    logger.info("Borne %s connecteur %s : transaction %s clôturée côté serveur (%s sans StopTransaction)",
                charger_id, connector_id, txn_id, "/".join(expected_statuses))

    await mqtt_bridge.publish_charge_control_state(charger_id, connector_id, False)
    updates = {}
    if duration_min is not None:
        updates["session_duration_min"] = duration_min
    if last_energy_wh is not None:
        updates["last_session_energy_wh"] = last_energy_wh
    if last_cost is not None:
        updates["last_session_cost"] = last_cost
    if updates:
        await mqtt_bridge.publish_connector_state(charger_id, connector_id, **updates)
    if last_vehicle_id:
        await mqtt_bridge.publish_vehicle_session_idle(last_vehicle_id)
        await mqtt_bridge.publish_vehicle_last_charge(last_vehicle_id)
    sse_notify("transaction_stopped", {
        "charger_id": charger_id,
        "connector_id": connector_id,
        "transaction_id": txn_id,
    })
    return True


class LocalChargePoint(ChargePoint16):

    def _db(self):
        return SessionLocal()

    def _get_or_create_charger(self, db):
        charger = db.query(Charger).filter(Charger.id == self.id).first()
        if not charger:
            charger = Charger(id=self.id, mode=ChargerMode.local)
            db.add(charger)
        elif charger.deleted_at is not None:
            # La borne avait été désactivée (suppression logique) mais se
            # reconnecte physiquement : la connexion fait foi, réactivation
            # automatique, pas besoin d'action manuelle.
            charger.deleted_at = None
            logger.info("Borne %s : reconnexion détectée, réactivation automatique (était désactivée)", self.id)
        return charger

    @on(Action.boot_notification)
    async def on_boot_notification(self, charge_point_vendor, charge_point_model, **kwargs):
        ocpp_logs.record(self.id, "in", "BootNotification",
                         summary=f"{charge_point_vendor} {charge_point_model}",
                         payload={"vendor": charge_point_vendor, "model": charge_point_model, **kwargs})
        # La borne vient de (re)démarrer : tout changement de configuration qui
        # attendait un reboot est désormais supposé appliqué.
        PENDING_REBOOT_KEYS.pop(self.id, None)
        queue_watch.note_boot(self.id)
        db = self._db()
        try:
            charger = self._get_or_create_charger(db)
            charger.vendor = charge_point_vendor
            charger.model = charge_point_model
            charger.serial = kwargs.get("charge_point_serial_number")
            charger.ocpp_version = "1.6"
            charger.last_seen = datetime.utcnow()
            db.commit()
        finally:
            db.close()

        asyncio.create_task(self._detect_smart_charging())
        # Reconnexion = bon moment pour resynchroniser la luminosité en mode
        # auto (le réglage aurait pu dériver pendant la coupure), et pour
        # repousser la liste d'autorisation locale (la borne redémarre sa
        # mémoire de cette liste à chaque reboot selon certains firmwares).
        asyncio.create_task(self.apply_light_intensity())
        asyncio.create_task(sync_local_auth_list(self.id))
        # SSE : la borne vient de (re)connecter
        sse_notify("charger_connected", {"charger_id": self.id})

        return call_result.BootNotification(
            current_time=now_iso(),
            interval=300,
            status=RegistrationStatus.accepted,
        )

    async def _detect_smart_charging(self):
        try:
            ocpp_logs.record(self.id, "out", "GetConfiguration",
                             summary="SupportedFeatureProfiles", payload={"key": ["SupportedFeatureProfiles"]})
            resp = await self.call(call.GetConfiguration(key=["SupportedFeatureProfiles"]))
            for item in getattr(resp, "configuration_key", None) or []:
                if item.get("key") == "SupportedFeatureProfiles":
                    val = (item.get("value") or "").lower()
                    supported = "smartcharging" in val
                    SMART_CHARGING_SUPPORT[self.id] = supported
                    # Mise à jour en base
                    db = self._db()
                    try:
                        charger = db.query(Charger).filter(Charger.id == self.id).first()
                        if charger:
                            charger.smart_charging = supported
                            db.commit()
                    finally:
                        db.close()
                    logger.info("Borne %s : SmartCharging %s", self.id,
                                "supporté" if supported else "non supporté")
                    return
        except Exception:
            logger.debug("Détection SmartCharging impossible pour %s", self.id, exc_info=True)

    @on(Action.heartbeat)
    async def on_heartbeat(self, **kwargs):
        ocpp_logs.record(self.id, "in", "Heartbeat")
        return call_result.Heartbeat(current_time=now_iso())

    @on("SecurityEventNotification", skip_schema_validation=True)
    async def on_security_event_notification(self, **kwargs):
        """Action de l'extension sécurité d'OCPP 1.6 (hors profil standard),
        envoyée par la borne au démarrage (StartupOfTheDevice,
        SettingSystemTime...). On se contente d'accuser réception : sans ce
        handler, la bibliothèque répondait NotImplemented et loguait une
        erreur à chaque démarrage de la borne. skip_schema_validation car la
        bibliothèque n'embarque pas de schéma pour cette action en 1.6."""
        ocpp_logs.record(self.id, "in", "SecurityEventNotification",
                         summary=str(kwargs.get("type") or ""),
                         payload={"type": kwargs.get("type"),
                                  "timestamp": kwargs.get("timestamp"),
                                  "techInfo": kwargs.get("tech_info")})
        if kwargs.get("type") == "StartupOfTheDevice":
            # Heure réelle de démarrage de la borne (sert à l'uptime affiché).
            queue_watch.note_boot(self.id, _ocpp_ts(kwargs.get("timestamp")), from_device=True)
        return _EmptyResponse()

    @on(Action.authorize)
    async def on_authorize(self, id_tag, **kwargs):
        ocpp_logs.record(self.id, "in", "Authorize", summary=f"idTag={id_tag}",
                         payload={"idTag": id_tag})
        db = self._db()
        try:
            charger = db.query(Charger).filter(Charger.id == self.id).first()
            allowed = _tag_is_authorized(db, charger, id_tag)
        finally:
            db.close()
        status = AuthorizationStatus.accepted if allowed else AuthorizationStatus.blocked
        return call_result.Authorize(id_tag_info={"status": status})

    @on(Action.status_notification)
    async def on_status_notification(self, connector_id, status, **kwargs):
        ocpp_logs.record(self.id, "in", "StatusNotification",
                         summary=f"conn {connector_id} -> {status}",
                         payload={"connectorId": connector_id, "status": status, **kwargs},
                         connector_id=connector_id)
        db = self._db()
        closed_duration_min = None
        last_energy_wh = None
        last_cost = None
        last_vehicle_id = None
        do_auto_start = False
        check_finishing = False
        ev_disconnect_txn_id = None
        try:
            charger = self._get_or_create_charger(db)
            charger.last_seen = datetime.utcnow()

            entry = db.query(ConnectorStatus).filter(
                ConnectorStatus.charger_id == self.id,
                ConnectorStatus.connector_id == connector_id,
            ).first()
            if not entry:
                entry = ConnectorStatus(charger_id=self.id, connector_id=connector_id)
                db.add(entry)
            old_status = entry.status
            entry.status = status
            entry.error_code = kwargs.get("error_code")
            entry.updated_at = datetime.utcnow()

            # Première arrivée sur Finishing : on vérifiera dans un instant que
            # la borne a bien envoyé son StopTransaction (voir
            # FINISHING_STOP_GRACE_S), sinon on clôt la transaction nous-mêmes.
            check_finishing = connector_id != 0 and status == "Finishing" and old_status != "Finishing"

            if connector_id == 0:
                charger.status = status

            # Suivi du temps de charge EFFECTIF (voir commentaire sur
            # Transaction.charging_seconds) : uniquement sur une vraie
            # transition touchant "Charging", jamais sur un renvoi redondant du
            # même statut (une borne peut resignaler périodiquement le même
            # statut, ce qui ne doit pas réinitialiser charging_since).
            if connector_id != 0 and status != old_status:
                active_txn = db.query(Transaction).filter(
                    Transaction.charger_id == self.id,
                    Transaction.connector_id == connector_id,
                    Transaction.status == "active",
                ).order_by(Transaction.id.desc()).first()
                if active_txn:
                    now = datetime.utcnow()
                    if status == "Charging":
                        active_txn.charging_since = now
                    elif old_status == "Charging" and active_txn.charging_since:
                        active_txn.charging_seconds = (active_txn.charging_seconds or 0.0) + (now - active_txn.charging_since).total_seconds()
                        active_txn.charging_since = None

            # "Preparing" ne peut physiquement survenir que depuis un
            # connecteur déjà libre (câble qu'on vient de brancher sur un
            # connecteur au repos) : au même titre qu'"Available", c'est donc
            # un signal fiable qu'une transaction encore marquée "active" ici
            # est en réalité obsolète (ex. StopTransaction perdu lors d'une
            # coupure réseau). Sans ce filet, une transaction fantôme peut
            # rester ouverte des heures (observé en prod : 37h), et surtout
            # bloque à tort la vérification has_active ci-dessous, empêchant
            # le démarrage automatique d'une VRAIE nouvelle charge.
            if status in ("Available", "Preparing") and connector_id != 0:
                stale = db.query(Transaction).filter(
                    Transaction.charger_id == self.id,
                    Transaction.connector_id == connector_id,
                    Transaction.status == "active",
                ).first()
                if stale and _ev_disconnect_keeps_transaction(db, self.id):
                    # La borne garde sa transaction ouverte malgré le véhicule
                    # vu débranché : on ne clôt rien tout de suite (voir
                    # EV_DISCONNECT_GRACE_S), la charge peut reprendre.
                    if (self.id, connector_id) not in EV_DISCONNECT_PENDING:
                        EV_DISCONNECT_PENDING.add((self.id, connector_id))
                        ev_disconnect_txn_id = stale.id
                    stale = None
                if stale:
                    stale.stop_time = datetime.utcnow()
                    if stale.meter_stop is None:
                        stale.meter_stop = _last_register_wh(db, self.id, connector_id, stale.id, stale.meter_start)
                    stale.status = "completed"
                    stale.close_reason = "reconciled"
                    if stale.start_time:
                        closed_duration_min = round((stale.stop_time - stale.start_time).total_seconds() / 60, 1)
                    db.flush()
                    freeze_transaction_cost(db, stale)
                    # Démarrage avorté (voir ABORTED_START_MAX_S) : le
                    # StopTransaction de la borne suit dans quelques secondes
                    # et la charge va être relancée. On ne publie donc pas
                    # cette tentative à 0 Wh comme « dernière charge ».
                    blip = (
                        not stale.unauthorized
                        and stale.start_time is not None
                        and (stale.stop_time - stale.start_time).total_seconds() <= ABORTED_START_MAX_S
                        and (stale.energy_wh or 0.0) <= ABORTED_START_MAX_WH
                    )
                    if blip:
                        closed_duration_min = None
                    else:
                        last_energy_wh = stale.energy_wh
                        last_cost = stale.cost
                        last_vehicle_id = stale.vehicle_id

            if connector_id != 0:
                if status == "Available":
                    AUTO_START_ATTEMPTED.discard((self.id, connector_id))
                elif (
                    status == "Preparing"
                    and _auth_mode_value(charger) == "free"
                    and (self.id, connector_id) not in AUTO_START_ATTEMPTED
                ):
                    has_active = db.query(Transaction).filter(
                        Transaction.charger_id == self.id,
                        Transaction.connector_id == connector_id,
                        Transaction.status == "active",
                    ).first() is not None
                    from .scheduler import connector_should_charge_now
                    allowed_now = connector_should_charge_now(db, charger, connector_id)
                    if not has_active and allowed_now:
                        AUTO_START_ATTEMPTED.add((self.id, connector_id))
                        do_auto_start = True

            mode_value = charger.mode.value
            display_name = charger.display_name
            # Pilotage automatique de la luminosité : on ne déclenche un
            # recalcul que si l'occupation GLOBALE de la borne (au moins un
            # connecteur occupé, cf. _charger_occupied) vient de basculer, pas
            # à chaque StatusNotification (ex. Preparing -> Charging reste
            # "occupé" des deux côtés, aucune raison de repousser une valeur).
            light_transition = False
            if connector_id != 0:
                other_rows = db.query(ConnectorStatus.status).filter(
                    ConnectorStatus.charger_id == self.id,
                    ConnectorStatus.connector_id != 0,
                    ConnectorStatus.connector_id != connector_id,
                ).all()
                other_occupied = any(r[0] in OCCUPIED_CONNECTOR_STATUSES for r in other_rows)
                occupied_before = other_occupied or (old_status in OCCUPIED_CONNECTOR_STATUSES)
                occupied_after = other_occupied or (status in OCCUPIED_CONNECTOR_STATUSES)
                light_transition = occupied_before != occupied_after
            db.commit()
        finally:
            db.close()

        # SSE : changement de statut d'un connecteur
        sse_notify("connector_status", {
            "charger_id": self.id,
            "connector_id": connector_id,
            "status": status,
        })

        if connector_id == 0:
            await mqtt_bridge.publish_state(self.id, status=status)
        else:
            await mqtt_bridge.publish_connector_discovery(self.id, connector_id, mode_value, display_name)
            await mqtt_bridge.publish_connector_state(self.id, connector_id, status=status)
            await mqtt_bridge.publish_charge_control_state(self.id, connector_id, status == "Charging")
            closed_updates = {}
            if closed_duration_min is not None:
                closed_updates["session_duration_min"] = closed_duration_min
            if last_energy_wh is not None:
                closed_updates["last_session_energy_wh"] = last_energy_wh
            if last_cost is not None:
                closed_updates["last_session_cost"] = last_cost
            if closed_updates:
                await mqtt_bridge.publish_connector_state(self.id, connector_id, **closed_updates)
            if status == "Available":
                # Connecteur libre : plus aucune session en cours à afficher
                # (voir mqtt_bridge.publish_connector_session_idle).
                await mqtt_bridge.publish_connector_session_idle(self.id, connector_id)
            # Select « véhicule » : créé avec le connecteur, et remis sur le
            # choix de l'utilisateur quand une session vient de se terminer.
            await mqtt_bridge.publish_vehicle_select(self.id, connector_id)
            if last_vehicle_id:
                # Véhicule = appareil MQTT indépendant de la borne (voir
                # publish_vehicle_discovery) : sa « dernière charge » doit être
                # mise à jour ici aussi, même quand la transaction se termine
                # par ce chemin (connecteur redevenu Available sans
                # StopTransaction explicite) plutôt que via on_stop_transaction.
                await mqtt_bridge.publish_vehicle_session_idle(last_vehicle_id)
                await mqtt_bridge.publish_vehicle_last_charge(last_vehicle_id)

        if do_auto_start:
            asyncio.create_task(self._auto_start(connector_id))

        if check_finishing:
            asyncio.create_task(self._close_if_no_stop_after_finishing(connector_id, datetime.utcnow()))

        if ev_disconnect_txn_id is not None:
            asyncio.create_task(self._reconcile_after_ev_disconnect(connector_id, ev_disconnect_txn_id))

        if light_transition:
            asyncio.create_task(self.apply_light_intensity())

        return call_result.StatusNotification()

    @on(Action.start_transaction)
    async def on_start_transaction(self, connector_id, id_tag, meter_start, **kwargs):
        ocpp_logs.record(self.id, "in", "StartTransaction",
                         summary=f"conn {connector_id}, idTag={id_tag}",
                         payload={"connectorId": connector_id, "idTag": id_tag, "meterStart": meter_start, **kwargs},
                         connector_id=connector_id)
        queue_watch.note_start_transaction(self.id, connector_id)
        db = self._db()
        deferred_label = None
        vehicle_id_for_mqtt = None
        charger_label = None
        try:
            charger = db.query(Charger).filter(Charger.id == self.id).first()
            charger_label = (charger.display_name if charger else None) or self.id
            pending_vehicle_id = PENDING_REMOTE_STARTS.pop((self.id, connector_id), None)

            # Non autorisé ne veut plus dire "ignoré" : voir l'incident du
            # 26/09, où une borne a démarré une charge locale (idTag par
            # défaut) après un Blocked du serveur, sans qu'aucune trace ne le
            # signale nulle part. Le serveur continue de répondre Blocked (il
            # ne ment pas sur l'autorisation), mais crée et suit quand même la
            # transaction comme n'importe quelle autre, avec un identifiant
            # réel (jamais 0) pour que les MeterValues/StopTransaction qui
            # suivront s'y rattachent normalement plutôt que de rester
            # orphelins.
            unauthorized = not _tag_is_authorized(db, charger, id_tag)

            vehicle = db.query(Vehicle).filter(Vehicle.id_tag == id_tag).first() if id_tag else None
            if vehicle is None and pending_vehicle_id is not None:
                vehicle = db.query(Vehicle).filter(Vehicle.id == pending_vehicle_id).first()
            vehicle_id_for_mqtt = vehicle.id if vehicle else None

            # Départ différé : on vérifie si la charge peut commencer maintenant
            suspend_now = False
            if charger and charger.mode == ChargerMode.local:
                from .scheduler import connector_should_charge_now, _get_active_conditions
                suspend_now = not connector_should_charge_now(db, charger, connector_id)
                if suspend_now:
                    # Trouver le libellé de la condition pour l'affichage UI
                    conds = _get_active_conditions(db, charger, connector_id)
                    for cond in conds:
                        if cond.type.value == "start_after" and cond.time_value:
                            deferred_label = f"Démarrage après {cond.time_value}"
                            break
                        elif cond.type.value == "off_peak":
                            deferred_label = "Heures creuses"
                            break
                    if not deferred_label:
                        deferred_label = "Charge différée"

            txn = Transaction(
                charger_id=self.id,
                connector_id=connector_id,
                id_tag=id_tag,
                vehicle_id=vehicle.id if vehicle else None,
                meter_start=meter_start,
                # Date réelle du démarrage annoncée par la borne (et non
                # l'heure de réception, décalée si la borne rejoue sa file).
                start_time=_ocpp_ts(kwargs.get("timestamp")),
                status="active",
                unauthorized=unauthorized,
                deferred_until=deferred_label if suspend_now else None,
            )
            db.add(txn)
            db.flush()
            # Relance d'un démarrage avorté (automatique ou manuelle) : la
            # tentative précédente est fusionnée dans cette transaction.
            retry_ctx = START_RETRIES.pop((self.id, connector_id), None)
            if retry_ctx and not unauthorized \
                    and (datetime.utcnow() - retry_ctx["at"]).total_seconds() <= START_RETRY_TTL_S:
                prev = db.query(Transaction).filter(
                    Transaction.id == retry_ctx["txn_id"],
                    Transaction.charger_id == self.id,
                    Transaction.connector_id == connector_id,
                    Transaction.status == "completed",
                ).first()
                if _absorb_aborted_start(db, txn, prev):
                    vehicle_id_for_mqtt = txn.vehicle_id
                    logger.info("Borne %s connecteur %s : démarrage avorté (transaction %s) fusionné dans la transaction %s, %d tentatives",
                                self.id, connector_id, retry_ctx["txn_id"], txn.id, txn.start_attempts)
            db.commit()
            db.refresh(txn)
            txn_id = txn.id
        finally:
            db.close()

        await mqtt_bridge.publish_charge_control_state(self.id, connector_id, True)
        # Remise à zéro explicite des compteurs « de session » : sans ça, les
        # anciennes valeurs de la session précédente resteraient affichées
        # (retain=True) jusqu'au premier MeterValues de cette nouvelle charge.
        await mqtt_bridge.publish_connector_state(
            self.id, connector_id,
            session_start_time=now_iso(),
            session_energy_wh=0,
            session_cost=0,
            session_duration_min=0,
        )
        if vehicle_id_for_mqtt:
            # Véhicule = appareil MQTT indépendant de la borne : la même
            # information de « session en cours » y est reflétée, indépendamment
            # d'où il est physiquement branché (utile s'il change de borne).
            await mqtt_bridge.publish_vehicle_state(
                vehicle_id_for_mqtt,
                is_charging="ON",
                charging_at=f"{charger_label} / Connecteur {connector_id}",
                session_start_time=now_iso(),
                session_energy_wh=0,
                session_cost=0,
                session_duration_min=0,
            )

        # Pendant la session, le select « véhicule » affiche celui de la session.
        await mqtt_bridge.publish_vehicle_select(self.id, connector_id)

        if suspend_now:
            asyncio.create_task(self._suspend_for_schedule(connector_id))

        # SSE : nouvelle transaction (le champ unauthorized permet à l'UI de
        # la signaler distinctement, ex. bandeau ou badge, sans attendre le
        # prochain calcul de compute_health)
        sse_notify("transaction_started", {
            "charger_id": self.id,
            "connector_id": connector_id,
            "transaction_id": txn_id,
            "deferred": suspend_now,
            "unauthorized": unauthorized,
        })

        status_out = AuthorizationStatus.blocked if unauthorized else AuthorizationStatus.accepted
        ocpp_logs.record(self.id, "out", "StartTransaction.conf",
                         summary=f"conn {connector_id} -> {status_out.value} (txn {txn_id}, idTag={id_tag})",
                         payload={"connectorId": connector_id, "status": status_out.value, "transactionId": txn_id},
                         connector_id=connector_id)
        return call_result.StartTransaction(
            transaction_id=txn_id,
            id_tag_info={"status": status_out},
        )

    async def _close_if_no_stop_after_finishing(self, connector_id: int, finishing_at: datetime):
        try:
            await asyncio.sleep(FINISHING_STOP_GRACE_S)
            await finalize_orphan_transaction(self.id, connector_id, stop_time=finishing_at)
        except Exception:
            logger.debug("Clôture serveur après Finishing échouée sur %s/%s", self.id, connector_id, exc_info=True)

    async def _reconcile_after_ev_disconnect(self, connector_id: int, txn_id: int):
        """Borne réglée avec StopTransactionOnEVSideDisconnect = false : le
        connecteur est repassé sur Available/Preparing alors que la
        transaction reste ouverte. Si la charge n'a pas repris après
        EV_DISCONNECT_GRACE_S, on demande l'arrêt à la borne, puis on clôt la
        session nous-mêmes si elle n'envoie pas son StopTransaction."""
        key = (self.id, connector_id)
        try:
            await asyncio.sleep(EV_DISCONNECT_GRACE_S)
            status, active_id = _connector_snapshot(self.id, connector_id)
            if active_id != txn_id or status not in ("Available", "Preparing"):
                return  # la charge a repris, ou la borne a arrêté d'elle-même
            logger.info("Borne %s connecteur %s : toujours %s %d s après la déconnexion du véhicule, arrêt de la transaction %s demandé",
                        self.id, connector_id, status, EV_DISCONNECT_GRACE_S, txn_id)
            cp = CONNECTED_CHARGERS.get(self.id) or self
            try:
                await cp.trigger_remote_stop(txn_id)
            except Exception:
                logger.debug("RemoteStop après déconnexion véhicule échoué sur %s/%s", self.id, connector_id, exc_info=True)
            await asyncio.sleep(EV_DISCONNECT_STOP_WAIT_S)
            await finalize_orphan_transaction(
                self.id, connector_id,
                expected_statuses=("Available", "Preparing"), reason="reconciled", only_txn_id=txn_id)
        except Exception:
            logger.warning("Clôture après déconnexion véhicule échouée sur %s/%s", self.id, connector_id, exc_info=True)
        finally:
            EV_DISCONNECT_PENDING.discard(key)

    async def _retry_aborted_start(self, connector_id: int, retry: dict):
        """Relance une charge dont le démarrage vient d'avorter (voir
        ABORTED_START_MAX_S), avec le même idTag et le même véhicule. La
        fusion des deux tentatives est faite par on_start_transaction."""
        key = (self.id, connector_id)
        txn_id = retry["txn_id"]

        def _superseded() -> bool:
            ctx = START_RETRIES.get(key)
            return not ctx or ctx.get("txn_id") != txn_id

        try:
            await asyncio.sleep(START_RETRY_DELAY_S)
            waited = 0
            while True:
                if _superseded():
                    return  # déjà relancée (à la main ou démarrage automatique) et fusionnée
                status, active_id = _connector_snapshot(self.id, connector_id)
                if active_id is not None:
                    START_RETRIES.pop(key, None)
                    return
                if status == "Preparing":
                    break
                if waited >= START_RETRY_WAIT_PREPARING_S:
                    await self._give_up_retry(connector_id, retry, f"connecteur resté sur {status}")
                    return
                await asyncio.sleep(1)
                waited += 1

            cp = CONNECTED_CHARGERS.get(self.id) or self
            if retry.get("vehicle_id"):
                PENDING_REMOTE_STARTS[key] = retry["vehicle_id"]
            logger.info("Borne %s connecteur %s : démarrage avorté (transaction %s, EVDisconnected), relance automatique %d/%d",
                        self.id, connector_id, txn_id, retry["attempt"], retry["max"])
            ocpp_logs.record(self.id, "out", "RemoteStartTransaction.retry",
                             summary=f"conn {connector_id}, relance {retry['attempt']}/{retry['max']} après démarrage avorté (txn {txn_id})",
                             payload={"connectorId": connector_id, "idTag": retry["id_tag"],
                                      "abortedTransactionId": txn_id, "attempt": retry["attempt"]},
                             connector_id=connector_id)
            resp = await cp.trigger_remote_start(connector_id, retry["id_tag"] or "WEBADMIN")
            if getattr(resp.status, "value", resp.status) != "Accepted":
                PENDING_REMOTE_STARTS.pop(key, None)
                await self._give_up_retry(connector_id, retry, "relance refusée par la borne")
                return

            waited = 0
            while waited < START_RETRY_WAIT_START_S:
                if _superseded():
                    return
                await asyncio.sleep(1)
                waited += 1
            await self._give_up_retry(connector_id, retry, "aucun StartTransaction après la relance")
        except Exception:
            logger.warning("Relance après démarrage avorté échouée sur %s/%s", self.id, connector_id, exc_info=True)
            try:
                await self._give_up_retry(connector_id, retry, "erreur pendant la relance")
            except Exception:
                logger.debug("Abandon de la relance échoué sur %s/%s", self.id, connector_id, exc_info=True)

    async def _give_up_retry(self, connector_id: int, retry: dict, why: str):
        """La relance n'a pas abouti : la tentative avortée reste dans
        l'historique et est publiée comme une fin de session ordinaire (ces
        publications avaient été retenues dans l'attente de la relance). Le
        contexte de fusion est conservé jusqu'à START_RETRY_TTL_S : une
        relance manuelle dans ce délai sera encore fusionnée."""
        logger.warning("Borne %s connecteur %s : relance après démarrage avorté abandonnée (%s)",
                       self.id, connector_id, why)
        db = self._db()
        updates = {}
        vehicle_id = None
        try:
            txn = db.query(Transaction).filter(Transaction.id == retry["txn_id"]).first()
            if txn is not None:
                vehicle_id = txn.vehicle_id
                if txn.start_time and txn.stop_time:
                    updates["session_duration_min"] = round((txn.stop_time - txn.start_time).total_seconds() / 60, 1)
                if txn.energy_wh is not None:
                    updates["last_session_energy_wh"] = txn.energy_wh
                if txn.cost is not None:
                    updates["last_session_cost"] = txn.cost
        finally:
            db.close()
        if updates:
            await mqtt_bridge.publish_connector_state(self.id, connector_id, **updates)
        if vehicle_id:
            await mqtt_bridge.publish_vehicle_session_idle(vehicle_id)
            await mqtt_bridge.publish_vehicle_last_charge(vehicle_id)
        await mqtt_bridge.publish_vehicle_select(self.id, connector_id)
        sse_notify("start_retry_failed", {
            "charger_id": self.id,
            "connector_id": connector_id,
            "transaction_id": retry["txn_id"],
            "reason": why,
        })

    async def _suspend_for_schedule(self, connector_id: int):
        try:
            await self.set_charging_limit(connector_id, 0)
        except Exception:
            logger.debug("Suspension programmée échouée sur %s/%s", self.id, connector_id, exc_info=True)

    @on(Action.stop_transaction)
    async def on_stop_transaction(self, transaction_id, meter_stop, **kwargs):
        ocpp_logs.record(self.id, "in", "StopTransaction",
                         summary=f"txn {transaction_id}",
                         payload={"transactionId": transaction_id, "meterStop": meter_stop, **kwargs})
        queue_watch.note_stop_transaction(self.id)
        db = self._db()
        duration_min = None
        connector_id = None
        last_energy_wh = None
        last_cost = None
        last_vehicle_id = None
        aborted_start = False   # démarrage avorté : fin de session non publiée
        retry = None            # relance automatique à lancer
        try:
            txn = db.query(Transaction).filter(Transaction.id == transaction_id).first()
            if txn:
                connector_id = txn.connector_id
                txn.meter_stop = meter_stop
                # Date réelle de l'arrêt annoncée par la borne (voir _ocpp_ts) ;
                # jamais antérieure au début de la session (une transaction
                # créée avant ce correctif peut porter l'heure de réception).
                txn.stop_time = _ocpp_ts(kwargs.get("timestamp"))
                if txn.start_time and txn.stop_time < txn.start_time:
                    txn.stop_time = txn.start_time
                txn.status = "completed"
                txn.close_reason = None  # vrai arrêt reçu : données de fin fiables
                txn.deferred_until = None
                # Filet de sécurité : si un StopTransaction arrive alors que le
                # connecteur était encore signalé "Charging" (pas de transition
                # StatusNotification intermédiaire vue avant l'arrêt), on clôt
                # quand même la période de charge en cours au lieu de la perdre.
                if txn.charging_since:
                    # max(0, ...) : avec un stop_time rejoué, charging_since
                    # (posé à l'heure de réception) peut lui être postérieur.
                    txn.charging_seconds = (txn.charging_seconds or 0.0) + max(0.0, (txn.stop_time - txn.charging_since).total_seconds())
                    txn.charging_since = None
                if txn.start_time:
                    duration_min = round((txn.stop_time - txn.start_time).total_seconds() / 60, 1)
                db.flush()
                freeze_transaction_cost(db, txn)
                last_energy_wh = txn.energy_wh
                last_cost = txn.cost
                last_vehicle_id = txn.vehicle_id
                # Démarrage avorté (voir ABORTED_START_MAX_S) : la charge va
                # être relancée et cette tentative fusionnée dans la suivante.
                if connector_id is not None and _is_aborted_start(txn, kwargs.get("reason")):
                    aborted_start = True
                    key = (self.id, connector_id)
                    newer = db.query(Transaction).filter(
                        Transaction.charger_id == self.id,
                        Transaction.connector_id == connector_id,
                        Transaction.status == "active",
                        Transaction.id > txn.id,
                    ).order_by(Transaction.id.desc()).first()
                    if newer is not None and not newer.unauthorized:
                        # La charge a déjà été relancée (démarrage automatique
                        # ou relance manuelle arrivée avant cet arrêt).
                        aborted_id = txn.id
                        _absorb_aborted_start(db, newer, txn)
                        logger.info("Borne %s connecteur %s : démarrage avorté (transaction %s) fusionné dans la transaction %s déjà relancée",
                                    self.id, connector_id, aborted_id, newer.id)
                    else:
                        START_RETRIES[key] = {"txn_id": txn.id, "at": datetime.utcnow()}
                        attempts = txn.start_attempts or 1
                        max_retries = start_retry_max(db)
                        if attempts <= max_retries:
                            retry = {"txn_id": txn.id, "id_tag": txn.id_tag,
                                     "vehicle_id": txn.vehicle_id, "attempt": attempts,
                                     "max": max_retries}
                        else:
                            aborted_start = False  # plus de relance : fin de session ordinaire
                db.commit()
        finally:
            db.close()

        if connector_id is not None:
            await mqtt_bridge.publish_charge_control_state(self.id, connector_id, False)
            updates = {}
            if duration_min is not None:
                updates["session_duration_min"] = duration_min
            if last_energy_wh is not None:
                updates["last_session_energy_wh"] = last_energy_wh
            if last_cost is not None:
                updates["last_session_cost"] = last_cost
            # Démarrage avorté : rien n'est publié comme « dernière charge »,
            # la relance suit (sinon _give_up_retry publiera cette fin).
            if updates and not aborted_start:
                await mqtt_bridge.publish_connector_state(self.id, connector_id, **updates)
            if last_vehicle_id and not aborted_start:
                await mqtt_bridge.publish_vehicle_session_idle(last_vehicle_id)
                await mqtt_bridge.publish_vehicle_last_charge(last_vehicle_id)

        if retry is not None:
            asyncio.create_task(self._retry_aborted_start(connector_id, retry))

        # SSE : transaction terminée
        sse_notify("transaction_stopped", {
            "charger_id": self.id,
            "connector_id": connector_id,
            "transaction_id": transaction_id,
        })

        return call_result.StopTransaction(
            id_tag_info={"status": AuthorizationStatus.accepted}
        )

    @on(Action.meter_values)
    async def on_meter_values(self, connector_id, meter_value, transaction_id=None, **kwargs):
        ocpp_logs.record(self.id, "in", "MeterValues",
                         summary=f"conn {connector_id}",
                         payload={"connectorId": connector_id, "transactionId": transaction_id, "meterValue": meter_value},
                         connector_id=connector_id)
        # Suivi de la file de la borne (voir queue_watch) : dernier relevé reçu,
        # compteur par transaction, et trace d'un relevé rejoué en retard même
        # quand la capture complète des MeterValues est désactivée.
        stamps = [_ocpp_ts(mv.get("timestamp")) for mv in meter_value]
        if queue_watch.note_meter_values(self.id, connector_id, transaction_id, stamps):
            lag_s = int((datetime.utcnow() - max(stamps)).total_seconds()) if stamps else 0
            ocpp_logs.record(self.id, "in", "MeterValues.replay",
                             summary=f"conn {connector_id}, txn {transaction_id}, retard {lag_s} s",
                             payload={"connectorId": connector_id, "transactionId": transaction_id,
                                      "timestamps": [t.isoformat() + "Z" for t in stamps]},
                             connector_id=connector_id)
        db = self._db()
        late_txn = None
        late_hit = False
        mqtt_updates = {}
        vehicle_id_for_mqtt = None
        try:
            # Le transaction_id reçu est l'identifiant OCPP interne de la borne,
            # pas notre id SQLite. On résout notre transaction active sur ce connecteur.
            our_txn = db.query(Transaction).filter(
                Transaction.charger_id == self.id,
                Transaction.connector_id == connector_id,
                Transaction.status == "active",
            ).order_by(Transaction.id.desc()).first()
            # Le transactionId annoncé par la borne doit correspondre à notre id
            # interne (celui qu'on lui a nous-mêmes renvoyé à l'acceptation du
            # StartTransaction). Une borne peut rejouer un backlog de relevés
            # bufferisés avec un ancien/autre transactionId (souvent 0) : observé
            # en prod, une Schneider a rejoué 5 jours de relevés bufferisés en
            # pleine session réelle, ce qui écrasait régulièrement la progression
            # d'énergie de la vraie session avec une valeur figée proche du
            # relevé de départ (delta retombant à 0 à chaque relevé rejoué). On
            # ignore ces relevés pour la session active (mais on les stocke quand
            # même, juste sans les y rattacher, pour ne rien perdre du
            # diagnostic) ; ne casse pas la compatibilité avec les bornes qui
            # n'envoient pas transactionId du tout (nos simulateurs par exemple).
            if our_txn is not None and transaction_id is not None and transaction_id != our_txn.id:
                our_txn = None
            # Relevés rejoués APRÈS la clôture de leur transaction (clôturée par
            # le serveur faute de StopTransaction, file de la borne bloquée,
            # voir queue_watch) : on les rattache à cette transaction, au lieu
            # de les laisser orphelins, pour que l'énergie et le coût soient
            # recalculés avec les vraies mesures. Gardes : même borne, même
            # connecteur, transactionId identique à NOTRE id, et date du relevé
            # dans la fenêtre de la session (marge de compute_session_cost).
            if our_txn is None and transaction_id is not None:
                late_txn = db.query(Transaction).filter(
                    Transaction.id == transaction_id,
                    Transaction.charger_id == self.id,
                    Transaction.connector_id == connector_id,
                    Transaction.status == "completed",
                    Transaction.is_external.is_(False),
                ).first()
            our_txn_id = our_txn.id if our_txn else None
            vehicle_id_for_mqtt = our_txn.vehicle_id if our_txn else None

            latest_register_wh = None
            for mv in meter_value:
                # Date réelle du relevé annoncée par la borne (voir _ocpp_ts) :
                # une file rejouée après coupure ne doit pas être datée de
                # l'heure de réception. Garde-fou : pour un relevé rattaché à
                # la session active, une date antérieure à son début (horloge de
                # borne déréglée) ne peut pas être authentique ; on retombe
                # alors sur l'heure de réception pour ne pas sortir ce relevé
                # de la fenêtre de calcul d'énergie et de coût.
                mv_ts = _ocpp_ts(mv.get("timestamp"))
                if our_txn is not None and our_txn.start_time and mv_ts < our_txn.start_time - timedelta(minutes=1):
                    mv_ts = datetime.utcnow()
                row_txn_id = our_txn_id
                if late_txn is not None and late_txn.start_time and mv_ts >= late_txn.start_time - timedelta(minutes=1) \
                        and mv_ts <= (late_txn.stop_time or datetime.utcnow()) + timedelta(minutes=10):
                    row_txn_id = late_txn.id
                    late_hit = True
                for sv in mv.get("sampled_value", []):
                    try:
                        value = float(sv.get("value"))
                    except (TypeError, ValueError):
                        continue
                    measurand = sv.get("measurand", "Energy.Active.Import.Register")
                    unit = sv.get("unit")
                    stored_value = value
                    if measurand == "Energy.Active.Import.Register":
                        if unit and unit.lower() in ("kwh", "kw·h", "kw-h"):
                            stored_value = value * 1000.0
                            unit = "Wh"
                    db.add(MeterValue(
                        charger_id=self.id,
                        transaction_id=row_txn_id,
                        connector_id=connector_id,
                        timestamp=mv_ts,
                        measurand=measurand,
                        value=stored_value,
                        unit=unit,
                    ))
                    if measurand == "Power.Active.Import":
                        mqtt_updates["power_w"] = value
                    elif measurand == "Current.Import":
                        mqtt_updates["current_a"] = value
                    elif measurand == "Voltage":
                        mqtt_updates["voltage_v"] = value
                    elif measurand == "Energy.Active.Import.Register":
                        mqtt_updates["energy_wh"] = stored_value
                        latest_register_wh = stored_value

            if our_txn:
                if our_txn.start_time:
                    elapsed_min = (datetime.utcnow() - our_txn.start_time).total_seconds() / 60
                    mqtt_updates["session_duration_min"] = round(elapsed_min, 1)
                # Énergie de la SEULE session en cours (à ne pas confondre avec
                # energy_wh, le registre à vie) : registre actuel moins le
                # relevé de départ de cette transaction.
                if latest_register_wh is not None and our_txn.meter_start is not None:
                    mqtt_updates["session_energy_wh"] = max(0.0, latest_register_wh - our_txn.meter_start)
                # Coût en direct de la session en cours : même logique que le
                # coût final (compute_session_cost), avec ignore_meter_stop=True
                # puisque la session n'est pas terminée. db.flush() d'abord pour
                # que les MeterValues qu'on vient d'ajouter soient visibles à
                # la requête qui suit, sans attendre le commit.
                try:
                    db.flush()
                    charger = db.query(Charger).filter(Charger.id == self.id).first()
                    plan = resolve_plan_for_charger(db, charger)
                    mv_rows = db.query(MeterValue).filter(
                        MeterValue.transaction_id == our_txn.id,
                        MeterValue.charger_id == self.id,
                        MeterValue.connector_id == connector_id,
                    ).all()
                    result = compute_session_cost(our_txn, mv_rows, plan, ignore_meter_stop=True)
                    if result["cost"] is not None:
                        mqtt_updates["session_cost"] = result["cost"]
                except Exception:
                    logger.debug("Calcul du coût en direct échoué pour %s/%s", self.id, connector_id, exc_info=True)

            if not our_txn:
                # Hors session, la borne remonte une tension résiduelle de
                # quelques volts : ce n'est pas une mesure utile côté HA.
                mqtt_updates.pop("voltage_v", None)

            if late_hit and late_txn is not None:
                try:
                    db.flush()
                    freeze_transaction_cost(db, late_txn)
                    logger.info("Borne %s : relevés tardifs rattachés à la transaction %s (énergie %.0f Wh, coût %s)",
                                self.id, late_txn.id, late_txn.energy_wh or 0, late_txn.cost)
                except Exception:
                    logger.debug("Recalcul de la transaction %s après relevés tardifs échoué", late_txn.id, exc_info=True)

            db.commit()
        finally:
            db.close()

        if mqtt_updates and connector_id:
            await mqtt_bridge.publish_connector_state(self.id, connector_id, **mqtt_updates)
            if vehicle_id_for_mqtt:
                # Même trio « session en cours » reflété sur l'appareil véhicule,
                # indépendamment de la borne utilisée (voir on_start_transaction).
                veh_session_updates = {
                    k: v for k, v in mqtt_updates.items()
                    if k in ("session_energy_wh", "session_cost", "session_duration_min")
                }
                if veh_session_updates:
                    await mqtt_bridge.publish_vehicle_state(vehicle_id_for_mqtt, **veh_session_updates)

        return call_result.MeterValues()

    # --- Actions déclenchées depuis l'API ---

    async def trigger_remote_start(self, connector_id: int, id_tag: str):
        ocpp_logs.record(self.id, "out", "RemoteStartTransaction",
                         summary=f"conn {connector_id}, idTag={id_tag}",
                         payload={"connectorId": connector_id, "idTag": id_tag},
                         connector_id=connector_id)
        resp = await self.call(call.RemoteStartTransaction(
            connector_id=connector_id, id_tag=id_tag
        ))
        ocpp_logs.record(self.id, "in", "RemoteStartTransaction.conf",
                         summary=f"conn {connector_id} -> {resp.status}",
                         payload={"connectorId": connector_id, "status": resp.status},
                         connector_id=connector_id)
        queue_watch.note_remote_start(
            self.id, connector_id, getattr(resp.status, "value", resp.status) == "Accepted")
        return resp

    async def _auto_start(self, connector_id: int):
        try:
            await self.trigger_remote_start(
                connector_id, mqtt_bridge.start_identity(self.id, connector_id, "WEBADMIN"))
        except Exception:
            AUTO_START_ATTEMPTED.discard((self.id, connector_id))

    async def trigger_remote_stop(self, transaction_id: int):
        ocpp_logs.record(self.id, "out", "RemoteStopTransaction",
                         summary=f"txn {transaction_id}", payload={"transactionId": transaction_id})
        return await self.call(call.RemoteStopTransaction(transaction_id=transaction_id))

    async def fetch_configuration(self):
        response = await self.call(call.GetConfiguration())
        db = self._db()
        try:
            db.query(ConfigurationKey).filter(ConfigurationKey.charger_id == self.id).delete()
            for item in response.configuration_key or []:
                db.add(ConfigurationKey(
                    charger_id=self.id,
                    key=item["key"],
                    value=item.get("value"),
                    readonly=item.get("readonly", False),
                ))
            db.commit()
        finally:
            db.close()
        return response.configuration_key

    async def push_configuration(self, key: str, value: str):
        ocpp_logs.record(self.id, "out", "ChangeConfiguration",
                         summary=f"{key} = {value}", payload={"key": key, "value": value})
        response = await self.call(call.ChangeConfiguration(key=key, value=value))
        # RebootRequired signifie que la borne a bien pris en compte et stocké
        # la nouvelle valeur (confirmé par un GetConfiguration ultérieur coté
        # borne), simplement pas encore appliquée à son comportement actif tant
        # qu'elle n'a pas redémarré. Le cache local doit donc suivre ce cas au
        # même titre qu'Accepted, contrairement à Rejected/NotSupported.
        accepted_like = response.status in (ConfigurationStatus.accepted, ConfigurationStatus.reboot_required)
        if accepted_like or key == "LightIntensity":
            db = self._db()
            try:
                if accepted_like:
                    entry = db.query(ConfigurationKey).filter(
                        ConfigurationKey.charger_id == self.id,
                        ConfigurationKey.key == key,
                    ).first()
                    if entry:
                        entry.value = value
                if key == "LightIntensity":
                    charger = db.query(Charger).filter(Charger.id == self.id).first()
                    if charger:
                        # Apprentissage passif : la première fois qu'un essai à 0
                        # (manuel ou automatique) obtient une réponse définitive,
                        # on mémorise si cette borne accepte l'extinction totale.
                        # Sert de plancher à la réduction nocturne.
                        if value == "0":
                            charger.light_zero_supported = accepted_like
                        # La "dernière valeur fixe utilisée" ne doit suivre que
                        # les poussées MANUELLES (mode fixed) : en mode auto,
                        # push_configuration est appelé par apply_light_intensity
                        # avec la valeur calculée (occupation/nuit), pas un choix
                        # utilisateur à mémoriser comme réglage fixe.
                        if accepted_like and (charger.light_mode or "fixed") != "auto":
                            try:
                                charger.light_fixed_value = int(value)
                            except ValueError:
                                pass
                db.commit()
            finally:
                db.close()
        if response.status == ConfigurationStatus.reboot_required:
            PENDING_REBOOT_KEYS.setdefault(self.id, {})[key] = datetime.utcnow()
        else:
            PENDING_REBOOT_KEYS.get(self.id, {}).pop(key, None)
        return response.status

    async def apply_light_intensity(self):
        """Recalcule et pousse, si nécessaire, la valeur LightIntensity cible
        (voir compute_light_target). Ne fait rien si la borne n'a pas cette
        clé, ou si rien n'est calculable (mode fixe sans valeur fixe connue,
        mode auto sans les deux valeurs). En mode fixe SANS réduction
        nocturne, calcule quand même la valeur pleine à chaque appel : c'est
        un no-op tant que la borne est déjà à cette valeur (comparaison avec
        le cache avant tout envoi), mais permet de se resynchroniser tout
        seul si elle a dérivé. N'envoie un ChangeConfiguration que si la
        cible diffère de la dernière valeur connue en cache, pour éviter de
        solliciter la borne à chaque appel (transition de connecteur, tick du
        planificateur, reconnexion)."""
        db = self._db()
        try:
            charger = db.query(Charger).filter(Charger.id == self.id).first()
            if not charger:
                return
            entry = db.query(ConfigurationKey).filter(
                ConfigurationKey.charger_id == self.id,
                ConfigurationKey.key == "LightIntensity",
            ).first()
            if not entry:
                return  # borne sans cette clé : rien à automatiser
            occupied = _charger_occupied(db, self.id)
            target = compute_light_target(
                charger.light_mode, occupied, charger.light_fixed_value,
                charger.light_auto_charge_value, charger.light_auto_free_value,
                bool(charger.light_night_enabled), charger.light_night_reduction,
                charger.light_night_start, charger.light_night_end,
                bool(charger.light_zero_supported), datetime.utcnow(),
            )
            current = entry.value
        finally:
            db.close()
        if target is None:
            return
        if current is not None and str(target) == str(current):
            return  # déjà à la bonne valeur, rien à pousser
        try:
            await self.push_configuration("LightIntensity", str(target))
        except Exception:
            logger.debug("Application automatique de LightIntensity échouée sur %s", self.id, exc_info=True)

    async def trigger_reset(self, reset_type: str = "Soft"):
        """Demande un redémarrage à la borne via OCPP (Reset.req). 'Soft' relance
        le logiciel de la borne (recommandé pour appliquer un changement de
        configuration comme l'URL du backend) ; 'Hard' fait un redémarrage
        matériel complet. Les deux peuvent interrompre une charge en cours,
        c'est à l'appelant de prévenir l'utilisateur avant d'appeler ceci."""
        rtype = ResetType.hard if (reset_type or "").lower() == "hard" else ResetType.soft
        ocpp_logs.record(self.id, "out", "Reset", summary=rtype.value, payload={"type": rtype.value})
        return await self.call(call.Reset(type=rtype))

    async def push_local_auth_list(self, id_tags: list[str]):
        """Pousse la liste complète des idTags autorisés localement sur la
        borne (SendLocalList, mise à jour Full). Objectif : que la borne
        puisse elle-même refuser un idTag inconnu (typiquement son propre
        idTag par défaut) au lieu de démarrer une charge que le serveur n'a
        pas validée (voir l'incident du 26/09). N'a d'effet réel que si la
        borne supporte et respecte LocalAuthListEnabled ; comme on l'a
        constaté, rien ne garantit qu'un firmware donné s'y conforme
        strictement, donc à vérifier en conditions réelles après déploiement.
        Le numéro de version doit augmenter strictement à chaque mise à jour
        complète (spec OCPP 1.6) ; on l'incrémente et le persiste sur la borne
        à chaque appel plutôt que de recalculer un delta."""
        db = self._db()
        try:
            charger = db.query(Charger).filter(Charger.id == self.id).first()
            if not charger:
                return None
            charger.local_list_version = (charger.local_list_version or 0) + 1
            version = charger.local_list_version
            db.commit()
        finally:
            db.close()
        entries = [{"id_tag": tag, "id_tag_info": {"status": "Accepted"}} for tag in id_tags]
        ocpp_logs.record(self.id, "out", "SendLocalList",
                         summary=f"{len(entries)} tag(s), version {version}",
                         payload={"listVersion": version, "updateType": "Full", "tagCount": len(entries)})
        resp = await self.call(call.SendLocalList(
            list_version=version,
            update_type=UpdateType.full,
            local_authorization_list=entries,
        ))
        ocpp_logs.record(self.id, "in", "SendLocalList.conf",
                         summary=f"-> {resp.status.value}", payload={"status": resp.status.value})
        return resp.status

    async def set_charging_limit(self, connector_id: int, limit_w: float | None):
        try:
            if limit_w is None:
                ocpp_logs.record(self.id, "out", "ClearChargingProfile",
                                 summary=f"conn {connector_id}", payload={"connectorId": connector_id},
                                 connector_id=connector_id)
                resp = await self.call(call.ClearChargingProfile(
                    connector_id=connector_id,
                    charging_profile_purpose=ChargingProfilePurposeType.tx_default_profile,
                ))
                ok = getattr(resp, "status", None) in (
                    ClearChargingProfileStatus.accepted, ClearChargingProfileStatus.unknown,
                )
            else:
                profile = {
                    "charging_profile_id": 1,
                    "stack_level": 0,
                    "charging_profile_purpose": ChargingProfilePurposeType.tx_default_profile,
                    "charging_profile_kind": ChargingProfileKindType.absolute,
                    "charging_schedule": {
                        "charging_rate_unit": ChargingRateUnitType.watts,
                        "charging_schedule_period": [
                            {"start_period": 0, "limit": float(limit_w)},
                        ],
                    },
                }
                ocpp_logs.record(self.id, "out", "SetChargingProfile",
                                 summary=f"conn {connector_id}, limite {limit_w} W",
                                 payload={"connectorId": connector_id, "limitW": limit_w},
                                 connector_id=connector_id)
                resp = await self.call(call.SetChargingProfile(
                    connector_id=connector_id, cs_charging_profiles=profile,
                ))
                ok = getattr(resp, "status", None) == "Accepted"
            SMART_CHARGING_SUPPORT[self.id] = bool(ok)
            return bool(ok)
        except Exception:
            SMART_CHARGING_SUPPORT[self.id] = False
            return False


async def sync_local_auth_list(charger_id: str):
    """Recalcule et pousse la liste d'autorisation locale d'une borne
    CONNECTÉE, d'après les véhicules qui lui sont associés (VehicleCharger).
    À appeler après toute modification de cette association (création d'un
    véhicule, changement des bornes associées, désactivation/suppression), et
    automatiquement à chaque BootNotification (voir on_boot_notification). Ne
    fait rien silencieusement si la borne n'est pas connectée (elle sera de
    toute façon resynchronisée à sa prochaine reconnexion) ou si l'appel OCPP
    échoue (borne ne supportant pas SendLocalList, par exemple)."""
    cp = CONNECTED_CHARGERS.get(charger_id)
    if cp is None:
        return None
    db = cp._db()
    try:
        tags = _authorized_id_tags_for_charger(db, charger_id)
    finally:
        db.close()
    try:
        return await cp.push_local_auth_list(tags)
    except Exception:
        logger.debug("Synchronisation de la liste locale échouée pour %s", charger_id, exc_info=True)
        return None
