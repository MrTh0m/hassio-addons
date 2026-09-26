"""Bandeau "État de santé" de l'onglet Débug.

Détecte des anomalies passées ou en cours à partir de ce qui est déjà en
base ou dans les journaux en mémoire (ocpp_logs, server_logs), sans nouvelle
collecte de données. Chaque indicateur est un petit contrôle indépendant,
protégé individuellement (voir compute_health) : une panne de l'un ne doit
jamais empêcher le calcul des autres ni faire échouer toute la route.

La plupart de ces indicateurs reprennent, sous forme de vigie permanente,
des incidents réels déjà rencontrés en production (voir CHANGELOG) plutôt
que d'avoir à les reconstituer après coup en fouillant les exports CSV.
"""

from datetime import datetime, timedelta

from .db import SessionLocal
from .models import Charger, ConnectorStatus, Transaction, Vehicle
from . import ocpp_logs, server_logs
from .csms_local import CONNECTED_CHARGERS, PENDING_REBOOT_KEYS

# Cohérent avec le seuil déjà utilisé côté planificateur (scheduler.py) pour
# redemander automatiquement le statut d'un connecteur resté sur "Finishing".
STUCK_FINISHING_S = 180
STALE_TRANSACTION_H = 12
ZERO_ENERGY_MIN_COST = 0.01
ZERO_ENERGY_MIN_DURATION_MIN = 5
NULL_DURATION_MAX_S = 60
ANTI_TRIPPING_FAULT_WINDOW_MIN = 5
REPEATED_REJECT_WINDOW_MIN = 30
REPEATED_REJECT_THRESHOLD = 2
RECONNECT_STORM_WINDOW_S = 120
RECONNECT_STORM_THRESHOLD = 3
LOOKBACK_H = 24
PENDING_KEY_STALE_DAYS = 3
IMPLAUSIBLE_KWH_100KM_LOW = 5
IMPLAUSIBLE_KWH_100KM_HIGH = 40
STUCK_PREPARING_AFTER_FAILED_START_MIN = 10


def _badge(key, label, severity, count, items=None, detail=None):
    return {
        "key": key, "label": label, "severity": severity, "count": count,
        "items": items or [], "detail": detail,
    }


def _check_connecteurs_bloques():
    """"Finishing" est censé être transitoire (voir scheduler.py) ;
    "Faulted" est toujours anormal. Les deux sont distingués dans les items
    mais regroupés sous un même badge."""
    db = SessionLocal()
    try:
        cutoff = datetime.utcnow() - timedelta(seconds=STUCK_FINISHING_S)
        rows = db.query(ConnectorStatus, Charger).join(
            Charger, Charger.id == ConnectorStatus.charger_id
        ).filter(
            ConnectorStatus.connector_id != 0,
            ConnectorStatus.status.in_(["Finishing", "Faulted"]),
        ).all()
        items = []
        for cs, charger in rows:
            if cs.status == "Finishing" and cs.updated_at and cs.updated_at >= cutoff:
                continue
            since_min = round((datetime.utcnow() - cs.updated_at).total_seconds() / 60, 1) if cs.updated_at else None
            items.append({
                "charger_id": cs.charger_id,
                "charger_name": charger.display_name or cs.charger_id,
                "connector_id": cs.connector_id,
                "status": cs.status,
                "since_min": since_min,
            })
        severity = "danger" if any(i["status"] == "Faulted" for i in items) else ("warn" if items else "ok")
        return _badge("connecteurs_bloques", "Connecteurs en défaut ou bloqués", severity, len(items), items)
    finally:
        db.close()


def _check_transactions_fantomes():
    """Filet indépendant de celui déjà posé sur Preparing/Available (voir
    csms_local.on_status_notification) : au cas où un autre enchaînement
    laisserait une session ouverte sans jamais repasser par ces statuts."""
    db = SessionLocal()
    try:
        cutoff = datetime.utcnow() - timedelta(hours=STALE_TRANSACTION_H)
        rows = db.query(Transaction).filter(
            Transaction.status == "active",
            Transaction.start_time < cutoff,
        ).all()
        items = [{
            "id": t.id, "charger_id": t.charger_id, "connector_id": t.connector_id,
            "since_hours": round((datetime.utcnow() - t.start_time).total_seconds() / 3600, 1),
        } for t in rows]
        return _badge("transactions_fantomes", "Sessions actives depuis anormalement longtemps",
                      "danger" if items else "ok", len(items), items)
    finally:
        db.close()


