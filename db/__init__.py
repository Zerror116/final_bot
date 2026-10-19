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
from .reservation_stat_events import ReservationStatEvent
from .delivery_broadcast_campaigns import DeliveryBroadcastCampaign
from .delivery_broadcast_recipients import DeliveryBroadcastRecipient
from .delivery_collection_items import DeliveryCollectionItem
from .missing_delivery_items import MissingDeliveryItem
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
    run_migration("005_reservation_stat_events", ensure_reservation_stat_events)
    run_migration("006_delivery_broadcast_campaigns", ensure_delivery_broadcast_campaigns)
    run_migration("007_black_list_silent_block", ensure_black_list_silent_block)
    run_migration("008_for_delivery_collection_claim", ensure_for_delivery_collection_claim)
    run_migration("009_delivery_broadcast_summary_tracking", ensure_delivery_broadcast_summary_tracking)
    run_migration("010_delivery_collection_items_and_missing", ensure_delivery_collection_items_and_missing)
    run_migration("011_delivery_collector_sheet_tracking", ensure_delivery_collector_sheet_tracking)
    run_migration("012_reconcile_post_channel_state", reconcile_post_channel_state)
    run_migration("013_clients_user_id_unique", ensure_clients_user_id_unique)
    run_migration("014_remove_orphan_wait_queue_entries", remove_orphan_wait_queue_entries)
    run_migration("015_remove_orphan_cart_records", remove_orphan_cart_records)
    run_migration("016_release_orphan_live_reservations", release_orphan_live_reservations)
    run_migration("017_remove_orphan_wait_queue_posts", remove_orphan_wait_queue_posts)
    run_migration("018_remove_unplaced_orphan_delivery_collection_items", remove_unplaced_orphan_delivery_collection_items)
    run_migration("019_remove_unlinked_missing_product_temp_fulfilled", remove_unlinked_missing_product_temp_fulfilled)
    run_migration("020_backfill_missing_product_snapshots", backfill_missing_product_snapshots)
    run_migration("021_delivery_collection_post_id_indexes", ensure_delivery_collection_post_id_indexes)
    run_migration("022_core_declared_indexes", ensure_core_declared_indexes)
    run_migration(
        "023_delivery_collection_item_collector_sheet_tracking",
        ensure_delivery_collection_item_collector_sheet_tracking,
    )
    run_migration(
        "024_backfill_completed_delivery_collection_item_collector_sheet_tracking",
        backfill_completed_delivery_collection_item_collector_sheet_tracking,
    )
    run_migration("025_for_delivery_cart_merge", ensure_for_delivery_cart_merge)
    run_migration(
        "026_delivery_collection_report_message_tracking",
        ensure_delivery_collection_report_message_tracking,
    )


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


def ensure_reservation_stat_events():
    AbstractModel.metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_reservation_stat_events_event_reservation "
            "ON reservation_stat_events (event_type, reservation_id)"
        ))
        connection.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_reservation_stat_events_type_created "
            "ON reservation_stat_events (event_type, created_at)"
        ))
        connection.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_reservation_stat_events_reservation_id "
            "ON reservation_stat_events (reservation_id)"
        ))
        connection.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_reservation_stat_events_user_id "
            "ON reservation_stat_events (user_id)"
        ))
        connection.execute(text(
            "INSERT INTO reservation_stat_events "
            "(event_type, reservation_id, user_id, post_id, quantity, created_at) "
            "SELECT 'created', r.id, r.user_id, r.post_id, r.quantity, "
            "COALESCE(r.created_at, CURRENT_TIMESTAMP) "
            "FROM reservations r "
            "WHERE NOT EXISTS ("
            "SELECT 1 FROM reservation_stat_events e "
            "WHERE e.event_type = 'created' AND e.reservation_id = r.id"
            ")"
        ))
        connection.execute(text(
            "INSERT INTO reservation_stat_events "
            "(event_type, reservation_id, user_id, post_id, quantity, created_at) "
            "SELECT 'fulfilled', r.id, r.user_id, r.post_id, r.quantity, r.fulfilled_at "
            "FROM reservations r "
            "WHERE r.is_fulfilled = TRUE "
            "AND r.fulfilled_at IS NOT NULL "
            "AND NOT EXISTS ("
            "SELECT 1 FROM reservation_stat_events e "
            "WHERE e.event_type = 'fulfilled' AND e.reservation_id = r.id"
            ")"
        ))


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


