import asyncio
import json
import logging
import os
from collections import deque
from datetime import datetime, timezone

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

# File d'attente des publications sortantes. Les gestionnaires OCPP n'attendent
# jamais le broker : ils déposent leurs messages ici et rendent la main tout de
# suite, un worker dédié (voir _publisher) les envoie en arrière-plan.
# Bornée : si le broker est injoignable longtemps, les plus anciens sont
# écartés (ce sont des états retenus, seul le dernier compte ; la découverte et
# les états sont de toute façon republiés en entier à chaque reconnexion).
OUTBOX_MAX = 2000
_outbox: deque = deque(maxlen=OUTBOX_MAX)
_wakeup: asyncio.Event | None = None

# Durée max d'attente de la confirmation du broker pour UNE publication (la
# valeur par défaut d'aiomqtt est de 10 s) et nombre d'échecs consécutifs
# au-delà duquel la connexion MQTT est considérée comme morte et recréée.
PUBLISH_TIMEOUT_S = 5
MAX_CONSECUTIVE_PUBLISH_FAILURES = 3


async def _safe_publish(topic: str, payload: str, retain: bool = True):
    """Enveloppe UNIQUE autour de _client.publish() : ne laisse JAMAIS une panne
    MQTT (broker indisponible, timeout, etc.) remonter jusqu'à l'appelant.

    Point critique : ces publications sont déclenchées depuis les gestionnaires
    de messages OCPP eux-mêmes (StatusNotification, MeterValues, StartTransaction,
    ...). Constaté en prod : une simple panne MQTT temporaire a fait échouer un
    _client.publish() non protégé, l'exception n'était rattrapée nulle part, ce
    qui a fait planter la boucle de traitement OCPP et déconnecté en boucle la
    borne physique tant que le broker n'était pas revenu. Côté Home Assistant,
    une entité qui reste brièvement périmée est sans conséquence ; couper la
    connexion à une vraie borne en charge, si. La publication MQTT doit donc
    rester strictement « best effort » du point de vue du protocole OCPP.

    Second volet (incident du 10/10, borne déconnectée en boucle pendant une
    charge) : rattraper les exceptions ne suffit pas, il ne faut pas non plus
    ATTENDRE le broker. Un broker injoignable ou à moitié mort ne lève pas
    d'erreur tout de suite : chaque publish() attend sa confirmation jusqu'au
    délai d'aiomqtt (10 s). Un seul MeterValues déclenchait une dizaine de
    publications à la suite, d'où des réponses OCPP rendues ~40 s trop tard,
    au-delà du MessageTimeout (30 s) de la borne, qui coupait alors sa
    connexion WebSocket. Cette fonction ne fait donc que déposer le message
    dans une file d'attente, et rend la main immédiatement ; l'envoi réel
    est fait par _publisher(), hors du chemin de réponse OCPP."""
    if _client is None:
        return
    _outbox.append((topic, payload, retain))
    if _wakeup is not None:
        _wakeup.set()


async def _publisher(client: aiomqtt.Client, wakeup: asyncio.Event):
    """Worker d'envoi : vide la file d'attente vers le broker, un message à la
    fois, avec un délai borné. Après plusieurs échecs consécutifs la connexion
    est jugée morte : on lève, ce qui fait retomber run_mqtt_bridge dans sa
    boucle de reconnexion (et ce n'est jamais visible côté OCPP)."""
    failures = 0
    while True:
        while _outbox:
            topic, payload, retain = _outbox.popleft()
            try:
                await client.publish(topic, payload, retain=retain, timeout=PUBLISH_TIMEOUT_S)
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception:
                failures += 1
                logger.debug("Publication MQTT échouée sur %s (%d/%d)", topic, failures,
                             MAX_CONSECUTIVE_PUBLISH_FAILURES, exc_info=True)
                if failures >= MAX_CONSECUTIVE_PUBLISH_FAILURES:
                    raise ConnectionError("Broker MQTT ne répond plus aux publications")
        await wakeup.wait()
        wakeup.clear()


async def _consume_commands(client: aiomqtt.Client):
    async for message in client.messages:
        await _handle_command(message)


def _slug(charger_id: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in charger_id)


def _device_info(charger_id: str, display_name: str | None = None) -> dict:
    slug = _slug(charger_id)
    return {
        "identifiers": [f"ocppserver_{slug}"],
        "name": display_name or f"Borne {charger_id}",
        "manufacturer": "OCPP Backoffice Server",
    }


# Valeur à publier pour repasser un capteur HA à « Inconnu » (convention des
# capteurs MQTT de Home Assistant). publish_state() ignore les valeurs Python
# None (« ne rien publier »), d'où cette chaîne explicite.
UNKNOWN = "None"

# Les topics et unique_id gardent leur suffixe historique « _wh » et les
# valeurs publiées restent en Wh (rien ne change pour les entités déjà
# créées côté HA ni pour les éventuels abonnés tiers) ; c'est la découverte
# qui demande à HA de convertir et d'afficher en kWh.
_WH_TO_KWH = "{{ (value | float(0) / 1000) | round(3) }}"