def _check_charges_sans_capacite():
    """Motif de la session 20 (0.00 kWh affiché malgré un coût et une durée
    réels) : corrigé au niveau du calcul (pricing.py), ce contrôle sert à
    repérer les sessions déjà enregistrées avant le correctif, ou toute
    récidive sous une autre forme."""
    db = SessionLocal()
    try:
        rows = db.query(Transaction).filter(
            Transaction.status == "completed",
            Transaction.is_external.is_(False),
            (Transaction.energy_wh.is_(None)) | (Transaction.energy_wh == 0),
        ).order_by(Transaction.start_time.desc()).limit(500).all()
        items = []
        for t in rows:
            duration_min = None
            if t.start_time and t.stop_time:
                duration_min = (t.stop_time - t.start_time).total_seconds() / 60
            suspect = (t.cost or 0) > ZERO_ENERGY_MIN_COST or (duration_min or 0) > ZERO_ENERGY_MIN_DURATION_MIN
            if suspect:
                items.append({
                    "id": t.id, "charger_id": t.charger_id,
                    "cost": t.cost, "duration_min": round(duration_min, 1) if duration_min else None,
                })
        return _badge("charges_sans_capacite", "Sessions à 0 kWh malgré un coût ou une durée réels",
                      "warn" if items else "ok", len(items), items)
    finally:
        db.close()


def _check_charges_duree_nulle():
    """Repère les sessions "fantômes" comme la transaction 16 du 12/09
    (12 secondes, 0 kWh) : pas forcément une panne, mais un artefact à
    vérifier."""
    db = SessionLocal()
    try:
        rows = db.query(Transaction).filter(
            Transaction.status == "completed",
            Transaction.is_external.is_(False),
            Transaction.start_time.isnot(None),
            Transaction.stop_time.isnot(None),
        ).order_by(Transaction.start_time.desc()).limit(500).all()
        items = []
        for t in rows:
            duration_s = (t.stop_time - t.start_time).total_seconds()
            if duration_s < NULL_DURATION_MAX_S:
                items.append({"id": t.id, "charger_id": t.charger_id, "duration_s": round(duration_s, 1)})
        return _badge("charges_duree_nulle", "Sessions de durée quasi nulle (artefacts probables)",
                      "warn" if items else "ok", len(items), items)
    finally:
        db.close()


def _check_orages_reseau():
    """Compte les rafales de "Nouvelle connexion OCPP"/"déconnectée"
    rapprochées dans le temps (voir l'incident du 11/09 : plantage
    websocket.send après websocket.close pendant une bourrasque de
    reconnexions). Ne distingue pas par borne : dans un usage mono-borne,
    une rafale globale est déjà le signal utile."""
    entries = server_logs.get_entries(limit=2000)
    cutoff = datetime.utcnow() - timedelta(hours=LOOKBACK_H)
    relevant = []
    for e in entries:
        if "Nouvelle connexion OCPP" not in e["message"] and "déconnectée" not in e["message"]:
            continue
        ts = datetime.fromisoformat(e["ts"])
        if ts < cutoff:
            continue
        relevant.append(ts)
    relevant.sort()
    storm_count = 0
    i = 0
    n = len(relevant)
    while i < n:
        j = i
        while j < n and (relevant[j] - relevant[i]).total_seconds() <= RECONNECT_STORM_WINDOW_S:
            j += 1
        if (j - i) >= RECONNECT_STORM_THRESHOLD:
            storm_count += 1
            i = j
        else:
            i += 1
    return _badge("orages_reseau", "Bourrasques de reconnexion réseau (24h)",
                  "warn" if storm_count else "ok", storm_count,
                  detail=f"{len(relevant)} événements de (dé)connexion sur 24h")


def _check_coupure_mqtt():
    """Distincte des orages réseau OCPP : casse silencieusement les capteurs
    Home Assistant sans forcément affecter la charge elle-même."""
    entries = server_logs.get_entries(logger="mqtt-bridge", limit=1000)
    cutoff = datetime.utcnow() - timedelta(hours=LOOKBACK_H)
    items = [e for e in entries if e["level"] in ("WARNING", "ERROR") and datetime.fromisoformat(e["ts"]) >= cutoff]
    return _badge("coupure_mqtt", "Coupures MQTT (24h)", "warn" if items else "ok", len(items))


