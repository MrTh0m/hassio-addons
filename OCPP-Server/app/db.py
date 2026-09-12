import logging
import os

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateTable

from .models import Base, User, UserRole, UserPermission
from .auth import hash_password, verify_password

logger = logging.getLogger("db")

DATA_DIR = os.environ.get("OCPP_DATA_DIR", "/data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "ocpp_server.sqlite")

engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _migrate_schema():
    """Ajoute les colonnes manquantes sur les tables déjà existantes."""
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            existing_columns = {col["name"] for col in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing_columns:
                    continue
                col_type = column.type.compile(engine.dialect)
                logger.info("Migration : ajout de la colonne %s.%s (%s)", table.name, column.name, col_type)
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {col_type}'))


def _migrate_relax_not_null(table_name: str) -> bool:
    """Retire une contrainte NOT NULL héritée d'un ancien schéma SQLite sur une
    colonne que le modèle SQLAlchemy déclare désormais nullable=True (ex.
    transactions.charger_id, devenue nullable avec l'introduction des charges
    externes, mais la table existante gardait l'ancienne contrainte). SQLite
    ne permettant pas ALTER COLUMN, on recrée la table selon le protocole
    officiel : nouvelle table au bon schéma, copie des données, bascule.
    Ne fait rien (retourne False) si le schéma en base est déjà cohérent."""
    conn = engine.connect()
    try:
        pragma = conn.exec_driver_sql(f'PRAGMA table_info("{table_name}")').fetchall()
        if not pragma:
            return False
        model_table = Base.metadata.tables[table_name]
        model_columns = {c.name for c in model_table.columns}
        db_columns_ordered = [row[1] for row in pragma]

        stale_not_null = []
        for row in pragma:
            col_name = row[1]
            db_notnull = bool(row[3])
            if not db_notnull or col_name not in model_columns:
                continue
            model_col = model_table.columns[col_name]
            if model_col.nullable and not model_col.primary_key:
                stale_not_null.append(col_name)

        if not stale_not_null:
            return False

        logger.info(
            "Migration : suppression de la contrainte NOT NULL héritée sur %s (%s)",
            table_name, ", ".join(stale_not_null),
        )

        tmp_name = f"_migrate_tmp_{table_name}"
        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        conn.exec_driver_sql(f'DROP TABLE IF EXISTS "{tmp_name}"')

        ddl = str(CreateTable(model_table).compile(engine)).strip()
        ddl = ddl.replace(f'CREATE TABLE "{table_name}"', f'CREATE TABLE "{tmp_name}"', 1)
        if f'CREATE TABLE {table_name} ' in ddl:
            ddl = ddl.replace(f'CREATE TABLE {table_name} ', f'CREATE TABLE "{tmp_name}" ', 1)
        conn.exec_driver_sql(ddl)

        common_cols = [c for c in db_columns_ordered if c in model_columns]
        cols_csv = ", ".join(f'"{c}"' for c in common_cols)
        conn.exec_driver_sql(
            f'INSERT INTO "{tmp_name}" ({cols_csv}) SELECT {cols_csv} FROM "{table_name}"'
        )
        conn.exec_driver_sql(f'DROP TABLE "{table_name}"')
        conn.exec_driver_sql(f'ALTER TABLE "{tmp_name}" RENAME TO "{table_name}"')
        conn.commit()
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")
        return True
    finally:
        conn.close()


def _sync_admin_password(db):
    configured = os.environ.get("OCPP_ADMIN_PASSWORD")
    if not configured or configured == "__keep__":
        return
    admin = db.query(User).filter(User.username == "admin").first()
    if not admin:
        return
    if not verify_password(configured, admin.password_hash):
        admin.password_hash = hash_password(configured)
        db.commit()
        logger.info("Mot de passe du compte admin mis à jour depuis la configuration de l'add-on")


def _ensure_user_permissions(db):
    """Crée les lignes UserPermission manquantes pour les users existants."""
    users = db.query(User).filter(User.deleted_at == None).all()
    for u in users:
        if u.permissions is None:
            perm = UserPermission(user_id=u.id)
            db.add(perm)
    db.commit()


def init_db():
    Base.metadata.create_all(engine)
    _migrate_schema()
    # transactions.charger_id/connector_id sont devenues nullable (charges
    # externes) mais une base créée avant ce changement garde l'ancienne
    # contrainte NOT NULL en dur dans le schéma SQLite : à corriger une fois.
    _migrate_relax_not_null("transactions")
    db = SessionLocal()
    try:
        if db.query(User).count() == 0:
            default_password = os.environ.get("OCPP_ADMIN_PASSWORD", "admin")
            admin = User(
                username="admin",
                password_hash=hash_password(default_password),
                role=UserRole.admin,
            )
            db.add(admin)
            db.commit()
        else:
            _sync_admin_password(db)
        _ensure_user_permissions(db)
    finally:
        db.close()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
