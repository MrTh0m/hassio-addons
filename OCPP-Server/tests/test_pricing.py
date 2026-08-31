"""
Tests pour app/pricing.py — calcul d'énergie et de coût.

Cas couverts :
- Index absolu (borne Schneider) : meter_start/stop sont des index cumulés
- Valeur relative (simulateurs) : meter_start=0, MeterValues relatifs
- Unité kWh dans les MeterValues
- Session sans MeterValues (uniquement meter_start/stop)
- Session sans meter_stop (active) avec ignore_meter_stop
- Plages tarifaires heures creuses/pleines
- Calcul coût = 0 si aucun tarif configuré
- total_wh négatif → 0
"""
import sys, os
from datetime import datetime, timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.pricing import compute_session_cost, _to_wh, price_at


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_txn(meter_start, meter_stop=None, start_time=None, stop_time=None, status="completed"):
    start_time = start_time or datetime(2026, 8, 7, 10, 0, 0)
    stop_time = stop_time or (start_time + timedelta(hours=1) if meter_stop is not None else None)
    return SimpleNamespace(
        meter_start=meter_start,
        meter_stop=meter_stop,
        start_time=start_time,
        stop_time=stop_time,
        status=status,
    )


def make_mv(value, unit="Wh", measurand="Energy.Active.Import.Register", minutes_after_start=30):
    return SimpleNamespace(
        measurand=measurand,
        value=value,
        unit=unit,
        timestamp=datetime(2026, 8, 7, 10, 0, 0) + timedelta(minutes=minutes_after_start),
    )


def make_plan(price=0.2076, periods=None):
    """Plan tarifaire simple avec un prix fixe ou des plages."""
    if periods is None:
        period = SimpleNamespace(
            name="HC",
            price=price,
            days_of_week="0,1,2,3,4,5,6",
            start_time="00:00",
            end_time="23:59",
        )
        periods = [period]
    return SimpleNamespace(fixed_price=price, periods=periods)


# ---------------------------------------------------------------------------
# Tests _to_wh
# ---------------------------------------------------------------------------

def test_to_wh_unit_wh():
    assert _to_wh(1000.0, "Wh") == 1000.0

def test_to_wh_unit_kwh():
    assert _to_wh(1.0, "kWh") == 1000.0

def test_to_wh_unit_kwh_uppercase():
    assert _to_wh(2.5, "KWH") == 2500.0

def test_to_wh_no_unit():
    assert _to_wh(500.0, None) == 500.0

def test_to_wh_empty_unit():
    assert _to_wh(500.0, "") == 500.0


# ---------------------------------------------------------------------------
# Tests compute_session_cost — énergie
# ---------------------------------------------------------------------------

def test_index_absolu_sans_metervalues():
    """Schneider : meter_start=96543, meter_stop=98075 → 1532 Wh."""
    txn = make_txn(96543, 98075)
    result = compute_session_cost(txn, [], None)
    assert result["energy_wh"] == pytest.approx(1532.0)
    assert result["cost"] is None


def test_index_absolu_avec_metervalues():
    """MeterValues avec index absolu : calcul différentiel correct."""
    txn = make_txn(96543, 98075)
    mvs = [
        make_mv(96800, minutes_after_start=20),
        make_mv(97400, minutes_after_start=40),
    ]
    result = compute_session_cost(txn, mvs, None)
    # points : (start, 96543), (mv1, 96800), (mv2, 97400), (stop, 98075)
    assert result["energy_wh"] == pytest.approx(98075 - 96543)


def test_metervalues_relatifs_simulateur():
    """Simulateurs : meter_start=0, MeterValues relatifs."""
    txn = make_txn(0, 7400)
    mvs = [
        make_mv(3700, minutes_after_start=30),
    ]
    result = compute_session_cost(txn, mvs, None)
    assert result["energy_wh"] == pytest.approx(7400.0)


def test_metervalues_en_kwh():
    """Borne envoyant les MeterValues en kWh."""
    txn = make_txn(96543, 97543)
    mvs = [make_mv(97.0, unit="kWh", minutes_after_start=30)]
    # 97.0 kWh → 97000 Wh
    # points: (start, 96543), (mv, 97000), (stop, 97543)
    result = compute_session_cost(txn, mvs, None)
    assert result["energy_wh"] == pytest.approx(97543 - 96543)


def test_sans_meter_stop_sans_metervalues():
    """Session active sans MeterValues → 0 Wh (pas de données)."""
    txn = make_txn(98075, meter_stop=None)
    result = compute_session_cost(txn, [], None, ignore_meter_stop=True)
    assert result["energy_wh"] == 0.0
    assert result["cost"] is None


def test_ignore_meter_stop_pour_session_active():
    """ignore_meter_stop=True : meter_stop parasite ignoré."""
    # meter_stop renseigné par erreur sur une session active
    txn = make_txn(98075, meter_stop=196155)
    result = compute_session_cost(txn, [], None, ignore_meter_stop=True)
    # Sans MeterValues et en ignorant meter_stop → 0
    assert result["energy_wh"] == 0.0


