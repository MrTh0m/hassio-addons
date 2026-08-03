import asyncio
from datetime import datetime

from ocpp.routing import on
from ocpp.v16 import ChargePoint as ChargePoint16
from ocpp.v16 import call, call_result
from ocpp.v16.enums import (
    Action, RegistrationStatus, AuthorizationStatus, ConfigurationStatus,
)

from .db import SessionLocal
from .models import Charger, ChargerMode, Transaction, MeterValue, ConfigurationKey, ConnectorStatus, Vehicle
from .pricing import freeze_transaction_cost
from . import mqtt_bridge

# Registre des bornes actuellement connectées en mode local, pour que l'API
# puisse leur envoyer des commandes (RemoteStart, ChangeConfiguration, ...).
CONNECTED_CHARGERS: dict[str, "LocalChargePoint"] = {}

# Démarrages distants en attente : quand l'appli lance une charge pour un
# véhicule donné, on mémorise (borne, connecteur) -> vehicle_id, pour rattacher
# la session au bon véhicule même s'il n'a pas d'idTag (le StartTransaction qui
# suit ne portera alors aucun idTag connu).
PENDING_REMOTE_STARTS: dict[tuple[str, int], int] = {}

# Connecteurs pour lesquels un démarrage automatique (mode 'free') a déjà été
# tenté depuis le dernier branchement. Évite de relancer en boucle tant que le
# connecteur reste en "Preparing" (y compris après un arrêt manuel : on ne
# relance qu'après un débranchement, c'est-à-dire un retour à "Available").
AUTO_START_ATTEMPTED: set[tuple[str, int]] = set()

# idTags "système" toujours acceptés : ils ne proviennent que de nos propres
# actions authentifiées (bouton de l'appli, commande MQTT, démarrage distant).
RESERVED_TAGS = {"WEBADMIN", "MQTT"}


def _auth_mode_value(charger) -> str:
    """'free' par défaut : borne inconnue, ou base migrée sans la colonne."""
    mode = getattr(charger, "auth_mode", None) if charger is not None else None
    return mode.value if mode is not None else "free"


def _tag_is_authorized(db, charger, id_tag) -> bool:
    """En mode 'free', tout est accepté. En mode 'authorized', seuls un idTag
    associé à un véhicule connu, un tag réservé (bouton appli / MQTT) ou un
    tag de démarrage distant (préfixe REMOTE-) sont acceptés."""
    if _auth_mode_value(charger) == "free":
        return True
    if not id_tag:
        return False
    if id_tag in RESERVED_TAGS or id_tag.startswith("REMOTE-"):
        return True
    return db.query(Vehicle).filter(Vehicle.id_tag == id_tag).first() is not None


def now_iso() -> str:
    """Horodatage UTC conforme au type dateTime d'OCPP (ISO 8601, suffixe Z)."""
    return datetime.utcnow().isoformat() + "Z"