def _check_erreurs_recentes():
    """Aurait fait remonter le crash websocket.send/websocket.close (0.19.27)
    immédiatement, sans avoir à le débusquer dans les logs bruts."""
    entries = server_logs.get_entries(limit=2000)
    cutoff = datetime.utcnow() - timedelta(hours=LOOKBACK_H)
    items = [e for e in entries if e["level"] in ("ERROR", "CRITICAL") and datetime.fromisoformat(e["ts"]) >= cutoff]
    return _badge("erreurs_recentes", "Erreurs serveur (24h)", "danger" if items else "ok", len(items))


def _check_anti_tripping_puis_defaut():
    """L'alerte qu'on avait évoquée dès l'incident du 09/09 sans jamais
    l'implémenter : une suspension "Anti-Tripping" suivie d'un Faulted peu
    après, plutôt qu'une StatusNotification isolée."""
    entries = list(reversed(ocpp_logs.get_entries(action="StatusNotification", limit=2000)))
    items = []
    anti_trip = {}
    for e in entries:
        payload = e.get("payload") or {}
        key = (e["charger_id"], e["connector_id"])
        ts = datetime.fromisoformat(e["ts"])
        if payload.get("info") == "Anti-Tripping":
            anti_trip[key] = ts
        elif payload.get("status") == "Faulted" and key in anti_trip:
            delta_min = (ts - anti_trip[key]).total_seconds() / 60
            if delta_min <= ANTI_TRIPPING_FAULT_WINDOW_MIN:
                items.append({"charger_id": e["charger_id"], "connector_id": e["connector_id"], "ts": e["ts"]})
            del anti_trip[key]
    return _badge("anti_tripping_puis_defaut", "Anti-tripping suivi d'un défaut",
                  "danger" if items else "ok", len(items), items)


def _check_demarrages_refuses():
    """Motif de la soirée du 18/09 (1 Accepted puis 5 Rejected en 10 minutes,
    aucune charge n'a démarré) : repose sur l'enregistrement du résultat
    Accepted/Rejected/Blocked, ajouté dans csms_local.py spécifiquement pour
    cet indicateur (jusque-là visible uniquement dans le journal brut)."""
    cutoff = datetime.utcnow() - timedelta(minutes=REPEATED_REJECT_WINDOW_MIN)
    counts: dict[str, int] = {}
    for action in ("StartTransaction.conf", "RemoteStartTransaction.conf"):
        for e in ocpp_logs.get_entries(action=action, limit=500):
            ts = datetime.fromisoformat(e["ts"])
            if ts < cutoff:
                continue
            if (e.get("payload") or {}).get("status") != "Accepted":
                counts[e["charger_id"]] = counts.get(e["charger_id"], 0) + 1
    items = [{"charger_id": cid, "count": n} for cid, n in counts.items() if n >= REPEATED_REJECT_THRESHOLD]
    return _badge("demarrages_refuses", "Démarrages refusés à répétition (30 min)",
                  "warn" if items else "ok", sum(i["count"] for i in items), items)


def _check_cles_en_attente():
    now = datetime.utcnow()
    items = []
    for charger_id, keys in PENDING_REBOOT_KEYS.items():
        for key, since in keys.items():
            days = (now - since).total_seconds() / 86400
            if days >= PENDING_KEY_STALE_DAYS:
                items.append({"charger_id": charger_id, "key": key, "days": round(days, 1)})
    return _badge("cles_en_attente", "Clés de config en attente de redémarrage",
                  "warn" if items else "ok", len(items), items)


def _check_consommation_implausible():
    """Repère un kilométrage mal saisi plutôt qu'un vrai problème de charge :
    sévérité volontairement basse (warn, pas danger)."""
    db = SessionLocal()
    try:
        vehicles = db.query(Vehicle).filter(Vehicle.deleted_at.is_(None)).all()
        items = []
        for v in vehicles:
            sessions = db.query(Transaction).filter(
                Transaction.vehicle_id == v.id,
                Transaction.odometer_km.isnot(None),
                Transaction.status == "completed",
            ).order_by(Transaction.start_time.asc()).all()
            prev = None
            for s in sessions:
                if prev is not None and s.energy_wh:
                    delta_km = s.odometer_km - prev.odometer_km
                    if delta_km and delta_km > 0:
                        kwh_100 = (s.energy_wh / 1000.0) / delta_km * 100
                        if kwh_100 < IMPLAUSIBLE_KWH_100KM_LOW or kwh_100 > IMPLAUSIBLE_KWH_100KM_HIGH:
                            items.append({
                                "vehicle_id": v.id, "vehicle_name": v.name,
                                "session_id": s.id, "kwh_per_100km": round(kwh_100, 1),
                            })
                prev = s
        return _badge("consommation_implausible", "Consommation kWh/100km hors norme (odomètre suspect)",
                      "warn" if items else "ok", len(items), items)
    finally:
        db.close()