def ensure_delivery_broadcast_summary_tracking():
    timestamp_type = (
        "TIMESTAMP WITHOUT TIME ZONE"
        if engine.dialect.name == "postgresql"
        else "DATETIME"
    )

    AbstractModel.metadata.create_all(engine)
    add_column_if_missing("delivery_broadcast_campaigns", "summary_chat_id", "BIGINT")
    add_column_if_missing("delivery_broadcast_campaigns", "summary_message_id", "BIGINT")
    add_column_if_missing("delivery_broadcast_recipients", "response_status", "VARCHAR")
    add_column_if_missing("delivery_broadcast_recipients", "responded_at", timestamp_type)
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_delivery_broadcast_recipients_response_status "
            "ON delivery_broadcast_recipients (response_status)"
        ))


def ensure_black_list_silent_block():
    AbstractModel.metadata.create_all(engine)

    add_column_if_missing("black_list", "silent_block", "BOOLEAN NOT NULL DEFAULT FALSE")
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_black_list_silent_block "
            "ON black_list (silent_block)"
        ))


def ensure_for_delivery_collection_claim():
    AbstractModel.metadata.create_all(engine)
    timestamp_type = (
        "TIMESTAMP WITHOUT TIME ZONE"
        if engine.dialect.name == "postgresql"
        else "DATETIME"
    )

    add_column_if_missing("for_delivery", "collector_user_id", "BIGINT")
    add_column_if_missing("for_delivery", "collector_name", "VARCHAR")
    add_column_if_missing("for_delivery", "collection_started_at", timestamp_type)
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_for_delivery_collector_user_id "
            "ON for_delivery (collector_user_id)"
        ))


def ensure_delivery_collection_items_and_missing():
    AbstractModel.metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_delivery_collection_items_reservation_unit "
            "ON delivery_collection_items (reservation_id, unit_index)"
        ))
        connection.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_delivery_collection_items_delivery_id "
            "ON delivery_collection_items (delivery_id)"
        ))
        connection.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_delivery_collection_items_placed_at "
            "ON delivery_collection_items (placed_at)"
        ))
        connection.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_missing_delivery_items_status "
            "ON missing_delivery_items (status)"
        ))
        connection.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_missing_delivery_items_phone "
            "ON missing_delivery_items (phone)"
        ))


def ensure_delivery_collection_post_id_indexes():
    AbstractModel.metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_delivery_collection_items_post_id "
            "ON delivery_collection_items (post_id)"
        ))
        connection.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_missing_delivery_items_post_id "
            "ON missing_delivery_items (post_id)"
        ))


def ensure_core_declared_indexes():
    AbstractModel.metadata.create_all(engine)

    index_statements = [
        "CREATE INDEX IF NOT EXISTS ix_black_list_user_id ON black_list (user_id)",
        "CREATE INDEX IF NOT EXISTS ix_black_list_phone ON black_list (phone)",
        "CREATE INDEX IF NOT EXISTS ix_clients_user_id ON clients (user_id)",
        "CREATE INDEX IF NOT EXISTS ix_clients_phone ON clients (phone)",
        "CREATE INDEX IF NOT EXISTS ix_clients_name_phone ON clients (name, phone)",
        "CREATE INDEX IF NOT EXISTS ix_for_delivery_user_id ON for_delivery (user_id)",
        "CREATE INDEX IF NOT EXISTS ix_for_delivery_phone ON for_delivery (phone)",
        "CREATE INDEX IF NOT EXISTS ix_in_delivery_user_id ON in_delivery (user_id)",
        "CREATE INDEX IF NOT EXISTS ix_in_delivery_post_id ON in_delivery (post_id)",
        "CREATE INDEX IF NOT EXISTS ix_posts_chat_id ON posts (chat_id)",
        "CREATE INDEX IF NOT EXISTS ix_posts_is_sent ON posts (is_sent)",
        "CREATE INDEX IF NOT EXISTS ix_posts_message_id ON posts (message_id)",
        "CREATE INDEX IF NOT EXISTS ix_reservations_user_id ON reservations (user_id)",
        "CREATE INDEX IF NOT EXISTS ix_reservations_post_id ON reservations (post_id)",
        "CREATE INDEX IF NOT EXISTS ix_reservations_user_fulfilled ON reservations (user_id, is_fulfilled)",
        "CREATE INDEX IF NOT EXISTS ix_reservations_fulfilled_created_at ON reservations (is_fulfilled, created_at)",
        "CREATE INDEX IF NOT EXISTS ix_temp_fulfilled_user_post ON temp_fulfilled (user_id, post_id)",
        (
            "CREATE INDEX IF NOT EXISTS ix_temp_fulfilled_delivery_flags "
            "ON temp_fulfilled (user_id, in_delivery, defect, skidka)"
        ),
        "CREATE INDEX IF NOT EXISTS ix_temp_reservations_user_post ON temp_reservations (user_id, post_id)",
        (
            "CREATE INDEX IF NOT EXISTS ix_temp_reservations_post_fulfilled "
            "ON temp_reservations (post_id, temp_fulfilled)"
        ),
    ]

    with engine.begin() as connection:
        for statement in index_statements:
            connection.execute(text(statement))