class LocalChargePoint(ChargePoint16):
    """Gère une borne en mode 'local' : le serveur backoffice est le seul
    central system, avec accès complet au pilotage et à la configuration."""

    def _db(self):
        return SessionLocal()

    def _get_or_create_charger(self, db):
        charger = db.query(Charger).filter(Charger.id == self.id).first()
        if not charger:
            charger = Charger(id=self.id, mode=ChargerMode.local)
            db.add(charger)
        return charger

    @on(Action.boot_notification)
    async def on_boot_notification(self, charge_point_vendor, charge_point_model, **kwargs):
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

        return call_result.BootNotification(
            current_time=now_iso(),
            interval=300,
            status=RegistrationStatus.accepted,
        )

    @on(Action.heartbeat)
    async def on_heartbeat(self, **kwargs):
        return call_result.Heartbeat(current_time=now_iso())

    @on(Action.authorize)
    async def on_authorize(self, id_tag, **kwargs):
        # En mode 'authorized', on refuse un badge inconnu ; en mode 'free',
        # tout est accepté (voir _tag_is_authorized).
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
        db = self._db()
        closed_duration_min = None
        do_auto_start = False
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
            entry.status = status
            entry.error_code = kwargs.get("error_code")
            entry.updated_at = datetime.utcnow()

            # connectorId=0 désigne la borne elle-même (pas un connecteur
            # physique) au sens de la norme : c'est ce statut qui sert de
            # résumé au niveau de la borne, indépendamment de ses connecteurs.
            if connector_id == 0:
                charger.status = status

            # La borne annonce elle-même que ce connecteur est disponible :
            # si une transaction y restait "active" (StopTransaction jamais
            # reçu, ex. après une coupure réseau ou un redémarrage), on la
            # clôture nous-mêmes plutôt que de la laisser trainer pour
            # toujours comme "en cours".
            if status == "Available" and connector_id != 0:
                stale = db.query(Transaction).filter(
                    Transaction.charger_id == self.id,
                    Transaction.connector_id == connector_id,
                    Transaction.status == "active",
                ).first()
                if stale:
                    stale.stop_time = datetime.utcnow()
                    if stale.meter_stop is None:
                        stale.meter_stop = stale.meter_start
                    stale.status = "completed"
                    if stale.start_time:
                        closed_duration_min = round((stale.stop_time - stale.start_time).total_seconds() / 60, 1)
                    db.flush()
                    freeze_transaction_cost(db, stale)

            # --- Démarrage automatique (mode 'free') -------------------------
            # En "sans autorisation", brancher un véhicule doit lancer la charge
            # tout seul. La borne signale "Preparing" (câble branché, en
            # attente) mais n'émet pas forcément de StartTransaction de
            # lui-même : on déclenche alors un RemoteStart, une seule fois par
            # branchement (le drapeau est levé au débranchement).
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
                    if not has_active:
                        AUTO_START_ATTEMPTED.add((self.id, connector_id))
                        do_auto_start = True

            mode_value = charger.mode.value
            db.commit()
        finally:
            db.close()

        if connector_id == 0:
            await mqtt_bridge.publish_state(self.id, status=status)
        else:
            await mqtt_bridge.publish_connector_discovery(self.id, connector_id, mode_value)
            await mqtt_bridge.publish_connector_state(self.id, connector_id, status=status)
            await mqtt_bridge.publish_charge_control_state(self.id, connector_id, status == "Charging")
            if closed_duration_min is not None:
                await mqtt_bridge.publish_connector_state(self.id, connector_id, session_duration_min=closed_duration_min)

        if do_auto_start:
            asyncio.create_task(self._auto_start(connector_id))

        return call_result.StatusNotification()

    @on(Action.start_transaction)
    async def on_start_transaction(self, connector_id, id_tag, meter_start, **kwargs):
        db = self._db()
        try:
            charger = db.query(Charger).filter(Charger.id == self.id).first()

            # On consomme systématiquement l'éventuel démarrage distant en
            # attente pour ce connecteur (pour ne pas le laisser fuiter).
            pending_vehicle_id = PENDING_REMOTE_STARTS.pop((self.id, connector_id), None)

            if not _tag_is_authorized(db, charger, id_tag):
                # Autorisation refusée : aucune transaction créée.
                # transaction_id=0 = "pas de transaction", conforme à l'usage
                # OCPP quand l'idTag est rejeté.
                return call_result.StartTransaction(
                    transaction_id=0,
                    id_tag_info={"status": AuthorizationStatus.blocked},
                )

            vehicle = db.query(Vehicle).filter(Vehicle.id_tag == id_tag).first() if id_tag else None
            if vehicle is None and pending_vehicle_id is not None:
                vehicle = db.query(Vehicle).filter(Vehicle.id == pending_vehicle_id).first()
            txn = Transaction(
                charger_id=self.id,
                connector_id=connector_id,
                id_tag=id_tag,
                vehicle_id=vehicle.id if vehicle else None,
                meter_start=meter_start,
                status="active",
            )
            db.add(txn)
            db.commit()
            db.refresh(txn)
            txn_id = txn.id
        finally:
            db.close()

        await mqtt_bridge.publish_charge_control_state(self.id, connector_id, True)

        return call_result.StartTransaction(
            transaction_id=txn_id,
            id_tag_info={"status": AuthorizationStatus.accepted},
        )

    @on(Action.stop_transaction)
    async def on_stop_transaction(self, transaction_id, meter_stop, **kwargs):
        db = self._db()
        duration_min = None
        connector_id = None
        try:
            txn = db.query(Transaction).filter(Transaction.id == transaction_id).first()
            if txn:
                connector_id = txn.connector_id
                txn.meter_stop = meter_stop
                txn.stop_time = datetime.utcnow()
                txn.status = "completed"
                if txn.start_time:
                    duration_min = round((txn.stop_time - txn.start_time).total_seconds() / 60, 1)
                db.flush()
                freeze_transaction_cost(db, txn)
                db.commit()
        finally:
            db.close()

        if connector_id is not None:
            await mqtt_bridge.publish_charge_control_state(self.id, connector_id, False)
            if duration_min is not None:
                await mqtt_bridge.publish_connector_state(self.id, connector_id, session_duration_min=duration_min)

        return call_result.StopTransaction(
            id_tag_info={"status": AuthorizationStatus.accepted}
        )

    @on(Action.meter_values)
    async def on_meter_values(self, connector_id, meter_value, transaction_id=None, **kwargs):
        db = self._db()
        mqtt_updates = {}
        try:
            for mv in meter_value:
                for sv in mv.get("sampled_value", []):
                    try:
                        value = float(sv.get("value"))
                    except (TypeError, ValueError):
                        continue
                    measurand = sv.get("measurand", "Energy.Active.Import.Register")
                    db.add(MeterValue(
                        charger_id=self.id,
                        transaction_id=transaction_id,
                        connector_id=connector_id,
                        measurand=measurand,
                        value=value,
                        unit=sv.get("unit"),
                    ))
                    if measurand == "Power.Active.Import":
                        mqtt_updates["power_w"] = value
                    elif measurand == "Energy.Active.Import.Register":
                        mqtt_updates["energy_wh"] = value

            if transaction_id is not None:
                txn = db.query(Transaction).filter(Transaction.id == transaction_id).first()
                if txn and txn.start_time:
                    elapsed_min = (datetime.utcnow() - txn.start_time).total_seconds() / 60
                    mqtt_updates["session_duration_min"] = round(elapsed_min, 1)

            db.commit()
        finally:
            db.close()

        if mqtt_updates and connector_id:
            await mqtt_bridge.publish_connector_state(self.id, connector_id, **mqtt_updates)

        return call_result.MeterValues()

    # --- Actions déclenchées depuis l'API (backoffice -> borne) ---

    async def trigger_remote_start(self, connector_id: int, id_tag: str):
        return await self.call(call.RemoteStartTransaction(
            connector_id=connector_id, id_tag=id_tag
        ))

    async def _auto_start(self, connector_id: int):
        """Démarrage automatique (mode 'free'), lancé en tâche de fond : appeler
        self.call() directement dans un handler bloquerait la boucle de
        réception (la réponse ne pourrait jamais être lue)."""
        try:
            await self.trigger_remote_start(connector_id, "WEBADMIN")
        except Exception:
            AUTO_START_ATTEMPTED.discard((self.id, connector_id))

    async def trigger_remote_stop(self, transaction_id: int):
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
        response = await self.call(call.ChangeConfiguration(key=key, value=value))
        if response.status == ConfigurationStatus.accepted:
            db = self._db()
            try:
                entry = db.query(ConfigurationKey).filter(
                    ConfigurationKey.charger_id == self.id,
                    ConfigurationKey.key == key,
                ).first()
                if entry:
                    entry.value = value
                db.commit()
            finally:
                db.close()
        return response.status
