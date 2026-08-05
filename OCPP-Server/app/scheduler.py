"""Planificateur de charge : évalue périodiquement les conditions définies sur
chaque borne (heures creuses, différer le départ, viser une heure de fin) et
pilote la charge en conséquence.

Deux stratégies, choisies automatiquement par borne :

- **SmartCharging** (si la borne l'accepte) : on impose une limite de puissance
  via SetChargingProfile. « Suspendre » = limite 0 W (la borne passe alors en
  SuspendedEVSE, la transaction reste ouverte, aucune perte d'historique) ;
  « autoriser » = on efface le profil (pleine puissance).
- **RemoteStart / RemoteStop** (repli) : si la borne ne connaît pas
  SmartCharging, on démarre / arrête franchement la transaction. C'est plus
  brutal (la session est réellement close puis recréée), mais universel.

Le planificateur ne fait qu'APPLIQUER une intention (« cette charge doit-elle
être active maintenant ? »). Le calcul de cette intention est isolé dans
`should_charge_now`, ce qui le rend testable sans borne réelle.
"""
import asyncio
import logging
from datetime import datetime, time as dtime

from .db import SessionLocal
from .models import (
    Charger, ChargerMode, ChargeCondition, ChargeConditionType,
    ConnectorStatus, Transaction,
)
from .pricing import resolve_plan_for_charger, price_at
from .csms_local import CONNECTED_CHARGERS, SMART_CHARGING_SUPPORT

logger = logging.getLogger("scheduler")

# Période d'évaluation. 60 s suffit largement : les plages tarifaires sont à la
# minute, et on évite de marteler les bornes.
TICK_SECONDS = 60


def _parse_hhmm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


def _in_off_peak(plan, dt: datetime) -> bool:
    """Vrai si l'instant tombe dans une plage tarifaire nommée (heures creuses)
    du plan. On considère « heures creuses » = toute période explicitement
    définie ; le prix par défaut (hors période) est « heures pleines »."""
    if plan is None:
        return False
    weekday = dt.weekday()
    t = dt.time()
    for period in plan.periods:
        days = [int(d) for d in period.days_of_week.split(",") if d != ""]
        if weekday not in days:
            continue
        start = _parse_hhmm(period.start_time)
        end = _parse_hhmm(period.end_time)
        if start <= end:
            if start <= t < end:
                return True
        else:  # chevauche minuit
            if t >= start or t < end:
                return True
    return False


def should_charge_now(condition_type, time_value, plan, now: datetime,
                      already_started: bool) -> bool:
    """Décide si la charge doit être ACTIVE à l'instant `now`, pour une
    condition donnée. Isolé et pur pour être testable.

    - off_peak    : actif seulement pendant une plage tarifaire nommée.
    - start_after : inactif avant l'heure `time_value`, actif ensuite. Une fois
                    démarrée (already_started), on ne re-suspend pas.
    - ready_by    : on démarre tout de suite (pas d'estimation de durée fiable
                    ici), donc toujours actif ; la contrainte sert de repère.
    """
    if condition_type == ChargeConditionType.off_peak:
        return _in_off_peak(plan, now)
    if condition_type == ChargeConditionType.start_after:
        if not time_value:
            return True
        if already_started:
            return True
        return now.time() >= _parse_hhmm(time_value)
    if condition_type == ChargeConditionType.ready_by:
        return True
    return True


def connector_should_charge_now(db, charger, connector_id: int, now: datetime | None = None) -> bool:
    """Décision partagée (même logique ET que le planificateur) pour savoir si
    un connecteur donné a le droit de charger à l'instant présent, compte tenu
    des conditions de programmation définies sur la borne.

    Renvoie True s'il n'y a aucune condition applicable (comportement par
    défaut : la charge est autorisée). Utilisé aussi par le handler local pour
    suspendre IMMÉDIATEMENT au StartTransaction, sans attendre le prochain tick.
    """
    if now is None:
        now = datetime.utcnow()
    conds = (
        db.query(ChargeCondition)
        .filter(
            ChargeCondition.charger_id == charger.id,
            ChargeCondition.enabled.is_(True),
        )
        .all()
    )
    # On ne garde que les conditions visant ce connecteur (ou toutes : NULL).
    applicable = [c for c in conds if c.connector_id in (None, connector_id)]
    if not applicable:
        return True
    plan = resolve_plan_for_charger(db, charger)
    has_active = db.query(Transaction).filter(
        Transaction.charger_id == charger.id,
        Transaction.connector_id == connector_id,
        Transaction.status == "active",
    ).first() is not None
    return all(
        should_charge_now(c.type, c.time_value, plan, now, has_active)
        for c in applicable
    )


