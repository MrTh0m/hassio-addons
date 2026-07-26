import json
import logging
from datetime import datetime

import websockets
from starlette.websockets import WebSocket, WebSocketDisconnect

from .db import SessionLocal
from .models import MeterValue, Transaction

logger = logging.getLogger("relay")


def _snoop_frame(charger_id: str, raw: str):
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
        try:
            if action == "MeterValues":
                connector_id = payload.get("connectorId")
                transaction_id = payload.get("transactionId")
                for mv in payload.get("meterValue", []):
                    for sv in mv.get("sampledValue", []):
                        try:
                            value = float(sv.get("value"))
                        except (TypeError, ValueError):
                            continue
                        db.add(MeterValue(
                            charger_id=charger_id,
                            transaction_id=transaction_id,
                            connector_id=connector_id,
                            measurand=sv.get("measurand", "Energy.Active.Import.Register"),
                            value=value,
                            unit=sv.get("unit"),
                        ))
                db.commit()
            elif action == "StartTransaction":
                db.add(Transaction(
                    charger_id=charger_id,
                    connector_id=payload.get("connectorId", 1),
                    id_tag=payload.get("idTag"),
                    meter_start=payload.get("meterStart"),
                    status="active",
                ))
                db.commit()
            elif action == "StopTransaction":
                # Sans le transactionId assigné par le serveur officiel (reçu
                # dans la réponse, pas la requête), on ne peut pas relier la
                # transaction de façon fiable ici en v1 : limitation connue.
                pass
        finally:
            db.close()
    except Exception:
        logger.debug("Impossible d'analyser la trame en mode relais", exc_info=True)


async def run_relay(charger_id: str, relay_base_url: str, incoming_ws: WebSocket, subprotocol: str | None):
    """Relaie tel quel le trafic entre la borne (incoming_ws, déjà accepté) et
    le serveur OCPP officiel, en loggant passivement les métriques au passage."""
    target_url = relay_base_url.rstrip("/") + "/" + charger_id
    subprotocols = [subprotocol] if subprotocol else []

    async with websockets.connect(target_url, subprotocols=subprotocols) as upstream:

        async def charger_to_official():
            try:
                while True:
                    raw = await incoming_ws.receive_text()
                    _snoop_frame(charger_id, raw)
                    await upstream.send(raw)
            except (WebSocketDisconnect, websockets.ConnectionClosed):
                pass

        async def official_to_charger():
            try:
                async for raw in upstream:
                    await incoming_ws.send_text(raw)
            except (WebSocketDisconnect, websockets.ConnectionClosed):
                pass

        import asyncio
        await asyncio.gather(charger_to_official(), official_to_charger())