def ensure_delivery_collector_sheet_tracking():
    AbstractModel.metadata.create_all(engine)
    timestamp_type = (
        "TIMESTAMP WITHOUT TIME ZONE"
        if engine.dialect.name == "postgresql"
        else "DATETIME"
    )

    add_column_if_missing("for_delivery", "collector_sheet_added_at", timestamp_type)
    add_column_if_missing("for_delivery", "collector_sheet_row", "INTEGER")
    add_column_if_missing("for_delivery", "collector_sheet_error", "TEXT")
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_for_delivery_collector_sheet_added_at "
            "ON for_delivery (collector_sheet_added_at)"
        ))


def ensure_delivery_collection_item_collector_sheet_tracking():
    AbstractModel.metadata.create_all(engine)
    timestamp_type = (
        "TIMESTAMP WITHOUT TIME ZONE"
        if engine.dialect.name == "postgresql"
        else "DATETIME"
    )

    add_column_if_missing("delivery_collection_items", "collector_sheet_added_at", timestamp_type)
    add_column_if_missing("delivery_collection_items", "collector_sheet_row", "INTEGER")
    add_column_if_missing("delivery_collection_items", "collector_sheet_error", "TEXT")
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_delivery_collection_items_collector_sheet_added_at "
            "ON delivery_collection_items (collector_sheet_added_at)"
        ))


def backfill_completed_delivery_collection_item_collector_sheet_tracking():
    AbstractModel.metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(text(
            "UPDATE delivery_collection_items "
            "SET collector_sheet_added_at = COALESCE(created_at, CURRENT_TIMESTAMP), "
            "collector_sheet_error = NULL "
            "WHERE collector_name IS NOT NULL "
            "AND collector_sheet_added_at IS NULL "
            "AND NOT EXISTS ("
            "SELECT 1 FROM for_delivery fd WHERE fd.id = delivery_collection_items.delivery_id"
            ")"
        ))
        connection.execute(text(
            "UPDATE delivery_collection_items "
            "SET collector_sheet_added_at = COALESCE(("
            "SELECT fd.collector_sheet_added_at FROM for_delivery fd "
            "WHERE fd.id = delivery_collection_items.delivery_id"
            "), created_at, CURRENT_TIMESTAMP), "
            "collector_sheet_row = ("
            "SELECT fd.collector_sheet_row FROM for_delivery fd "
            "WHERE fd.id = delivery_collection_items.delivery_id"
            "), "
            "collector_sheet_error = ("
            "SELECT fd.collector_sheet_error FROM for_delivery fd "
            "WHERE fd.id = delivery_collection_items.delivery_id"
            ") "
            "WHERE collector_name IS NOT NULL "
            "AND collector_sheet_added_at IS NULL "
            "AND EXISTS ("
            "SELECT 1 FROM for_delivery fd "
            "WHERE fd.id = delivery_collection_items.delivery_id "
            "AND fd.collector_sheet_added_at IS NOT NULL"
            ")"
        ))


