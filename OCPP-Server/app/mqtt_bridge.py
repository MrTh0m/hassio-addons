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
    """Publie le capteur de statut global de la borne (connecteur 0, la
    borne elle-même au sens de la norme, pas un connecteur physique)."""
    if _client is None:
        return
    slug = _slug(charger_id)
    _slug_to_id[slug] = charger_id
    device = _device_info(charger_id)

    payload = {
        "name": "Statut borne",
        "unique_id": f"ocppserver_{slug}_status",
        "state_topic": f"{BASE_TOPIC}/{slug}/status",
        "device": device,
        "icon": "mdi:ev-station",
    }
    await _client.publish(f"{DISCOVERY_PREFIX}/sensor/{slug}_status/config", json.dumps(payload), retain=True)


async def publish_connector_discovery(charger_id: str, connector_id: int, mode: str):
    """Publie les entités MQTT Discovery pour UN connecteur physique donné
    (statut, puissance, énergie, durée de charge, et le switch de pilotage
    si la borne est en mode local). Regroupées sous le même appareil que
    la borne, mais ce sont bien des entités distinctes par connecteur."""
    if _client is None or connector_id == 0:
        return
    slug = _slug(charger_id)
    _slug_to_id[slug] = charger_id
    device = _device_info(charger_id)
    c = f"connector{connector_id}"

    sensors = {
        "status": {"name": f"Connecteur {connector_id} statut", "icon": "mdi:ev-plug-type2"},
        "power_w": {"name": f"Connecteur {connector_id} puissance", "unit": "W", "device_class": "power", "state_class": "measurement"},
        "energy_wh": {"name": f"Connecteur {connector_id} énergie", "unit": "Wh", "device_class": "energy", "state_class": "total_increasing"},
        "session_duration_min": {"name": f"Connecteur {connector_id} durée de charge", "unit": "min", "icon": "mdi:timer-outline"},
    }
    for key, meta in sensors.items():
        payload = {
            "name": meta["name"],
            "unique_id": f"ocppserver_{slug}_{c}_{key}",
            "state_topic": f"{BASE_TOPIC}/{slug}/{c}/{key}",
            "device": device,
        }
        for opt in ("unit", "device_class", "state_class", "icon"):
            if opt in meta:
                payload["unit_of_measurement" if opt == "unit" else opt] = meta[opt]
        await _client.publish(f"{DISCOVERY_PREFIX}/sensor/{slug}_{c}_{key}/config", json.dumps(payload), retain=True)

    switch_topic = f"{DISCOVERY_PREFIX}/switch/{slug}_{c}_charge_control/config"
    if mode == "local":
        payload = {
            "name": f"Connecteur {connector_id} autoriser la charge",
            "unique_id": f"ocppserver_{slug}_{c}_charge_control",
            "state_topic": f"{BASE_TOPIC}/{slug}/{c}/charge_control/state",
            "command_topic": f"{BASE_TOPIC}/{slug}/{c}/charge_control/set",
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


async def publish_connector_state(charger_id: str, connector_id: int, **values):
    if connector_id == 0:
        return
    prefixed = {f"connector{connector_id}/{k}": v for k, v in values.items()}
    await publish_state(charger_id, **prefixed)


async def publish_charge_control_state(charger_id: str, connector_id: int, is_charging: bool):
    await publish_connector_state(
        charger_id, connector_id, **{"charge_control/state": "ON" if is_charging else "OFF"}
    )


async def _handle_command(message: aiomqtt.Message):
    from .csms_local import CONNECTED_CHARGERS

    parts = str(message.topic).split("/")
    # ocppserver/{slug}/connector{N}/charge_control/set
    if (
        len(parts) != 5
        or parts[0] != BASE_TOPIC
        or not parts[2].startswith("connector")
        or parts[3:] != ["charge_control", "set"]
    ):
        return
    slug = parts[1]
    try:
        connector_id = int(parts[2].removeprefix("connector"))
    except ValueError:
        return

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
            await cp.trigger_remote_start(connector_id, "MQTT")
        elif payload == "OFF":
            from .db import SessionLocal
            from .models import Transaction
            db = SessionLocal()
            try:
                txn = db.query(Transaction).filter(
                    Transaction.charger_id == charger_id,
                    Transaction.connector_id == connector_id,
                    Transaction.status == "active",
                ).order_by(Transaction.id.desc()).first()
                txn_id = txn.id if txn else None
            finally:
                db.close()
            if txn_id:
                await cp.trigger_remote_stop(txn_id)
    except Exception:
        logger.exception("Erreur lors du traitement de la commande MQTT pour %s (connecteur %s)", charger_id, connector_id)


async def republish_all():
    """Republie la découverte (et le dernier statut connu, par borne ET par
    connecteur) pour tout ce qui est déjà en base. Appelé à chaque
    (re)connexion au broker : la découverte n'est sinon publiée qu'au moment
    où une borne se connecte au WebSocket, ce qui peut être manqué si le
    client MQTT n'est pas encore prêt à cet instant précis."""
    from .db import SessionLocal
    from .models import Charger, ConnectorStatus

    db = SessionLocal()
    try:
        chargers = db.query(Charger).all()
        connectors_by_charger: dict[str, list] = {}
        for cs in db.query(ConnectorStatus).all():
            connectors_by_charger.setdefault(cs.charger_id, []).append(cs)
    finally:
        db.close()

    for charger in chargers:
        await publish_discovery(charger.id, charger.mode.value)
        if charger.status:
            await publish_state(charger.id, status=charger.status)
        for cs in connectors_by_charger.get(charger.id, []):
            if cs.connector_id == 0:
                continue
            await publish_connector_discovery(charger.id, cs.connector_id, charger.mode.value)
            await publish_connector_state(charger.id, cs.connector_id, status=cs.status)


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
                await republish_all()
                await client.subscribe(f"{BASE_TOPIC}/+/+/charge_control/set")
                async for message in client.messages:
                    await _handle_command(message)
        except Exception:
            logger.warning("Connexion MQTT indisponible, nouvelle tentative dans 10s", exc_info=True)
            _client = None
            await asyncio.sleep(10)
