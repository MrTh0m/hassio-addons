import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from .models import Base, User, UserRole
from .auth import hash_password

DATA_DIR = os.environ.get("OCPP_DATA_DIR", "/data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "ocpp_server.sqlite")

engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def init_db():
    Base.metadata.create_all(engine)
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