def ensure_for_delivery_cart_merge():
    AbstractModel.metadata.create_all(engine)

    add_column_if_missing("for_delivery", "merged_into_delivery_id", "INTEGER")
    add_column_if_missing("for_delivery", "merge_order", "INTEGER NOT NULL DEFAULT 0")
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_for_delivery_merged_into_delivery_id "
            "ON for_delivery (merged_into_delivery_id)"
        ))
        connection.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_for_delivery_merge_order "
            "ON for_delivery (merge_order)"
        ))


def ensure_delivery_collection_report_message_tracking():
    AbstractModel.metadata.create_all(engine)

    add_column_if_missing("delivery_collection_items", "report_message_chat_id", "BIGINT")
    add_column_if_missing("delivery_collection_items", "report_message_id", "BIGINT")
    add_column_if_missing(
        "delivery_collection_items",
        "report_message_has_photo",
        "BOOLEAN NOT NULL DEFAULT FALSE",
    )
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_delivery_collection_items_report_message "
            "ON delivery_collection_items (report_message_chat_id, report_message_id)"
        ))


def reconcile_post_channel_state():
    with engine.begin() as connection:
        connection.execute(text(
            "UPDATE posts SET is_sent = TRUE "
            "WHERE message_id IS NOT NULL AND is_sent = FALSE"
        ))
        connection.execute(text(
            "UPDATE posts SET is_sent = FALSE "
            "WHERE message_id IS NULL AND is_sent = TRUE"
        ))


def ensure_clients_user_id_unique():
    AbstractModel.metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(text(
            "DELETE FROM clients "
            "WHERE id IN ("
            "SELECT id FROM ("
            "SELECT id, ROW_NUMBER() OVER ("
            "PARTITION BY user_id "
            "ORDER BY CASE role "
            "WHEN 'supreme_leader' THEN 4 "
            "WHEN 'admin' THEN 3 "
            "WHEN 'worker' THEN 2 "
            "WHEN 'audit' THEN 1 "
            "ELSE 0 END DESC, id DESC"
            ") AS duplicate_rank "
            "FROM clients"
            ") ranked_clients "
            "WHERE duplicate_rank > 1"
            ")"
        ))
        connection.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_clients_user_id "
            "ON clients (user_id)"
        ))


def remove_orphan_wait_queue_entries():
    AbstractModel.metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(text(
            "DELETE FROM temp_reservations "
            "WHERE NOT EXISTS ("
            "SELECT 1 FROM clients c WHERE c.user_id = temp_reservations.user_id"
            ")"
        ))


def remove_orphan_cart_records():
    AbstractModel.metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(text(
            "DELETE FROM temp_fulfilled "
            "WHERE NOT EXISTS ("
            "SELECT 1 FROM clients c WHERE c.user_id = temp_fulfilled.user_id"
            ") "
            "AND NOT EXISTS ("
            "SELECT 1 FROM reservations r WHERE r.id = temp_fulfilled.reservation_id"
            ") "
            "AND NOT EXISTS ("
            "SELECT 1 FROM in_delivery i "
            "WHERE i.reservation_id = temp_fulfilled.reservation_id "
            "OR (i.user_id = temp_fulfilled.user_id AND i.post_id = temp_fulfilled.post_id)"
            ") "
            "AND NOT EXISTS ("
            "SELECT 1 FROM for_delivery fd WHERE fd.user_id = temp_fulfilled.user_id"
            ")"
        ))
        connection.execute(text(
            "DELETE FROM reservations "
            "WHERE NOT EXISTS ("
            "SELECT 1 FROM clients c WHERE c.user_id = reservations.user_id"
            ") "
            "AND NOT EXISTS ("
            "SELECT 1 FROM posts p WHERE p.id = reservations.post_id"
            ") "
            "AND NOT EXISTS ("
            "SELECT 1 FROM deleted_post_snapshots s WHERE s.post_id = reservations.post_id"
            ") "
            "AND NOT EXISTS ("
            "SELECT 1 FROM temp_fulfilled tf WHERE tf.reservation_id = reservations.id"
            ") "
            "AND NOT EXISTS ("
            "SELECT 1 FROM in_delivery i "
            "WHERE i.reservation_id = reservations.id "
            "OR (i.user_id = reservations.user_id AND i.post_id = reservations.post_id)"
            ")"
        ))