def _connector_sensors(connector_id: int) -> dict:
    n = f"Connecteur {connector_id}"
    return {
        "status": {"name": f"{n} statut", "icon": "mdi:ev-plug-type2"},
        "power_w": {"name": f"{n} puissance", "unit": "W", "device_class": "power", "state_class": "measurement", "precision": 0},
        "current_a": {"name": f"{n} courant", "unit": "A", "device_class": "current", "state_class": "measurement", "precision": 1},
        "voltage_v": {"name": f"{n} tension", "unit": "V", "device_class": "voltage", "state_class": "measurement", "precision": 0},
        "energy_wh": {"name": f"{n} énergie totale", "unit": "kWh", "device_class": "energy", "state_class": "total_increasing", "value_template": _WH_TO_KWH, "precision": 1},
        "session_energy_wh": {"name": f"{n} énergie session", "unit": "kWh", "device_class": "energy", "state_class": "total_increasing", "value_template": _WH_TO_KWH, "precision": 2},
        "session_cost": {"name": f"{n} coût session", "unit": "EUR", "device_class": "monetary", "state_class": "total", "precision": 2},
        "session_duration_min": {"name": f"{n} durée de charge", "unit": "min", "device_class": "duration", "icon": "mdi:timer-outline"},
        "session_start_time": {"name": f"{n} début de session", "device_class": "timestamp"},
        "last_session_energy_wh": {"name": f"{n} dernière charge énergie", "unit": "kWh", "device_class": "energy", "value_template": _WH_TO_KWH, "precision": 2},
        "last_session_cost": {"name": f"{n} dernière charge coût", "unit": "EUR", "device_class": "monetary", "precision": 2},
    }


_VEHICLE_SENSORS = {
    "charging_at": {"name": "En charge sur", "icon": "mdi:ev-station"},
    "session_energy_wh": {"name": "Énergie session", "unit": "kWh", "device_class": "energy", "state_class": "total_increasing", "value_template": _WH_TO_KWH, "precision": 2},
    "session_cost": {"name": "Coût session", "unit": "EUR", "device_class": "monetary", "state_class": "total", "precision": 2},
    "session_duration_min": {"name": "Durée de charge", "unit": "min", "device_class": "duration", "icon": "mdi:timer-outline"},
    "session_start_time": {"name": "Début de session", "device_class": "timestamp"},
    "last_session_energy_wh": {"name": "Dernière charge énergie", "unit": "kWh", "device_class": "energy", "value_template": _WH_TO_KWH, "precision": 2},
    "last_session_cost": {"name": "Dernière charge coût", "unit": "EUR", "device_class": "monetary", "precision": 2},
    "last_session_charger": {"name": "Dernière charge borne", "icon": "mdi:ev-station"},
    "last_session_battery_pct": {"name": "Dernière charge part de batterie", "unit": "%", "icon": "mdi:battery-charging", "precision": 0},
    "odometer_km": {"name": "Odomètre (dernière charge)", "unit": "km", "device_class": "distance"},
    "km_since_last": {"name": "Km depuis la charge précédente", "unit": "km", "device_class": "distance"},
    "consumption_kwh_100km": {"name": "Consommation estimée", "unit": "kWh/100km", "icon": "mdi:speedometer", "precision": 1},
    "cost_per_100km": {"name": "Coût aux 100 km", "unit": "EUR/100km", "icon": "mdi:cash", "precision": 2},
    "battery_capacity_kwh": {"name": "Capacité batterie", "unit": "kWh", "device_class": "energy_storage", "entity_category": "diagnostic"},
    # Cumuls du mois civil en cours (fuseau d'affichage), charges terminées.
    "month_charge_count": {"name": "Charges du mois", "icon": "mdi:counter"},
    "month_energy_kwh": {"name": "Énergie du mois", "unit": "kWh", "device_class": "energy", "precision": 1},
    "month_cost": {"name": "Coût du mois", "unit": "EUR", "device_class": "monetary", "precision": 2},
    "month_km": {"name": "Distance du mois", "unit": "km", "device_class": "distance", "precision": 0},
}


def _sensor_payload(meta: dict, unique_id: str, state_topic: str, device: dict) -> dict:
    payload = {
        "name": meta["name"],
        "unique_id": unique_id,
        "state_topic": state_topic,
        "device": device,
    }
    renamed = {"unit": "unit_of_measurement", "precision": "suggested_display_precision"}
    for opt in ("unit", "device_class", "state_class", "icon", "entity_category", "value_template", "precision"):
        if opt in meta:
            payload[renamed.get(opt, opt)] = meta[opt]
    return payload


# En dessous de cette distance, la consommation et le coût aux 100 km ne
# veulent rien dire (un simple complément de charge après 2 km donnerait des
# centaines de kWh/100 km).
MIN_KM_FOR_EFFICIENCY = 10


