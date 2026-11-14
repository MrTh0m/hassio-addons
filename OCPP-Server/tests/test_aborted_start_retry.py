"""
Tests de la relance automatique d'un démarrage avorté et de la fusion des
tentatives en une seule charge (0.19.44).

Contexte (dump du 14/11, 10 cas en un mois) : quelques secondes après le
StartTransaction, la Schneider voit le véhicule comme débranché (connecteur
Available puis Preparing) et clôt la transaction avec reason EVDisconnected,
à 0 Wh. La tentative suivante fonctionne. Le serveur relance donc lui-même la
charge avec le même idTag et le même véhicule, puis fusionne la tentative
avortée dans la suivante (une seule charge dans l'historique).

Couvre aussi la clôture différée quand la borne est réglée avec
StopTransactionOnEVSideDisconnect = false, et les réglages de la base
(réserve de connexions, WAL, index).
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("OCPP_DATA_DIR", "/tmp/test_ocpp")
os.environ.setdefault("OCPP_ADMIN_PASSWORD", "testpass")
os.environ.setdefault("OCPP_SECRET_KEY", "test-secret")

import app.db as _app_db
import app.csms_local as _csms
import app.mqtt_bridge as _mqtt
from app.models import (
    Base, Charger, ChargerMode, AuthMode, Transaction, Vehicle, MeterValue,
    ConnectorStatus, ConfigurationKey,
)
from app.csms_local import LocalChargePoint

TEST_ENGINE = create_engine(
    "sqlite:///file:abortedstart?mode=memory&cache=shared&uri=true",
    connect_args={"check_same_thread": False},
)
TestingSession = sessionmaker(bind=TEST_ENGINE, autoflush=False, autocommit=False)


@pytest.fixture(autouse=True)
def setup_db(monkeypatch):
    monkeypatch.setattr(_app_db, "SessionLocal", TestingSession)
    monkeypatch.setattr(_csms, "SessionLocal", TestingSession)
    # Délais réduits : les tests ne doivent pas attendre des secondes réelles.
    monkeypatch.setattr(_csms, "START_RETRY_DELAY_S", 0)
    monkeypatch.setattr(_csms, "START_RETRY_WAIT_PREPARING_S", 2)
    monkeypatch.setattr(_csms, "START_RETRY_WAIT_START_S", 2)
    monkeypatch.setattr(_csms, "EV_DISCONNECT_GRACE_S", 0)
    monkeypatch.setattr(_csms, "EV_DISCONNECT_STOP_WAIT_S", 0)
    _csms.START_RETRIES.clear()
    _csms.PENDING_REMOTE_STARTS.clear()
    _csms.EV_DISCONNECT_PENDING.clear()
    _csms.CONNECTED_CHARGERS.clear()
    Base.metadata.create_all(TEST_ENGINE)
    yield
    Base.metadata.drop_all(TEST_ENGINE)
    _csms.START_RETRIES.clear()
    _csms.PENDING_REMOTE_STARTS.clear()
    _csms.EV_DISCONNECT_PENDING.clear()


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat() + "Z"


class FakeCP(LocalChargePoint):
    """Borne simulée : enregistre les commandes envoyées au lieu de les
    transmettre, et répond Accepted (ou ce que le test choisit)."""

    def __init__(self, charger_id="c1", remote_start_status="Accepted"):
        super().__init__(charger_id, connection=object())
        self.remote_starts = []
        self.remote_stops = []
        self.remote_start_status = remote_start_status

    async def trigger_remote_start(self, connector_id, id_tag):
        self.remote_starts.append((connector_id, id_tag))
        return SimpleNamespace(status=self.remote_start_status)

    async def trigger_remote_stop(self, transaction_id):
        self.remote_stops.append(transaction_id)
        return SimpleNamespace(status="Accepted")


def _setup(vehicle=True, keep_txn_on_disconnect=False):
    db = TestingSession()
    db.add(Charger(id="c1", mode=ChargerMode.local, auth_mode=AuthMode.authorized))
    vid = None
    if vehicle:
        v = Vehicle(name="Zoé")
        db.add(v)
        db.flush()
        vid = v.id
    if keep_txn_on_disconnect:
        db.add(ConfigurationKey(charger_id="c1", key="StopTransactionOnEVSideDisconnect", value="false"))
    db.add(ConnectorStatus(charger_id="c1", connector_id=1, status="Preparing"))
    db.commit()
    db.close()
    return vid


def _txns():
    db = TestingSession()
    try:
        return [
            SimpleNamespace(**{c.name: getattr(t, c.name) for c in Transaction.__table__.columns})
            for t in db.query(Transaction).order_by(Transaction.id).all()
        ]
    finally:
        db.close()


async def _aborted_start(cp, vid, t0, meter=539780):
    """Rejoue la séquence du dump : RemoteStart avec véhicule, StartTransaction,
    Available, Preparing, StopTransaction EVDisconnected 6 s plus tard."""
    if vid is not None:
        _csms.PENDING_REMOTE_STARTS[("c1", 1)] = vid
    resp = await cp.on_start_transaction(connector_id=1, id_tag=f"REMOTE-{vid}" if vid else "WEBADMIN",
                                         meter_start=meter, timestamp=_iso(t0))
    tid = resp.transaction_id
    await cp.on_status_notification(connector_id=1, status="Available", error_code="NoError")
    await cp.on_status_notification(connector_id=1, status="Preparing", error_code="NoError")
    await cp.on_stop_transaction(transaction_id=tid, meter_stop=meter,
                                 timestamp=_iso(t0 + timedelta(seconds=6)), reason="EVDisconnected")
    return tid


def test_aborted_start_is_retried_and_merged():
    vid = _setup()

    async def scenario():
        cp = FakeCP()
        t0 = datetime.utcnow() - timedelta(seconds=10)
        first = await _aborted_start(cp, vid, t0)
        # Relevé de la tentative avortée : doit suivre la fusion.
        db = TestingSession()
        db.add(MeterValue(charger_id="c1", transaction_id=first, connector_id=1,
                          timestamp=t0, measurand="Energy.Active.Import.Register", value=539780))
        db.commit()
        db.close()

        await asyncio.sleep(0.2)   # la relance part en tâche de fond
        assert cp.remote_starts == [(1, f"REMOTE-{vid}")]
        assert _csms.PENDING_REMOTE_STARTS.get(("c1", 1)) == vid

        resp = await cp.on_start_transaction(connector_id=1, id_tag=f"REMOTE-{vid}",
                                             meter_start=539780, timestamp=_iso(datetime.utcnow()))
        await asyncio.sleep(0.1)
        return first, resp.transaction_id, t0

    first, second, t0 = asyncio.run(scenario())

    txns = _txns()
    assert len(txns) == 1, "les deux tentatives doivent former une seule charge"
    t = txns[0]
    assert t.id == second and t.id != first
    assert t.vehicle_id == vid
    assert t.status == "active"
    assert t.start_attempts == 2
    assert abs((t.start_time - t0.replace(microsecond=0)).total_seconds()) < 1
    assert t.meter_start == 539780

    db = TestingSession()
    rows = db.query(MeterValue).all()
    db.close()
    assert [r.transaction_id for r in rows] == [second]
    assert ("c1", 1) not in _csms.START_RETRIES


def test_manual_restart_before_retry_is_merged_without_double_start():
    """Cas du 13/11 16:04 : tentative sans véhicule (WEBADMIN) avortée, puis
    relance manuelle avec véhicule au même moment. Pas de seconde relance, et
    la charge garde le véhicule de la relance manuelle."""
    vid = _setup()

    async def scenario():
        cp = FakeCP()
        t0 = datetime.utcnow() - timedelta(seconds=10)
        first = await _aborted_start(cp, None, t0)
        _csms.PENDING_REMOTE_STARTS[("c1", 1)] = vid
        resp = await cp.on_start_transaction(connector_id=1, id_tag=f"REMOTE-{vid}",
                                             meter_start=539780, timestamp=_iso(datetime.utcnow()))
        await asyncio.sleep(0.3)
        return cp, first, resp.transaction_id

    cp, first, second = asyncio.run(scenario())
    txns = _txns()
    assert len(txns) == 1
    assert txns[0].id == second
    assert txns[0].vehicle_id == vid
    assert txns[0].start_attempts == 2
    assert cp.remote_starts == [], "la relance automatique ne doit pas partir en plus"


def test_retry_already_started_before_stop_is_merged():
    """StopTransaction de la tentative avortée reçu APRÈS le StartTransaction
    de la suivante (ordre possible quand la borne rejoue sa file)."""
    vid = _setup()

    async def scenario():
        cp = FakeCP()
        t0 = datetime.utcnow() - timedelta(seconds=10)
        _csms.PENDING_REMOTE_STARTS[("c1", 1)] = vid
        r1 = await cp.on_start_transaction(connector_id=1, id_tag=f"REMOTE-{vid}",
                                           meter_start=1000, timestamp=_iso(t0))
        await cp.on_status_notification(connector_id=1, status="Available", error_code="NoError")
        await cp.on_status_notification(connector_id=1, status="Preparing", error_code="NoError")
        _csms.PENDING_REMOTE_STARTS[("c1", 1)] = vid
        r2 = await cp.on_start_transaction(connector_id=1, id_tag=f"REMOTE-{vid}",
                                           meter_start=1000, timestamp=_iso(t0 + timedelta(seconds=8)))
        await cp.on_stop_transaction(transaction_id=r1.transaction_id, meter_stop=1000,
                                     timestamp=_iso(t0 + timedelta(seconds=6)), reason="EVDisconnected")
        await asyncio.sleep(0.2)
        return cp, r2.transaction_id

    cp, second = asyncio.run(scenario())
    txns = _txns()
    assert [t.id for t in txns] == [second]
    assert txns[0].status == "active"
    assert txns[0].start_attempts == 2
    assert cp.remote_starts == []


def test_retry_gives_up_when_connector_stays_available():
    """Véhicule réellement débranché : pas de relance, la tentative reste
    dans l'historique comme une session ordinaire."""
    vid = _setup()

    async def scenario():
        cp = FakeCP()
        t0 = datetime.utcnow() - timedelta(seconds=10)
        await _aborted_start(cp, vid, t0)
        await cp.on_status_notification(connector_id=1, status="Available", error_code="NoError")
        await asyncio.sleep(3)
        return cp

    cp = asyncio.run(scenario())
    assert cp.remote_starts == []
    txns = _txns()
    assert len(txns) == 1 and txns[0].status == "completed"


