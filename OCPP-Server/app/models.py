import enum
from datetime import datetime

from sqlalchemy import (
    Column, String, Integer, Float, Boolean, DateTime, ForeignKey, Enum, UniqueConstraint, Text
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


class ChargerMode(str, enum.Enum):
    local = "local"
    relay = "relay"


class AuthMode(str, enum.Enum):
    free = "free"
    authorized = "authorized"


class UserRole(str, enum.Enum):
    admin = "admin"
    user = "user"


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String, unique=True, nullable=False)
    password_hash = Column(String, nullable=False)
    role = Column(Enum(UserRole), default=UserRole.user, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    deleted_at = Column(DateTime, nullable=True)

    vehicle_links = relationship("UserVehicle", back_populates="user", cascade="all, delete-orphan")
    charger_links = relationship("UserCharger", back_populates="user", cascade="all, delete-orphan")
    permissions = relationship("UserPermission", back_populates="user", uselist=False, cascade="all, delete-orphan")


class UserVehicle(Base):
    """Association user <-> véhicule (N:N)."""
    __tablename__ = "user_vehicles"
    __table_args__ = (UniqueConstraint("user_id", "vehicle_id", name="uq_user_vehicle"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    vehicle_id = Column(Integer, ForeignKey("vehicles.id"), nullable=False)

    user = relationship("User", back_populates="vehicle_links")
    vehicle = relationship("Vehicle", back_populates="user_links")


class UserCharger(Base):
    """Association user <-> borne (N:N)."""
    __tablename__ = "user_chargers"
    __table_args__ = (UniqueConstraint("user_id", "charger_id", name="uq_user_charger"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    charger_id = Column(String, ForeignKey("chargers.id"), nullable=False)

    user = relationship("User", back_populates="charger_links")
    charger = relationship("Charger", back_populates="user_links")


class UserPermission(Base):
    """Droits granulaires d'un user (admin = tous les droits implicitement)."""
    __tablename__ = "user_permissions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False)
    # Réglages borne (mode, tarif, programmation, auth_mode)
    can_manage_chargers = Column(Boolean, default=False)
    # Abonnements électriques
    can_manage_tariffs = Column(Boolean, default=False)
    # Gérer les voitures (créer, modifier ses voitures)
    can_manage_vehicles = Column(Boolean, default=False)
    # Voir les logs OCPP
    can_view_logs = Column(Boolean, default=False)
    # Exporter / importer les données
    can_export_import = Column(Boolean, default=False)

    user = relationship("User", back_populates="permissions")


class Charger(Base):
    __tablename__ = "chargers"

    id = Column(String, primary_key=True)
    vendor = Column(String, nullable=True)
    model = Column(String, nullable=True)
    serial = Column(String, nullable=True)
    ocpp_version = Column(String, nullable=True)
    mode = Column(Enum(ChargerMode), default=ChargerMode.local, nullable=False)
    auth_mode = Column(Enum(AuthMode), default=AuthMode.free, nullable=True)
    relay_url = Column(String, nullable=True)
    display_name = Column(String, nullable=True)
    status = Column(String, default="Unknown")
    # true/false/null : support SmartCharging détecté, non supporté, ou inconnu
    smart_charging = Column(Boolean, nullable=True)
    tariff_plan_id = Column(Integer, ForeignKey("tariff_plans.id"), nullable=True)
    last_seen = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    deleted_at = Column(DateTime, nullable=True)

    transactions = relationship("Transaction", back_populates="charger")
    meter_values = relationship("MeterValue", back_populates="charger")
    config_keys = relationship("ConfigurationKey", back_populates="charger")
    connector_statuses = relationship("ConnectorStatus", back_populates="charger")
    tariff_plan = relationship("TariffPlan", back_populates="chargers")
    charge_conditions = relationship(
        "ChargeCondition", back_populates="charger", cascade="all, delete-orphan",
    )
    user_links = relationship("UserCharger", back_populates="charger", cascade="all, delete-orphan")


class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    charger_id = Column(String, ForeignKey("chargers.id"), nullable=True)
    connector_id = Column(Integer, nullable=True)
    id_tag = Column(String, nullable=True)
    vehicle_id = Column(Integer, ForeignKey("vehicles.id"), nullable=True)
    meter_start = Column(Float, nullable=True)
    meter_stop = Column(Float, nullable=True)
    start_time = Column(DateTime, default=datetime.utcnow)
    stop_time = Column(DateTime, nullable=True)
    status = Column(String, default="active")  # active | completed

    is_external = Column(Boolean, default=False)
    location_label = Column(String, nullable=True)

    cost = Column(Float, nullable=True)
    energy_wh = Column(Float, nullable=True)
    tariff_plan_name = Column(String, nullable=True)

    odometer_km = Column(Float, nullable=True)
    battery_percent_start = Column(Float, nullable=True)
    battery_percent_end = Column(Float, nullable=True)

    # Indique que la charge est intentionnellement suspendue en attente d'une
    # condition de programmation (start_after, off_peak…). La transaction est
    # ouverte (câble verrouillé) mais aucun kWh ne transite encore.
    deferred_until = Column(String, nullable=True)  # "HH:MM" ou description libre

    charger = relationship("Charger", back_populates="transactions")
    vehicle = relationship("Vehicle", back_populates="transactions")


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


class ConnectorStatus(Base):
    __tablename__ = "connector_statuses"
    __table_args__ = (
        UniqueConstraint("charger_id", "connector_id", name="uq_charger_connector"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    charger_id = Column(String, ForeignKey("chargers.id"), nullable=False)
    connector_id = Column(Integer, nullable=False)
    status = Column(String, nullable=False)
    error_code = Column(String, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow)

    charger = relationship("Charger", back_populates="connector_statuses")


class Vehicle(Base):
    __tablename__ = "vehicles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
    id_tag = Column(String, nullable=True, unique=True)
    battery_capacity_kwh = Column(Float, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    deleted_at = Column(DateTime, nullable=True)

    transactions = relationship("Transaction", back_populates="vehicle")
    user_links = relationship("UserVehicle", back_populates="vehicle", cascade="all, delete-orphan")


class TariffPlan(Base):
    __tablename__ = "tariff_plans"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
    is_default = Column(Boolean, default=False)
    fixed_price = Column(Float, nullable=True)
    subscribed_power_kva = Column(Float, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    periods = relationship(
        "TariffPeriod", back_populates="plan",
        cascade="all, delete-orphan", order_by="TariffPeriod.id",
    )
    chargers = relationship("Charger", back_populates="tariff_plan")


class TariffPeriod(Base):
    __tablename__ = "tariff_periods"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tariff_plan_id = Column(Integer, ForeignKey("tariff_plans.id"), nullable=False)
    name = Column(String, nullable=False)
    price = Column(Float, nullable=False)
    days_of_week = Column(String, nullable=False, default="0,1,2,3,4,5,6")
    start_time = Column(String, nullable=False)
    end_time = Column(String, nullable=False)

    plan = relationship("TariffPlan", back_populates="periods")


class ChargeConditionType(str, enum.Enum):
    off_peak = "off_peak"
    start_after = "start_after"
    ready_by = "ready_by"


class ChargeCondition(Base):
    __tablename__ = "charge_conditions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    charger_id = Column(String, ForeignKey("chargers.id"), nullable=False)
    connector_id = Column(Integer, nullable=True)
    type = Column(Enum(ChargeConditionType), nullable=False)
    time_value = Column(String, nullable=True)
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    charger = relationship("Charger", back_populates="charge_conditions")
