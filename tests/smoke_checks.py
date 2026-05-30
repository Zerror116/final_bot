from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "main.py"
sys.path.insert(0, str(ROOT))

from services.pricing import calculate_audit_price
from services.telegram_safe import is_message_not_modified_error, safe_delete_message


def assert_equal(actual, expected, label):
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")


def test_audit_prices():
    examples = {
        1: 50,
        49: 50,
        50: 50,
        100: 50,
        500: 450,
        1200: 1100,
        3450: 3100,
    }
    for old_price, new_price in examples.items():
        assert_equal(calculate_audit_price(old_price), new_price, f"audit price {old_price}")


def test_delivery_callbacks_are_namespaced():
    text = MAIN.read_text(encoding="utf-8")
    if 'callback_data="yes"' in text or 'callback_data="no"' in text:
        raise AssertionError("delivery keyboard must not create generic yes/no callbacks")
    if "delivery_yes" not in text or "delivery_no" not in text:
        raise AssertionError("delivery callbacks are missing")


def test_manual_audit_old_flow_removed():
    text = MAIN.read_text(encoding="utf-8")
    forbidden = [
        "def handle_edit_price_for_audit",
        "def edit_post_price_for_audit",
        "def handle_edit_description_for_audit",
        "def edit_post_description_for_audit",
        "def handle_edit_quantity_for_audit",
        "def edit_post_quantity_for_audit",
        "def delete_post_handler_for_audit",
        "def confirm_post(call)",
    ]
    for marker in forbidden:
        if marker in text:
            raise AssertionError(f"manual audit flow still contains {marker}")


def test_reservation_sum_queries_use_reserved_price_without_posts_join():
    text = (ROOT / "handlers" / "reservations_manage.py").read_text(encoding="utf-8")
    if text.count("func.coalesce(Reservations.old_price, 0)") < 2:
        raise AssertionError("reservation sums must use reservation old_price without live posts")
    if ".join(" in text or "Posts.price" in text:
        raise AssertionError("reservation sums must not drop rows when posts were snapshotted")


def test_order_amount_uses_reserved_price():
    text = MAIN.read_text(encoding="utf-8")
    required = [
        "def calculate_order_amount(order, post=None):",
        "unit_price = order.old_price if order.old_price is not None else fallback_price",
        "def get_post_or_snapshot(session, post_id):",
        "DeletedPostSnapshot.post_id == post_id",
    ]
    for marker in required:
        if marker not in text:
            raise AssertionError(f"reserved price calculation missing {marker}")


def test_delivery_cutoff_markers():
    main_text = MAIN.read_text(encoding="utf-8")
    db_init_text = (ROOT / "db" / "__init__.py").read_text(encoding="utf-8")
    required = [
        "LEGACY_DELIVERY_CUTOFF_AT = datetime(2026, 5, 16, 14, 0, 0)",
        "Reservations.fulfilled_at <= cutoff_at",
        "delivery_cutoff_at=delivery_cutoff_at",
        "get_delivery_reservations_query(",
        "reservation_id=reservation.id",
    ]
    for marker in required:
        if marker not in main_text:
            raise AssertionError(f"delivery cutoff implementation missing {marker}")

    yes_block = main_text.split('elif response == "delivery_yes":', 1)[1].split('elif response == "delivery_no":', 1)[0]
    for marker in [
        '"delivery_cutoff_at": serialize_datetime(delivery_cutoff_at)',
        "set_user_state(user_id, \"WAITING_FOR_ADDRESS\")",
    ]:
        if marker not in yes_block:
            raise AssertionError(f"delivery yes cutoff block missing {marker}")

    for marker in [
        '"002_delivery_cutoff_metadata"',
        '"fulfilled_at"',
        '"reserved_group_message_id"',
        '"delivery_cutoff_at"',
        '"reservation_id"',
    ]:
        if marker not in db_init_text:
            raise AssertionError(f"delivery migration missing {marker}")


def test_reserved_group_flow_markers():
    text = MAIN.read_text(encoding="utf-8")
    required = [
        "f\"Id товара: {reservation.post_id}\"",
        "def send_reserved_group_message",
        "def store_reserved_group_message_id",
        "TARGET_GROUP_ID",
        "reserved_group_message_id = message_id",
        "def update_reserved_group_message_by_id",
        "Статус: {status}",
        "Обработано:",
    ]
    for marker in required:
        if marker not in text:
            raise AssertionError(f"reserved group flow missing {marker}")

    if "✅ Положил" in text:
        raise AssertionError("reserved group messages must not include the old Положил button")


