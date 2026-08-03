import enum
from datetime import datetime

from sqlalchemy import (
    Column, String, Integer, Float, Boolean, DateTime, ForeignKey, Enum, UniqueConstraint
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


class ChargerMode(str, enum.Enum):
    local = "local"
    relay = "relay"


class AuthMode(str, enum.Enum):
    """Politique de déclenchement de la charge, par borne (mode local seulement).

    - free       : sans autorisation. Tout idTag est accepté ; la charge peut
                   démarrer automatiquement au branchement (plug & charge),
                   sans badge ni bouton. C'est le comportement historique.
    - authorized : avec autorisation. Seuls un idTag connu (associé à un
                   véhicule) ou un démarrage explicite depuis l'appli / MQTT
                   sont acceptés ; un badge inconnu est refusé.
    """
    free = "free"
    authorized = "authorized"


class Charger(Base):
    __tablename__ = "chargers"

    id = Column(String, primary_key=True)  # charge_point_id / boxIdentity
    vendor = Column(String, nullable=True)
    model = Column(String, nullable=True)
    serial = Column(String, nullable=True)
    ocpp_version = Column(String, nullable=True)  # "1.6" ou "2.0.1", détecté à la connexion
    mode = Column(Enum(ChargerMode), default=ChargerMode.local, nullable=False)
    # Politique d'autorisation de charge (mode local). NULL possible sur une
    # base migrée depuis une version antérieure : interprété comme 'free'.
    auth_mode = Column(Enum(AuthMode), default=AuthMode.free, nullable=True)
    relay_url = Column(String, nullable=True)  # URL de base du serveur officiel, si mode=relay
    status = Column(String, default="Unknown")  # dernier StatusNotification connu
    tariff_plan_id = Column(Integer, ForeignKey("tariff_plans.id"), nullable=True)
    last_seen = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    transactions = relationship("Transaction", back_populates="charger")
    meter_values = relationship("MeterValue", back_populates="charger")
    config_keys = relationship("ConfigurationKey", back_populates="charger")
    connector_statuses = relationship("ConnectorStatus", back_populates="charger")
    tariff_plan = relationship("TariffPlan", back_populates="chargers")


class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    charger_id = Column(String, ForeignKey("chargers.id"), nullable=False)
    connector_id = Column(Integer, nullable=False)
    id_tag = Column(String, nullable=True)
    vehicle_id = Column(Integer, ForeignKey("vehicles.id"), nullable=True)
    meter_start = Column(Float, nullable=True)
    meter_stop = Column(Float, nullable=True)
    start_time = Column(DateTime, default=datetime.utcnow)
    stop_time = Column(DateTime, nullable=True)
    status = Column(String, default="active")  # active | completed

    # Figés au moment de la clôture (StopTransaction), pour qu'une modification
    # ultérieure des tarifs (prix, suppression d'une période...) n'altère
    # jamais rétroactivement le coût d'une charge déjà terminée.
    cost = Column(Float, nullable=True)
    energy_wh = Column(Float, nullable=True)
    tariff_plan_name = Column(String, nullable=True)

    # Renseignés manuellement par l'utilisateur (aucun capteur ne les fournit)
    odometer_km = Column(Float, nullable=True)
    battery_percent_start = Column(Float, nullable=True)
    battery_percent_end = Column(Float, nullable=True)

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
    connector_id = Column(Integer, nullable=False)  # 0 = la borne elle-même
    status = Column(String, nullable=False)
    error_code = Column(String, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow)

    charger = relationship("Charger", back_populates="connector_statuses")


class Vehicle(Base):
    __tablename__ = "vehicles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
    id_tag = Column(String, nullable=True, unique=True)  # badge associé à ce véhicule
    battery_capacity_kwh = Column(Float, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    transactions = relationship("Transaction", back_populates="vehicle")


class TariffPlan(Base):
    __tablename__ = "tariff_plans"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
    is_default = Column(Boolean, default=False)
    fixed_price = Column(Float, nullable=True)  # €/kWh, utilisé si aucune période ne correspond
    subscribed_power_kva = Column(Float, nullable=True)  # puissance souscrite de l'abonnement, informatif
    created_at = Column(DateTime, default=datetime.utcnow)

    periods = relationship(
        "TariffPeriod", back_populates="plan",
        cascade="all, delete-orphan", order_by="TariffPeriod.id",
    )
    chargers = relationship("Charger", back_populates="tariff_plan")


class TariffPeriod(Base):
    """Une plage horaire nommée avec son propre prix. Plusieurs périodes
    peuvent être définies par plan (heures pleines, heures creuses, tarif
    week-end, etc.) ; en cas de chevauchement, la première période qui
    correspond (par ordre de création) l'emporte."""
    __tablename__ = "tariff_periods"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tariff_plan_id = Column(Integer, ForeignKey("tariff_plans.id"), nullable=False)
    name = Column(String, nullable=False)
    price = Column(Float, nullable=False)  # €/kWh
    days_of_week = Column(String, nullable=False, default="0,1,2,3,4,5,6")  # 0=lundi ... 6=dimanche
    start_time = Column(String, nullable=False)  # "HH:MM"
    end_time = Column(String, nullable=False)  # "HH:MM" ; si < start_time, chevauche minuit

    plan = relationship("TariffPlan", back_populates="periods")


class UserRole(str, enum.Enum):
    admin = "admin"
    user = "user"


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String, unique=True, nullable=False)
    password_hash = Column(String, nullable=False)
    role = Column(Enum(UserRole), default=UserRole.user, nullable=False)