@pytest.mark.parametrize("reason,duration_s,energy_wh", [
    ("Local", 6, 0),            # arrêt volontaire sur la borne
    ("SoftReset", 6, 0),        # redémarrage de la borne (08:23 le 14/11)
    ("EVDisconnected", 120, 0), # trop long pour un démarrage avorté
    ("EVDisconnected", 6, 500), # de l'énergie a été délivrée
])
def test_genuine_stops_are_not_retried(reason, duration_s, energy_wh):
    vid = _setup()

    async def scenario():
        cp = FakeCP()
        t0 = datetime.utcnow() - timedelta(seconds=duration_s + 5)
        _csms.PENDING_REMOTE_STARTS[("c1", 1)] = vid
        r = await cp.on_start_transaction(connector_id=1, id_tag=f"REMOTE-{vid}",
                                          meter_start=1000, timestamp=_iso(t0))
        await cp.on_stop_transaction(transaction_id=r.transaction_id, meter_stop=1000 + energy_wh,
                                     timestamp=_iso(t0 + timedelta(seconds=duration_s)), reason=reason)
        await asyncio.sleep(0.2)
        return cp

    cp = asyncio.run(scenario())
    assert cp.remote_starts == []
    assert ("c1", 1) not in _csms.START_RETRIES


def test_unauthorized_aborted_start_is_not_retried():
    """Charge locale freeCharge refusée par le serveur (nuit du 14/11) : il
    n'y a rien à relancer pour le compte d'un véhicule."""
    _setup(vehicle=False)

    async def scenario():
        cp = FakeCP()
        t0 = datetime.utcnow() - timedelta(seconds=10)
        r = await cp.on_start_transaction(connector_id=1, id_tag="freeCharge",
                                          meter_start=1000, timestamp=_iso(t0))
        await cp.on_stop_transaction(transaction_id=r.transaction_id, meter_stop=1000,
                                     timestamp=_iso(t0 + timedelta(seconds=8)), reason="EVDisconnected")
        await asyncio.sleep(0.2)
        return cp

    cp = asyncio.run(scenario())
    assert cp.remote_starts == []