def test_reservation_auto_fulfill_uses_local_time():
    main_text = MAIN.read_text(encoding="utf-8")
    reservations_text = (ROOT / "db" / "reservations.py").read_text(encoding="utf-8")
    fulfilled_text = (ROOT / "db" / "temp_fulfilied.py").read_text(encoding="utf-8")

    for marker in ["datetime.now(timezone.utc)", "datetime.datetime.utcnow"]:
        if marker in reservations_text or marker in fulfilled_text:
            raise AssertionError("reservation fulfillment timestamps must use local bot time")

    required = [
        "def now_local()",
        "created_at=now_local()",
        "now = now or now_local()",
        "fulfilled_at = reservation.fulfilled_at or now_local()",
    ]
    for marker in required:
        if marker not in main_text:
            raise AssertionError(f"auto-fulfill local time marker missing {marker}")


def test_channel_post_updates_are_centralized():
    text = MAIN.read_text(encoding="utf-8")
    required = [
        "def update_channel_post_message",
        "def log_channel_sync_error",
        "def telegram_retry_after_seconds",
        "build_channel_post_markup(post)",
        "if post.quantity > 0:",
        "there is no text in the message to edit",
        "post.quantity <= 0",
    ]
    for marker in required:
        if marker not in text:
            raise AssertionError(f"channel post update helper missing {marker}")


def test_callback_answers_are_safe():
    text = MAIN.read_text(encoding="utf-8")
    if "def safe_answer_callback_query" not in text:
        raise AssertionError("safe callback answer helper is missing")
    unsafe_calls = [
        line for line in text.splitlines()
        if "bot.answer_callback_query" in line and "return bot.answer_callback_query" not in line
    ]
    if unsafe_calls:
        raise AssertionError("callback answers must use safe_answer_callback_query")


def test_phoenix_broadcast_markers():
    main_text = MAIN.read_text(encoding="utf-8")
    keyboard_text = (ROOT / "bot" / "keyboard.py").read_text(encoding="utf-8")
    qr_path = ROOT / "images" / "phoenix_qr.jpg"

    if not qr_path.exists():
        raise AssertionError("Phoenix QR image is missing")

    for marker in [
        'PHOENIX_BROADCAST_BUTTON = "Рассылка о Фениксе"',
        "def require_creator",
        "def get_phoenix_broadcast_recipients",
        "def run_phoenix_broadcast",
        "time.sleep(PHOENIX_BROADCAST_DELAY_SECONDS)",
        "PHOENIX_BROADCAST_BATCH_PAUSE_SECONDS",
        "bot.send_photo(",
        "https://garphoenix.com/join/INV-R9JQ-F8UW?tenant=default",
    ]:
        if marker not in main_text:
            raise AssertionError(f"Phoenix broadcast marker missing {marker}")

    if "Рассылка о Фениксе" not in keyboard_text:
        raise AssertionError("Phoenix broadcast button missing from creator menu")