def _check_connecteur_bloque_apres_echec():
    """Signature précise observée deux fois (18/09 et 25/09) : le connecteur
    reste en Preparing bien après une tentative de démarrage, qu'elle ait été
    acceptée sans jamais aboutir (25/09) ou rejetée (18/09), sans qu'aucune
    charge ne devienne active. Plus parlant qu'un simple comptage de rejets
    (_check_demarrages_refuses) : c'est le symptôme exact qui a fait
    suspecter un blocage interne à la borne les deux fois."""
    db = SessionLocal()
    try:
        cutoff = datetime.utcnow() - timedelta(minutes=STUCK_PREPARING_AFTER_FAILED_START_MIN)
        rows = db.query(ConnectorStatus, Charger).join(
            Charger, Charger.id == ConnectorStatus.charger_id
        ).filter(
            ConnectorStatus.connector_id != 0,
            ConnectorStatus.status == "Preparing",
            ConnectorStatus.updated_at < cutoff,
        ).all()
        items = []
        for cs, charger in rows:
            has_active = db.query(Transaction).filter(
                Transaction.charger_id == cs.charger_id,
                Transaction.connector_id == cs.connector_id,
                Transaction.status == "active",
            ).first()
            if has_active:
                continue
            attempted = False
            for action in ("StartTransaction.conf", "RemoteStartTransaction.conf"):
                for e in ocpp_logs.get_entries(charger_id=cs.charger_id, action=action, limit=200):
                    if datetime.fromisoformat(e["ts"]) >= cs.updated_at:
                        attempted = True
                        break
                if attempted:
                    break
            if attempted:
                since_min = round((datetime.utcnow() - cs.updated_at).total_seconds() / 60, 1)
                items.append({
                    "charger_id": cs.charger_id,
                    "charger_name": charger.display_name or cs.charger_id,
                    "connector_id": cs.connector_id,
                    "since_min": since_min,
                })
        return _badge("connecteur_bloque_echec_demarrage",
                      "Connecteur bloqué en Preparing après un échec de démarrage",
                      "danger" if items else "ok", len(items), items)
    finally:
        db.close()


def _check_bornes_hors_ligne():
    db = SessionLocal()
    try:
        chargers = db.query(Charger).filter(Charger.deleted_at.is_(None)).all()
        items = []
        for c in chargers:
            if c.id in CONNECTED_CHARGERS:
                continue
            since_min = round((datetime.utcnow() - c.last_seen).total_seconds() / 60, 1) if c.last_seen else None
            items.append({"charger_id": c.id, "charger_name": c.display_name or c.id, "since_min": since_min})
        return _badge("bornes_hors_ligne", "Bornes hors ligne", "warn" if items else "ok", len(items), items)
    finally:
        db.close()


_CHECKS = [
    _check_connecteurs_bloques,
    _check_connecteur_bloque_apres_echec,
    _check_transactions_fantomes,
    _check_charges_sans_capacite,
    _check_charges_duree_nulle,
    _check_orages_reseau,
    _check_coupure_mqtt,
    _check_erreurs_recentes,
    _check_anti_tripping_puis_defaut,
    _check_demarrages_refuses,
    _check_cles_en_attente,
    _check_consommation_implausible,
    _check_bornes_hors_ligne,
]


def compute_health() -> list[dict]:
    """Calcule tous les indicateurs. Chacun est protégé individuellement :
    si l'un plante, il remonte en sévérité 'unknown' plutôt que de faire
    échouer les autres ou toute la route."""
    out = []
    for fn in _CHECKS:
        try:
            out.append(fn())
        except Exception as exc:
            out.append(_badge(fn.__name__.removeprefix("_check_"), fn.__name__, "unknown", 0, detail=str(exc)))
    return out
