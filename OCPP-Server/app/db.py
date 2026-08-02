import logging
import os

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from .models import Base, User, UserRole
from .auth import hash_password

logger = logging.getLogger("db")

DATA_DIR = os.environ.get("OCPP_DATA_DIR", "/data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "ocpp_server.sqlite")

engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _migrate_schema():
    """Ajoute les colonnes manquantes sur les tables déjà existantes.

    `Base.metadata.create_all()` ne crée que les tables absentes, il ne
    modifie jamais une table qui existe déjà. Sans ça, une base créée par une
    version antérieure de l'add-on garde son ancien schéma pour toujours, et
    toute requête référençant une colonne ajoutée depuis (ex. `tariff_plan_id`,
    `vehicle_id`) échoue avec "no such column"."""
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue  # table neuve : déjà créée avec le bon schéma par create_all()
            existing_columns = {col["name"] for col in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing_columns:
                    continue
                col_type = column.type.compile(engine.dialect)
                logger.info("Migration : ajout de la colonne %s.%s (%s)", table.name, column.name, col_type)
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {col_type}'))


def init_db():
    Base.metadata.create_all(engine)
    _migrate_schema()
    # Crée un compte admin par défaut si aucun utilisateur n'existe encore
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
    finally:
        db.close()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