def test_reservation_stats_markers():
    main_text = MAIN.read_text(encoding="utf-8")
    db_init_text = (ROOT / "db" / "__init__.py").read_text(encoding="utf-8")
    keyboard_text = (ROOT / "bot" / "keyboard.py").read_text(encoding="utf-8")
    event_text = (ROOT / "db" / "reservation_stat_events.py").read_text(encoding="utf-8")

    for marker in [
        "class ReservationStatEvent",
        "__tablename__ = \"reservation_stat_events\"",
        "uq_reservation_stat_events_event_reservation",
        "ix_reservation_stat_events_type_created",
    ]:
        if marker not in event_text:
            raise AssertionError(f"reservation stat event model marker missing {marker}")

    for marker in [
        "ReservationStatEvent",
        '"005_reservation_stat_events"',
        "def ensure_reservation_stat_events():",
        "INSERT INTO reservation_stat_events",
    ]:
        if marker not in db_init_text:
            raise AssertionError(f"reservation stat migration marker missing {marker}")

    for marker in [
        "RESERVATION_STATS_REPORT_HOUR = 16",
        "RESERVATION_STATS_REPORT_MINUTE = 10",
        "RESERVATION_STATS_DAY_START_HOUR = 6",
        "RESERVATION_STATS_DAY_END_HOUR = 23",
        "current.replace(hour=RESERVATION_STATS_DAY_START_HOUR",
        "current.replace(hour=RESERVATION_STATS_DAY_END_HOUR",
        "\"period_start\": start",
        "\"period_end\": end",
        "Период: {snapshot['period_start'].strftime('%H:%M')}-{snapshot['period_end'].strftime('%H:%M')} по Самаре",
        "def add_reservation_stat_event(session, event_type, reservation, event_time=None):",
        "RESERVATION_STATS_EVENT_CREATED",
        "RESERVATION_STATS_EVENT_CANCELED",
        "RESERVATION_STATS_EVENT_FULFILLED",
        "def send_daily_reservation_stats_report",
        "def start_reservation_stats_daily_report_worker():",
        "start_reservation_stats_daily_report_worker()",
        "def show_live_reservation_stats(message):",
        "message.text == \"БроньСтатистик\"",
        "def close_live_reservation_stats(call):",
        "time.sleep(10)",
        "build_reservation_stats_close_markup(user_id)",
        "add_reservation_stat_event(session, RESERVATION_STATS_EVENT_CREATED, reservation",
        "record_reservation_stat_event(RESERVATION_STATS_EVENT_CANCELED, order)",
        "add_reservation_stat_event(session, RESERVATION_STATS_EVENT_FULFILLED, reservation",
    ]:
        if marker not in main_text:
            raise AssertionError(f"reservation stat marker missing {marker}")

    if "БроньСтатистик" not in keyboard_text:
        raise AssertionError("reservation stats secret button missing from supreme leader menu")


def test_delivery_broadcast_campaign_markers():
    main_text = MAIN.read_text(encoding="utf-8")
    db_init_text = (ROOT / "db" / "__init__.py").read_text(encoding="utf-8")
    campaign_text = (ROOT / "db" / "delivery_broadcast_campaigns.py").read_text(encoding="utf-8")
    recipient_text = (ROOT / "db" / "delivery_broadcast_recipients.py").read_text(encoding="utf-8")

    for marker in [
        "class DeliveryBroadcastCampaign",
        "__tablename__ = \"delivery_broadcast_campaigns\"",
        "campaign_date",
        "cutoff_at",
        "last_scan_at",
    ]:
        if marker not in campaign_text:
            raise AssertionError(f"delivery broadcast campaign marker missing {marker}")

    for marker in [
        "class DeliveryBroadcastRecipient",
        "__tablename__ = \"delivery_broadcast_recipients\"",
        "uq_delivery_broadcast_recipients_campaign_phone",
        "telegram_message_id",
        "status",
    ]:
        if marker not in recipient_text:
            raise AssertionError(f"delivery broadcast recipient marker missing {marker}")

    for marker in [
        '"006_delivery_broadcast_campaigns"',
        "ensure_delivery_broadcast_campaigns",
        "DeliveryBroadcastCampaign",
        "DeliveryBroadcastRecipient",
    ]:
        if marker not in db_init_text:
            raise AssertionError(f"delivery broadcast migration marker missing {marker}")

    for marker in [
        "DELIVERY_BROADCAST_CUTOFF_HOUR = 14",
        "def get_or_create_delivery_broadcast_campaign",
        "def scan_delivery_broadcast_campaign",
        "def start_delivery_broadcast_monitor_worker",
        "start_delivery_broadcast_monitor_worker()",
        "DeliveryBroadcastRecipient(",
        "DELIVERY_RECIPIENT_STATUS_SENDING",
        "DELIVERY_RECIPIENT_STATUS_UNKNOWN",
        "send_delivery_offer_message",
        "Reservations.fulfilled_at == None",
        "Reservations.fulfilled_at <= cutoff_at",
    ]:
        if marker not in main_text:
            raise AssertionError(f"delivery broadcast main marker missing {marker}")


