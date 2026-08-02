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
        # Simplification v1 : tout badge est accepté. À affiner avec une vraie
        # liste blanche d'idTags si plusieurs utilisateurs doivent être gérés.
        return call_result.Authorize(
            id_tag_info={"status": AuthorizationStatus.accepted}
        )

    @on(Action.status_notification)
    async def on_status_notification(self, connector_id, status, **kwargs):
        db = self._db()
        closed_duration_min = None
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

        return call_result.StatusNotification()

    @on(Action.start_transaction)
    async def on_start_transaction(self, connector_id, id_tag, meter_start, **kwargs):
        db = self._db()
        try:
            vehicle = db.query(Vehicle).filter(Vehicle.id_tag == id_tag).first() if id_tag else None
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