def test_ignore_meter_stop_avec_metervalues():
    """Session active avec MeterValues mais meter_stop parasite."""
    txn = make_txn(98075, meter_stop=999999)
    mvs = [make_mv(98200, minutes_after_start=15)]
    result = compute_session_cost(txn, mvs, None, ignore_meter_stop=True)
    # points: (start, 98075), (mv, 98200) → 125 Wh
    assert result["energy_wh"] == pytest.approx(125.0)


def test_energie_negative_retourne_zero():
    """meter_stop < meter_start (données corrompues) → 0."""
    txn = make_txn(98075, 90000)
    result = compute_session_cost(txn, [], None)
    assert result["energy_wh"] == 0.0


def test_metervalues_power_ignores():
    """Les MeterValues Power.Active.Import ne comptent pas dans l'énergie."""
    txn = make_txn(0, 7400)
    mvs = [
        make_mv(7200, measurand="Power.Active.Import", minutes_after_start=30),
    ]
    result = compute_session_cost(txn, mvs, None)
    # Seuls start et stop → 7400 Wh
    assert result["energy_wh"] == pytest.approx(7400.0)


def test_un_seul_point_retourne_zero():
    """Un seul point (ni MeterValues, ni meter_stop) → 0 Wh."""
    txn = make_txn(96543, meter_stop=None)
    result = compute_session_cost(txn, [], None)
    assert result["energy_wh"] == 0.0


# ---------------------------------------------------------------------------
# Tests compute_session_cost — coût
# ---------------------------------------------------------------------------

def test_cout_avec_plan_simple():
    """1532 Wh × 0.2076 €/kWh = 0.3180 €."""
    txn = make_txn(96543, 98075)
    plan = make_plan(price=0.2076)
    result = compute_session_cost(txn, [], plan)
    assert result["energy_wh"] == pytest.approx(1532.0)
    assert result["cost"] == pytest.approx(1532 / 1000 * 0.2076, rel=1e-3)


def test_cout_sans_plan():
    """Sans tarif → cost=None."""
    txn = make_txn(0, 7400)
    result = compute_session_cost(txn, [], None)
    assert result["cost"] is None
    assert result["energy_wh"] == pytest.approx(7400.0)


def test_cout_zero_energie():
    """0 Wh → coût nul."""
    txn = make_txn(0, 0)
    plan = make_plan(price=0.2076)
    result = compute_session_cost(txn, [], plan)
    assert result["energy_wh"] == 0.0


def test_cout_plages_hc_hp():
    """Session chevauchant HC (0.15) et HP (0.25) : coût correctement découpé."""
    # Session de 2h : 22h→00h en HC, 00h→02h en HP
    start = datetime(2026, 8, 6, 22, 0, 0)
    stop = datetime(2026, 8, 7, 2, 0, 0)
    txn = make_txn(0, 14800, start_time=start, stop_time=stop)

    hc = SimpleNamespace(name="HC", price=0.15, days_of_week="0,1,2,3,4,5,6",
                          start_time="22:00", end_time="00:00")
    hp = SimpleNamespace(name="HP", price=0.25, days_of_week="0,1,2,3,4,5,6",
                          start_time="00:00", end_time="22:00")
    plan = SimpleNamespace(fixed_price=None, periods=[hc, hp])

    # MeterValue au milieu pour créer les deux segments
    mv_minuit = SimpleNamespace(
        measurand="Energy.Active.Import.Register",
        value=7400.0, unit="Wh",
        timestamp=datetime(2026, 8, 7, 0, 0, 0),
    )
    result = compute_session_cost(txn, [mv_minuit], plan)
    # 7400 Wh en HC + 7400 Wh en HP
    expected = (7400/1000 * 0.15) + (7400/1000 * 0.25)
    assert result["cost"] == pytest.approx(expected, rel=1e-3)
    assert result["energy_wh"] == pytest.approx(14800.0)


# ---------------------------------------------------------------------------
# Tests price_at
# ---------------------------------------------------------------------------

def test_price_at_dans_plage():
    hc = SimpleNamespace(name="HC", price=0.15, days_of_week="0,1,2,3,4,5,6",
                          start_time="22:00", end_time="06:00")
    plan = SimpleNamespace(fixed_price=0.25, periods=[hc])
    # 23h un lundi → HC
    assert price_at(plan, datetime(2026, 8, 3, 23, 0, 0)) == pytest.approx(0.15)


def test_price_at_hors_plage():
    hc = SimpleNamespace(name="HC", price=0.15, days_of_week="0,1,2,3,4,5,6",
                          start_time="22:00", end_time="06:00")
    plan = SimpleNamespace(fixed_price=0.25, periods=[hc])
    # 14h → HP (prix fixe)
    assert price_at(plan, datetime(2026, 8, 3, 14, 0, 0)) == pytest.approx(0.25)


def test_price_at_plan_none():
    assert price_at(None, datetime(2026, 8, 7, 10, 0)) is None


import pytest

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