def vehicle_efficiency(energy_wh, cost, km_since_last, battery_capacity_kwh) -> dict:
    """Indicateurs dérivés de la dernière charge d'un véhicule. Estimations :
    elles supposent que la charge a rendu à la batterie ce que les km
    parcourus depuis la charge précédente avaient consommé. Une valeur non
    calculable est renvoyée à UNKNOWN, pour ne pas laisser une ancienne
    estimation affichée à côté d'une charge qui n'a rien à voir."""
    out = {"consumption_kwh_100km": UNKNOWN, "cost_per_100km": UNKNOWN, "last_session_battery_pct": UNKNOWN}
    if km_since_last and km_since_last >= MIN_KM_FOR_EFFICIENCY:
        if energy_wh:
            out["consumption_kwh_100km"] = round(energy_wh / 1000 / km_since_last * 100, 1)
        if cost is not None:
            out["cost_per_100km"] = round(cost / km_since_last * 100, 2)
    if battery_capacity_kwh and energy_wh is not None:
        out["last_session_battery_pct"] = round(energy_wh / 1000 / battery_capacity_kwh * 100)
    return out


def vehicle_month_totals(db, vehicle_id: int, tz, now: datetime | None = None) -> dict:
    """Cumuls du mois civil en cours pour un véhicule : même règle que les
    agrégats mensuels de l'interface (api._monthly_from_rows). Seules les
    charges terminées comptent, rattachées au mois de leur début dans le
    fuseau d'affichage ; les km d'une charge sont ceux parcourus depuis la
    charge précédente du véhicule."""
    from .models import Transaction

    local_now = (now or datetime.utcnow()).replace(tzinfo=timezone.utc).astimezone(tz)
    count, energy_wh, cost, km_total = 0, 0.0, 0.0, 0.0
    prev_odo = None
    txns = db.query(Transaction).filter(
        Transaction.vehicle_id == vehicle_id,
        Transaction.start_time.isnot(None),
    ).order_by(Transaction.start_time.asc()).all()
    for txn in txns:
        km = None
        if txn.odometer_km is not None:
            if prev_odo is not None and txn.odometer_km - prev_odo > 0:
                km = txn.odometer_km - prev_odo
            prev_odo = txn.odometer_km
        if txn.status != "completed":
            continue
        start = txn.start_time.replace(tzinfo=timezone.utc).astimezone(tz)
        if (start.year, start.month) != (local_now.year, local_now.month):
            continue
        count += 1
        energy_wh += txn.energy_wh or 0.0
        cost += txn.cost or 0.0
        km_total += km or 0.0
    return {
        "month_charge_count": count,
        "month_energy_kwh": round(energy_wh / 1000.0, 2),
        "month_cost": round(cost, 2),
        "month_km": round(km_total, 1),
    }


async def publish_vehicle_month(vehicle_id: int):
    if _client is None:
        return
    from .db import SessionLocal
    from .api import _get_display_tz

    db = SessionLocal()
    try:
        values = vehicle_month_totals(db, vehicle_id, _get_display_tz(db))
    finally:
        db.close()
    await publish_vehicle_state(vehicle_id, **values)


# --- Choix du véhicule pour la prochaine charge (entité select par connecteur) ---

NO_VEHICLE = "Aucun"
# Véhicule choisi dans Home Assistant, par (borne, connecteur). En mémoire :
# au redémarrage, le choix par défaut est recalculé (voir vehicle_select_view).
SELECTED_VEHICLE: dict[tuple[str, int], int] = {}
# Connecteurs dont le choix par défaut a déjà été proposé : au-delà, un
# « Aucun » choisi par l'utilisateur est respecté et n'est pas remplacé.
_select_defaulted: set[tuple[str, int]] = set()


def _eligible_vehicles(db, charger_id: str) -> list:
    """Véhicules actifs associés à la borne ; à défaut d'association, tous
    les véhicules actifs (même tolérance que le démarrage depuis l'interface)."""
    from .models import Vehicle, VehicleCharger

    active = db.query(Vehicle).filter(Vehicle.deleted_at.is_(None))
    linked = active.join(VehicleCharger, VehicleCharger.vehicle_id == Vehicle.id).filter(
        VehicleCharger.charger_id == charger_id
    ).order_by(Vehicle.name).all()
    return linked or active.order_by(Vehicle.name).all()


def vehicle_select_view(db, charger_id: str, connector_id: int) -> tuple[list, str]:
    """Options et valeur courante du select « véhicule » d'un connecteur.
    Pendant une session, la valeur est le véhicule de la session. Sinon c'est
    le choix fait dans HA ; la première fois, le véhicule de la dernière
    charge sur ce connecteur est proposé (ou l'unique véhicule possible)."""
    from .models import Transaction, Vehicle

    key = (charger_id, connector_id)
    names = {v.id: v.name for v in _eligible_vehicles(db, charger_id)}
    options = [NO_VEHICLE] + list(dict.fromkeys(names.values()))

    active = db.query(Transaction).filter(
        Transaction.charger_id == charger_id,
        Transaction.connector_id == connector_id,
        Transaction.status == "active",
    ).order_by(Transaction.id.desc()).first()
    if active is not None:
        name = names.get(active.vehicle_id)
        if name is None and active.vehicle_id:
            vehicle = db.query(Vehicle).filter(Vehicle.id == active.vehicle_id).first()
            if vehicle:
                name = vehicle.name
                if name not in options:
                    options.append(name)
        return options, name or NO_VEHICLE

    selected = SELECTED_VEHICLE.get(key)
    if selected not in names:
        selected = None
        if key not in _select_defaulted:
            last = db.query(Transaction).filter(
                Transaction.charger_id == charger_id,
                Transaction.connector_id == connector_id,
                Transaction.vehicle_id.isnot(None),
                Transaction.status == "completed",
            ).order_by(Transaction.start_time.desc()).first()
            if last is not None and last.vehicle_id in names:
                selected = last.vehicle_id
            elif len(names) == 1:
                selected = next(iter(names))
    _select_defaulted.add(key)
    if selected is None:
        SELECTED_VEHICLE.pop(key, None)
        return options, NO_VEHICLE
    SELECTED_VEHICLE[key] = selected
    return options, names[selected]