async def _apply(cp, charger_id: str, connector_id: int, should_charge: bool,
                 has_active_txn: bool):
    """Applique l'intention sur un connecteur, en choisissant la stratégie
    selon le support SmartCharging de la borne."""
    supports_smart = SMART_CHARGING_SUPPORT.get(charger_id)

    if should_charge:
        if supports_smart is not False:
            # On tente d'abord de lever une éventuelle limite (profil effacé).
            ok = await cp.set_charging_limit(connector_id, None)
            if ok:
                return
        # Repli RemoteStart : seulement si aucune transaction n'est en cours.
        if not has_active_txn:
            try:
                await cp.trigger_remote_start(connector_id, "SCHED")
            except Exception:
                logger.debug("RemoteStart planifié échoué sur %s/%s", charger_id, connector_id, exc_info=True)
    else:
        if supports_smart is not False:
            ok = await cp.set_charging_limit(connector_id, 0)
            if ok:
                return
        # Repli RemoteStop : on ferme la transaction active s'il y en a une.
        if has_active_txn:
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
                try:
                    await cp.trigger_remote_stop(txn_id)
                except Exception:
                    logger.debug("RemoteStop planifié échoué sur %s", charger_id, exc_info=True)


async def _evaluate_once():
    now = datetime.utcnow()
    db = SessionLocal()
    try:
        conditions = (
            db.query(ChargeCondition)
            .filter(ChargeCondition.enabled.is_(True))
            .all()
        )
        # Regroupe par (borne, connecteur) : plusieurs conditions se combinent
        # en ET logique (toutes doivent autoriser pour que la charge tourne).
        plans = {}
        by_target: dict[tuple[str, int | None], list[ChargeCondition]] = {}
        for cond in conditions:
            by_target.setdefault((cond.charger_id, cond.connector_id), []).append(cond)

        work = []  # (cp, charger_id, connector_id, should_charge, has_active_txn)
        for (charger_id, cond_connector), conds in by_target.items():
            charger = db.query(Charger).filter(
                Charger.id == charger_id, Charger.deleted_at.is_(None)
            ).first()
            if not charger or charger.mode != ChargerMode.local:
                continue
            cp = CONNECTED_CHARGERS.get(charger_id)
            if not cp:
                continue
            if charger_id not in plans:
                plans[charger_id] = resolve_plan_for_charger(db, charger)
            plan = plans[charger_id]

            # Connecteurs concernés : celui de la condition, ou tous les
            # connecteurs physiques connus si NULL.
            if cond_connector is not None:
                connector_ids = [cond_connector]
            else:
                connector_ids = [
                    row[0] for row in db.query(ConnectorStatus.connector_id).filter(
                        ConnectorStatus.charger_id == charger_id,
                        ConnectorStatus.connector_id != 0,
                    ).distinct().all()
                ] or [1]

            for connector_id in connector_ids:
                has_active = db.query(Transaction).filter(
                    Transaction.charger_id == charger_id,
                    Transaction.connector_id == connector_id,
                    Transaction.status == "active",
                ).first() is not None
                # ET logique sur toutes les conditions de cette cible.
                should = all(
                    should_charge_now(c.type, c.time_value, plan, now, has_active)
                    for c in conds
                )
                # On n'agit que si un véhicule est branché (statut connecteur
                # pertinent) : inutile de démarrer sur un connecteur libre.
                status_row = db.query(ConnectorStatus).filter(
                    ConnectorStatus.charger_id == charger_id,
                    ConnectorStatus.connector_id == connector_id,
                ).first()
                plugged = status_row and status_row.status in (
                    "Preparing", "Charging", "SuspendedEV", "SuspendedEVSE", "Finishing",
                )
                if not plugged:
                    continue
                work.append((cp, charger_id, connector_id, should, has_active))
    finally:
        db.close()

    for cp, charger_id, connector_id, should, has_active in work:
        try:
            await _apply(cp, charger_id, connector_id, should, has_active)
        except Exception:
            logger.debug("Application planificateur échouée sur %s/%s", charger_id, connector_id, exc_info=True)


async def run_scheduler():
    """Boucle de fond du planificateur, lancée au démarrage."""
    logger.info("Planificateur de charge démarré (tick %ds)", TICK_SECONDS)
    while True:
        try:
            await _evaluate_once()
        except Exception:
            logger.warning("Erreur dans le planificateur de charge", exc_info=True)
        await asyncio.sleep(TICK_SECONDS)