@pytest.mark.parametrize("setting", [None, 1, 5])
def test_retries_are_capped(setting):
    """Une borne qui avorte à chaque fois n'est pas relancée indéfiniment :
    autant de relances que le réglage (3 par défaut), puis la charge reste
    arrêtée."""
    vid = _setup()
    if setting is not None:
        _csms.set_start_retry_max(setting)
    max_retries = _csms.START_RETRY_MAX if setting is None else setting
    assert _csms.start_retry_max() == max_retries

    async def scenario():
        cp = FakeCP()
        tag = f"REMOTE-{vid}"
        for i in range(max_retries + 1):
            t0 = datetime.utcnow() - timedelta(seconds=10)
            _csms.PENDING_REMOTE_STARTS[("c1", 1)] = vid
            r = await cp.on_start_transaction(connector_id=1, id_tag=tag, meter_start=1000, timestamp=_iso(t0))
            await cp.on_status_notification(connector_id=1, status="Available", error_code="NoError")
            await cp.on_status_notification(connector_id=1, status="Preparing", error_code="NoError")
            await cp.on_stop_transaction(transaction_id=r.transaction_id, meter_stop=1000,
                                         timestamp=_iso(t0 + timedelta(seconds=6)), reason="EVDisconnected")
            await asyncio.sleep(0.2)
        return cp

    cp = asyncio.run(scenario())
    assert len(cp.remote_starts) == max_retries
    txns = _txns()
    assert len(txns) == 1, "toutes les tentatives restent fusionnées en une seule ligne"
    assert txns[0].start_attempts == max_retries + 1
    assert txns[0].status == "completed"