async def publish_vehicle_select(charger_id: str, connector_id: int):
    """Publie (découverte + valeur) le select « véhicule » d'un connecteur.
    Mode local uniquement : en mode relais le serveur ne démarre pas les
    charges, l'entité est retirée."""
    if _client is None or connector_id == 0:
        return
    from .db import SessionLocal
    from .models import Charger

    slug = _slug(charger_id)
    c = f"connector{connector_id}"
    config_topic = f"{DISCOVERY_PREFIX}/select/{slug}_{c}_vehicle/config"
    view = None
    display_name = None
    db = SessionLocal()
    try:
        charger = db.query(Charger).filter(Charger.id == charger_id).first()
        if charger is not None and charger.mode.value == "local":
            view = vehicle_select_view(db, charger_id, connector_id)
            display_name = charger.display_name
    finally:
        db.close()
    if view is None:
        await _safe_publish(config_topic, "", retain=True)
        return
    options, state = view
    _slug_to_id[slug] = charger_id
    payload = {
        "name": f"Connecteur {connector_id} véhicule",
        "unique_id": f"ocppserver_{slug}_{c}_vehicle",
        "state_topic": f"{BASE_TOPIC}/{slug}/{c}/vehicle/state",
        "command_topic": f"{BASE_TOPIC}/{slug}/{c}/vehicle/set",
        "options": options,
        "icon": "mdi:car-electric",
        "device": _device_info(charger_id, display_name),
    }
    await _safe_publish(config_topic, json.dumps(payload), retain=True)
    await publish_connector_state(charger_id, connector_id, **{"vehicle/state": state})


async def refresh_vehicle_selects():
    """Republie tous les selects « véhicule » : à appeler quand la liste des
    véhicules change (création, renommage, désactivation, association)."""
    if _client is None:
        return
    from .db import SessionLocal
    from .models import ConnectorStatus

    db = SessionLocal()
    try:
        pairs = [(cs.charger_id, cs.connector_id) for cs in db.query(ConnectorStatus).all() if cs.connector_id != 0]
    finally:
        db.close()
    for charger_id, connector_id in pairs:
        await publish_vehicle_select(charger_id, connector_id)


async def _handle_vehicle_select(charger_id: str, connector_id: int, name: str):
    from .db import SessionLocal
    from .models import Transaction

    key = (charger_id, connector_id)
    db = SessionLocal()
    try:
        has_active = db.query(Transaction).filter(
            Transaction.charger_id == charger_id,
            Transaction.connector_id == connector_id,
            Transaction.status == "active",
        ).first() is not None
        # Pendant une session le véhicule est celui de la session : le choix
        # est ignoré, et la republication ci-dessous remet la bonne valeur.
        if not has_active:
            match = next((v for v in _eligible_vehicles(db, charger_id) if v.name == name), None)
            _select_defaulted.add(key)
            if match is not None:
                SELECTED_VEHICLE[key] = match.id
            else:
                SELECTED_VEHICLE.pop(key, None)
    finally:
        db.close()
    await publish_vehicle_select(charger_id, connector_id)


def start_identity(charger_id: str, connector_id: int, default_tag: str) -> str:
    """idTag à utiliser pour un démarrage à distance lancé sans véhicule
    explicite (interrupteur HA, démarrage automatique). Si un véhicule est
    choisi pour ce connecteur, la charge lui est rattachée exactement comme
    un démarrage depuis l'interface (voir api.start_charge)."""
    vehicle_id = SELECTED_VEHICLE.get((charger_id, connector_id))
    if not vehicle_id:
        return default_tag
    from .db import SessionLocal
    from .models import Vehicle
    from .csms_local import PENDING_REMOTE_STARTS

    db = SessionLocal()
    try:
        vehicle = db.query(Vehicle).filter(Vehicle.id == vehicle_id, Vehicle.deleted_at.is_(None)).first()
        if vehicle is None:
            return default_tag
        tag = vehicle.id_tag or f"REMOTE-{vehicle.id}"
    finally:
        db.close()
    PENDING_REMOTE_STARTS[(charger_id, connector_id)] = vehicle_id
    return tag


