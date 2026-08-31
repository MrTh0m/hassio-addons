"""
Tests pour le pilotage automatique de la clé OCPP LightIntensity.

Trois couches testées séparément :
- compute_light_target / _in_time_window : fonctions pures (pas de DB, pas de
  réseau), sur le même principe que should_charge_now dans scheduler.py.
- push_configuration : apprentissage passif de light_zero_supported et
  synchronisation de light_fixed_value (uniquement pour les poussées
  manuelles, mode "fixed").
- apply_light_intensity : bout en bout, avec ChargePoint.call remplacé par
  une réponse pré-fabriquée (pas de vraie connexion WebSocket), sur le même
  principe que test_reboot_pending.py.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncio
from datetime import datetime, time as dtime
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("OCPP_DATA_DIR", "/tmp/test_ocpp")
os.environ.setdefault("OCPP_ADMIN_PASSWORD", "testpass")
os.environ.setdefault("OCPP_SECRET_KEY", "test-secret")

from ocpp.v16 import call_result
from ocpp.v16.enums import ConfigurationStatus

import app.db as _app_db
import app.csms_local as _csms_local
from app.models import Base, ConfigurationKey, Charger, ChargerMode
from app.csms_local import (
    LocalChargePoint, compute_light_target, _in_time_window, _charger_occupied,
)

# Même stratégie d'isolation que test_reboot_pending.py : moteur dédié à ce
# fichier, et patch explicite de app.csms_local.SessionLocal (capturé à
# l'import, ne suit pas un app.db.SessionLocal réaffecté après coup par un
# autre fichier de test).
TEST_ENGINE = create_engine(
    "sqlite:///file:lightintensity?mode=memory&cache=shared&uri=true",
    connect_args={"check_same_thread": False},
)
TestingSession = sessionmaker(bind=TEST_ENGINE, autoflush=False, autocommit=False)


@pytest.fixture(autouse=True)
def setup_db():
    orig_db_engine, orig_db_session = _app_db.engine, _app_db.SessionLocal
    orig_csms_session = _csms_local.SessionLocal
    _app_db.engine, _app_db.SessionLocal = TEST_ENGINE, TestingSession
    _csms_local.SessionLocal = TestingSession
    Base.metadata.create_all(TEST_ENGINE)
    yield
    Base.metadata.drop_all(TEST_ENGINE)
    _app_db.engine, _app_db.SessionLocal = orig_db_engine, orig_db_session
    _csms_local.SessionLocal = orig_csms_session


def SessionLocal():
    return TestingSession()


def _make_cp(charger_id="test-charger-light"):
    return LocalChargePoint(charger_id, connection=object())


def _stub_call(cp, response):
    async def _fake_call(payload, **kwargs):
        return response
    cp.call = _fake_call


def _run(coro):
    return asyncio.run(coro)


# --- compute_light_target / _in_time_window : fonctions pures -------------

def test_fixed_mode_without_night_reduction_returns_base_value():
    # Mode fixe, réduction nocturne désactivée : la fonction renvoie quand
    # même la valeur fixe telle quelle (permet à apply_light_intensity() de
    # se resynchroniser si la borne a dérivé), mais ça reste un no-op tant
    # que la borne est déjà à cette valeur (comparaison faite par l'appelant).
    assert compute_light_target(
        "fixed", True, 50, None, None, False, None, None, None, False, datetime(2026, 1, 1, 23, 0),
    ) == 50


def test_fixed_mode_without_fixed_value_returns_none():
    # Réduction nocturne activée mais aucune valeur fixe connue : rien à réduire.
    assert compute_light_target(
        "fixed", True, None, None, None, True, 10, "22:00", "06:00", False, datetime(2026, 1, 1, 23, 0),
    ) is None


def test_fixed_mode_with_night_reduction_applies_it():
    # Réduction nocturne indépendante du mode (voir échange avec Thomas) :
    # s'applique aussi en mode fixe, sur la valeur fixe elle-même.
    now = datetime(2026, 1, 1, 23, 0)  # dans la fenêtre 22h-06h
    assert compute_light_target(
        "fixed", True, 50, None, None, True, 10, "22:00", "06:00", False, now,
    ) == 40


def test_fixed_mode_night_enabled_outside_window_restores_base():
    # Hors fenêtre nocturne, avec la réduction activée : on revient à la
    # valeur fixe pleine (permet la restauration automatique après la nuit).
    now = datetime(2026, 1, 1, 12, 0)
    assert compute_light_target(
        "fixed", True, 50, None, None, True, 10, "22:00", "06:00", False, now,
    ) == 50


def test_auto_mode_without_both_values_returns_none():
    assert compute_light_target(
        "auto", True, None, None, 20, False, None, None, None, False, datetime(2026, 1, 1, 12, 0),
    ) is None


def test_auto_mode_occupied_uses_charge_value():
    # Hors fenêtre nocturne (midi) : valeur "en charge" telle quelle.
    assert compute_light_target(
        "auto", True, None, 50, 20, True, 10, "22:00", "06:00", False, datetime(2026, 1, 1, 12, 0),
    ) == 50


def test_auto_mode_free_uses_free_value():
    assert compute_light_target(
        "auto", False, None, 50, 20, True, 10, "22:00", "06:00", False, datetime(2026, 1, 1, 12, 0),
    ) == 20


def test_night_reduction_example_from_thomas():
    # L'exemple exact donné : 50% en charge / 20% libre, réduction de 10
    # points la nuit -> 40% en charge, 10% libre.
    now = datetime(2026, 1, 1, 23, 0)  # 23h, dans la fenêtre 22h-06h
    assert compute_light_target("auto", True, None, 50, 20, True, 10, "22:00", "06:00", False, now) == 40
    assert compute_light_target("auto", False, None, 50, 20, True, 10, "22:00", "06:00", False, now) == 10


def test_night_reduction_floors_at_1_by_default():
    # libre=5, réduction=10 -> -5, plafonné à 1% (zero_supported=False,
    # comportement demandé par défaut tant que 0 n'est pas confirmé accepté).
    now = datetime(2026, 1, 1, 23, 0)
    assert compute_light_target("auto", False, None, 50, 5, True, 10, "22:00", "06:00", False, now) == 1


def test_night_reduction_floors_at_0_when_zero_confirmed_supported():
    now = datetime(2026, 1, 1, 23, 0)
    assert compute_light_target("auto", False, None, 50, 5, True, 10, "22:00", "06:00", True, now) == 0


def test_night_reduction_disabled_ignores_window():
    now = datetime(2026, 1, 1, 23, 0)
    assert compute_light_target("auto", True, None, 50, 20, False, 10, "22:00", "06:00", False, now) == 50


def test_night_window_outside_hours_no_reduction():
    now = datetime(2026, 1, 1, 12, 0)  # midi : hors 22h-06h
    assert compute_light_target("auto", True, None, 50, 20, True, 10, "22:00", "06:00", False, now) == 50


def test_time_window_wraps_midnight():
    assert _in_time_window("22:00", "06:00", datetime(2026, 1, 1, 23, 30)) is True
    assert _in_time_window("22:00", "06:00", datetime(2026, 1, 1, 3, 0)) is True
    assert _in_time_window("22:00", "06:00", datetime(2026, 1, 1, 12, 0)) is False
    assert _in_time_window("22:00", "06:00", datetime(2026, 1, 1, 6, 0)) is False  # borne exclue


def test_time_window_same_day():
    assert _in_time_window("08:00", "18:00", datetime(2026, 1, 1, 12, 0)) is True
    assert _in_time_window("08:00", "18:00", datetime(2026, 1, 1, 20, 0)) is False


def test_time_window_missing_values_returns_false():
    assert _in_time_window(None, "06:00", datetime(2026, 1, 1, 23, 0)) is False
    assert _in_time_window("22:00", None, datetime(2026, 1, 1, 23, 0)) is False
    assert _in_time_window("pas une heure", "06:00", datetime(2026, 1, 1, 23, 0)) is False


# --- push_configuration : apprentissage light_zero_supported / light_fixed_value ---

def _add_charger(charger_id, **kwargs):
    db = SessionLocal()
    c = Charger(id=charger_id, mode=ChargerMode.local, **kwargs)
    db.add(c)
    db.commit()
    db.close()


def test_push_zero_accepted_learns_supported():
    _add_charger("c1")
    cp = _make_cp("c1")
    _stub_call(cp, call_result.ChangeConfiguration(status=ConfigurationStatus.accepted))
    _run(cp.push_configuration("LightIntensity", "0"))
    db = SessionLocal()
    charger = db.query(Charger).filter_by(id="c1").first()
    assert charger.light_zero_supported is True
    db.close()


def test_push_zero_rejected_learns_not_supported():
    _add_charger("c2")
    cp = _make_cp("c2")
    _stub_call(cp, call_result.ChangeConfiguration(status=ConfigurationStatus.rejected))
    _run(cp.push_configuration("LightIntensity", "0"))
    db = SessionLocal()
    charger = db.query(Charger).filter_by(id="c2").first()
    assert charger.light_zero_supported is False
    db.close()


def test_push_fixed_mode_syncs_fixed_value():
    _add_charger("c3", light_mode="fixed")
    cp = _make_cp("c3")
    _stub_call(cp, call_result.ChangeConfiguration(status=ConfigurationStatus.accepted))
    _run(cp.push_configuration("LightIntensity", "35"))
    db = SessionLocal()
    charger = db.query(Charger).filter_by(id="c3").first()
    assert charger.light_fixed_value == 35
    db.close()


def test_push_auto_mode_does_not_overwrite_fixed_value():
    # En mode auto, push_configuration est appelé par apply_light_intensity
    # avec la valeur CALCULÉE (occupation/nuit) : ça ne doit pas écraser la
    # dernière valeur fixe mémorisée pour un futur retour en mode fixe.
    _add_charger("c4", light_mode="auto", light_fixed_value=42)
    cp = _make_cp("c4")
    _stub_call(cp, call_result.ChangeConfiguration(status=ConfigurationStatus.accepted))
    _run(cp.push_configuration("LightIntensity", "17"))
    db = SessionLocal()
    charger = db.query(Charger).filter_by(id="c4").first()
    assert charger.light_fixed_value == 42  # inchangé
    db.close()


# --- apply_light_intensity : bout en bout ----------------------------------

def _add_config_key(charger_id, key, value):
    db = SessionLocal()
    db.add(ConfigurationKey(charger_id=charger_id, key=key, value=value, readonly=False))
    db.commit()
    db.close()


def test_apply_skips_when_key_absent():
    _add_charger("c5", light_mode="auto", light_auto_charge_value=50, light_auto_free_value=20)
    cp = _make_cp("c5")
    calls = []
    async def _fake_call(payload, **kwargs):
        calls.append(payload)
        return call_result.ChangeConfiguration(status=ConfigurationStatus.accepted)
    cp.call = _fake_call
    _run(cp.apply_light_intensity())
    assert calls == []  # pas de LightIntensity en cache -> rien à automatiser


def test_apply_skips_in_fixed_mode():
    _add_charger("c6", light_mode="fixed")
    _add_config_key("c6", "LightIntensity", "10")
    cp = _make_cp("c6")
    calls = []
    async def _fake_call(payload, **kwargs):
        calls.append(payload)
        return call_result.ChangeConfiguration(status=ConfigurationStatus.accepted)
    cp.call = _fake_call
    _run(cp.apply_light_intensity())
    assert calls == []


def test_apply_pushes_when_target_differs():
    _add_charger("c7", light_mode="auto", light_auto_charge_value=50, light_auto_free_value=20)
    _add_config_key("c7", "LightIntensity", "5")  # valeur en cache différente de la cible
    cp = _make_cp("c7")
    calls = []
    async def _fake_call(payload, **kwargs):
        calls.append(payload)
        return call_result.ChangeConfiguration(status=ConfigurationStatus.accepted)
    cp.call = _fake_call
    _run(cp.apply_light_intensity())
    assert len(calls) == 1
    assert calls[0].value == "20"  # aucun connecteur occupé -> valeur "libre"


def test_apply_skips_when_target_already_matches_cache():
    _add_charger("c8", light_mode="auto", light_auto_charge_value=50, light_auto_free_value=20)
    _add_config_key("c8", "LightIntensity", "20")  # déjà à la bonne valeur ("libre")
    cp = _make_cp("c8")
    calls = []
    async def _fake_call(payload, **kwargs):
        calls.append(payload)
        return call_result.ChangeConfiguration(status=ConfigurationStatus.accepted)
    cp.call = _fake_call
    _run(cp.apply_light_intensity())
    assert calls == []  # déjà bon, aucun ChangeConfiguration inutile


def test_apply_pushes_reduced_value_in_fixed_mode_at_night():
    _add_charger(
        "c11", light_mode="fixed", light_fixed_value=50,
        light_night_enabled=True, light_night_reduction=10,
        light_night_start="22:00", light_night_end="06:00",
    )
    _add_config_key("c11", "LightIntensity", "50")  # valeur de jour en cache
    cp = _make_cp("c11")
    calls = []
    async def _fake_call(payload, **kwargs):
        calls.append(payload)
        return call_result.ChangeConfiguration(status=ConfigurationStatus.accepted)
    cp.call = _fake_call
    import app.csms_local as _csms_local
    orig_utcnow = _csms_local.datetime
    class _FrozenDatetime(orig_utcnow):
        @classmethod
        def utcnow(cls):
            return orig_utcnow(2026, 1, 1, 23, 0)
    _csms_local.datetime = _FrozenDatetime
    try:
        _run(cp.apply_light_intensity())
    finally:
        _csms_local.datetime = orig_utcnow
    assert len(calls) == 1
    assert calls[0].value == "40"  # 50 - 10 points


def test_charger_occupied_aggregates_across_connectors():
    db = SessionLocal()
    from app.models import ConnectorStatus
    db.add(ConnectorStatus(charger_id="c9", connector_id=1, status="Available"))
    db.add(ConnectorStatus(charger_id="c9", connector_id=2, status="Charging"))
    db.commit()
    assert _charger_occupied(db, "c9") is True  # connecteur 2 occupé suffit
    db.close()


def test_charger_not_occupied_when_all_available():
    db = SessionLocal()
    from app.models import ConnectorStatus
    db.add(ConnectorStatus(charger_id="c10", connector_id=1, status="Available"))
    db.commit()
    assert _charger_occupied(db, "c10") is False
    db.close()