def test_channel_post_auto_publish_markers():
    text = MAIN.read_text(encoding="utf-8")
    for marker in [
        'AUTO_CHANNEL_POST_HOURS = {10, 12, 14, 16}',
        'SAMARA_TZ = ZoneInfo("Europe/Samara")',
        "channel_post_publish_lock = threading.Lock()",
        "CHANNEL_POST_SEND_ATTEMPTS = 3",
        "def send_channel_post_with_retries(post, caption, markup, source):",
        "is_connect_timeout_error(exc)",
        "failed_post_ids = []",
        "Channel post send failed for post_id=%s source=%s",
        "def publish_unsent_posts_to_channel(",
        "Отправка постов в канал уже выполняется",
        "def should_auto_publish_channel_posts(value):",
        "value.weekday() != 6",
        "value.minute == 0",
        "def start_channel_post_auto_publish_worker():",
        "start_channel_post_auto_publish_worker()",
        "def send_phoenix_channel_footer():",
        'if source == "auto" and sent_count and not failed_post_ids:',
        "send_phoenix_channel_footer()",
        "Phoenix footer sent to channel after post publish",
    ]:
        if marker not in text:
            raise AssertionError(f"channel post auto publish marker missing {marker}")


def test_post_management_only_shows_unpublished_posts():
    posts_text = (ROOT / "db" / "posts.py").read_text(encoding="utf-8")
    main_text = MAIN.read_text(encoding="utf-8")
    for marker in [
        "def get_unsent_posts():",
        "Posts.is_sent == False",
        "Posts.message_id == None",
        "return Posts.get_unsent_posts()",
    ]:
        if marker not in posts_text:
            raise AssertionError(f"unpublished post query marker missing {marker}")
    if "post.message_id = None" not in main_text:
        raise AssertionError("audit repost flow must clear old channel message_id")


def test_post_dates_use_samara_and_shift_sunday():
    posts_text = (ROOT / "db" / "posts.py").read_text(encoding="utf-8")
    main_text = MAIN.read_text(encoding="utf-8")
    for marker in [
        'SAMARA_TZ = ZoneInfo("Europe/Samara")',
        "def normalize_post_created_at(value=None):",
        "def default_post_created_at():",
        "datetime.now(SAMARA_TZ).replace(tzinfo=None)",
        "if value.weekday() == 6:",
        "value = value + timedelta(days=1)",
        "default=default_post_created_at",
        "created_at=normalize_post_created_at(created_at)",
        "def next_created_at(value=None):",
    ]:
        if marker not in posts_text:
            raise AssertionError(f"Samara post date marker missing {marker}")

    for marker in [
        "def now_local():",
        "return now_samara().replace(tzinfo=None)",
        "now = Posts.next_created_at()",
    ]:
        if marker not in main_text:
            raise AssertionError(f"Samara bot time marker missing {marker}")

    if "datetime.utcnow" in posts_text:
        raise AssertionError("post logic must not use UTC time")


def test_reservation_creation_is_atomic_and_missing_posts_do_not_queue():
    text = MAIN.read_text(encoding="utf-8")
    block = text.split("def handle_reservation(call):", 1)[1].split("# Получение бронирования пользователя", 1)[0]
    if "if not post:" not in block or "Товар больше недоступен." not in block:
        raise AssertionError("missing posts must not create temp queue entries")
    reservation_tail = block.split("post.quantity -= 1", 1)[1]
    if not (reservation_tail.index("session.add(reservation)") < reservation_tail.index("session.commit()")):
        raise AssertionError("stock decrement and reservation insert must commit together")


def test_post_delete_and_zero_quantity_are_safe():
    text = MAIN.read_text(encoding="utf-8")
    for marker in [
        "def disable_channel_post_reservation",
        "has_related_rows = any([",
        "Пост связан с заказами",
        "quantity <= 0",
        "build_channel_post_markup(post)",
    ]:
        if marker not in text:
            raise AssertionError(f"safe post deletion/quantity marker missing {marker}")


def test_delivery_move_and_archive_are_loss_safe():
    text = MAIN.read_text(encoding="utf-8")
    for marker in [
        "problematic_count = 0",
        "Строк без карточек товаров перенесено",
        "def build_delivery_row_description",
        "archive_delivery_clear_yes",
        "def cleanup_in_delivery_records",
        "DeliveryCleanupRun",
    ]:
        if marker not in text:
            raise AssertionError(f"delivery loss-safety marker missing {marker}")