async def publish_discovery(charger_id: str, mode: str, display_name: str | None = None):
    """Publie le capteur de statut global de la borne (connecteur 0, la
    borne elle-même au sens de la norme, pas un connecteur physique).
    `display_name` : nom convivial de la borne (ex. « Borne maison »), pour que
    l'appareil côté HA porte ce nom plutôt que l'id technique brut. Repli sur
    l'id technique si non fourni ou non encore défini."""
    if _client is None:
        return
    slug = _slug(charger_id)
    _slug_to_id[slug] = charger_id
    device = _device_info(charger_id, display_name)

    payload = {
        "name": "Statut borne",
        "unique_id": f"ocppserver_{slug}_status",
        "state_topic": f"{BASE_TOPIC}/{slug}/status",
        "device": device,
        "icon": "mdi:ev-station",
        # Sur une borne à un seul connecteur, ce statut double celui du
        # connecteur : rangé en diagnostic pour ne pas encombrer les cartes.
        "entity_category": "diagnostic",
    }
    await _safe_publish(f"{DISCOVERY_PREFIX}/sensor/{slug}_status/config", json.dumps(payload), retain=True)

    # Lien OCPP borne <-> serveur (WebSocket). Les autres entités gardent leur
    # dernière valeur connue quand la borne est hors ligne ; c'est ce capteur
    # qui permet à une carte de les afficher comme « Indisponible ».
    online = {
        "name": "En ligne",
        "unique_id": f"ocppserver_{slug}_online",
        "state_topic": f"{BASE_TOPIC}/{slug}/online",
        "payload_on": "ON", "payload_off": "OFF",
        "device_class": "connectivity",
        "entity_category": "diagnostic",
        "device": device,
    }
    await _safe_publish(f"{DISCOVERY_PREFIX}/binary_sensor/{slug}_online/config", json.dumps(online), retain=True)

    # Nettoyage d'une entité obsolète : jusqu'en 0.7.0, le switch de pilotage
    # était publié au niveau de la borne (ocppserver_{slug}_charge_control).
    # Depuis 0.8.0 il est publié par connecteur ; l'ancienne entité reste
    # "fantôme" côté HA tant qu'on ne la retire pas explicitement (le
    # discovery MQTT est retenu sur le broker). Payload vide = suppression.
    await _safe_publish(f"{DISCOVERY_PREFIX}/switch/{slug}_charge_control/config", "", retain=True)


async def publish_connector_discovery(charger_id: str, connector_id: int, mode: str, display_name: str | None = None):
    """Publie les entités MQTT Discovery pour UN connecteur physique donné.
    Regroupées sous le même appareil que la borne, mais ce sont bien des
    entités distinctes par connecteur. `display_name` : voir publish_discovery.

    Deux familles d'entités énergie, à ne pas confondre :
    - energy_wh : le registre brut Energy.Active.Import.Register de la borne,
      cumulatif à vie (ne redescend jamais sauf remplacement/reset physique
      du compteur). C'est celui-ci qu'il faut ajouter au tableau de bord
      Energy de Home Assistant.
    - session_energy_wh : l'énergie de la SEULE session en cours (registre
      actuel moins meter_start de la transaction), qui repart de 0 à chaque
      nouvelle charge. Pratique pour une carte Lovelace ("combien a consommé
      cette charge"), mais NE PAS l'ajouter en plus de energy_wh au tableau
      de bord Energy : ce serait compter deux fois la même énergie.
    """
    if _client is None or connector_id == 0:
        return
    slug = _slug(charger_id)
    _slug_to_id[slug] = charger_id
    device = _device_info(charger_id, display_name)
    c = f"connector{connector_id}"

    for key, meta in _connector_sensors(connector_id).items():
        payload = _sensor_payload(meta, f"ocppserver_{slug}_{c}_{key}", f"{BASE_TOPIC}/{slug}/{c}/{key}", device)
        await _safe_publish(f"{DISCOVERY_PREFIX}/sensor/{slug}_{c}_{key}/config", json.dumps(payload), retain=True)

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
        await _safe_publish(switch_topic, json.dumps(payload), retain=True)
    else:
        # Mode relais : pas de pilotage possible, on retire un éventuel
        # switch resté d'un précédent mode local (payload vide = suppression
        # de l'entité côté HA, convention MQTT Discovery standard).
        await _safe_publish(switch_topic, "", retain=True)


def _vehicle_device_info(vehicle_id: int, name: str) -> dict:
    return {
        "identifiers": [f"ocppserver_vehicle_{vehicle_id}"],
        "name": name,
        "manufacturer": "OCPP Backoffice Server",
        "model": "Véhicule",
    }


