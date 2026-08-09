"""
Tests pour la migration de schéma app/db.py::_migrate_relax_not_null.

Reproduit le bug réel rencontré en production : une base SQLite créée avant
l'introduction des charges externes avait transactions.charger_id en NOT
NULL. La migration doit retirer cette contrainte sans perte de données.
"""
import os
import sqlite3
import tempfile

import pytest


def _make_legacy_db(path: str):
    """Recrée le schéma cassé exact rencontré en production : charger_id
    NOT NULL sur transactions, avec des données existantes."""
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            charger_id VARCHAR NOT NULL,
            connector_id INTEGER,
            id_tag VARCHAR,
            vehicle_id INTEGER,
            meter_start FLOAT,
            meter_stop FLOAT,
            start_time DATETIME,
            stop_time DATETIME,
            status VARCHAR,
            is_external BOOLEAN,
            location_label VARCHAR,
            cost FLOAT,
            energy_wh FLOAT,
            tariff_plan_name VARCHAR,
            odometer_km FLOAT,
            battery_percent_start FLOAT,
            battery_percent_end FLOAT,
            deferred_until VARCHAR
        )
    """)
    conn.execute("""
        CREATE TABLE meter_values (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            charger_id VARCHAR,
            transaction_id INTEGER,
            connector_id INTEGER,
            timestamp DATETIME,
            measurand VARCHAR,
            value FLOAT,
            unit VARCHAR
        )
    """)
    conn.execute("""
        CREATE TABLE chargers (
            id VARCHAR PRIMARY KEY,
            vendor VARCHAR, model VARCHAR, serial VARCHAR, ocpp_version VARCHAR,
            mode VARCHAR, auth_mode VARCHAR, relay_url VARCHAR, display_name VARCHAR,
            status VARCHAR, smart_charging BOOLEAN, tariff_plan_id INTEGER,
            last_seen DATETIME, created_at DATETIME, deleted_at DATETIME
        )
    """)
    conn.execute("INSERT INTO chargers (id) VALUES ('charger-A')")
    conn.execute("""
        INSERT INTO transactions
            (charger_id, connector_id, meter_start, meter_stop, start_time, stop_time,
             status, is_external, energy_wh, cost, odometer_km)
        VALUES
            ('charger-A', 1, 95972.0, 98075.0, '2026-08-06 15:06:51', '2026-08-06 15:27:38',
             'completed', 0, 2103.0, 0.4191, 5161.0)
    """)
    conn.execute("""
        INSERT INTO meter_values (charger_id, transaction_id, connector_id, timestamp, measurand, value, unit)
        VALUES ('charger-A', 1, 1, '2026-08-06 15:27:36', 'Energy.Active.Import.Register', 98075.0, 'Wh')
    """)
    conn.commit()
    conn.close()


@pytest.fixture
def legacy_engine():
    """Moteur SQLAlchemy pointant vers une base au schéma legacy cassé."""
    path = tempfile.mktemp(suffix=".sqlite")
    _make_legacy_db(path)
    from sqlalchemy import create_engine
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    yield engine
    engine.dispose()
    os.remove(path)


def test_charger_id_is_not_null_before_migration(legacy_engine):
    """Confirme que la base de test reproduit bien le bug (sanity check)."""
    conn = legacy_engine.connect()
    info = conn.exec_driver_sql('PRAGMA table_info("transactions")').fetchall()
    charger_id_col = next(r for r in info if r[1] == "charger_id")
    assert bool(charger_id_col[3]) is True  # notnull=1
    conn.close()


def test_migration_removes_not_null_constraint(legacy_engine):
    import app.db as db_module
    original_engine = db_module.engine
    db_module.engine = legacy_engine
    try:
        changed = db_module._migrate_relax_not_null("transactions")
        assert changed is True

        conn = legacy_engine.connect()
        info = conn.exec_driver_sql('PRAGMA table_info("transactions")').fetchall()
        charger_id_col = next(r for r in info if r[1] == "charger_id")
        assert bool(charger_id_col[3]) is False  # notnull=0 après migration
        conn.close()
    finally:
        db_module.engine = original_engine


def test_migration_preserves_existing_data(legacy_engine):
    import app.db as db_module
    original_engine = db_module.engine
    db_module.engine = legacy_engine
    try:
        db_module._migrate_relax_not_null("transactions")
        conn = legacy_engine.connect()
        rows = conn.exec_driver_sql(
            "SELECT id, charger_id, energy_wh, cost, odometer_km FROM transactions"
        ).fetchall()
        assert rows == [(1, "charger-A", 2103.0, 0.4191, 5161.0)]
        conn.close()
    finally:
        db_module.engine = original_engine


def test_migration_preserves_meter_values(legacy_engine):
    """Les MeterValues (table séparée, non touchée par la migration) doivent
    rester intacts après recréation de la table transactions."""
    import app.db as db_module
    original_engine = db_module.engine
    db_module.engine = legacy_engine
    try:
        db_module._migrate_relax_not_null("transactions")
        conn = legacy_engine.connect()
        rows = conn.exec_driver_sql(
            "SELECT id, transaction_id, value FROM meter_values"
        ).fetchall()
        assert rows == [(1, 1, 98075.0)]
        conn.close()
    finally:
        db_module.engine = original_engine


def test_migration_allows_null_charger_id_after(legacy_engine):
    """Le vrai test de non-régression : après migration, une charge externe
    (charger_id=NULL) doit pouvoir être insérée sans IntegrityError."""
    import app.db as db_module
    original_engine = db_module.engine
    db_module.engine = legacy_engine
    try:
        db_module._migrate_relax_not_null("transactions")
        conn = legacy_engine.connect()
        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        conn.exec_driver_sql(
            "INSERT INTO transactions (charger_id, connector_id, vehicle_id, is_external, energy_wh, status) "
            "VALUES (NULL, NULL, NULL, 1, 5000.0, 'completed')"
        )
        conn.commit()
        rows = conn.exec_driver_sql(
            "SELECT id, charger_id, is_external FROM transactions ORDER BY id"
        ).fetchall()
        assert rows == [(1, "charger-A", 0), (2, None, 1)]
        conn.close()
    finally:
        db_module.engine = original_engine


def test_migration_is_idempotent(legacy_engine):
    """Appeler la migration deux fois ne doit rien casser : la seconde fois
    ne trouve plus de contrainte à retirer et ne fait rien (retourne False)."""
    import app.db as db_module
    original_engine = db_module.engine
    db_module.engine = legacy_engine
    try:
        first = db_module._migrate_relax_not_null("transactions")
        second = db_module._migrate_relax_not_null("transactions")
        assert first is True
        assert second is False

        conn = legacy_engine.connect()
        rows = conn.exec_driver_sql("SELECT id, charger_id FROM transactions").fetchall()
        assert rows == [(1, "charger-A")]
        conn.close()
    finally:
        db_module.engine = original_engine


def test_fresh_db_has_no_stale_constraint():
    """Une base créée directement depuis le modèle actuel (nouvelle
    installation) n'a jamais eu cette contrainte : la migration ne doit rien
    faire dessus."""
    from sqlalchemy import create_engine
    from app.models import Base
    path = tempfile.mktemp(suffix=".sqlite")
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)

    import app.db as db_module
    original_engine = db_module.engine
    db_module.engine = engine
    try:
        changed = db_module._migrate_relax_not_null("transactions")
        assert changed is False
    finally:
        db_module.engine = original_engine
        engine.dispose()
        os.remove(path)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
