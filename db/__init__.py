from .posts import Posts
from .clients import Clients
from .black_list import BlackList
from .reservations import Reservations
from .for_delivery import ForDelivery
from .temp_fulfilied import Temp_Fulfilled
from .in_delivery import InDelivery
from .temp_reservations import TempReservations
from .bot_session import BotSession
from .revision_logs import RevisionLog
from .deleted_post_snapshots import DeletedPostSnapshot
from .delivery_cleanup_runs import DeliveryCleanupRun
from .post_id_reservations import PostIdReservation
from .db import AbstractModel, engine
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session
from datetime import datetime

LEGACY_DELIVERY_CUTOFF_AT = datetime(2026, 5, 16, 14, 0, 0)

def init_db():
    AbstractModel.metadata.create_all(engine)
    run_schema_migrations()


def run_schema_migrations():
    ensure_schema_migrations()
    run_migration("001_reservations_created_at", ensure_reservations_created_at)
    run_migration("002_delivery_cutoff_metadata", ensure_delivery_cutoff_metadata)
    run_migration("003_revision_delivery_cleanup_tables", ensure_revision_delivery_cleanup_tables)
    run_migration("004_post_id_reservations", ensure_post_id_reservations)


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


def add_column_if_missing(table_name, column_name, ddl):
    inspector = inspect(engine)
    if table_name not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns(table_name)}
    if column_name in columns:
        return

    with engine.begin() as connection:
        connection.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {ddl}"))


