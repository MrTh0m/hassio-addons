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


def compute_session_cost(transaction, meter_values, plan) -> dict:
    """Calcule le coût d'une session en découpant son énergie par tranche de
    temps entre relevés successifs, et en appliquant le tarif actif à chaque
    tranche (pas juste énergie totale x un seul prix, pour bien gérer une
    session qui chevauche plusieurs plages tarifaires)."""
    points = []
    if transaction.meter_start is not None and transaction.start_time:
        points.append((transaction.start_time, transaction.meter_start))
    for mv in meter_values:
        if mv.measurand == "Energy.Active.Import.Register":
            points.append((mv.timestamp, mv.value))
    if transaction.meter_stop is not None and transaction.stop_time:
        points.append((transaction.stop_time, transaction.meter_stop))

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