def test_revision_excludes_linked_posts_and_logs_work():
    text = MAIN.read_text(encoding="utf-8")
    db_init_text = (ROOT / "db" / "__init__.py").read_text(encoding="utf-8")
    for marker in [
        "def get_revision_blocked_post_ids(session):",
        "session.query(Reservations.post_id)",
        "session.query(InDelivery.post_id)",
        "session.query(Temp_Fulfilled.post_id)",
        "session.query(TempReservations.post_id).filter(",
        "Posts.quantity > 0",
        "session.add(RevisionLog(",
        "Исключено из-за корзины/доставки/очереди",
    ]:
        if marker not in text:
            raise AssertionError(f"revision safety marker missing {marker}")

    revision_block = text.split("def apply_auto_audit_for_date", 1)[1].split("def answer_manual_audit_disabled", 1)[0]
    if "today_start" in revision_block:
        raise AssertionError("revision must not move sold-out posts to today 00:00")

    for marker in [
        "RevisionLog",
        "DeletedPostSnapshot",
        "DeliveryCleanupRun",
        '"003_revision_delivery_cleanup_tables"',
    ]:
        if marker not in db_init_text:
            raise AssertionError(f"new additive migration marker missing {marker}")


def test_delivery_cleanup_schedule_markers():
    text = MAIN.read_text(encoding="utf-8")
    for marker in [
        "DELIVERY_CLEANUP_WEEKDAYS = {0, 2, 4}",
        "DELIVERY_CLEANUP_HOUR = 22",
        "def delivery_cleanup_slot_key(value):",
        "def should_run_delivery_cleanup(value):",
        "value.hour == DELIVERY_CLEANUP_HOUR",
        "def run_scheduled_delivery_cleanup(current=None):",
        "slot_key=slot_key",
        "def start_delivery_cleanup_worker():",
        "start_delivery_cleanup_worker()",
    ]:
        if marker not in text:
            raise AssertionError(f"delivery cleanup schedule marker missing {marker}")


def test_midnight_posts_are_snapshotted_before_delete():
    text = MAIN.read_text(encoding="utf-8")
    for marker in [
        "def ensure_deleted_post_snapshot(session, post, reason):",
        "def cleanup_midnight_posts_after_snapshot",
        "post.created_at.time() == datetime.min.time()",
        "session.delete(post)",
        "with_reservations",
        "with_in_delivery",
        "with_temp_fulfilled",
        "with_wait_queue",
    ]:
        if marker not in text:
            raise AssertionError(f"midnight cleanup marker missing {marker}")


def test_cart_clear_processed_is_available():
    text = MAIN.read_text(encoding="utf-8")
    for marker in [
        "Расформировать обработанные",
        "Расформировать полностью",
        "admin_remove_cart_item_",
        "def clear_client_cart(",
        "def clear_processed(user_id):",
        "def delete_temp_fulfilled_for_reservation",
        "Temp_Fulfilled.reservation_id == reservation.id",
        "cleanup_or_refresh_for_delivery_by_phone",
        "Reservations.is_fulfilled == True",
        "def handle_clear_full_cart",
    ]:
        if marker not in text:
            raise AssertionError(f"cart clear processed marker missing {marker}")
    if "Обработанные товары удаляются только через" in text:
        raise AssertionError("cart clear processed warning must not be shown")


def test_channel_delete_happens_only_after_delivery_cleanup():
    text = MAIN.read_text(encoding="utf-8")
    required = [
        "def delete_delivered_channel_post_message",
        "bot.delete_message(chat_id=CHANNEL_ID",
        "Товар уехал в доставку",
        "Deleted delivered channel post",
        "cleanup_in_delivery_records",
        "delete_delivered_channel_post_message(post, source=source)",
    ]
    for marker in required:
        if marker not in text:
            raise AssertionError(f"delivery-only channel deletion missing {marker}")

    auto_fulfill_block = text.split("def auto_fulfill_expired_reservations", 1)[1].split("def reservation_auto_fulfill_loop", 1)[0]
    if "delete_delivered_channel_post_message" in auto_fulfill_block:
        raise AssertionError("auto-fulfilled reservations must not delete channel posts")
    if "delete_channel_post_if_fully_processed" in text:
        raise AssertionError("sold-out channel deletion helper must not be used")

    helper = text.split("def delete_delivered_channel_post_message", 1)[1].split("def build_telegram_proxy_url", 1)[0]
    if "TARGET_GROUP_ID" in helper:
        raise AssertionError("delivery channel deletion must not touch reserved group messages")


