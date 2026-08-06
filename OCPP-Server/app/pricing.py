from datetime import datetime, time as dtime


def _parse_hhmm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


def _time_in_range(t: dtime, start: dtime, end: dtime) -> bool:
    if start <= end:
        return start <= t < end
    # La plage chevauche minuit (ex. 23:00-07:00)
    return t >= start or t < end


def price_at(plan, dt: datetime):
    """Prix (€/kWh) applicable à cet instant pour ce plan, ou None si aucun
    tarif n'est configuré du tout pour ce plan."""
    if plan is None:
        return None
    weekday = dt.weekday()  # 0 = lundi ... 6 = dimanche
    t = dt.time()
    for period in plan.periods:
        days = [int(d) for d in period.days_of_week.split(",") if d != ""]
        if weekday not in days:
            continue
        start = _parse_hhmm(period.start_time)
        end = _parse_hhmm(period.end_time)
        if _time_in_range(t, start, end):
            return period.price
    return plan.fixed_price


def _to_wh(value: float, unit: str | None) -> float:
    """Normalise une valeur énergétique en Wh.

    OCPP 1.6 spécifie Wh pour meterStart/meterStop et pour les MeterValues
    de type Energy.Active.Import.Register. Mais certaines bornes (dont
    certaines Schneider) envoient des kWh. On détecte ça via l'unité
    renvoyée dans le SampledValue.
    """
    if unit and unit.lower() in ("kwh", "kw·h", "kw-h"):
        return value * 1000.0
    return value


def compute_session_cost(transaction, meter_values, plan) -> dict:
    """Calcule le coût d'une session en découpant son énergie par tranche de
    temps entre relevés successifs, et en appliquant le tarif actif à chaque
    tranche (pas juste énergie totale x un seul prix, pour bien gérer une
    session qui chevauche plusieurs plages tarifaires).

    Les valeurs énergétiques sont normalisées en Wh avant tout calcul, quelle
    que soit l'unité déclarée par la borne (Wh ou kWh).
    """
    # meterStart / meterStop sont en Wh par la spec OCPP 1.6
    points = []
    if transaction.meter_start is not None and transaction.start_time:
        points.append((transaction.start_time, float(transaction.meter_start)))
    for mv in meter_values:
        if mv.measurand == "Energy.Active.Import.Register":
            points.append((mv.timestamp, _to_wh(mv.value, mv.unit)))
    if transaction.meter_stop is not None and transaction.stop_time:
        points.append((transaction.stop_time, float(transaction.meter_stop)))

    points.sort(key=lambda p: p[0])

    if len(points) < 2:
        return {"cost": None, "energy_wh": 0.0}

    total_wh = max(0.0, points[-1][1] - points[0][1])

    if plan is None:
        return {"cost": None, "energy_wh": total_wh}

    cost = 0.0
    has_price_info = False
    for (t1, e1), (t2, e2) in zip(points, points[1:]):
        delta = e2 - e1
        if delta <= 0:
            continue
        mid = t1 + (t2 - t1) / 2
        price = price_at(plan, mid)
        if price is None:
            continue
        has_price_info = True
        cost += (delta / 1000.0) * price

    return {"cost": round(cost, 4) if has_price_info else None, "energy_wh": total_wh}


def resolve_plan_for_charger(db, charger):
    """Le tarif d'une borne : celui qui lui est explicitement assigné, sinon
    le plan marqué comme actif par défaut, s'il y en a un."""
    from .models import TariffPlan
    if charger is not None and charger.tariff_plan is not None:
        return charger.tariff_plan
    return db.query(TariffPlan).filter(TariffPlan.is_default.is_(True)).first()


def freeze_transaction_cost(db, transaction):
    """Calcule le coût final d'une transaction terminée et le fige en base
    (montant, énergie, nom du tarif utilisé), pour qu'une modification
    ultérieure des tarifs (prix, suppression d'une période...) n'altère
    jamais rétroactivement le coût d'une charge déjà terminée."""
    from .models import Charger, MeterValue
    charger = db.query(Charger).filter(Charger.id == transaction.charger_id).first()
    plan = resolve_plan_for_charger(db, charger)
    meter_values = db.query(MeterValue).filter(MeterValue.transaction_id == transaction.id).all()
    result = compute_session_cost(transaction, meter_values, plan)
    transaction.cost = result["cost"]
    transaction.energy_wh = result["energy_wh"]
    transaction.tariff_plan_name = plan.name if plan else None
    return result