async def publish_vehicle_discovery(vehicle_id: int, name: str):
    """Publie les entités MQTT Discovery d'UN véhicule, en tant qu'appareil HA
    à part entière (pas de simples capteurs texte accrochés à la borne) : le
    véhicule reste le même appareil quelle que soit la borne utilisée pour le
    recharger, ce qui permet une carte Lovelace dédiée à la voiture."""
    if _client is None:
        return
    device = _vehicle_device_info(vehicle_id, name)
    v = f"vehicle{vehicle_id}"

    payload = {
        "name": "En charge",
        "unique_id": f"ocppserver_{v}_is_charging",
        "state_topic": f"{BASE_TOPIC}/{v}/is_charging",
        "payload_on": "ON", "payload_off": "OFF",
        "device_class": "battery_charging",
        "device": device,
    }
    await _safe_publish(f"{DISCOVERY_PREFIX}/binary_sensor/{v}_is_charging/config", json.dumps(payload), retain=True)

    for key, meta in _VEHICLE_SENSORS.items():
        payload = _sensor_payload(meta, f"ocppserver_{v}_{key}", f"{BASE_TOPIC}/{v}/{key}", device)
        await _safe_publish(f"{DISCOVERY_PREFIX}/sensor/{v}_{key}/config", json.dumps(payload), retain=True)


async def unpublish_vehicle_discovery(vehicle_id: int):
    """Retire toutes les entités MQTT d'un véhicule (payload vide = suppression
    côté HA, même convention que le nettoyage d'entités obsolètes des bornes).
    À appeler uniquement sur une suppression DÉFINITIVE : une simple
    désactivation (réversible) laisse les entités en place."""
    if _client is None:
        return
    v = f"vehicle{vehicle_id}"
    await _safe_publish(f"{DISCOVERY_PREFIX}/binary_sensor/{v}_is_charging/config", "", retain=True)
    for key in _VEHICLE_SENSORS:
        await _safe_publish(f"{DISCOVERY_PREFIX}/sensor/{v}_{key}/config", "", retain=True)


async def unpublish_charger_discovery(charger_id: str, connector_ids: list[int]):
    """Retire toutes les entités MQTT d'une borne ET de ses connecteurs (payload
    vide = suppression côté HA, même convention que unpublish_vehicle_discovery
    ci-dessus). À appeler uniquement sur une suppression DÉFINITIVE (voir
    hard_delete_charger dans api.py) : une simple désactivation (réversible,
    la borne peut se reconnecter et redevenir active toute seule) laisse les
    entités en place."""
    if _client is None:
        return
    slug = _slug(charger_id)
    await _safe_publish(f"{DISCOVERY_PREFIX}/sensor/{slug}_status/config", "", retain=True)
    await _safe_publish(f"{DISCOVERY_PREFIX}/binary_sensor/{slug}_online/config", "", retain=True)
    await _safe_publish(f"{DISCOVERY_PREFIX}/switch/{slug}_charge_control/config", "", retain=True)
    for connector_id in connector_ids:
        if connector_id == 0:
            continue
        c = f"connector{connector_id}"
        for key in _connector_sensors(connector_id):
            await _safe_publish(f"{DISCOVERY_PREFIX}/sensor/{slug}_{c}_{key}/config", "", retain=True)
        await _safe_publish(f"{DISCOVERY_PREFIX}/switch/{slug}_{c}_charge_control/config", "", retain=True)
        await _safe_publish(f"{DISCOVERY_PREFIX}/select/{slug}_{c}_vehicle/config", "", retain=True)
        SELECTED_VEHICLE.pop((charger_id, connector_id), None)
        _select_defaulted.discard((charger_id, connector_id))
    _slug_to_id.pop(slug, None)


async def publish_vehicle_state(vehicle_id: int, **values):
    if _client is None:
        return
    v = f"vehicle{vehicle_id}"
    for key, value in values.items():
        if value is None:
            continue
        await _safe_publish(f"{BASE_TOPIC}/{v}/{key}", str(value), retain=True)


async def publish_vehicle_last_charge(vehicle_id: int):
    """Recalcule et republie le résumé « dernière charge » d'un véhicule
    (énergie, coût, borne, odomètre, km depuis la charge précédente) à partir
    de la base de données. Autonome (fait sa propre requête) pour pouvoir être
    appelée aussi bien depuis csms_local.py (fin de session OCPP réelle) que
    depuis api.py (édition manuelle a posteriori d'une session ou charge
    externe) : l'odomètre en particulier n'est en général renseigné par
    l'utilisateur qu'APRÈS coup, pas au moment même de StopTransaction, donc
    ce deuxième appelant est le cas d'usage le plus fréquent en pratique."""
    if _client is None:
        return
    # Les cumuls du mois dépendent des mêmes événements (fin de session,
    # édition d'une charge ou de son odomètre) : republiés au même moment.
    await publish_vehicle_month(vehicle_id)
    from .db import SessionLocal
    from .models import Transaction, Charger, Vehicle

    db = SessionLocal()
    try:
        last = db.query(Transaction).filter(
            Transaction.vehicle_id == vehicle_id,
            Transaction.status == "completed",
        ).order_by(Transaction.start_time.desc()).first()
        if not last:
            return
        km_since_last = None
        if last.odometer_km is not None and last.start_time:
            prev = db.query(Transaction).filter(
                Transaction.vehicle_id == vehicle_id,
                Transaction.odometer_km.isnot(None),
                Transaction.id != last.id,
                Transaction.start_time.isnot(None),
                Transaction.start_time < last.start_time,
            ).order_by(Transaction.start_time.desc()).first()
            if prev and prev.odometer_km is not None:
                delta = last.odometer_km - prev.odometer_km
                if delta > 0:
                    km_since_last = round(delta, 1)
        charger_label = None
        if last.charger_id:
            charger_obj = db.query(Charger).filter(Charger.id == last.charger_id).first()
            charger_label = (charger_obj.display_name if charger_obj else None) or last.charger_id
        elif last.is_external:
            charger_label = last.location_label or "Externe"
        values = {}
        if last.energy_wh is not None:
            values["last_session_energy_wh"] = last.energy_wh
        if last.cost is not None:
            values["last_session_cost"] = last.cost
        if charger_label:
            values["last_session_charger"] = charger_label
        if last.odometer_km is not None:
            values["odometer_km"] = last.odometer_km
        if km_since_last is not None:
            values["km_since_last"] = km_since_last
        vehicle = db.query(Vehicle).filter(Vehicle.id == vehicle_id).first()
        values.update(vehicle_efficiency(
            last.energy_wh, last.cost, km_since_last,
            vehicle.battery_capacity_kwh if vehicle else None,
        ))
    finally:
        db.close()

    if values:
        await publish_vehicle_state(vehicle_id, **values)