def test_delivery_clients_summary_markers():
    text = MAIN.read_text(encoding="utf-8")
    for marker in [
        '"Список Клиентов"',
        "def get_delivery_clients_summary():",
        "total_orders_sum",
        "processed_orders_sum",
        'group["processed_orders_sum"] < DELIVERY_THRESHOLD',
        'key=lambda row: (row["processed_orders_sum"], row["total_orders_sum"])',
        "def build_delivery_clients_summary_messages",
    ]:
        if marker not in text:
            raise AssertionError(f"delivery clients summary marker missing {marker}")


def test_delivery_collection_pauses_reserved_group_flow():
    text = MAIN.read_text(encoding="utf-8")
    for marker in [
        "RESERVED_GROUP_FLOW_STATE_KEY = 0",
        "def activate_reserved_group_delivery_pause():",
        "def is_reserved_group_delivery_paused():",
        "def send_delivery_reserved_group_snapshot():",
        "bot.send_message(TARGET_GROUP_ID, \"Брони на доставку\")",
        "f\"Id товара: {item['post_id']}\"",
        "def start_delivery_reserved_group_pause_and_snapshot():",
        "start_delivery_reserved_group_pause_and_snapshot()",
        "def flush_reserved_group_queue_after_delivery(",
        "def start_reserved_group_resume_flush_if_delivery_done(",
        "Reservations.reserved_group_message_id == None",
        "RESERVED_GROUP_SEND_INTERVAL_SECONDS = 5",
        "RESERVED_GROUP_MESSAGE_SKIPPED = -1",
        "def mark_reserved_group_message_skipped(session, reservation, reason):",
        "reservation_is_stale_for_current_post(reservation, post)",
        "reserved_group_resume_delay(remaining_count)",
        "return RESERVED_GROUP_SEND_INTERVAL_SECONDS",
        "wait_seconds = max(retry_after + 1, RESERVED_GROUP_SEND_INTERVAL_SECONDS)",
        "time.sleep(RESERVED_GROUP_SEND_INTERVAL_SECONDS)",
        "send_reserved_group_message(session, reservation, post, client, force=True)",
        "if not force and is_reserved_group_delivery_paused():",
        "recover_stale=False",
        "Recovering stale reserved group resume flag after process start",
        "state[\"delivery_collection_paused\"] = False",
        "state[\"resume_flush_started_at\"]",
        "start_reserved_group_resume_flush_if_delivery_done(message.chat.id)",
        "start_reserved_group_resume_flush_if_delivery_done(recover_stale=True)",
    ]:
        if marker not in text:
            raise AssertionError(f"delivery reserved group pause marker missing {marker}")


