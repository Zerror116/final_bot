from .posts import Posts
from .clients import Clients
from .black_list import BlackList
from .reservations import Reservations
from .for_delivery import ForDelivery
from .temp_fulfilied import Temp_Fulfilled
from .in_delivery import InDelivery
from .temp_reservations import TempReservations
from .bot_session import BotSession
from .delivery_broadcast_campaigns import DeliveryBroadcastCampaign
from .delivery_broadcast_recipients import DeliveryBroadcastRecipient
from .db import AbstractModel, engine
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

def init_db():
    AbstractModel.metadata.create_all(engine)
    run_schema_migrations()


def run_schema_migrations():
    ensure_schema_migrations()
    run_migration("001_reservations_created_at", ensure_reservations_created_at)
    run_migration("002_delivery_broadcast_campaigns", ensure_delivery_broadcast_campaigns)
    run_migration("003_delivery_cutoff_and_fulfilled_at", ensure_delivery_cutoff_and_fulfilled_at)


def ensure_schema_migrations():
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "name VARCHAR(255) PRIMARY KEY, "
            "applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
            ")"
        ))


def run_migration(name, migration_func):
    with engine.begin() as connection:
        exists = connection.execute(
            text("SELECT 1 FROM schema_migrations WHERE name = :name"),
            {"name": name},
        ).first()
    if exists:
        return

    migration_func()
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO schema_migrations (name) VALUES (:name)"),
            {"name": name},
        )


def ensure_reservations_created_at():
    inspector = inspect(engine)
    if "reservations" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("reservations")}
    if "created_at" in columns:
        return

    with engine.begin() as connection:
        if engine.dialect.name == "postgresql":
            connection.execute(
                text(
                    "ALTER TABLE reservations "
                    "ADD COLUMN created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW()"
                )
            )
        else:
            connection.execute(text("ALTER TABLE reservations ADD COLUMN created_at DATETIME"))
            connection.execute(
                text("UPDATE reservations SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL")
            )


def ensure_delivery_broadcast_campaigns():
    AbstractModel.metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_delivery_broadcast_campaigns_date "
            "ON delivery_broadcast_campaigns (campaign_date)"
        ))
        connection.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_delivery_broadcast_recipients_campaign_phone "
            "ON delivery_broadcast_recipients (campaign_id, phone)"
        ))
        connection.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_delivery_broadcast_recipients_status "
            "ON delivery_broadcast_recipients (status)"
        ))


def ensure_delivery_cutoff_and_fulfilled_at():
    inspector = inspect(engine)
    table_names = inspector.get_table_names()

    with engine.begin() as connection:
        if "reservations" in table_names:
            reservation_columns = {column["name"] for column in inspector.get_columns("reservations")}
            if "fulfilled_at" not in reservation_columns:
                if engine.dialect.name == "postgresql":
                    connection.execute(text("ALTER TABLE reservations ADD COLUMN fulfilled_at TIMESTAMP WITHOUT TIME ZONE"))
                else:
                    connection.execute(text("ALTER TABLE reservations ADD COLUMN fulfilled_at DATETIME"))
            connection.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_reservations_fulfilled_at "
                "ON reservations (is_fulfilled, fulfilled_at)"
            ))

        if "for_delivery" in table_names:
            delivery_columns = {column["name"] for column in inspector.get_columns("for_delivery")}
            if "delivery_cutoff_at" not in delivery_columns:
                if engine.dialect.name == "postgresql":
                    connection.execute(text("ALTER TABLE for_delivery ADD COLUMN delivery_cutoff_at TIMESTAMP WITHOUT TIME ZONE"))
                else:
                    connection.execute(text("ALTER TABLE for_delivery ADD COLUMN delivery_cutoff_at DATETIME"))

__all__ = {
    "Posts",
    "Clients",
    "BlackList",
    "Reservations",
    "TempReservations",
    "ForDelivery",
    "InDelivery",
    "Temp_Fulfilled",
    "BotSession",
    "DeliveryBroadcastCampaign",
    "DeliveryBroadcastRecipient",
    "init_db",
}