async def publish_state(charger_id: str, **values):
    if _client is None:
        return
    slug = _slug(charger_id)
    for key, value in values.items():
        if value is None:
            continue
        await _safe_publish(f"{BASE_TOPIC}/{slug}/{key}", str(value), retain=True)


async def publish_connector_state(charger_id: str, connector_id: int, **values):
    if connector_id == 0:
        return
    prefixed = {f"connector{connector_id}/{k}": v for k, v in values.items()}
    await publish_state(charger_id, **prefixed)


async def publish_online(charger_id: str, online: bool):
    await publish_state(charger_id, online="ON" if online else "OFF")


async def publish_connector_session_idle(charger_id: str, connector_id: int):
    """Connecteur libre (Available) : remet à zéro tout ce qui décrit une
    session EN COURS. Sans ça, les valeurs retenues de la dernière session
    (énergie, coût, durée, début) restaient affichées jusqu'à la charge
    suivante, en doublon de « dernière charge », et la tension résiduelle
    lue à vide (quelques volts) passait pour une mesure. Appelé au retour à
    Available et non à la fin de la transaction : en Finishing (câble encore
    branché) le bilan de la session reste donc lisible."""
    await publish_connector_state(
        charger_id, connector_id,
        session_energy_wh=0, session_cost=0, session_duration_min=0,
        session_start_time=UNKNOWN, power_w=0, current_a=0, voltage_v=UNKNOWN,
    )


async def publish_vehicle_session_idle(vehicle_id: int):
    """Véhicule sans session en cours : même remise à zéro que pour le
    connecteur, début de session compris."""
    await publish_vehicle_state(
        vehicle_id, is_charging="OFF", charging_at="Aucune",
        session_energy_wh=0, session_cost=0, session_duration_min=0,
        session_start_time=UNKNOWN,
    )


async def publish_charge_control_state(charger_id: str, connector_id: int, is_charging: bool):
    await publish_connector_state(
        charger_id, connector_id, **{"charge_control/state": "ON" if is_charging else "OFF"}
    )


