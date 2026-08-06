import logging
import os

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

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
