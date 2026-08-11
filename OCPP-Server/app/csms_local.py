import asyncio
import logging
from datetime import datetime, time as dtime

from ocpp.routing import on
from ocpp.v16 import ChargePoint as ChargePoint16
from ocpp.v16 import call, call_result
from ocpp.v16.enums import (
    Action, RegistrationStatus, AuthorizationStatus, ConfigurationStatus,
    ChargingProfilePurposeType, ChargingProfileKindType, ChargingRateUnitType,
    ClearChargingProfileStatus, ResetType,
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
# Clés de configuration en attente d'un redémarrage de la borne pour être
# effectivement appliquées (la borne a répondu RebootRequired à un
# ChangeConfiguration). Purement en mémoire, remis à zéro pour une borne
# dès qu'un nouveau BootNotification est reçu (elle vient de redémarrer,
# pour quelque raison que ce soit) ou dès que le serveur redémarre.
PENDING_REBOOT_KEYS: dict[str, set[str]] = {}

# Statuts de connecteur considérés comme "occupés" (véhicule branché, quelle
# que soit l'étape) pour le pilotage automatique de la luminosité. Mêmes
# valeurs que celles utilisées par le planificateur (scheduler.py) pour
# décider si un connecteur est "plugged".
OCCUPIED_CONNECTOR_STATUSES = ("Preparing", "Charging", "SuspendedEV", "SuspendedEVSE", "Finishing")


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

    Retourne None si rien n'est à pousser automatiquement : le mode "fixed"
    ne déclenche jamais de push automatique (le curseur manuel s'en charge
    déjà), et le mode "auto" sans les deux valeurs configurées ne peut rien
    calculer."""
    if mode != "auto":
        return None
    if auto_charge_value is None or auto_free_value is None:
        return None
    base = auto_charge_value if occupied else auto_free_value
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
        # auto (le réglage aurait pu dériver pendant la coupure).
        asyncio.create_task(self.apply_light_intensity())
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
            old_status = entry.status
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
            await mqtt_bridge.publish_connector_discovery(self.id, connector_id, mode_value)
            await mqtt_bridge.publish_connector_state(self.id, connector_id, status=status)
            await mqtt_bridge.publish_charge_control_state(self.id, connector_id, status == "Charging")
            if closed_duration_min is not None:
                await mqtt_bridge.publish_connector_state(self.id, connector_id, session_duration_min=closed_duration_min)

        if do_auto_start:
            asyncio.create_task(self._auto_start(connector_id))

        if light_transition:
            asyncio.create_task(self.apply_light_intensity())

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

    @on(Action.meter_values)
    async def on_meter_values(self, connector_id, meter_value, transaction_id=None, **kwargs):
        ocpp_logs.record(self.id, "in", "MeterValues",
                         summary=f"conn {connector_id}",
                         payload={"connectorId": connector_id, "transactionId": transaction_id, "meterValue": meter_value},
                         connector_id=connector_id)
        db = self._db()
        mqtt_updates = {}
        try:
            # Le transaction_id reçu est l'identifiant OCPP interne de la borne,
            # pas notre id SQLite. On résout notre transaction active sur ce connecteur.
            our_txn = db.query(Transaction).filter(
                Transaction.charger_id == self.id,
                Transaction.connector_id == connector_id,
                Transaction.status == "active",
            ).order_by(Transaction.id.desc()).first()
            our_txn_id = our_txn.id if our_txn else None

            for mv in meter_value:
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
                        transaction_id=our_txn_id,
                        connector_id=connector_id,
                        measurand=measurand,
                        value=stored_value,
                        unit=unit,
                    ))
                    if measurand == "Power.Active.Import":
                        mqtt_updates["power_w"] = value
                    elif measurand == "Energy.Active.Import.Register":
                        mqtt_updates["energy_wh"] = stored_value

            if our_txn and our_txn.start_time:
                elapsed_min = (datetime.utcnow() - our_txn.start_time).total_seconds() / 60
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
            PENDING_REBOOT_KEYS.setdefault(self.id, set()).add(key)
        else:
            PENDING_REBOOT_KEYS.get(self.id, set()).discard(key)
        return response.status

    async def apply_light_intensity(self):
        """Recalcule et pousse, si nécessaire, la valeur LightIntensity cible
        d'après le mode auto (occupation + réduction nocturne éventuelle). Ne
        fait rien en mode fixe (le curseur manuel s'en charge déjà) ni si la
        borne n'a pas cette clé. N'envoie un ChangeConfiguration que si la
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