def test_retry_disabled_but_manual_restart_still_merged():
    """Réglage à 0 : aucune relance automatique, mais une relance manuelle
    dans les 2 minutes est quand même regroupée avec la tentative avortée."""
    vid = _setup()
    _csms.set_start_retry_max(0)

    async def scenario():
        cp = FakeCP()
        await _aborted_start(cp, vid, datetime.utcnow() - timedelta(seconds=10))
        await asyncio.sleep(0.3)
        assert cp.remote_starts == []
        _csms.PENDING_REMOTE_STARTS[("c1", 1)] = vid
        r = await cp.on_start_transaction(connector_id=1, id_tag=f"REMOTE-{vid}",
                                          meter_start=539780, timestamp=_iso(datetime.utcnow()))
        return r.transaction_id

    second = asyncio.run(scenario())
    txns = _txns()
    assert [t.id for t in txns] == [second]
    assert txns[0].start_attempts == 2


def test_start_retry_setting_is_clamped():
    _setup()
    assert _csms.set_start_retry_max(9) == _csms.START_RETRY_MAX_LIMIT
    assert _csms.set_start_retry_max(-2) == 0
    assert _csms.start_retry_max() == 0


def test_aborted_start_not_published_as_last_charge(monkeypatch):
    """Home Assistant ne doit pas voir passer la tentative avortée comme
    « dernière charge » du véhicule (0 Wh) avant la relance."""
    vid = _setup()
    calls = []

    async def rec(vehicle_id):
        calls.append(vehicle_id)
    monkeypatch.setattr(_mqtt, "publish_vehicle_last_charge", rec)

    async def scenario():
        cp = FakeCP()
        await _aborted_start(cp, vid, datetime.utcnow() - timedelta(seconds=10))
        await asyncio.sleep(0.2)
    asyncio.run(scenario())
    assert calls == []


# --- StopTransactionOnEVSideDisconnect = false ---

def test_keep_transaction_on_ev_disconnect_then_resume():
    """Clé à false : un Available/Preparing pendant la charge ne clôt plus la
    session ; la charge qui reprend reste la même transaction."""
    vid = _setup(keep_txn_on_disconnect=True)

    async def scenario():
        cp = FakeCP()
        _csms.CONNECTED_CHARGERS["c1"] = cp
        _csms.PENDING_REMOTE_STARTS[("c1", 1)] = vid
        r = await cp.on_start_transaction(connector_id=1, id_tag=f"REMOTE-{vid}",
                                          meter_start=1000, timestamp=_iso(datetime.utcnow()))
        await cp.on_status_notification(connector_id=1, status="Available", error_code="NoError")
        await cp.on_status_notification(connector_id=1, status="Preparing", error_code="NoError")
        await cp.on_status_notification(connector_id=1, status="Charging", error_code="NoError")
        await asyncio.sleep(0.2)
        return cp, r.transaction_id

    cp, tid = asyncio.run(scenario())
    txns = _txns()
    assert [t.id for t in txns] == [tid]
    assert txns[0].status == "active"
    assert cp.remote_stops == []


