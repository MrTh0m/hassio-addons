import asyncio
import json
import logging
import os

import aiomqtt

logger = logging.getLogger("mqtt-bridge")

MQTT_HOST = os.environ.get("MQTT_HOST", "core-mosquitto")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USERNAME = os.environ.get("MQTT_USERNAME") or None
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD") or None
MQTT_ENABLED = os.environ.get("MQTT_ENABLED", "true").lower() == "true"

DISCOVERY_PREFIX = "homeassistant"
BASE_TOPIC = os.environ.get("MQTT_BASE_TOPIC", "ocppserver")

_client: aiomqtt.Client | None = None
_slug_to_id: dict[str, str] = {}


def _slug(charger_id: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in charger_id)


def _device_info(charger_id: str) -> dict:
    slug = _slug(charger_id)
    return {
        "identifiers": [f"ocppserver_{slug}"],
        "name": f"Borne {charger_id}",
        "manufacturer": "OCPP Backoffice Server",
    }


async def publish_discovery(charger_id: str, mode: str):
    """Publie (ou met à jour) les entités MQTT Discovery pour une borne.
    Appelé à chaque (re)connexion, pour que HA retrouve toujours l'état
    à jour, y compris si le mode a changé entre-temps."""
    if _client is None:
        return
    slug = _slug(charger_id)
    _slug_to_id[slug] = charger_id
    device = _device_info(charger_id)

    sensors = {
        "status": {"name": "Statut", "icon": "mdi:ev-station"},
        "power_w": {"name": "Puissance", "unit": "W", "device_class": "power", "state_class": "measurement"},
        "energy_wh": {"name": "Énergie", "unit": "Wh", "device_class": "energy", "state_class": "total_increasing"},
        "session_duration_min": {"name": "Durée de charge", "unit": "min", "icon": "mdi:timer-outline"},
    }
    for key, meta in sensors.items():
        payload = {
            "name": meta["name"],
            "unique_id": f"ocppserver_{slug}_{key}",
            "state_topic": f"{BASE_TOPIC}/{slug}/{key}",
            "device": device,
        }
        for opt in ("unit", "device_class", "state_class", "icon"):
            if opt in meta:
                payload["unit_of_measurement" if opt == "unit" else opt] = meta[opt]
        await _client.publish(f"{DISCOVERY_PREFIX}/sensor/{slug}_{key}/config", json.dumps(payload), retain=True)

    switch_topic = f"{DISCOVERY_PREFIX}/switch/{slug}_charge_control/config"
    if mode == "local":
        payload = {
            "name": "Autoriser la charge",
            "unique_id": f"ocppserver_{slug}_charge_control",
            "state_topic": f"{BASE_TOPIC}/{slug}/charge_control/state",
            "command_topic": f"{BASE_TOPIC}/{slug}/charge_control/set",
            "payload_on": "ON",
            "payload_off": "OFF",
            "device": device,
        }
        await _client.publish(switch_topic, json.dumps(payload), retain=True)
    else:
        # Mode relais : pas de pilotage possible, on retire un éventuel
        # switch resté d'un précédent mode local (payload vide = suppression
        # de l'entité côté HA, convention MQTT Discovery standard).
        await _client.publish(switch_topic, "", retain=True)


async def publish_state(charger_id: str, **values):
    if _client is None:
        return
    slug = _slug(charger_id)
    for key, value in values.items():
        if value is None:
            continue
        await _client.publish(f"{BASE_TOPIC}/{slug}/{key}", str(value), retain=True)


async def publish_charge_control_state(charger_id: str, is_charging: bool):
    await publish_state(charger_id, **{"charge_control/state": "ON" if is_charging else "OFF"})


async def _handle_command(message: aiomqtt.Message):
    from .csms_local import CONNECTED_CHARGERS

    parts = str(message.topic).split("/")
    if len(parts) != 4 or parts[0] != BASE_TOPIC or parts[2:] != ["charge_control", "set"]:
        return
    slug = parts[1]
    charger_id = _slug_to_id.get(slug)
    if not charger_id:
        logger.warning("Commande MQTT reçue pour une borne inconnue (%s)", slug)
        return
    cp = CONNECTED_CHARGERS.get(charger_id)
    if not cp:
        logger.warning("Commande MQTT pour %s ignorée : borne non connectée", charger_id)
        return

    payload = message.payload.decode() if isinstance(message.payload, (bytes, bytearray)) else str(message.payload)
    try:
        if payload == "ON":
            await cp.trigger_remote_start(1, "MQTT")
        elif payload == "OFF":
            from .db import SessionLocal
            from .models import Transaction
            db = SessionLocal()
            try:
                txn = db.query(Transaction).filter(
                    Transaction.charger_id == charger_id, Transaction.status == "active"
                ).order_by(Transaction.id.desc()).first()
                txn_id = txn.id if txn else None
            finally:
                db.close()
            if txn_id:
                await cp.trigger_remote_stop(txn_id)
    except Exception:
        logger.exception("Erreur lors du traitement de la commande MQTT pour %s", charger_id)


async def run_mqtt_bridge():
    """Boucle de fond : maintient la connexion MQTT et reconnecte
    automatiquement en cas de coupure."""
    global _client
    if not MQTT_ENABLED:
        logger.info("Pont MQTT désactivé (MQTT_ENABLED=false)")
        return
    while True:
        try:
            async with aiomqtt.Client(
                hostname=MQTT_HOST, port=MQTT_PORT,
                username=MQTT_USERNAME, password=MQTT_PASSWORD,
            ) as client:
                _client = client
                logger.info("Connecté au broker MQTT %s:%s", MQTT_HOST, MQTT_PORT)
                await client.subscribe(f"{BASE_TOPIC}/+/charge_control/set")
                async for message in client.messages:
                    await _handle_command(message)
        except Exception:
            logger.warning("Connexion MQTT indisponible, nouvelle tentative dans 10s", exc_info=True)
            _client = None
            await asyncio.sleep(10)