def release_orphan_live_reservations():
    AbstractModel.metadata.create_all(engine)

    releasable_query = (
        "SELECT r.id, r.user_id, r.post_id, "
        "CASE WHEN COALESCE(r.quantity, 1) > 1 THEN COALESCE(r.quantity, 1) ELSE 1 END AS release_quantity "
        "FROM reservations r "
        "JOIN posts p ON p.id = r.post_id "
        "WHERE r.is_fulfilled = FALSE "
        "AND NOT EXISTS ("
        "SELECT 1 FROM clients c WHERE c.user_id = r.user_id"
        ") "
        "AND NOT EXISTS ("
        "SELECT 1 FROM temp_fulfilled tf WHERE tf.reservation_id = r.id"
        ") "
        "AND NOT EXISTS ("
        "SELECT 1 FROM in_delivery i "
        "WHERE i.reservation_id = r.id "
        "OR (i.user_id = r.user_id AND i.post_id = r.post_id)"
        ") "
        "AND NOT EXISTS ("
        "SELECT 1 FROM delivery_collection_items d WHERE d.reservation_id = r.id"
        ") "
        "AND NOT EXISTS ("
        "SELECT 1 FROM temp_reservations tq "
        "WHERE tq.post_id = r.post_id AND tq.temp_fulfilled = FALSE"
        ")"
    )

    with engine.begin() as connection:
        connection.execute(text(
            "WITH releasable AS ("
            f"{releasable_query}"
            ") "
            "INSERT INTO reservation_stat_events "
            "(event_type, reservation_id, user_id, post_id, quantity, created_at) "
            "SELECT 'canceled', r.id, r.user_id, r.post_id, r.release_quantity, CURRENT_TIMESTAMP "
            "FROM releasable r "
            "WHERE NOT EXISTS ("
            "SELECT 1 FROM reservation_stat_events e "
            "WHERE e.event_type = 'canceled' AND e.reservation_id = r.id"
            ")"
        ))
        connection.execute(text(
            "WITH releasable AS ("
            f"{releasable_query}"
            "), release_totals AS ("
            "SELECT post_id, SUM(release_quantity) AS release_quantity "
            "FROM releasable GROUP BY post_id"
            ") "
            "UPDATE posts "
            "SET quantity = COALESCE(quantity, 0) + ("
            "SELECT release_totals.release_quantity "
            "FROM release_totals "
            "WHERE release_totals.post_id = posts.id"
            ") "
            "WHERE id IN (SELECT post_id FROM release_totals)"
        ))
        connection.execute(text(
            "WITH releasable AS ("
            f"{releasable_query}"
            ") "
            "DELETE FROM reservations "
            "WHERE id IN (SELECT id FROM releasable)"
        ))


def remove_orphan_wait_queue_posts():
    AbstractModel.metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(text(
            "DELETE FROM temp_reservations "
            "WHERE NOT EXISTS ("
            "SELECT 1 FROM posts p WHERE p.id = temp_reservations.post_id"
            ")"
        ))


def remove_unplaced_orphan_delivery_collection_items():
    AbstractModel.metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(text(
            "DELETE FROM delivery_collection_items "
            "WHERE placed_at IS NULL "
            "AND NOT EXISTS ("
            "SELECT 1 FROM for_delivery fd WHERE fd.id = delivery_collection_items.delivery_id"
            ")"
        ))


def remove_unlinked_missing_product_temp_fulfilled():
    AbstractModel.metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(text(
            "DELETE FROM temp_fulfilled "
            "WHERE in_delivery = FALSE "
            "AND defect = FALSE "
            "AND skidka = FALSE "
            "AND NOT EXISTS ("
            "SELECT 1 FROM posts p WHERE p.id = temp_fulfilled.post_id"
            ") "
            "AND NOT EXISTS ("
            "SELECT 1 FROM deleted_post_snapshots s WHERE s.post_id = temp_fulfilled.post_id"
            ") "
            "AND NOT EXISTS ("
            "SELECT 1 FROM reservations r WHERE r.id = temp_fulfilled.reservation_id"
            ") "
            "AND NOT EXISTS ("
            "SELECT 1 FROM in_delivery i "
            "WHERE i.reservation_id = temp_fulfilled.reservation_id "
            "OR (i.user_id = temp_fulfilled.user_id AND i.post_id = temp_fulfilled.post_id)"
            ") "
            "AND NOT EXISTS ("
            "SELECT 1 FROM delivery_collection_items d WHERE d.reservation_id = temp_fulfilled.reservation_id"
            ")"
        ))


