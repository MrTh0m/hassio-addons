import json
import logging
from datetime import datetime

import websockets
from starlette.websockets import WebSocket, WebSocketDisconnect

from .db import SessionLocal
from .models import MeterValue, Transaction, Charger, ConnectorStatus, Vehicle
from .pricing import freeze_transaction_cost, _to_wh
from . import mqtt_bridge
from . import ocpp_logs

logger = logging.getLogger("relay")

# OCPP over WebSocket : [messageTypeId, uniqueId, action, payload] pour un CALL
# (type 2). Les CALLRESULT (3) et CALLERROR (4) n'ont pas de champ action ; on
# tente de retrouver l'action de la requête correspondante via l'uniqueId.
_MSG_TYPE = {2: "CALL", 3: "CALLRESULT", 4: "CALLERROR"}


def _log_frame(charger_id: str, direction: str, raw: str, pending: dict):
    """Journalise une trame OCPP brute pour la vue Logs (les deux sens).

    `pending` associe uniqueId -> action pour retrouver l'action d'un
    CALLRESULT/CALLERROR (qui ne la portent pas). `direction` vaut "in"
    (borne -> officiel) ou "out" (officiel -> borne)."""
    try:
        frame = json.loads(raw)
        if not isinstance(frame, list) or len(frame) < 3:
            return
        mtype = frame[0]
        uid = frame[1]
        if mtype == 2:
            action = frame[2]
            payload = frame[3] if len(frame) > 3 else {}
            pending[uid] = action
        else:
            action = pending.pop(uid, _MSG_TYPE.get(mtype, str(mtype)))
            payload = frame[2] if len(frame) > 2 else {}
        connector_id = payload.get("connectorId") if isinstance(payload, dict) else None
        kind = _MSG_TYPE.get(mtype, str(mtype))
        ocpp_logs.record(
            charger_id, direction, action,
            summary=kind if mtype != 2 else "",
            payload=payload, connector_id=connector_id,
        )
    except Exception:
        logger.debug("Trame non journalisable (%s)", direction, exc_info=True)

# Diagnostic : mesurandes déjà observés par borne, pour ne journaliser chaque
# type qu'une fois (voir ce que la borne expose réellement, TIC compris).
_SEEN_MEASURANDS: dict[str, set] = {}


async def _snoop_frame(charger_id: str, raw: str):
    """Analyse au mieux une trame OCPP brute (envoyée par la borne) pour en
    extraire des métriques, sans jamais bloquer ni modifier le relais lui-même.
    Toute erreur de parsing est silencieusement ignorée : le relais doit
    rester transparent avant tout."""
    try:
        frame = json.loads(raw)
        if not isinstance(frame, list) or len(frame) < 4 or frame[0] != 2:
            return  # on ne s'intéresse qu'aux CALL (2) envoyés par la borne
        action = frame[2]
        payload = frame[3]
        db = SessionLocal()
        charger_mqtt_updates = {}
        connector_mqtt_updates: dict[int, dict] = {}
        connector_id = None
        try:
            if action == "StatusNotification":
                connector_id = payload.get("connectorId")
                status = payload.get("status")
                entry = db.query(ConnectorStatus).filter(
                    ConnectorStatus.charger_id == charger_id,
                    ConnectorStatus.connector_id == connector_id,
                ).first()
                if not entry:
                    entry = ConnectorStatus(charger_id=charger_id, connector_id=connector_id)
                    db.add(entry)
                entry.status = status
                entry.error_code = payload.get("errorCode")
                entry.updated_at = datetime.utcnow()
                if connector_id == 0:
                    charger = db.query(Charger).filter(Charger.id == charger_id).first()
                    if charger:
                        charger.status = status
                        if charger.deleted_at is not None:
                            # Même logique qu'en mode local : un signal de vie
                            # (ici une StatusNotification relayée) réactive
                            # automatiquement une borne désactivée.
                            charger.deleted_at = None
                            logger.info("Borne %s (relais) : signal reçu, réactivation automatique (était désactivée)", charger_id)
                    charger_mqtt_updates["status"] = status
                else:
                    await mqtt_bridge.publish_connector_discovery(charger_id, connector_id, "relay")
                    connector_mqtt_updates.setdefault(connector_id, {})["status"] = status
                    if status == "Available":
                        stale = db.query(Transaction).filter(
                            Transaction.charger_id == charger_id,
                            Transaction.connector_id == connector_id,
                            Transaction.status == "active",
                        ).first()
                        if stale:
                            stale.stop_time = datetime.utcnow()
                            if stale.meter_stop is None:
                                stale.meter_stop = stale.meter_start
                            stale.status = "completed"
                            db.flush()
                            freeze_transaction_cost(db, stale)
                db.commit()
            elif action == "MeterValues":
                connector_id = payload.get("connectorId")
                transaction_id = payload.get("transactionId")
                for mv in payload.get("meterValue", []):
                    for sv in mv.get("sampledValue", []):
                        try:
                            value = float(sv.get("value"))
                        except (TypeError, ValueError):
                            continue
                        measurand = sv.get("measurand", "Energy.Active.Import.Register")
                        unit = sv.get("unit")
                        if measurand not in _SEEN_MEASURANDS.get(charger_id, set()):
                            _SEEN_MEASURANDS.setdefault(charger_id, set()).add(measurand)
                            logger.info("Relais %s : nouveau measurand observé « %s » (unité %s)",
                                        charger_id, measurand, unit)
                        # Normalisation kWh -> Wh (certaines bornes, dont Schneider,
                        # annoncent l'énergie en kWh). Même logique qu'en mode local
                        # (csms_local.py), pour que la valeur stockée en base et celle
                        # publiée en MQTT soient toujours cohérentes en Wh.
                        stored_value = value
                        if measurand == "Energy.Active.Import.Register" and unit and unit.lower() in ("kwh", "kw·h", "kw-h"):
                            stored_value = _to_wh(value, unit)
                            unit = "Wh"
                        db.add(MeterValue(
                            charger_id=charger_id,
                            transaction_id=transaction_id,
                            connector_id=connector_id,
                            measurand=measurand,
                            value=stored_value,
                            unit=unit,
                        ))
                        if measurand == "Power.Active.Import":
                            connector_mqtt_updates.setdefault(connector_id, {})["power_w"] = stored_value
                        elif measurand == "Energy.Active.Import.Register":
                            connector_mqtt_updates.setdefault(connector_id, {})["energy_wh"] = stored_value
                db.commit()
            elif action == "StartTransaction":
                id_tag = payload.get("idTag")
                vehicle = db.query(Vehicle).filter(Vehicle.id_tag == id_tag).first() if id_tag else None
                db.add(Transaction(
                    charger_id=charger_id,
                    connector_id=payload.get("connectorId", 1),
                    id_tag=id_tag,
                    vehicle_id=vehicle.id if vehicle else None,
                    meter_start=payload.get("meterStart"),
                    status="active",
                ))
                db.commit()
            elif action == "StopTransaction":
                # Sans le transactionId assigné par le serveur officiel (reçu
                # dans la réponse, pas la requête), on ne peut pas relier la
                # transaction de façon fiable ici en v1 : limitation connue.
                pass
            elif action == "DataTransfer":
                # Canal propriétaire d'OCPP 1.6 : certains constructeurs y font
                # transiter des infos hors mesurandes standard (ex. données TIC
                # Linky, période tarifaire, puissance foyer). On les journalise
                # pour diagnostic, sans rien en présumer.
                logger.info("Relais %s : DataTransfer vendorId=%s messageId=%s data=%s",
                            charger_id, payload.get("vendorId"),
                            payload.get("messageId"), payload.get("data"))
        finally:
            db.close()

        if charger_mqtt_updates:
            await mqtt_bridge.publish_state(charger_id, **charger_mqtt_updates)
        for cid, updates in connector_mqtt_updates.items():
            if cid and cid != 0:
                await mqtt_bridge.publish_connector_state(charger_id, cid, **updates)
    except Exception:
        logger.debug("Impossible d'analyser la trame en mode relais", exc_info=True)


