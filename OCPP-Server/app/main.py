import asyncio
import logging
import os
from datetime import datetime

from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse
from starlette.websockets import WebSocketDisconnect
from sqlalchemy.orm import Session
from ocpp.v16 import call
from ocpp.v16.enums import MessageTrigger

from .db import init_db, SessionLocal
from .models import Charger, ChargerMode, Transaction, ConnectorStatus
from .pricing import freeze_transaction_cost
from .api import router as api_router
from .ws_adapter import StarletteWebSocketAdapter
from .csms_local import LocalChargePoint, CONNECTED_CHARGERS
from .relay import run_relay
from . import mqtt_bridge

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ocpp-server")

app = FastAPI(title="OCPP Server")
app.include_router(api_router)

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


# Sert la même page directement à la racine ET sur /admin, sans redirection.
# Une redirection avec un chemin absolu ("/admin") fait sortir le navigateur
# du sous-chemin dynamique de l'ingress de Home Assistant (il atterrit sur
# "/admin" du frontend HA lui-même, d'où le "404 Not Found" observé).
@app.get("/")
@app.get("/admin")
def admin_page():
    return FileResponse(os.path.join(STATIC_DIR, "admin.html"))


def _reconcile_stale_transactions():
    """Au démarrage : clôture toute transaction encore "active" en base dont
    le connecteur est en réalité déjà "Available" (cas d'une session restée
    bloquée d'avant l'introduction de la clôture automatique, ou d'une
    coupure survenue pendant que le serveur était arrêté). Le correctif
    réactif (voir csms_local.py) ne se déclenche que sur un NOUVEAU
    StatusNotification ; celui-ci rattrape les cas déjà figés en base."""
    db = SessionLocal()
    try:
        active_transactions = db.query(Transaction).filter(Transaction.status == "active").all()
        closed = 0
        for txn in active_transactions:
            status_row = db.query(ConnectorStatus).filter(
                ConnectorStatus.charger_id == txn.charger_id,
                ConnectorStatus.connector_id == txn.connector_id,
            ).first()
            if status_row and status_row.status == "Available":
                txn.stop_time = datetime.utcnow()
                if txn.meter_stop is None:
                    txn.meter_stop = txn.meter_start
                txn.status = "completed"
                db.flush()
                freeze_transaction_cost(db, txn)
                closed += 1
        if closed:
            db.commit()
            logger.info("Rattrapage au démarrage : %d session(s) restée(s) active(s) à tort clôturée(s)", closed)
    finally:
        db.close()


@app.on_event("startup")
async def on_startup():
    init_db()
    _reconcile_stale_transactions()
    asyncio.create_task(mqtt_bridge.run_mqtt_bridge())


APP_VERSION = "0.14.0"


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/api/version")
def get_version():
    return {"version": APP_VERSION}


def _pick_subprotocol(websocket: WebSocket) -> str | None:
    offered = websocket.headers.get("sec-websocket-protocol", "")
    offered_list = [p.strip() for p in offered.split(",") if p.strip()]
    for preferred in ("ocpp1.6", "ocpp2.0.1", "ocpp2.0"):
        if preferred in offered_list:
            return preferred
    return offered_list[0] if offered_list else None


async def _refresh_connector_statuses(cp, charger_id: str):
    """Redemande explicitement à la borne qui vient de se (re)connecter le
    statut réel de chacun de ses connecteurs, plutôt que d'attendre qu'elle
    le renvoie spontanément. Après une coupure, c'est le seul moyen fiable
    de savoir si une charge est réellement toujours en cours. Si la borne ne
    supporte pas TriggerMessage (RemoteTrigger n'est pas systématiquement
    implémenté), on l'ignore silencieusement : elle renverra son statut au
    prochain changement d'état comme avant."""
    db = SessionLocal()
    try:
        connector_ids = [
            row[0] for row in
            db.query(ConnectorStatus.connector_id).filter(
                ConnectorStatus.charger_id == charger_id, ConnectorStatus.connector_id != 0
            ).distinct().all()
        ]
    finally:
        db.close()
    for connector_id in connector_ids:
        try:
            await cp.call(call.TriggerMessage(
                requested_message=MessageTrigger.status_notification, connector_id=connector_id
            ))
        except Exception:
            logger.debug("TriggerMessage StatusNotification non supporté par %s", charger_id, exc_info=True)


@app.websocket("/ocpp/{charge_point_id}")
async def ocpp_endpoint(websocket: WebSocket, charge_point_id: str):
    subprotocol = _pick_subprotocol(websocket)
    await websocket.accept(subprotocol=subprotocol)
    logger.info("Nouvelle connexion OCPP : %s (sous-protocole %s)", charge_point_id, subprotocol)

    db: Session = SessionLocal()
    try:
        charger = db.query(Charger).filter(Charger.id == charge_point_id).first()
        if not charger:
            # Découverte automatique : une borne inconnue est enregistrée
            # en mode local par défaut, modifiable ensuite via l'API.
            charger = Charger(id=charge_point_id, mode=ChargerMode.local)
            db.add(charger)
            db.commit()
        mode = charger.mode
        relay_url = charger.relay_url
    finally:
        db.close()

    await mqtt_bridge.publish_discovery(charge_point_id, mode.value)

    try:
        if mode == ChargerMode.relay:
            if not relay_url:
                logger.error("Borne %s en mode relais sans relay_url configurée", charge_point_id)
                await websocket.close()
                return
            await run_relay(charge_point_id, relay_url, websocket, subprotocol)
        else:
            connection = StarletteWebSocketAdapter(websocket)
            cp = LocalChargePoint(charge_point_id, connection)
            CONNECTED_CHARGERS[charge_point_id] = cp
            try:
                asyncio.create_task(_refresh_connector_statuses(cp, charge_point_id))
                await cp.start()
            finally:
                CONNECTED_CHARGERS.pop(charge_point_id, None)
    except (ConnectionError, WebSocketDisconnect):
        logger.info("Borne %s déconnectée", charge_point_id)
    except Exception:
        logger.exception("Erreur sur la connexion OCPP de %s", charge_point_id)
