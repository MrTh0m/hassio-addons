import enum
from datetime import datetime

from sqlalchemy import (
    Column, String, Integer, Float, Boolean, DateTime, ForeignKey, Enum
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


class ChargerMode(str, enum.Enum):
    local = "local"
    relay = "relay"


class Charger(Base):
    __tablename__ = "chargers"

    id = Column(String, primary_key=True)  # charge_point_id / boxIdentity
    vendor = Column(String, nullable=True)
    model = Column(String, nullable=True)
    serial = Column(String, nullable=True)
    ocpp_version = Column(String, nullable=True)  # "1.6" ou "2.0.1", détecté à la connexion
    mode = Column(Enum(ChargerMode), default=ChargerMode.local, nullable=False)
    relay_url = Column(String, nullable=True)  # URL de base du serveur officiel, si mode=relay
    status = Column(String, default="Unknown")  # dernier StatusNotification connu
    last_seen = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    transactions = relationship("Transaction", back_populates="charger")
    meter_values = relationship("MeterValue", back_populates="charger")
    config_keys = relationship("ConfigurationKey", back_populates="charger")


class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    charger_id = Column(String, ForeignKey("chargers.id"), nullable=False)
    connector_id = Column(Integer, nullable=False)
    id_tag = Column(String, nullable=True)
    meter_start = Column(Float, nullable=True)
    meter_stop = Column(Float, nullable=True)
    start_time = Column(DateTime, default=datetime.utcnow)
    stop_time = Column(DateTime, nullable=True)
    status = Column(String, default="active")  # active | completed

    charger = relationship("Charger", back_populates="transactions")


class MeterValue(Base):
    __tablename__ = "meter_values"

    id = Column(Integer, primary_key=True, autoincrement=True)
    charger_id = Column(String, ForeignKey("chargers.id"), nullable=False)
    transaction_id = Column(Integer, ForeignKey("transactions.id"), nullable=True)
    connector_id = Column(Integer, nullable=True)
    timestamp = Column(DateTime, default=datetime.utcnow)
    measurand = Column(String, nullable=False)
    value = Column(Float, nullable=False)
    unit = Column(String, nullable=True)

    charger = relationship("Charger", back_populates="meter_values")


class ConfigurationKey(Base):
    __tablename__ = "configuration_keys"

    id = Column(Integer, primary_key=True, autoincrement=True)
    charger_id = Column(String, ForeignKey("chargers.id"), nullable=False)
    key = Column(String, nullable=False)
    value = Column(String, nullable=True)
    readonly = Column(Boolean, default=False)

    charger = relationship("Charger", back_populates="config_keys")


class UserRole(str, enum.Enum):
    admin = "admin"
    user = "user"


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String, unique=True, nullable=False)
    password_hash = Column(String, nullable=False)
    role = Column(Enum(UserRole), default=UserRole.user, nullable=False)
