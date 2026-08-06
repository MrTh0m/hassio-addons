import asyncio
import logging
from datetime import datetime

from ocpp.routing import on
from ocpp.v16 import ChargePoint as ChargePoint16
from ocpp.v16 import call, call_result
from ocpp.v16.enums import (
    Action, RegistrationStatus, AuthorizationStatus, ConfigurationStatus,
    ChargingProfilePurposeType, ChargingProfileKindType, ChargingRateUnitType,
    ClearChargingProfileStatus,
)

from .db import SessionLocal
from .models import Charger, ChargerMode, Transaction, MeterValue, ConfigurationKey, ConnectorStatus, Vehicle
from .pricing import freeze_transaction_cost
from . import mqtt_bridge
from . import ocpp_logs
from .sse import sse_notify

logger = logging.getLogger("csms_local")

CONNECTED_CHARGERS: dict[str, "LocalChargePoint"] = {}
PENDING_REMOTE_STARTS: dict[tuple[str, int], int] = {}
AUTO_START_ATTEMPTED: set[tuple[str, int]] = set()
RESERVED_TAGS = {"WEBADMIN", "MQTT", "SCHED"}
SMART_CHARGING_SUPPORT: dict[str, bool] = {}


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


def now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


class LocalChargePoint(ChargePoint16):

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
        ocpp_logs.record(self.id, "in", "BootNotification",
                         summary=f"{charge_point_vendor} {charge_point_model}",
                         payload={"vendor": charge_point_vendor, "model": charge_point_model, **kwargs})
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

            if connector_id == 0:
                charger.status = status

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
        ocpp_logs.record(self.id, "in", "StartTransaction",
                         summary=f"conn {connector_id}, idTag={id_tag}",
                         payload={"connectorId": connector_id, "idTag": id_tag, "meterStart": meter_start, **kwargs},
                         connector_id=connector_id)
        db = self._db()
        deferred_label = None
        try:
            charger = db.query(Charger).filter(Charger.id == self.id).first()
            pending_vehicle_id = PENDING_REMOTE_STARTS.pop((self.id, connector_id), None)

            if not _tag_is_authorized(db, charger, id_tag):
                return call_result.StartTransaction(
                    transaction_id=0,
                    id_tag_info={"status": AuthorizationStatus.blocked},
                )

            vehicle = db.query(Vehicle).filter(Vehicle.id_tag == id_tag).first() if id_tag else None
            if vehicle is None and pending_vehicle_id is not None:
                vehicle = db.query(Vehicle).filter(Vehicle.id == pending_vehicle_id).first()

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
                status="active",
                deferred_until=deferred_label if suspend_now else None,
            )
            db.add(txn)
            db.commit()
            db.refresh(txn)
            txn_id = txn.id
        finally:
            db.close()

        await mqtt_bridge.publish_charge_control_state(self.id, connector_id, True)

        if suspend_now:
            asyncio.create_task(self._suspend_for_schedule(connector_id))

        # SSE : nouvelle transaction
        sse_notify("transaction_started", {
            "charger_id": self.id,
            "connector_id": connector_id,
            "transaction_id": txn_id,
            "deferred": suspend_now,
        })

        return call_result.StartTransaction(
            transaction_id=txn_id,
            id_tag_info={"status": AuthorizationStatus.accepted},
        )

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
                txn.deferred_until = None
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

        # SSE : transaction terminée
        sse_notify("transaction_stopped", {
            "charger_id": self.id,
            "connector_id": connector_id,
            "transaction_id": transaction_id,
        })

        return call_result.StopTransaction(
            id_tag_info={"status": AuthorizationStatus.accepted}
        )

    async def on_meter_values(self, connector_id, meter_value, transaction_id=None, **kwargs):
        ocpp_logs.record(self.id, "in", "MeterValues",
                         summary=f"conn {connector_id}",
                         payload={"connectorId": connector_id, "transactionId": transaction_id, "meterValue": meter_value},
                         connector_id=connector_id)
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
                    unit = sv.get("unit")
                    # Normaliser l'énergie en Wh : certaines bornes envoient des kWh
                    stored_value = value
                    if measurand == "Energy.Active.Import.Register":
                        if unit and unit.lower() in ("kwh", "kw·h", "kw-h"):
                            stored_value = value * 1000.0
                            unit = "Wh"  # on normalise l'unité stockée
                    db.add(MeterValue(
                        charger_id=self.id,
                        transaction_id=transaction_id,
                        connector_id=connector_id,
                        measurand=measurand,
                        value=stored_value,
                        unit=unit,
                    ))
                    if measurand == "Power.Active.Import":
                        mqtt_updates["power_w"] = value
                    elif measurand == "Energy.Active.Import.Register":
                        mqtt_updates["energy_wh"] = stored_value

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

    # --- Actions déclenchées depuis l'API ---

    async def trigger_remote_start(self, connector_id: int, id_tag: str):
        ocpp_logs.record(self.id, "out", "RemoteStartTransaction",
                         summary=f"conn {connector_id}, idTag={id_tag}",
                         payload={"connectorId": connector_id, "idTag": id_tag},
                         connector_id=connector_id)
        return await self.call(call.RemoteStartTransaction(
            connector_id=connector_id, id_tag=id_tag
        ))

    async def _auto_start(self, connector_id: int):
        try:
            await self.trigger_remote_start(connector_id, "WEBADMIN")
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