def test_keep_transaction_on_ev_disconnect_real_unplug_is_stopped():
    """Clé à false et vrai débranchement : après le délai, le serveur demande
    l'arrêt puis clôt lui-même la session faute de StopTransaction."""
    vid = _setup(keep_txn_on_disconnect=True)

    async def scenario():
        cp = FakeCP()
        _csms.CONNECTED_CHARGERS["c1"] = cp
        _csms.PENDING_REMOTE_STARTS[("c1", 1)] = vid
        r = await cp.on_start_transaction(connector_id=1, id_tag=f"REMOTE-{vid}",
                                          meter_start=1000, timestamp=_iso(datetime.utcnow()))
        await cp.on_status_notification(connector_id=1, status="Available", error_code="NoError")
        await asyncio.sleep(0.3)
        return cp, r.transaction_id

    cp, tid = asyncio.run(scenario())
    assert cp.remote_stops == [tid]
    txns = _txns()
    assert txns[0].status == "completed"
    assert txns[0].close_reason == "reconciled"
    assert ("c1", 1) not in _csms.EV_DISCONNECT_PENDING


def test_default_key_still_closes_immediately():
    """Clé absente ou à true (réglage d'usine) : comportement inchangé."""
    vid = _setup()

    async def scenario():
        cp = FakeCP()
        _csms.PENDING_REMOTE_STARTS[("c1", 1)] = vid
        await cp.on_start_transaction(connector_id=1, id_tag=f"REMOTE-{vid}",
                                      meter_start=1000,
                                      timestamp=_iso(datetime.utcnow() - timedelta(minutes=10)))
        await cp.on_status_notification(connector_id=1, status="Available", error_code="NoError")
        await asyncio.sleep(0.1)
        return cp

    cp = asyncio.run(scenario())
    txns = _txns()
    assert txns[0].status == "completed"
    assert txns[0].close_reason == "reconciled"
    assert cp.remote_stops == []


# --- Base de données (incident du 14/11 vers 00:05) ---

def test_engine_pool_and_sqlite_pragmas(tmp_path):
    eng = _app_db.make_engine(f"sqlite:///{tmp_path / 'x.sqlite'}")
    try:
        assert eng.pool.size() == _app_db.POOL_SIZE
        assert eng.pool._max_overflow == _app_db.POOL_MAX_OVERFLOW
        assert eng.pool._timeout == _app_db.POOL_TIMEOUT_S
        assert _app_db.POOL_SIZE + _app_db.POOL_MAX_OVERFLOW > 40, \
            "la réserve doit dépasser le nombre de threads de l'API (40)"
        with eng.connect() as conn:
            assert conn.exec_driver_sql("PRAGMA journal_mode").scalar().lower() == "wal"
            assert conn.exec_driver_sql("PRAGMA busy_timeout").scalar() == _app_db.SQLITE_BUSY_TIMEOUT_MS
    finally:
        eng.dispose()


def test_meter_values_indexes_created_on_existing_db(tmp_path, monkeypatch):
    """Une base existante (table meter_values créée sans index) reçoit les
    index au démarrage."""
    eng = _app_db.make_engine(f"sqlite:///{tmp_path / 'old.sqlite'}")
    try:
        with eng.begin() as conn:
            conn.exec_driver_sql(
                "CREATE TABLE meter_values (id INTEGER PRIMARY KEY, charger_id VARCHAR NOT NULL, "
                "transaction_id INTEGER, connector_id INTEGER, timestamp DATETIME, "
                "measurand VARCHAR NOT NULL, value FLOAT NOT NULL, unit VARCHAR)")
        monkeypatch.setattr(_app_db, "engine", eng)
        _app_db._ensure_indexes()
        names = {ix["name"] for ix in inspect(eng).get_indexes("meter_values")}
        assert {"ix_meter_values_txn", "ix_meter_values_charger_conn_ts"} <= names
        _app_db._ensure_indexes()  # idempotent
    finally:
        eng.dispose()