async def run_relay(charger_id: str, relay_base_url: str, incoming_ws: WebSocket,
                    subprotocol: str | None, incoming_headers=None):
    """Relaie tel quel le trafic entre la borne (incoming_ws, déjà accepté) et
    le serveur OCPP officiel, en loggant passivement les métriques au passage.

    incoming_headers : en-têtes du handshake WebSocket envoyés par la borne.
    Certains serveurs (dont EcoStruxure) exigent une authentification au niveau
    du handshake, le plus souvent HTTP Basic (clé OCPP standard AuthorizationKey)
    matérialisée par un en-tête `Authorization`. On la propage donc vers l'amont,
    sinon le serveur officiel refuserait la connexion. (Le mTLS, lui, ne peut
    pas être relayé : le certificat client réside dans la borne.)
    """
    target_url = relay_base_url.rstrip("/") + "/" + charger_id
    subprotocols = [subprotocol] if subprotocol else []

    # En-têtes à repropager vers le serveur officiel : uniquement ceux qui
    # portent l'authentification / l'identification, pas les en-têtes propres au
    # tunnel WebSocket (Host, Sec-WebSocket-*, Upgrade, Connection...) que la
    # librairie cliente régénère elle-même.
    forward = {}
    if incoming_headers:
        for name in ("authorization", "x-api-key", "api-key"):
            val = incoming_headers.get(name)
            if val:
                forward["Authorization" if name == "authorization" else name] = val
        if forward:
            logger.info("Relais %s : propagation des en-têtes d'auth %s vers l'amont",
                        charger_id, list(forward.keys()))

    try:
        upstream_cm = websockets.connect(
            target_url, subprotocols=subprotocols, additional_headers=forward or None,
        )
    except TypeError:
        # Compat : anciennes versions de `websockets` utilisent extra_headers.
        upstream_cm = websockets.connect(
            target_url, subprotocols=subprotocols, extra_headers=forward or None,
        )

    async with upstream_cm as upstream:

        # Corrélation uniqueId -> action, partagée entre les deux sens pour
        # retrouver l'action d'un CALLRESULT/CALLERROR.
        pending: dict = {}

        async def charger_to_official():
            try:
                while True:
                    raw = await incoming_ws.receive_text()
                    _log_frame(charger_id, "in", raw, pending)
                    await _snoop_frame(charger_id, raw)
                    await upstream.send(raw)
            except (WebSocketDisconnect, websockets.ConnectionClosed):
                pass

        async def official_to_charger():
            try:
                async for raw in upstream:
                    _log_frame(charger_id, "out", raw, pending)
                    await incoming_ws.send_text(raw)
            except (WebSocketDisconnect, websockets.ConnectionClosed):
                pass

        import asyncio
        await asyncio.gather(charger_to_official(), official_to_charger())