def test_post_id_labels_for_new_posts_and_delivery_collection():
    main_text = MAIN.read_text(encoding="utf-8")
    posts_text = (ROOT / "db" / "posts.py").read_text(encoding="utf-8")
    posts_manage_text = (ROOT / "handlers" / "posts_manage.py").read_text(encoding="utf-8")
    db_init_text = (ROOT / "db" / "__init__.py").read_text(encoding="utf-8")
    reservation_text = (ROOT / "db" / "post_id_reservations.py").read_text(encoding="utf-8")

    for marker in [
        "def reserve_next_id(chat_id=None, max_attempts=POST_ID_RESERVATION_ATTEMPTS):",
        "POST_ID_RESERVATION_ATTEMPTS = 10",
        "for attempt in range(1, max_attempts + 1):",
        "post_id_reservations",
        "SELECT 1 AS id",
        "WITH used_ids AS",
        "SELECT post_id AS id FROM reservations WHERE post_id > 0",
        "SELECT post_id AS id FROM temp_fulfilled WHERE post_id > 0",
        "SELECT post_id AS id FROM in_delivery WHERE post_id > 0",
        "SELECT post_id AS id FROM temp_reservations WHERE post_id > 0",
        "SELECT post_id AS id FROM deleted_post_snapshots WHERE post_id > 0",
        "SELECT post_id AS id FROM revision_logs WHERE post_id > 0",
        "SELECT id + 1 FROM used_ids",
        "PostIdReservation(",
        "except IntegrityError:",
        "def release_reserved_id(post_id, chat_id=None):",
        "def post_id_has_any_links(session, post_id):",
        "post_id: int = None",
        "id=post_id",
        "ID {post_id} уже занят другим товаром.",
        "ID {post_id} уже используется в истории товара.",
    ]:
        if marker not in posts_text:
            raise AssertionError(f"post id reservation marker missing {marker}")
    if "pg_advisory_xact_lock" in posts_text:
        raise AssertionError("post id reservation must not use advisory locks")

    for marker in [
        "class PostIdReservation",
        "__tablename__ = \"post_id_reservations\"",
        "post_id = mapped_column(Integer, primary_key=True)",
    ]:
        if marker not in reservation_text:
            raise AssertionError(f"post id reservation table marker missing {marker}")

    for marker in [
        "PostIdReservation",
        '"004_post_id_reservations"',
        "def ensure_post_id_reservations():",
    ]:
        if marker not in db_init_text:
            raise AssertionError(f"post id reservation migration marker missing {marker}")

    for marker in [
        "post_id: int = None",
        "post_id=post_id",
    ]:
        if marker not in posts_manage_text:
            raise AssertionError(f"save post id marker missing {marker}")

    for marker in [
        "reserved_post_id = Posts.reserve_next_id(chat_id=user_id)",
        "Id товара: {reserved_post_id}",
        "Отправьте фото",
        '"post_id": reserved_post_id',
        "Posts.release_reserved_id(data.get(\"post_id\"), chat_id=chat_id)",
        "Id товара: {created_post_id}",
        "Ваш пост успешно создан!",
        "def build_item_list_caption(description, price, quantity, created_at, post_id=None):",
        "Id товара: {post_id}",
        'post_id=item["post_id"]',
    ]:
        if marker not in main_text:
            raise AssertionError(f"post id display marker missing {marker}")


class RaisingBot:
    def __init__(self, exc):
        self.exc = exc

    def delete_message(self, **kwargs):
        raise self.exc


def test_telegram_safe_helpers():
    assert_equal(
        is_message_not_modified_error(Exception("Bad Request: message is not modified")),
        True,
        "message-not-modified detection",
    )
    assert_equal(
        safe_delete_message(
            RaisingBot(Exception("Bad Request: message to delete not found")),
            1,
            2,
        ),
        False,
        "safe delete ignores deleted messages",
    )
    assert_equal(
        safe_delete_message(RaisingBot(Exception("unused")), 1, None),
        False,
        "safe delete ignores empty message ids",
    )


def main():
    test_audit_prices()
    test_delivery_callbacks_are_namespaced()
    test_manual_audit_old_flow_removed()
    test_reservation_sum_queries_use_reserved_price_without_posts_join()
    test_order_amount_uses_reserved_price()
    test_delivery_cutoff_markers()
    test_reserved_group_flow_markers()
    test_reservation_auto_fulfill_uses_local_time()
    test_channel_post_updates_are_centralized()
    test_callback_answers_are_safe()
    test_phoenix_broadcast_markers()
    test_reservation_stats_markers()
    test_delivery_broadcast_campaign_markers()
    test_channel_post_auto_publish_markers()
    test_post_management_only_shows_unpublished_posts()
    test_post_dates_use_samara_and_shift_sunday()
    test_reservation_creation_is_atomic_and_missing_posts_do_not_queue()
    test_post_delete_and_zero_quantity_are_safe()
    test_delivery_move_and_archive_are_loss_safe()
    test_revision_excludes_linked_posts_and_logs_work()
    test_delivery_cleanup_schedule_markers()
    test_midnight_posts_are_snapshotted_before_delete()
    test_cart_clear_processed_is_available()
    test_channel_delete_happens_only_after_delivery_cleanup()
    test_delivery_clients_summary_markers()
    test_delivery_collection_pauses_reserved_group_flow()
    test_post_id_labels_for_new_posts_and_delivery_collection()
    test_telegram_safe_helpers()
    print("smoke checks ok")


if __name__ == "__main__":
    main()
