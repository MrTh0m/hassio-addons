import logging
import os

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from .models import Base, User, UserRole
from .auth import hash_password, verify_password

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


def _sync_admin_password(db):
    """Aligne le mot de passe du compte admin sur la valeur configurée dans
    l'add-on (option `admin_password` -> OCPP_ADMIN_PASSWORD).

    Sans ça, le mot de passe n'était pris en compte qu'à la toute première
    création du compte : le modifier ensuite depuis la page de configuration
    Home Assistant n'avait aucun effet (le compte existait déjà), d'où le
    fait que seul le mot de passe par défaut restait accepté.

    On ne réécrit le hash que si le mot de passe configuré ne correspond plus
    à celui en base, pour ne pas invalider inutilement les sessions à chaque
    démarrage. Un jeton sentinelle (`__keep__`) permet de ne PAS toucher au
    mot de passe (utile si l'utilisateur l'a changé par un autre moyen)."""
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


def init_db():
    Base.metadata.create_all(engine)
    _migrate_schema()
    # Crée un compte admin par défaut si aucun utilisateur n'existe encore,
    # puis aligne son mot de passe sur la configuration de l'add-on.
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
    finally:
        db.close()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