def ensure_delivery_cutoff_metadata():
    timestamp_type = (
        "TIMESTAMP WITHOUT TIME ZONE"
        if engine.dialect.name == "postgresql"
        else "DATETIME"
    )

    add_column_if_missing("reservations", "fulfilled_at", timestamp_type)
    add_column_if_missing("reservations", "reserved_group_message_id", "BIGINT")
    add_column_if_missing("for_delivery", "delivery_cutoff_at", timestamp_type)
    add_column_if_missing("temp_fulfilled", "reservation_id", "INTEGER")
    add_column_if_missing("in_delivery", "reservation_id", "INTEGER")

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())

    with engine.begin() as connection:
        if "reservations" in tables:
            connection.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_reservations_fulfilled_at "
                "ON reservations (is_fulfilled, fulfilled_at)"
            ))
        if "for_delivery" in tables:
            connection.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_for_delivery_cutoff "
                "ON for_delivery (delivery_cutoff_at)"
            ))
            connection.execute(
                text(
                    "UPDATE for_delivery "
                    "SET delivery_cutoff_at = :cutoff "
                    "WHERE delivery_cutoff_at IS NULL"
                ),
                {"cutoff": LEGACY_DELIVERY_CUTOFF_AT},
            )
        if "temp_fulfilled" in tables:
            connection.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_temp_fulfilled_reservation_id "
                "ON temp_fulfilled (reservation_id)"
            ))
        if "in_delivery" in tables:
            connection.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_in_delivery_reservation_id "
                "ON in_delivery (reservation_id)"
            ))

    if not {"reservations", "temp_fulfilled"}.issubset(tables):
        return

    if engine.dialect.name == "postgresql":
        backfill_query = """
            WITH one_to_one AS (
                SELECT
                    MIN(r.id) AS reservation_id,
                    MIN(tf.id) AS temp_fulfilled_id,
                    MIN(tf.created_at) AS fulfilled_at
                FROM reservations r
                JOIN temp_fulfilled tf
                    ON tf.user_id = r.user_id
                    AND tf.post_id = r.post_id
                WHERE r.is_fulfilled = TRUE
                    AND r.fulfilled_at IS NULL
                    AND tf.reservation_id IS NULL
                GROUP BY r.user_id, r.post_id
                HAVING COUNT(DISTINCT r.id) = 1
                    AND COUNT(DISTINCT tf.id) = 1
            )
            UPDATE reservations r
            SET fulfilled_at = one_to_one.fulfilled_at
            FROM one_to_one
            WHERE r.id = one_to_one.reservation_id
        """
        temp_backfill_query = """
            WITH one_to_one AS (
                SELECT
                    MIN(r.id) AS reservation_id,
                    MIN(tf.id) AS temp_fulfilled_id
                FROM reservations r
                JOIN temp_fulfilled tf
                    ON tf.user_id = r.user_id
                    AND tf.post_id = r.post_id
                WHERE r.is_fulfilled = TRUE
                    AND tf.reservation_id IS NULL
                GROUP BY r.user_id, r.post_id
                HAVING COUNT(DISTINCT r.id) = 1
                    AND COUNT(DISTINCT tf.id) = 1
            )
            UPDATE temp_fulfilled tf
            SET reservation_id = one_to_one.reservation_id
            FROM one_to_one
            WHERE tf.id = one_to_one.temp_fulfilled_id
        """
    else:
        backfill_query = """
            UPDATE reservations
            SET fulfilled_at = (
                SELECT MIN(tf.created_at)
                FROM temp_fulfilled tf
                WHERE tf.user_id = reservations.user_id
                    AND tf.post_id = reservations.post_id
            )
            WHERE is_fulfilled = 1
                AND fulfilled_at IS NULL
                AND 1 = (
                    SELECT COUNT(*)
                    FROM reservations r2
                    WHERE r2.user_id = reservations.user_id
                        AND r2.post_id = reservations.post_id
                        AND r2.is_fulfilled = 1
                )
                AND 1 = (
                    SELECT COUNT(*)
                    FROM temp_fulfilled tf2
                    WHERE tf2.user_id = reservations.user_id
                        AND tf2.post_id = reservations.post_id
                )
        """
        temp_backfill_query = """
            UPDATE temp_fulfilled
            SET reservation_id = (
                SELECT MIN(r.id)
                FROM reservations r
                WHERE r.user_id = temp_fulfilled.user_id
                    AND r.post_id = temp_fulfilled.post_id
                    AND r.is_fulfilled = 1
            )
            WHERE reservation_id IS NULL
                AND 1 = (
                    SELECT COUNT(*)
                    FROM reservations r2
                    WHERE r2.user_id = temp_fulfilled.user_id
                        AND r2.post_id = temp_fulfilled.post_id
                        AND r2.is_fulfilled = 1
                )
                AND 1 = (
                    SELECT COUNT(*)
                    FROM temp_fulfilled tf2
                    WHERE tf2.user_id = temp_fulfilled.user_id
                        AND tf2.post_id = temp_fulfilled.post_id
                )
        """

    with engine.begin() as connection:
        connection.execute(text(backfill_query))
        connection.execute(text(temp_backfill_query))


def ensure_revision_delivery_cleanup_tables():
    AbstractModel.metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_revision_logs_auditor_created "
            "ON revision_logs (auditor_user_id, created_at)"
        ))
        connection.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_revision_logs_post_id "
            "ON revision_logs (post_id)"
        ))
        connection.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_revision_logs_selected_date "
            "ON revision_logs (selected_date)"
        ))
        connection.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_deleted_post_snapshots_post_id "
            "ON deleted_post_snapshots (post_id)"
        ))
        connection.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_deleted_post_snapshots_created_at "
            "ON deleted_post_snapshots (created_at)"
        ))
        connection.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_delivery_cleanup_runs_slot_key "
            "ON delivery_cleanup_runs (slot_key)"
        ))
        connection.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_delivery_cleanup_runs_started_at "
            "ON delivery_cleanup_runs (started_at)"
        ))


def ensure_post_id_reservations():
    AbstractModel.metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_post_id_reservations_chat_id "
            "ON post_id_reservations (chat_id)"
        ))
        connection.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_post_id_reservations_reserved_at "
            "ON post_id_reservations (reserved_at)"
        ))

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
    "RevisionLog",
    "DeletedPostSnapshot",
    "DeliveryCleanupRun",
    "PostIdReservation",
    "init_db",
}