async def _handle_command(message: aiomqtt.Message):
    from .csms_local import CONNECTED_CHARGERS

    parts = str(message.topic).split("/")
    # ocppserver/{slug}/connector{N}/charge_control/set
    # ocppserver/{slug}/connector{N}/vehicle/set
    if (
        len(parts) != 5
        or parts[0] != BASE_TOPIC
        or not parts[2].startswith("connector")
        or parts[3] not in ("charge_control", "vehicle")
        or parts[4] != "set"
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
    if parts[3] == "vehicle":
        # Le choix du véhicule ne demande pas que la borne soit connectée.
        name = message.payload.decode() if isinstance(message.payload, (bytes, bytearray)) else str(message.payload)
        try:
            await _handle_vehicle_select(charger_id, connector_id, name)
        except Exception:
            logger.exception("Erreur lors du choix du véhicule pour %s (connecteur %s)", charger_id, connector_id)
        return
    cp = CONNECTED_CHARGERS.get(charger_id)
    if not cp:
        logger.warning("Commande MQTT pour %s ignorée : borne non connectée", charger_id)
        return

    payload = message.payload.decode() if isinstance(message.payload, (bytes, bytearray)) else str(message.payload)
    try:
        if payload == "ON":
            await cp.trigger_remote_start(connector_id, start_identity(charger_id, connector_id, "MQTT"))
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
    client MQTT n'est pas encore prêt à cet instant précis. Republie aussi les
    véhicules (appareils MQTT indépendants des bornes), avec leur vrai statut
    de charge (pas juste la découverte) : sans ça, un véhicule qui n'a encore
    jamais chargé depuis l'introduction de cette fonctionnalité resterait sur
    « Inconnu » côté HA pour En charge/En charge sur/les compteurs de session,
    même s'il ne charge de toute évidence pas."""
    from .db import SessionLocal
    from .models import Charger, ConnectorStatus, Vehicle, Transaction

    db = SessionLocal()
    try:
        chargers = db.query(Charger).all()
        chargers_by_id = {c.id: c for c in chargers}
        connectors_by_charger: dict[str, list] = {}
        for cs in db.query(ConnectorStatus).all():
            connectors_by_charger.setdefault(cs.charger_id, []).append(cs)
        vehicles = db.query(Vehicle).filter(Vehicle.deleted_at.is_(None)).all()
        active_by_vehicle: dict[int, Transaction] = {}
        active_connectors: set[tuple] = set()
        for txn in db.query(Transaction).filter(Transaction.status == "active").all():
            active_connectors.add((txn.charger_id, txn.connector_id))
            if txn.vehicle_id is not None:
                active_by_vehicle.setdefault(txn.vehicle_id, txn)
    finally:
        db.close()

    for charger in chargers:
        await publish_discovery(charger.id, charger.mode.value, charger.display_name)
        if charger.status:
            await publish_state(charger.id, status=charger.status)
        if charger.mode.value == "local":
            # En mode relais la connexion n'est pas suivie dans
            # CONNECTED_CHARGERS : la valeur retenue (publiée à la connexion
            # et à la déconnexion, voir main.py) reste la référence.
            from .csms_local import CONNECTED_CHARGERS
            await publish_online(charger.id, charger.id in CONNECTED_CHARGERS)
        for cs in connectors_by_charger.get(charger.id, []):
            if cs.connector_id == 0:
                continue
            await publish_connector_discovery(charger.id, cs.connector_id, charger.mode.value, charger.display_name)
            await publish_connector_state(charger.id, cs.connector_id, status=cs.status)
            if cs.status == "Available" and (charger.id, cs.connector_id) not in active_connectors:
                await publish_connector_session_idle(charger.id, cs.connector_id)
            await publish_vehicle_select(charger.id, cs.connector_id)

    for vehicle in vehicles:
        await publish_vehicle_discovery(vehicle.id, vehicle.name)
        if vehicle.battery_capacity_kwh is not None:
            await publish_vehicle_state(vehicle.id, battery_capacity_kwh=vehicle.battery_capacity_kwh)
        active = active_by_vehicle.get(vehicle.id)
        if active:
            charger_obj = chargers_by_id.get(active.charger_id)
            label = (charger_obj.display_name if charger_obj else None) or active.charger_id or "?"
            await publish_vehicle_state(
                vehicle.id, is_charging="ON",
                charging_at=f"{label} / Connecteur {active.connector_id}",
            )
        else:
            await publish_vehicle_session_idle(vehicle.id)
        # Résumé « dernière charge » et indicateurs dérivés : republiés ici
        # pour qu'ils existent dès la mise à jour, sans attendre une charge.
        await publish_vehicle_last_charge(vehicle.id)


async def _hourly_refresh():
    """Les cumuls du mois repartent de zéro au changement de mois, même sans
    nouvelle charge : republiés toutes les heures."""
    from .db import SessionLocal
    from .models import Vehicle

    while True:
        await asyncio.sleep(3600)
        try:
            db = SessionLocal()
            try:
                ids = [v.id for v in db.query(Vehicle).filter(Vehicle.deleted_at.is_(None)).all()]
            finally:
                db.close()
            for vehicle_id in ids:
                await publish_vehicle_month(vehicle_id)
        except Exception:
            logger.debug("Rafraîchissement horaire MQTT échoué", exc_info=True)


async def run_mqtt_bridge():
    """Boucle de fond : maintient la connexion MQTT et reconnecte
    automatiquement en cas de coupure."""
    global _client, _wakeup
    if not MQTT_ENABLED:
        logger.info("Pont MQTT désactivé (MQTT_ENABLED=false)")
        return
    while True:
        try:
            async with aiomqtt.Client(
                hostname=MQTT_HOST, port=MQTT_PORT,
                username=MQTT_USERNAME, password=MQTT_PASSWORD,
            ) as client:
                # File repartie de zéro : republish_all() ci-dessous renvoie
                # tout l'état courant, les anciens messages sont périmés.
                _outbox.clear()
                wakeup = asyncio.Event()
                _wakeup = wakeup
                _client = client
                logger.info("Connecté au broker MQTT %s:%s", MQTT_HOST, MQTT_PORT)
                await republish_all()
                await client.subscribe(f"{BASE_TOPIC}/+/+/charge_control/set")
                await client.subscribe(f"{BASE_TOPIC}/+/+/vehicle/set")
                # Envoi et réception de commandes tournent côte à côte ; si
                # l'un échoue, l'autre est annulé et on se reconnecte.
                async with asyncio.TaskGroup() as tg:
                    tg.create_task(_publisher(client, wakeup))
                    tg.create_task(_consume_commands(client))
                    tg.create_task(_hourly_refresh())
        except Exception:
            logger.warning("Connexion MQTT indisponible, nouvelle tentative dans 10s", exc_info=True)
            _client = None
            _wakeup = None
            _outbox.clear()
            await asyncio.sleep(10)