def backfill_missing_product_snapshots():
    AbstractModel.metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(text(
            "WITH ranked_temp AS ("
            "SELECT "
            "tf.post_id, "
            "COALESCE(r.old_price, CASE "
            "WHEN COALESCE(tf.price, 0) / CASE WHEN COALESCE(tf.quantity, 1) > 1 THEN COALESCE(tf.quantity, 1) ELSE 1 END > 0 "
            "THEN COALESCE(tf.price, 0) / CASE WHEN COALESCE(tf.quantity, 1) > 1 THEN COALESCE(tf.quantity, 1) ELSE 1 END "
            "ELSE 0 END) AS price, "
            "COALESCE(NULLIF(tf.item_description, ''), 'Товар удалён из карточек (ID ' || tf.post_id || ')') AS description, "
            "COALESCE(r.created_at, tf.created_at, CURRENT_TIMESTAMP) AS created_at, "
            "ROW_NUMBER() OVER ("
            "PARTITION BY tf.post_id "
            "ORDER BY CASE WHEN r.id IS NOT NULL THEN 0 ELSE 1 END, tf.created_at DESC, tf.id DESC"
            ") AS row_rank "
            "FROM temp_fulfilled tf "
            "LEFT JOIN reservations r ON r.id = tf.reservation_id "
            "WHERE NOT EXISTS ("
            "SELECT 1 FROM posts p WHERE p.id = tf.post_id"
            ") "
            "AND NOT EXISTS ("
            "SELECT 1 FROM deleted_post_snapshots s WHERE s.post_id = tf.post_id"
            ")"
            ") "
            "INSERT INTO deleted_post_snapshots "
            "(post_id, chat_id, photo, price, description, message_id, quantity, is_sent, created_at, deleted_at, reason) "
            "SELECT "
            "post_id, 0, NULL, price, description, NULL, 0, FALSE, created_at, CURRENT_TIMESTAMP, "
            "'migration:temp_fulfilled_missing_product' "
            "FROM ranked_temp "
            "WHERE row_rank = 1"
        ))
        connection.execute(text(
            "WITH ranked_reservations AS ("
            "SELECT "
            "r.post_id, "
            "CASE WHEN COALESCE(r.old_price, 0) > 0 THEN COALESCE(r.old_price, 0) ELSE 0 END AS price, "
            "'Товар удалён из карточек (ID ' || r.post_id || ')' AS description, "
            "COALESCE(r.created_at, CURRENT_TIMESTAMP) AS created_at, "
            "ROW_NUMBER() OVER ("
            "PARTITION BY r.post_id "
            "ORDER BY r.created_at DESC, r.id DESC"
            ") AS row_rank "
            "FROM reservations r "
            "WHERE NOT EXISTS ("
            "SELECT 1 FROM posts p WHERE p.id = r.post_id"
            ") "
            "AND NOT EXISTS ("
            "SELECT 1 FROM deleted_post_snapshots s WHERE s.post_id = r.post_id"
            ")"
            ") "
            "INSERT INTO deleted_post_snapshots "
            "(post_id, chat_id, photo, price, description, message_id, quantity, is_sent, created_at, deleted_at, reason) "
            "SELECT "
            "post_id, 0, NULL, price, description, NULL, 0, FALSE, created_at, CURRENT_TIMESTAMP, "
            "'migration:reservation_missing_product' "
            "FROM ranked_reservations "
            "WHERE row_rank = 1"
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
    "ReservationStatEvent",
    "DeliveryBroadcastCampaign",
    "DeliveryBroadcastRecipient",
    "DeliveryCollectionItem",
    "MissingDeliveryItem",
    "init_db",
}
