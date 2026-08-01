import asyncio
import logging
import os

from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse
from starlette.websockets import WebSocketDisconnect
from sqlalchemy.orm import Session

from .db import init_db, SessionLocal
from .models import Charger, ChargerMode
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


@app.on_event("startup")
async def on_startup():
    init_db()
    asyncio.create_task(mqtt_bridge.run_mqtt_bridge())


APP_VERSION = "0.9.0"


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
                await cp.start()
            finally:
                CONNECTED_CHARGERS.pop(charge_point_id, None)
    except (ConnectionError, WebSocketDisconnect):
        logger.info("Borne %s déconnectée", charge_point_id)
    except Exception:
        logger.exception("Erreur sur la connexion OCPP de %s", charge_point_id)
