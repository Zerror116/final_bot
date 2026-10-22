from pathlib import Path
import ast
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "main.py"
sys.path.insert(0, str(ROOT))

from services.pricing import calculate_audit_price
from services.telegram_safe import (
    is_message_not_modified_error,
    is_transient_telegram_error,
    safe_delete_message,
    sanitize_telegram_error_text,
)


def assert_equal(actual, expected, label):
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")


def assert_marker_order(text, first, second, label):
    first_index = text.find(first)
    second_index = text.find(second)
    if first_index == -1:
        raise AssertionError(f"{label}: missing first marker {first!r}")
    if second_index == -1:
        raise AssertionError(f"{label}: missing second marker {second!r}")
    if first_index > second_index:
        raise AssertionError(f"{label}: {first!r} must appear before {second!r}")


def test_audit_prices():
    examples = {
        1: 100,
        49: 100,
        50: 100,
        100: 100,
        150: 100,
        200: 150,
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
        "def delivery_response_deadline_label():",
        "Ответьте до {delivery_response_deadline_label()}",
    ]
    for marker in required:
        if marker not in main_text:
            raise AssertionError(f"delivery cutoff implementation missing {marker}")

    yes_block = main_text.split('elif response == "delivery_yes":', 1)[1].split('elif response == "delivery_no":', 1)[0]
    for marker in [
        "address_prompt = safe_send_message(",
        "if not address_prompt:",
        "Delivery address prompt was not sent for user_id=%s",
        "safe_delete_message(bot, user_id, message_id, logger=logger)",
        '"delivery_cutoff_at": serialize_datetime(delivery_cutoff_at)',
        "set_user_state(user_id, \"WAITING_FOR_ADDRESS\")",
    ]:
        if marker not in yes_block:
            raise AssertionError(f"delivery yes cutoff block missing {marker}")
    response_block = main_text.split("def handle_delivery_response_callback(call):", 1)[1].split(
        "# Обрабатывает ввод адреса пользователя.",
        1,
    )[0]
    if "is_delivery_campaign_before_cutoff(current)" not in response_block:
        raise AssertionError("delivery response must use shared campaign cutoff helper")
    if "current_time.hour >= 16" in response_block:
        raise AssertionError("delivery response must not use a separate 16:00 cutoff")
    assert_marker_order(
        yes_block,
        "address_prompt = safe_send_message(",
        "set_user_state(user_id, \"WAITING_FOR_ADDRESS\")",
        "delivery yes state must be set only after address prompt is sent",
    )
    assert_marker_order(
        yes_block,
        "address_prompt = safe_send_message(",
        "safe_delete_message(bot, user_id, message_id, logger=logger)",
        "delivery yes offer buttons must be removed only after address prompt is sent",
    )
    assert_marker_order(
        yes_block,
        "safe_delete_message(bot, user_id, message_id, logger=logger)",
        "set_user_state(user_id, \"WAITING_FOR_ADDRESS\")",
        "delivery yes state must be set after stale offer buttons are removed",
    )

    if "Вы уже начали оформление доставки." in yes_block:
        raise AssertionError("delivery yes must continue checkout instead of blocking on an in-progress state")
    for marker in [
        "DELIVERY_CHECKOUT_STATES",
        "user_state_in(user_id, DELIVERY_CHECKOUT_STATES)",
    ]:
        if marker not in yes_block:
            raise AssertionError(f"delivery yes resume marker missing {marker}")

    address_header = main_text.split("def handle_address_input(message):", 1)[0][-500:]
    if "user_state_in(message.chat.id, DELIVERY_ADDRESS_INPUT_STATES)" not in address_header:
        raise AssertionError("delivery address handler must accept all checkout address states")
    address_block = main_text.split("def handle_address_input(message):", 1)[1].split(
        "@bot.message_handler(commands=[\"empty_delivery\"])",
        1,
    )[0]
    for marker in [
        "confirmation_message = safe_send_message(",
        "if not confirmation_message:",
        "Delivery confirmation prompt was not sent for user_id=%s",
        "set_user_state(user_id, \"WAITING_FOR_CONFIRMATION\")",
        "same_phone_users = [user_data]",
        "Не удалось отправить подтверждение. Отправьте адрес ещё раз.",
    ]:
        if marker not in address_block:
            raise AssertionError(f"delivery address state block missing {marker}")
    assert_marker_order(
        address_block,
        "confirmation_message = safe_send_message(",
        "set_user_state(user_id, \"WAITING_FOR_CONFIRMATION\")",
        "delivery confirmation state must be set only after confirmation prompt is sent",
    )

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
        "def truncate_telegram_caption(caption, limit=1024):",
        "caption=truncate_telegram_caption(caption)",
    ]
    for marker in required:
        if marker not in text:
            raise AssertionError(f"reserved group flow missing {marker}")

    reserved_group_block = text.split("def send_reserved_group_message", 1)[1].split(
        "def store_reserved_group_message_id",
        1,
    )[0]
    if "✅ Положил" in reserved_group_block:
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
        "call_telegram_with_retries(",
        "attempts=3",
        "Telegram channel post {action} caption edit",
        "Telegram channel post {action} text edit",
        "build_channel_post_markup(post)",
        "if post.quantity > 0:",
        "there is no text in the message to edit",
        "post.quantity <= 0",
    ]
    for marker in required:
        if marker not in text:
            raise AssertionError(f"channel post update helper missing {marker}")


def test_cancel_reservation_returns_stock_and_refreshes_channel():
    text = MAIN.read_text(encoding="utf-8")
    for marker in [
        "def cancel_unfulfilled_reservation(reservation_id, related_user_ids):",
        "post.quantity = int(post.quantity or 0) + quantity_to_release",
        "updated_post = Posts.get_row_by_id(post_id)",
        "update_channel_post_message(updated_post)",
        "send_queue_transfer_notifications(queued_notifications)",
        "cancel_result = cancel_unfulfilled_reservation(reservation_id, related_user_ids)",
    ]:
        if marker not in text:
            raise AssertionError(f"cancel reservation stock return marker missing {marker}")

    cancel_block = text.split("def cancel_reservation(call):", 1)[1].split(
        '@bot.callback_query_handler(func=lambda call: callback_data_startswith(call, "enqueue_"))',
        1,
    )[0]
    if "Posts.increment_quantity_by_id" in cancel_block:
        raise AssertionError("cancel flow must refresh stock and channel through the atomic helper")
    if "Reservations.cancel_order_by_id" in cancel_block:
        raise AssertionError("cancel flow must not delete reservation before stock/queue handling")


def test_callback_answers_are_safe():
    text = MAIN.read_text(encoding="utf-8")
    if "def safe_answer_callback_query" not in text:
        raise AssertionError("safe callback answer helper is missing")
    if "def parse_callback_int_suffix(call, prefix" not in text:
        raise AssertionError("safe callback integer parser is missing")
    if "def parse_delivery_collection_callback(data, prefix):" not in text:
        raise AssertionError("delivery collection callback parser is missing")
    callback_answer_block = text.split("def safe_answer_callback_query(", 1)[1].split(
        "def parse_callback_int_suffix(",
        1,
    )[0]
    for marker in [
        "error_text = sanitize_telegram_error_text(exc).lower()",
        "Telegram callback answer skipped: %s",
        "sanitize_telegram_error_text(exc)",
        "Telegram callback answer failed: %s",
    ]:
        if marker not in callback_answer_block:
            raise AssertionError(f"safe callback answer redaction marker missing {marker}")
    for forbidden in [
        "error_text = str(exc).lower()",
        "logger.debug(\"Telegram callback answer skipped: %s\", exc)",
        "logger.warning(\"Telegram callback answer failed: %s\", exc)",
    ]:
        if forbidden in callback_answer_block:
            raise AssertionError(f"safe callback answer must not log raw Telegram exception: {forbidden}")
    unsafe_calls = [
        line for line in text.splitlines()
        if "bot.answer_callback_query" in line and "return bot.answer_callback_query" not in line
    ]
    if unsafe_calls:
        raise AssertionError("callback answers must use safe_answer_callback_query")

    for marker in [
        'post_id = parse_callback_int_suffix(call, "reserve_")',
        "Вы не можете бронировать товары, так как вы были заблокированы",
        'reservation_id = parse_callback_int_suffix(call, "order_")',
        'page = parse_callback_int_suffix(call, "orders_page_", "Некорректная страница.")',
        'reservation_id = parse_callback_int_suffix(call, prefix, "Некорректные данные для отмены.")',
        'post_id = parse_callback_int_suffix(call, "enqueue_")',
        'user_id = parse_callback_int_suffix(call, "promote_")',
        'user_id = parse_callback_int_suffix(call, "demote_")',
        'page = parse_callback_int_suffix(call, "delivery_page_", "Некорректная страница.")',
        'item_id = parse_callback_int_suffix(call, "delivery_item_placed_")',
        'item_id = parse_callback_int_suffix(call, "delivery_report_found_")',
        'delivery_id = parse_callback_int_suffix(call, "delivery_collected_")',
        'delivery_id = parse_callback_int_suffix(call, "delivery_finish_confirm_")',
        'delivery_id = parse_callback_int_suffix(call, "delivery_finish_continue_")',
        'page = parse_callback_int_suffix(call, "missing_delivery_page_", "Некорректная страница.")',
        'item_id = parse_callback_int_suffix(call, "select_defective_")',
        'item_id = parse_callback_int_suffix(call, "confirm_discount_", "Ошибка: некорректные данные.")',
        'item_id = parse_callback_int_suffix(call, "return_discount_", "Ошибка: некорректные данные.")',
        'and not callback_data_startswith(call, "select_defective_")',
        "def is_admin_defect_action_callback(call):",
        'data.startswith("defect_") and not data.startswith("defect_items_page_")',
        'item_id = parse_callback_int_suffix(call, "defect_")',
        'item_id = parse_callback_int_suffix(call, "discount_")',
        'target_user_id = parse_callback_int_suffix(call, "contact_")',
        "contact_client(call, target_user_id)",
    ]:
        if marker not in text:
            raise AssertionError(f"callback parser marker missing {marker}")

    delivery_collection_parser_block = text.split("def parse_delivery_collection_callback(data, prefix):", 1)[1].split(
        "def build_delivery_collection_button_text(",
        1,
    )[0]
    for marker in [
        "text = str(data or \"\")",
        "if not text.startswith(prefix):",
        "tail = text[len(prefix):]",
        "if len(parts) not in {1, 2} or not parts[0].isdigit():",
        "if len(parts) == 2 and not parts[1].isdigit():",
        "page = int(parts[1]) if len(parts) == 2 else 0",
    ]:
        if marker not in delivery_collection_parser_block:
            raise AssertionError(f"delivery collection callback strict parser marker missing {marker}")
    for forbidden in ["replace(prefix", ".strip(\"_\")", "len(parts) > 1"]:
        if forbidden in delivery_collection_parser_block:
            raise AssertionError(f"delivery collection callback parser must not use loose parsing marker: {forbidden}")

    parser_tree = ast.parse(text)
    parser_node = next(
        node
        for node in parser_tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "parse_delivery_collection_callback"
    )
    parser_namespace = {}
    exec(
        compile(ast.Module(body=[parser_node], type_ignores=[]), str(MAIN), "exec"),
        parser_namespace,
    )
    parser = parser_namespace["parse_delivery_collection_callback"]
    if parser("collect_delivery_123_4", "collect_delivery_") != (123, 4):
        raise AssertionError("delivery collection callback parser must parse id and page")
    if parser("collect_delivery_123", "collect_delivery_") != (123, 0):
        raise AssertionError("delivery collection callback parser must default missing page to zero")
    for bad_data in [
        "bad_collect_delivery_123_4",
        "collect_delivery__123_4",
        "collect_delivery_123_bad",
        "collect_delivery_123_4_extra",
        "collect_delivery_",
        "collect_delivery_abc_1",
    ]:
        try:
            parser(bad_data, "collect_delivery_")
        except ValueError:
            continue
        raise AssertionError(f"delivery collection callback parser accepted malformed data {bad_data!r}")

    reservation_block = text.split("def handle_reservation(call):", 1)[1].split(
        "# Получение бронирования пользователя",
        1,
    )[0]
    blacklist_index = reservation_block.find("if is_user_blacklisted(user_id):")
    blacklist_return_index = reservation_block.find("return", blacklist_index)
    if blacklist_index == -1 or blacklist_return_index == -1:
        raise AssertionError("reservation blacklist callback branch is missing")
    if "safe_answer_callback_query(" not in reservation_block[blacklist_index:blacklist_return_index]:
        raise AssertionError("reservation blacklist callback branch must answer callback before returning")

    delivery_cancel_block = text.split("def handle_delivery_otmena(call):", 1)[1].split(
        "@bot.callback_query_handler(func=lambda call: get_user_state(call.from_user.id) == \"WAITING_FOR_DATA_EDIT\")",
        1,
    )[0]
    assert_marker_order(
        delivery_cancel_block,
        "safe_answer_callback_query(callback_query_id=call.id)",
        "mark_delivery_broadcast_response(call.from_user.id, DELIVERY_RECIPIENT_RESPONSE_DECLINED)",
        "delivery cancel callback must record declined response after answering callback",
    )
    assert_marker_order(
        delivery_cancel_block,
        "mark_delivery_broadcast_response(call.from_user.id, DELIVERY_RECIPIENT_RESPONSE_DECLINED)",
        "refresh_delivery_candidates_summary(campaign_id)",
        "delivery cancel callback must refresh delivery summary after marking decline",
    )
    assert_marker_order(
        delivery_cancel_block,
        "safe_answer_callback_query(callback_query_id=call.id)",
        "safe_delete_message(bot, call.message.chat.id, call.message.message_id, logger=logger)",
        "delivery cancel callback must answer before deleting or sending messages",
    )


def test_callback_handlers_answer_before_slow_work():
    text = MAIN.read_text(encoding="utf-8")

    block_checks = [
        (
            "def show_rules(call):",
            "@bot.callback_query_handler(func=lambda call: call.data == \"back_to_start\")",
            "safe_answer_callback_query(call.id)",
            "safe_edit_message_text(",
            "rules callback must answer before editing message",
        ),
        (
            "def back_to_start(call):",
            "# Хэндлер регистрации",
            "safe_answer_callback_query(call.id)",
            "safe_delete_message(bot, call.message.chat.id, call.message.message_id, logger=logger)",
            "back-to-start callback must answer before deleting message",
        ),
        (
            "def order_details(call):",
            "def cancel_unfulfilled_reservation(",
            "safe_answer_callback_query(call.id)",
            "edit_message_media_with_retries(",
            "order details callback must answer before editing media",
        ),
        (
            "def handle_edit_price(call):",
            "# Обработчик кнопки \"Редактировать описание\"",
            "safe_answer_callback_query(call.id)",
            "safe_send_message(bot, user_id, \"Введите новую цену для поста:\"",
            "edit price callback must answer before asking for input",
        ),
        (
            "def handle_edit_description(call):",
            "# Обработчик кнопки \"Редактировать количество\"",
            "safe_answer_callback_query(call.id)",
            "safe_send_message(bot, user_id, \"Введите новое описание для поста:\"",
            "edit description callback must answer before asking for input",
        ),
        (
            "def handle_edit_quantity(call):",
            "# Обработчик ввода новой цены",
            "safe_answer_callback_query(call.id)",
            "safe_send_message(bot, user_id, \"Введите новое количество товара:\"",
            "edit quantity callback must answer before asking for input",
        ),
        (
            "def handle_delivery_response_callback(call):",
            "# Обрабатывает ввод адреса пользователя.",
            "safe_answer_callback_query(call.id)",
            "current = now_local()",
            "delivery response callback must answer before delivery work",
        ),
    ]

    for start, end, first, second, label in block_checks:
        block = text.split(start, 1)[1].split(end, 1)[0]
        assert_marker_order(block, first, second, label)

    close_stats_block = text.split("def close_live_reservation_stats(call):", 1)[1].split(
        "@bot.message_handler(commands=[\"unsent_posts\"])",
        1,
    )[0]
    if "safe_answer_callback_query(call.id)\n    with reservation_stats_live_lock:" not in close_stats_block:
        raise AssertionError("reservation stats close callback must answer before lock/edit/send work")

    reservation_block = text.split("def handle_reservation(call):", 1)[1].split(
        "# Получение бронирования пользователя",
        1,
    )[0]
    reservation_success_tail = reservation_block.split("session.commit()", 1)[1].split(
        "except IntegrityError:",
        1,
    )[0]
    for marker in [
        "update_channel_post_message(post)",
        "send_reserved_group_message(session, reservation, post, client)",
        "send_photo_or_text(",
    ]:
        assert_marker_order(
            reservation_success_tail,
            "safe_answer_callback_query(",
            marker,
            "reservation callback must answer after commit before Telegram follow-up work",
        )
    reservation_exception_block = reservation_block.split("except Exception as exc:", 1)[1].split(
        "logger.exception(\"Reservation failed",
        1,
    )[0]
    assert_marker_order(
        reservation_exception_block,
        "if reservation_committed:",
        "session.rollback()",
        "reservation callback must not rollback after a committed reservation",
    )

    role_change_success_block = text.split("def handle_role_change(call):", 1)[1].split(
        "# Поиск пользователя по имени и последним 4 цифрам номера",
        1,
    )[0]
    assert_marker_order(
        role_change_success_block,
        "safe_answer_callback_query(call.id, f\"Роль изменена на {new_role}.\")",
        "safe_edit_message_text(",
        "role change callback must answer before message refresh",
    )

    defective_client_choice_block = text.split("def handle_defective_client_choice(call):", 1)[1].split(
        "@bot.message_handler(func=lambda message: get_user_state(message.chat.id) == \"awaiting_last_digits_defective\")",
        1,
    )[0]
    assert_marker_order(
        defective_client_choice_block,
        "safe_answer_callback_query(call.id)",
        "ask_defective_confirmation(call.message.chat.id, client)",
        "defective client choice callback must answer before sending confirmation",
    )

    defective_action_block = text.split("def handle_defective_action(call):", 1)[1].split(
        "# Ввод суммы брака",
        1,
    )[0]
    assert_marker_order(
        defective_action_block,
        "safe_answer_callback_query(call.id)",
        "safe_send_message(bot, call.message.chat.id, \"Введите сумму брака:\"",
        "defective confirm callback must answer before asking for sum",
    )
    stale_defective_block = text.split(
        '@bot.callback_query_handler(func=lambda call: call.data in ["confirm_defective", "cancel_defective"])',
        1,
    )[1].split(
        "# Ввод суммы брака",
        1,
    )[0]
    for marker in [
        'call.data in ["confirm_defective", "cancel_defective"]',
        "Сессия оформления брака потерялась",
        "show_alert=True",
    ]:
        if marker not in stale_defective_block:
            raise AssertionError(f"stale defective callback fallback marker missing: {marker}")

    defective_page_block = text.split("def paginate_defective_reservations(call):", 1)[1].split(
        "# Обработка выбора заказа для дефектного товара",
        1,
    )[0]
    assert_marker_order(
        defective_page_block,
        "safe_answer_callback_query(call.id)",
        "send_defective_reservation_page(",
        "defective reservation page callback must answer before rendering page",
    )
    stale_flow_start = text.find(
        '@bot.callback_query_handler(\n    func=lambda call: callback_data_startswith(call, "defective_client_")'
    )
    stale_flow_end = text.find("def send_defective_reservation_page(", stale_flow_start)
    if stale_flow_start == -1 or stale_flow_end == -1:
        raise AssertionError("stale defective flow callback fallback block missing")
    stale_defective_flow_block = text[stale_flow_start:stale_flow_end]
    for marker in [
        "callback_data_startswith(call, \"defective_client_\")",
        "callback_data_startswith(call, \"defective_res_page_\")",
        "callback_data_startswith(call, \"select_\")",
        "not callback_data_startswith(call, \"select_defective_\")",
        "Сессия оформления брака потерялась",
        "show_alert=True",
    ]:
        if marker not in stale_defective_flow_block:
            raise AssertionError(f"stale defective flow callback fallback marker missing: {marker}")

    archive_delivery_block = text.split("def handle_archive_delivery_clear_confirmation(call):", 1)[1].split(
        "def ensure_deleted_post_snapshot(",
        1,
    )[0]
    assert_marker_order(
        archive_delivery_block,
        "safe_answer_callback_query(call.id)",
        "cleanup_in_delivery_records(",
        "archive delivery callback must answer before cleanup",
    )
    assert_marker_order(
        archive_delivery_block,
        "safe_answer_callback_query(call.id)",
        "safe_edit_message_text(",
        "archive delivery callback must answer before editing message",
    )

    cart_callback_checks = [
        (
            "def handle_clear_cart(call):",
            "@bot.callback_query_handler(func=lambda call: callback_data_startswith(call, \"admin_remove_cart_item_\"))",
            "clear_client_cart(client.user_id, processed_only=False)",
            "legacy clear cart callback must answer before cart cleanup",
        ),
        (
            "def handle_admin_remove_cart_item(call):",
            "# Callback для кнопки \"Расформировать обработанные\"",
            "clear_client_cart(call.from_user.id, reservation_id=reservation_id)",
            "admin remove cart item callback must answer before cart cleanup",
        ),
        (
            "def handle_clear_processed(call):",
            "@bot.callback_query_handler(func=lambda call: callback_data_startswith(call, \"clear_full_cart_\"))",
            "clear_processed(user_id)",
            "clear processed callback must answer before cart cleanup",
        ),
        (
            "def handle_clear_full_cart(call):",
            "@bot.callback_query_handler(func=lambda call: callback_data_startswith(call, \"clear_no_channel_\"))",
            "clear_client_cart(user_id, processed_only=False)",
            "clear full cart callback must answer before cart cleanup",
        ),
        (
            "def handle_clear_without_channel_return(call):",
            "# Удаляет обработанные товары из корзины пользователя",
            "clear_client_cart_without_channel_return(user_id)",
            "clear no-channel callback must answer before cart cleanup",
        ),
    ]
    for start, end, cleanup_marker, label in cart_callback_checks:
        block = text.split(start, 1)[1].split(end, 1)[0]
        assert_marker_order(block, "safe_answer_callback_query(call.id", cleanup_marker, label)

    delivery_data_edit_block = text.split("def handle_data_editing(call):", 1)[1].split(
        "@bot.message_handler(func=lambda message: message_user_state_is(message, \"WAITING_FOR_NEW_ADDRESS\"))",
        1,
    )[0]
    assert_marker_order(
        delivery_data_edit_block,
        "safe_answer_callback_query(call.id)",
        "safe_edit_message_text(",
        "delivery data edit callback must answer before editing message",
    )
    if 'func=lambda call: call.data == "new_phone"' not in text:
        raise AssertionError("new phone callback must have stale-state fallback")
    stale_new_phone_block = text.split("def handle_new_phone_choice_without_delivery_state(call):", 1)[1].split(
        "@bot.callback_query_handler(\n    func=lambda call: call.data == \"edit_address\"",
        1,
    )[0]
    for marker in [
        "safe_answer_callback_query(",
        "Данные доставки потерялись",
        "show_alert=True",
    ]:
        if marker not in stale_new_phone_block:
            raise AssertionError(f"new phone stale-state fallback marker missing {marker}")
    stale_edit_address_block = text.split("def handle_edit_choice(call):", 1)[1].split(
        "# Для ревизии",
        1,
    )[0]
    for marker in [
        "safe_answer_callback_query(",
        "Данные доставки потерялись",
        "show_alert=True",
    ]:
        if marker not in stale_edit_address_block:
            raise AssertionError(f"edit address stale-state fallback marker missing {marker}")

    for marker in [
        "def message_from_user_id(message):",
        "def message_user_state_is(message, expected_state):",
        "def callback_data_startswith(call, prefixes):",
        "def callback_message_chat_id(call):",
        "def callback_message_user_state_is(call, expected_state):",
        "def require_message_user_id(message, context):",
        "return user_id is not None and get_user_state(user_id) == expected_state",
        "return str(getattr(call, \"data\", \"\") or \"\").startswith(prefixes)",
        "return chat_id is not None and get_user_state(chat_id) == expected_state",
        "user_id = message_from_user_id(message)",
        "return user_id",
        "Delivery address edit skipped: missing message.from_user",
        "Delivery phone edit skipped: missing message.from_user",
    ]:
        if marker not in text:
            raise AssertionError(f"safe delivery edit user marker missing {marker}")

    if "message.from_user.id" in text:
        raise AssertionError("message handlers must use safe message_from_user_id helpers")

    tree = ast.parse(text)
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"message_handler", "callback_query_handler"}
        ):
            continue
        for keyword in node.keywords:
            if keyword.arg != "func" or not isinstance(keyword.value, ast.Lambda):
                continue
            lambda_text = ast.get_source_segment(text, keyword.value) or ast.unparse(keyword.value)
            if node.func.attr == "message_handler" and "message.from_user.id" in lambda_text:
                raise AssertionError("message_handler lambda must not access message.from_user.id directly")
            if node.func.attr == "callback_query_handler":
                for unsafe_marker in (
                    "call.data.startswith",
                    "call.message.chat.id",
                ):
                    if unsafe_marker in lambda_text:
                        raise AssertionError(
                            f"callback_query_handler lambda must not access {unsafe_marker} directly"
                        )
    if "EDITING_ADDRESS" in text:
        raise AssertionError("edit address stale fallback must not set unreachable EDITING_ADDRESS state")

    edit_post_block = text.split("def edit_post(call):", 1)[1].split(
        "# Обработчик кнопки \"Редактировать цену\"",
        1,
    )[0]
    assert_marker_order(
        edit_post_block,
        "safe_answer_callback_query(call.id)",
        "safe_edit_message_text(",
        "edit post callback must answer before editing message",
    )

    delete_post_block = text.split("def delete_post_handler(call):", 1)[1].split(
        "# Кнопка назад",
        1,
    )[0]
    assert_marker_order(
        delete_post_block,
        "safe_answer_callback_query(call.id, \"Удаляю пост.\")",
        "safe_delete_message(bot, CHANNEL_ID, post.message_id, logger=logger)",
        "delete post callback must answer before channel delete",
    )

    select_defective_block = text.split("def select_defective_order(call):", 1)[1].split(
        "@bot.callback_query_handler(func=lambda call: call.data == \"enter_defect_reason\")",
        1,
    )[0]
    assert_marker_order(
        select_defective_block,
        "safe_answer_callback_query(call.id)",
        "safe_edit_message_text(",
        "select defective callback must answer before editing message",
    )

    defect_reason_block = text.split("def request_defect_reason(call):", 1)[1].split(
        "@bot.message_handler(",
        1,
    )[0]
    assert_marker_order(
        defect_reason_block,
        "safe_answer_callback_query(call.id)",
        "safe_send_message(",
        "defect reason callback must answer before prompting for text",
    )


def test_all_callback_handlers_answer_before_slow_work():
    text = MAIN.read_text(encoding="utf-8")
    tree = ast.parse(text)
    risk_names = {
        "bot.delete_message",
        "bot.edit_message_caption",
        "bot.edit_message_text",
        "bot.send_message",
        "bot.send_photo",
        "edit_message_media_with_retries",
        "safe_send_animation",
        "send_photo_or_text",
        "safe_delete_message",
        "safe_edit_message_caption",
        "safe_edit_message_text",
        "safe_send_message",
        "show_delivery_collection_list",
        "show_missing_delivery_item",
        "show_missing_delivery_list",
        "finish_delivery_collection",
        "return_missing_item_to_cart",
        "return_missing_item_to_channel",
        "add_collection_item_amount_to_sheet",
        "recover_missing_delivery_collection_items",
        "notify_client_after_delivery_collection",
        "add_delivery_collector_name_to_sheet",
        "send_user_orders_page",
        "send_delivery_orders_page",
    }
    answer_names = {
        "safe_answer_callback_query",
        "answer_manual_audit_disabled",
    }
    issues = []

    def call_name(node):
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            base_name = call_name(node.value)
            return f"{base_name}.{node.attr}" if base_name else node.attr
        return ""

    def iter_function_body(node):
        for statement in node.body:
            yield statement
            yield from ast.walk(statement)

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        is_callback_handler = any(
            isinstance(decorator, ast.Call)
            and getattr(decorator.func, "attr", "") == "callback_query_handler"
            for decorator in node.decorator_list
        )
        if not is_callback_handler:
            continue

        calls = []
        for child in iter_function_body(node):
            if not isinstance(child, ast.Call):
                continue
            name = call_name(child.func)
            if name in answer_names or name in risk_names:
                source = ast.get_source_segment(text, child) or ""
                calls.append((child.lineno, name, source.splitlines()[0].strip()))

        answer_lines = [line for line, name, _source in calls if name in answer_names]
        if not answer_lines:
            issues.append(f"{node.name}:{node.lineno}: no callback answer")
            continue

        first_answer_line = min(answer_lines)
        for line, name, source in calls:
            if line < first_answer_line and name not in answer_names:
                issues.append(f"{node.name}:{node.lineno}: {name} before answer at line {line}: {source}")

    if issues:
        raise AssertionError("callback handlers must answer before slow work: " + "; ".join(issues))


def test_callback_handlers_do_not_parse_call_data_directly():
    text = MAIN.read_text(encoding="utf-8")
    tree = ast.parse(text)
    risky_calls = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        is_callback_handler = any(
            isinstance(decorator, ast.Call)
            and getattr(decorator.func, "attr", "") == "callback_query_handler"
            for decorator in node.decorator_list
        )
        if not is_callback_handler:
            continue

        for child in ast.walk(node):
            if not isinstance(child, ast.Call):
                continue
            source = ast.get_source_segment(text, child) or ""
            is_direct_int_parse = getattr(child.func, "id", "") == "int" and "call.data" in source
            is_direct_string_parse = (
                isinstance(child.func, ast.Attribute)
                and child.func.attr in {"split", "rsplit", "replace"}
                and "call.data" in source
            )
            if is_direct_int_parse or is_direct_string_parse:
                risky_calls.append(f"{node.name}:{child.lineno}:{source}")

    if risky_calls:
        raise AssertionError("callback handlers must use safe callback parsers: " + "; ".join(risky_calls))


def test_exception_handlers_use_safe_telegram_send():
    text = MAIN.read_text(encoding="utf-8")
    tree = ast.parse(text)
    unsafe_calls = []

    def call_name(node):
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            base_name = call_name(node.value)
            return f"{base_name}.{node.attr}" if base_name else node.attr
        return ""

    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue

        for child in ast.walk(node):
            if not isinstance(child, ast.Call):
                continue
            if call_name(child.func) == "bot.send_message":
                source = ast.get_source_segment(text, child) or ""
                unsafe_calls.append(f"{child.lineno}:{source.splitlines()[0].strip()}")

    if unsafe_calls:
        raise AssertionError("exception handlers must use safe_send_message: " + "; ".join(unsafe_calls))


def test_delivery_confirmation_callback_answers_before_early_returns():
    text = MAIN.read_text(encoding="utf-8")
    block = text.split("def handle_confirmation(call):", 1)[1].split(
        "# Клавиатура для доставки да или нет",
        1,
    )[0]
    assert_marker_order(
        block,
        "safe_answer_callback_query(call.id)",
        "get_temp_user_data_or_reset(",
        "delivery confirmation callback must answer before data/database work",
    )
    for marker in [
        "Ошибка! Клиент не найден в базе данных. Попробуйте снова.",
        "Произошла ошибка при обработке данных. Попробуйте снова.",
        "Произошла ошибка при сохранении данных. Попробуйте снова.",
        "edited_message = safe_edit_message_text(",
        "if edited_message is None:",
        "edit_prompt = safe_edit_message_text(",
        "edit_prompt = safe_send_message(",
        "Delivery edit choice prompt was not sent for user_id=%s",
    ]:
        marker_index = block.find(marker)
        if marker_index == -1:
            raise AssertionError(f"delivery confirmation error marker missing: {marker}")
        if "ошибка" in marker.lower() or "Ошибка" in marker:
            return_index = block.find("return", marker_index)
            if return_index == -1:
                raise AssertionError(f"delivery confirmation return missing after: {marker}")
            if block.rfind("safe_answer_callback_query(call.id)", 0, return_index) == -1:
                raise AssertionError(f"delivery confirmation must answer callback before returning after: {marker}")
    confirm_no_block = block.split('elif confirmation == "confirm_no":', 1)[1]
    assert_marker_order(
        confirm_no_block,
        "edit_prompt = safe_edit_message_text(",
        "set_user_state(user_id, \"WAITING_FOR_DATA_EDIT\")",
        "delivery edit state must be set after edit prompt is available",
    )
    stale_block = text.split(
        '@bot.callback_query_handler(func=lambda call: call.data in ["confirm_yes", "confirm_no"])',
        1,
    )[1].split(
        "# Клавиатура для доставки да или нет",
        1,
    )[0]
    for marker in [
        "def handle_stale_delivery_confirmation(call):",
        "Данные доставки потерялись",
        "show_alert=True",
    ]:
        if marker not in stale_block:
            raise AssertionError(f"stale delivery confirmation fallback marker missing: {marker}")


def test_role_search_input_parsing_is_safe():
    text = MAIN.read_text(encoding="utf-8")
    block = text.split("def process_user_input(message):", 1)[1].split(
        "# Обработчик изменения роли",
        1,
    )[0]
    for marker in [
        'parts = str(message.text or "").split()',
        "name = \" \".join(parts[:-1]).strip()",
        "last_digits = parts[-1].strip()",
        "len(parts) < 2",
    ]:
        if marker not in block:
            raise AssertionError(f"role search safe parsing marker missing {marker}")
    if "name, last_digits = message.text.split()" in block:
        raise AssertionError("role search must allow multi-word names and empty text safely")


def test_delivery_phone_next_step_requires_text():
    text = MAIN.read_text(encoding="utf-8")
    block = text.split("def process_numbers(message):", 1)[1].split(
        "def archive_delivery_to_excel(",
        1,
    )[0]
    for marker in [
        "if not message.text:",
        "Отправьте номера телефонов текстом, каждый с новой строки.",
        "return",
        "numbers = message.text.splitlines()",
    ]:
        if marker not in block:
            raise AssertionError(f"delivery phone next-step text guard missing {marker}")
    assert_marker_order(
        block,
        "if not message.text:",
        "numbers = message.text.splitlines()",
        "delivery phone next-step must validate text before splitlines",
    )


def test_state_text_handlers_reject_non_text_messages():
    main_text = MAIN.read_text(encoding="utf-8")
    if "def require_text_message(" not in main_text:
        raise AssertionError("state text message guard helper missing")

    handler_signatures = [
        "def handle_name_registration(message):",
        "def handle_phone_registration(message):",
        "def confirm_phone_registration(message):",
        "def search_user_for_defective(message):",
        "def handle_defective_sum_entry(message):",
        "def handle_phone_input(message):",
        "def process_delete_client_phone(message):",
        "def handle_post_details(message):",
        "def edit_post_price(message):",
        "def edit_post_description(message):",
        "def edit_post_quantity(message):",
        "def handle_address_input(message):",
        "def handle_new_address(message):",
        "def handle_new_phone(message):",
        "def handle_defect_reason(message):",
        "def handle_discount_amount(message):",
        "def handle_delivery_cart_merge_main_input(message):",
        "def handle_delivery_cart_merge_extra_input(message):",
    ]
    for signature in handler_signatures:
        block = main_text.split(signature, 1)[1].split("\n@bot.", 1)[0]
        if "require_text_message(" not in block:
            raise AssertionError(f"state text handler must reject non-text messages: {signature}")

    forbidden_patterns = [
        "message.text.strip()",
        "int(message.text)",
        "message.text.isdigit()",
        "message.text.replace(",
        "normalize_phone(message.text)",
        "new_address = message.text",
        "address = message.text",
        "reason = message.text",
    ]
    for signature in handler_signatures:
        block = main_text.split(signature, 1)[1].split("\n@bot.", 1)[0]
        for pattern in forbidden_patterns:
            if pattern in block:
                raise AssertionError(f"state text handler still uses unsafe {pattern}: {signature}")


def test_phoenix_broadcast_markers():
    main_text = MAIN.read_text(encoding="utf-8")
    keyboard_text = (ROOT / "bot" / "keyboard.py").read_text(encoding="utf-8")
    qr_path = ROOT / "images" / "phoenix_qr.jpg"
    channel_footer_path = ROOT / "images" / "phoenix_channel_footer.jpg"

    if not qr_path.exists():
        raise AssertionError("Phoenix QR image is missing")
    if not channel_footer_path.exists():
        raise AssertionError("Phoenix channel footer image is missing")

    for marker in [
        'PHOENIX_BROADCAST_BUTTON = "Рассылка о Фениксе"',
        'PHOENIX_BROADCAST_BATCH_SIZE = env_int("PHOENIX_BROADCAST_BATCH_SIZE", 50, minimum=1)',
        "PHOENIX_CHANNEL_FOOTER_IMAGE_PATH",
        "PHOENIX_CHANNEL_FOOTER_TEXT",
        "Возвращение Феникса, большое обновление ВК",
        "https://vk.ru/club239413025",
        "def require_creator",
        "def get_phoenix_broadcast_recipients",
        "def run_phoenix_broadcast",
        "time.sleep(PHOENIX_BROADCAST_DELAY_SECONDS)",
        "PHOENIX_BROADCAST_BATCH_PAUSE_SECONDS",
        "safe_send_photo(",
        "Phoenix broadcast photo send failed for user_id=%s, falling back to text",
        "safe_send_message(bot, user_id, PHOENIX_BROADCAST_TEXT, logger=logger)",
        "Phoenix broadcast text send failed",
        "Рассылка продолжится текстом без QR-кода.",
        "https://garphoenix.com/join/INV-R9JQ-F8UW?tenant=default",
    ]:
        if marker not in main_text:
            raise AssertionError(f"Phoenix broadcast marker missing {marker}")

    if "Рассылка о Фениксе" not in keyboard_text:
        raise AssertionError("Phoenix broadcast button missing from creator menu")
    phoenix_block = main_text.split("def run_phoenix_broadcast(", 1)[1].split(
        "@bot.message_handler(func=lambda message: message.text == PHOENIX_BROADCAST_BUTTON)",
        1,
    )[0]
    if "QR-код не найден. Рассылка остановлена." in phoenix_block:
        raise AssertionError("Phoenix broadcast must continue with text fallback when QR is missing")


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
        "add_reservation_stat_event(\n            session,\n            RESERVATION_STATS_EVENT_CANCELED,\n            reservation",
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
        "summary_chat_id",
        "summary_message_id",
        "last_scan_at",
    ]:
        if marker not in campaign_text:
            raise AssertionError(f"delivery broadcast campaign marker missing {marker}")

    for marker in [
        "class DeliveryBroadcastRecipient",
        "__tablename__ = \"delivery_broadcast_recipients\"",
        "uq_delivery_broadcast_recipients_campaign_phone",
        "telegram_message_id",
        "response_status",
        "responded_at",
        "status",
    ]:
        if marker not in recipient_text:
            raise AssertionError(f"delivery broadcast recipient marker missing {marker}")

    for marker in [
        '"006_delivery_broadcast_campaigns"',
        '"009_delivery_broadcast_summary_tracking"',
        "ensure_delivery_broadcast_campaigns",
        "ensure_delivery_broadcast_summary_tracking",
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
        "DELIVERY_RECIPIENT_RESPONSE_ACCEPTED",
        "DELIVERY_RECIPIENT_RESPONSE_DECLINED",
        "def refresh_delivery_candidates_summary",
        "def mark_delivery_broadcast_response",
        "send_delivery_offer_message",
        "Telegram delivery offer send_message",
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
        "is_transient_telegram_error(exc)",
        "def send_archive_post_copy_best_effort(post_id, photo, group_caption):",
        "Archive copy failed for post_id=%s",
        "Archive copy returned no message for post_id=%s",
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
        "PHOENIX_CHANNEL_FOOTER_IMAGE_PATH",
        "PHOENIX_CHANNEL_FOOTER_TEXT",
        "send_photo_or_text(",
        'if source == "auto" and sent_count and not failed_post_ids:',
        "send_phoenix_channel_footer()",
        "Phoenix footer sent to channel after post publish",
        "group_caption = f\"Id товара: {post_id}\\nПост был создан пользователем: {creator_name}",
    ]:
        if marker not in text:
            raise AssertionError(f"channel post auto publish marker missing {marker}")

    publish_block = text.split("def publish_unsent_posts_to_channel(", 1)[1].split(
        "def send_phoenix_channel_footer(",
        1,
    )[0]
    if "send_archive_post_copy_best_effort(post_id, photo, group_caption)" not in publish_block:
        raise AssertionError("channel publish must use best-effort archive copy")
    if "archive_message = send_photo_or_text(" in publish_block:
        raise AssertionError("archive copy must not interrupt channel post publish loop")
    for marker in [
        "post_has_active_links(session, post_id)",
        "Channel post publish skipped unavailable post_id=",
        "int(live.quantity or 0) <= 0",
    ]:
        if marker not in publish_block:
            raise AssertionError(f"channel publish availability marker missing {marker}")


def test_post_management_only_shows_unpublished_posts():
    posts_text = (ROOT / "db" / "posts.py").read_text(encoding="utf-8")
    main_text = MAIN.read_text(encoding="utf-8")
    for marker in [
        "def get_unsent_posts():",
        "Posts.is_sent == False",
        "Posts.message_id == None",
        "Posts.quantity > 0",
        "return Posts.get_unsent_posts()",
    ]:
        if marker not in posts_text:
            raise AssertionError(f"unpublished post query marker missing {marker}")
    if "post.message_id = None" not in main_text:
        raise AssertionError("audit repost flow must clear old channel message_id")

    db_init_text = (ROOT / "db" / "__init__.py").read_text(encoding="utf-8")
    for marker in [
        '"012_reconcile_post_channel_state"',
        "def reconcile_post_channel_state():",
        "WHERE message_id IS NOT NULL AND is_sent = FALSE",
        "WHERE message_id IS NULL AND is_sent = TRUE",
    ]:
        if marker not in db_init_text:
            raise AssertionError(f"post channel state reconcile marker missing {marker}")


def test_order_pagination_recovers_missing_messages():
    main_text = MAIN.read_text(encoding="utf-8")
    for marker in [
        "def is_message_to_edit_missing_error(exc):",
        '"message to edit not found"',
        "ORDER_CART_IMAGE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), \"images\", \"my_cart.jpg\")",
        "DELIVERY_ORDER_GIF_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), \"images\", \"delivery_order.gif\")",
        "def edit_or_send_text_page(",
        "lambda: bot.edit_message_text(",
        "Orders page message missing for user=%s page=%s; sending a new one",
        "Orders page image unavailable path=%s: %s",
        "return edit_or_send_text_page(user_id, message_id, text, keyboard, \"orders\")",
        "Delivery orders page message missing for user=%s page=%s; sending a new one",
        "Delivery orders animation unavailable path=%s: %s",
        "return edit_or_send_text_page(user_id, message_id, text, keyboard, \"delivery orders\")",
        "Delivery order pagination failed for user_id=%s page=%s: %s",
    ]:
        if marker not in main_text:
            raise AssertionError(f"order pagination missing-message recovery marker missing {marker}")

    orders_page_block = main_text.split("def send_order_page(", 1)[1].split(
        "# Обработчик навигации между страницами",
        1,
    )[0]
    for marker in [
        "except OSError as exc:",
        "Orders page media send failed for user=%s page=%s: %s",
        "Orders page replacement media send failed for user=%s page=%s: %s",
        "return edit_or_send_text_page(user_id, message_id, text, keyboard, \"orders\")",
    ]:
        if marker not in orders_page_block:
            raise AssertionError(f"orders page text fallback marker missing {marker}")

    delivery_orders_page_block = main_text.split("def send_delivery_order_page(", 1)[1].split(
        "# Хэндлер для команды \"👔 Назначить работника\"",
        1,
    )[0]
    for marker in [
        "except OSError as exc:",
        "Delivery orders page animation send returned no message for user=%s page=%s",
        "Delivery orders animation unavailable path=%s: %s",
        "return edit_or_send_text_page(user_id, message_id, text, keyboard, \"delivery orders\")",
    ]:
        if marker not in delivery_orders_page_block:
            raise AssertionError(f"delivery orders page text fallback marker missing {marker}")

    order_pagination_block = main_text.split("def paginate_orders(call):", 1)[1].split(
        "# Обработка отмены заказа",
        1,
    )[0]
    for marker in [
        "new_message = send_order_page(",
        "if new_message:",
        "user_last_message_id[user_id] = new_message.message_id",
    ]:
        if marker not in order_pagination_block:
            raise AssertionError(f"order pagination message-id guard marker missing {marker}")

    delivery_pagination_block = main_text.split("def paginate_delivery_orders(call):", 1)[1].split(
        "# Перессылка забронированного товара в группу Брони Мега Скидки",
        1,
    )[0]
    for marker in [
        "new_message = send_delivery_order_page(",
        "if new_message:",
        "user_last_message_id[user_id] = new_message.message_id",
    ]:
        if marker not in delivery_pagination_block:
            raise AssertionError(f"delivery order pagination message-id guard marker missing {marker}")


def test_client_update_for_work_handles_errors_safely():
    clients_text = (ROOT / "db" / "clients.py").read_text(encoding="utf-8")
    db_init_text = (ROOT / "db" / "__init__.py").read_text(encoding="utf-8")

    insert_block = clients_text.split("def insert(", 1)[1].split("@staticmethod", 1)[0]
    for marker in [
        'Index("ix_clients_user_id", "user_id")',
        "def preserve_highest_role(",
        "def apply_registration_data(",
        "existing_client = session.query(Clients).filter(",
        "Clients.user_id == user_id",
        "Clients.apply_registration_data(existing_client, name, phone, role)",
        "except IntegrityError:",
    ]:
        if marker not in clients_text and marker not in insert_block:
            raise AssertionError(f"client user_id uniqueness marker missing {marker}")
    if 'Index("uq_clients_user_id", "user_id", unique=True)' in clients_text:
        raise AssertionError("clients model must not create unique index before duplicate cleanup migration")

    for marker in [
        '"013_clients_user_id_unique"',
        "def ensure_clients_user_id_unique():",
        "ROW_NUMBER() OVER",
        "PARTITION BY user_id",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_clients_user_id",
        "ON clients (user_id)",
        '"014_remove_orphan_wait_queue_entries"',
        "def remove_orphan_wait_queue_entries():",
        "DELETE FROM temp_reservations ",
        "WHERE NOT EXISTS (",
        "SELECT 1 FROM clients c WHERE c.user_id = temp_reservations.user_id",
        '"015_remove_orphan_cart_records"',
        "def remove_orphan_cart_records():",
        "DELETE FROM temp_fulfilled ",
        "SELECT 1 FROM clients c WHERE c.user_id = temp_fulfilled.user_id",
        "SELECT 1 FROM in_delivery i",
        "SELECT 1 FROM for_delivery fd WHERE fd.user_id = temp_fulfilled.user_id",
        "DELETE FROM reservations ",
        "SELECT 1 FROM clients c WHERE c.user_id = reservations.user_id",
        "SELECT 1 FROM posts p WHERE p.id = reservations.post_id",
        '"016_release_orphan_live_reservations"',
        "def release_orphan_live_reservations():",
        "WHERE r.is_fulfilled = FALSE",
        "SELECT 1 FROM temp_fulfilled tf WHERE tf.reservation_id = r.id",
        "SELECT 1 FROM delivery_collection_items d WHERE d.reservation_id = r.id",
        "SELECT 1 FROM temp_reservations tq",
        "INSERT INTO reservation_stat_events",
        "SELECT 'canceled', r.id, r.user_id, r.post_id, r.release_quantity, CURRENT_TIMESTAMP",
        "SET quantity = COALESCE(quantity, 0) + (",
        "WHERE id IN (SELECT post_id FROM release_totals)",
        "DELETE FROM reservations ",
        "WHERE id IN (SELECT id FROM releasable)",
        '"017_remove_orphan_wait_queue_posts"',
        "def remove_orphan_wait_queue_posts():",
        "DELETE FROM temp_reservations ",
        "SELECT 1 FROM posts p WHERE p.id = temp_reservations.post_id",
        '"018_remove_unplaced_orphan_delivery_collection_items"',
        "def remove_unplaced_orphan_delivery_collection_items():",
        "DELETE FROM delivery_collection_items ",
        "WHERE placed_at IS NULL",
        "SELECT 1 FROM for_delivery fd WHERE fd.id = delivery_collection_items.delivery_id",
        '"019_remove_unlinked_missing_product_temp_fulfilled"',
        "def remove_unlinked_missing_product_temp_fulfilled():",
        "DELETE FROM temp_fulfilled ",
        "WHERE in_delivery = FALSE",
        "AND defect = FALSE",
        "AND skidka = FALSE",
        "SELECT 1 FROM deleted_post_snapshots s WHERE s.post_id = temp_fulfilled.post_id",
        "SELECT 1 FROM reservations r WHERE r.id = temp_fulfilled.reservation_id",
        "SELECT 1 FROM delivery_collection_items d WHERE d.reservation_id = temp_fulfilled.reservation_id",
        '"020_backfill_missing_product_snapshots"',
        "def backfill_missing_product_snapshots():",
        "WITH ranked_temp AS",
        "INSERT INTO deleted_post_snapshots",
        "'migration:temp_fulfilled_missing_product'",
        "WITH ranked_reservations AS",
        "'migration:reservation_missing_product'",
        "ROW_NUMBER() OVER (",
        "SELECT 1 FROM posts p WHERE p.id = tf.post_id",
        "SELECT 1 FROM deleted_post_snapshots s WHERE s.post_id = tf.post_id",
        "CASE WHEN COALESCE(r.old_price, 0) > 0 THEN COALESCE(r.old_price, 0) ELSE 0 END AS price",
    ]:
        if marker not in db_init_text:
            raise AssertionError(f"client user_id uniqueness migration marker missing {marker}")

    update_block = clients_text.split("def update_row_for_work(", 1)[1]
    for marker in [
        "updates = dict(updates or {})",
        "if not updates:",
        "updates[\"phone\"] = Clients.normalize_phone(updates[\"phone\"])",
        "updated_count = session.query(Clients).filter(Clients.user_id == user_id).update(updates)",
        "return updated_count > 0",
        "session.rollback()",
    ]:
        if marker not in update_block:
            raise AssertionError(f"client update safety marker missing {marker}")

    old_broken_marker = (
        "except Exception as e:\n"
        "            print(f\"Error in update_row_for_work: {e}\")\n"
        "            session.rollback()"
    )
    if old_broken_marker in update_block:
        raise AssertionError("client update rollback must not run after a failed session setup")


def test_init_db_runs_on_clean_sqlite_database():
    with tempfile.TemporaryDirectory() as tmp_dir:
        sqlite_path = Path(tmp_dir) / "clean.sqlite"
        env = os.environ.copy()
        env["DATABASE_URL"] = f"sqlite:///{sqlite_path}"
        env["PATH_TO_CONFIG"] = str(Path(tmp_dir) / "missing-config.json")
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from db import init_db; init_db(); print('init_db ok')",
            ],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=20,
        )
        if result.returncode != 0:
            raise AssertionError(
                "init_db must run on a clean SQLite database; "
                f"stdout={result.stdout!r} stderr={result.stderr!r}"
            )
        if "init_db ok" not in result.stdout:
            raise AssertionError(f"init_db SQLite smoke did not complete: {result.stdout!r}")


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
    for marker in [
        "reservation_notice = (",
        "send_photo_or_text(",
        "bot was blocked by the user",
        "Reservation DM skipped",
        "safe_send_message(",
    ]:
        if marker not in block:
            raise AssertionError(f"reservation DM best-effort marker missing {marker}")


def test_post_delete_and_zero_quantity_are_safe():
    text = MAIN.read_text(encoding="utf-8")
    for marker in [
        "def disable_channel_post_reservation",
        "def post_has_active_links(session, post_id):",
        "has_related_rows = post_has_active_links(session, post_id)",
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
    pricing_text = (ROOT / "services" / "pricing.py").read_text(encoding="utf-8")
    db_init_text = (ROOT / "db" / "__init__.py").read_text(encoding="utf-8")
    for marker in [
        "def get_revision_blocked_post_ids(session):",
        "session.query(Reservations.post_id)",
        "session.query(InDelivery.post_id)",
        "session.query(Temp_Fulfilled.post_id)",
        "session.query(TempReservations.post_id).filter(",
        "session.query(DeliveryCollectionItem.post_id).join(",
        "MissingDeliveryItem.status == MISSING_DELIVERY_STATUS_MISSING",
        "Posts.quantity > 0",
        "session.add(RevisionLog(",
        "Исключено из-за корзины/доставки/очереди",
        "new_price = calculate_audit_price(old_price)",
        "post.price = new_price",
        "disable_reason=\"Товар на ревизии\"",
        'source="revision"',
    ]:
        if marker not in text:
            raise AssertionError(f"revision safety marker missing {marker}")

    for marker in [
        "AUDIT_MIN_PRICE = 100",
        "return max(AUDIT_MIN_PRICE, int(rounded_price))",
    ]:
        if marker not in pricing_text:
            raise AssertionError(f"audit minimum price marker missing {marker}")

    for marker in [
        "def sync_existing_posts_to_audit_min_price",
        "start_audit_min_price_sync_worker()",
    ]:
        if marker in text:
            raise AssertionError(f"audit minimum price must be applied only during selected-date revision, found {marker}")

    revision_block = text.split("def apply_auto_audit_for_date", 1)[1].split("def answer_manual_audit_disabled", 1)[0]
    if "today_start" in revision_block:
        raise AssertionError("revision must not move sold-out posts to today 00:00")
    if "delete_delivered_channel_post_message(" not in revision_block:
        raise AssertionError("revision must disable or delete the previous channel message")

    for marker in [
        "RevisionLog",
        "DeletedPostSnapshot",
        "DeliveryCleanupRun",
        '"003_revision_delivery_cleanup_tables"',
    ]:
        if marker not in db_init_text:
            raise AssertionError(f"new additive migration marker missing {marker}")


def test_statistic_excludes_revision_posts_from_created_posts():
    text = MAIN.read_text(encoding="utf-8")
    statistic_block = text.split("@bot.message_handler(commands=['statistic'])", 1)[1].split(
        "def close_expired_delivery_broadcast_campaigns",
        1,
    )[0]
    for marker in [
        "revision_post_ids_by_period",
        "revision_post_ids_by_period[key].add(revision_log.post_id)",
        "if post.id in revision_post_ids_by_period[key]:",
        "Общее количество созданных товаров",
        "{count} товаров",
    ]:
        if marker not in statistic_block:
            raise AssertionError(f"statistic revision exclusion marker missing {marker}")

    post_skip_index = statistic_block.index("if post.id in revision_post_ids_by_period[key]:")
    post_count_index = statistic_block.index("total_posts[key] += 1")
    if post_skip_index > post_count_index:
        raise AssertionError("revision posts must be skipped before incrementing created-post totals")


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
        "with_collection_items",
        "with_missing_delivery",
        "skipped_active",
        "if post_has_active_links(session, post.id):",
    ]:
        if marker not in text:
            raise AssertionError(f"midnight cleanup marker missing {marker}")
    cleanup_block = text.split("def cleanup_midnight_posts_after_snapshot", 1)[1].split(
        "def keyboard_for_editing",
        1,
    )[0]
    assert_marker_order(
        cleanup_block,
        "if post_has_active_links(session, post.id):",
        "session.delete(post)",
        "midnight cleanup must skip active linked posts before deleting",
    )


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
        "def build_cart_item_caption(post, reservation):",
        "post_id=reservation.post_id",
        "author=format_post_author(post)",
        "Выложил: {author}",
        "Прикарманить",
        "clear_no_channel_",
        "def clear_client_cart_without_channel_return(user_id):",
        "def remove_reservation_without_channel_return(session, reservation, stats):",
        "not_returned_to_channel",
        "wait_queue_deleted",
        "orphan_queue_deleted",
        "def promote_next_queue_entry(",
        "Wait queue entry removed because client is missing",
        'client_id = parse_callback_int_suffix(call, "clear_cart_", "Некорректный клиент.")',
        "def return_post_stock_to_today(post, quantity=1):",
        "post.created_at = Posts.next_created_at()",
        "return_post_stock_to_today(post)",
    ]:
        if marker not in text:
            raise AssertionError(f"cart clear processed marker missing {marker}")
    if "Обработанные товары удаляются только через" in text:
        raise AssertionError("cart clear processed warning must not be shown")
    no_channel_block = text.split("def clear_client_cart_without_channel_return(user_id):", 1)[1].split(
        "def delete_client_active_records(client_user_id, phone):",
        1,
    )[0]
    forbidden = [
        "update_channel_post_message",
        "send_queue_transfer_notifications",
        "create_reservation_from_queue",
        "post.quantity +=",
        "return_post_stock_to_today",
        "Posts.next_created_at()",
    ]
    for marker in forbidden:
        if marker in no_channel_block:
            raise AssertionError(f"cart no-channel delete must not use {marker}")

    legacy_clear_block = text.split("def handle_clear_cart(call):", 1)[1].split(
        "# Проверка на регистрацию",
        1,
    )[0]
    for marker in [
        "stats = clear_client_cart(client.user_id, processed_only=False)",
        "format_cart_cleanup_result(stats)",
    ]:
        if marker not in legacy_clear_block:
            raise AssertionError(f"legacy cart clear safety marker missing {marker}")
    if "Reservations.delete_rows_by_user_id" in legacy_clear_block:
        raise AssertionError("legacy cart clear must not delete reservations directly")


def test_client_delete_uses_safe_cart_cleanup():
    text = MAIN.read_text(encoding="utf-8")
    for marker in [
        "def delete_client_active_records(client_user_id, phone):",
        "TempReservations.user_id == client_user_id",
        "remove_reservation_from_cart(session, reservation, stats, queued_notifications)",
        "cleanup_or_refresh_for_delivery_by_phone(session, phone)",
        "send_queue_transfer_notifications(queued_notifications)",
        "cleanup_stats = delete_client_active_records(client_user_id, phone)",
        "format_cart_cleanup_result(cleanup_stats)",
    ]:
        if marker not in text:
            raise AssertionError(f"client delete safe cleanup marker missing {marker}")

    queue_transfer_block = text.split("def create_reservation_from_queue(", 1)[1].split(
        "def send_queue_transfer_notifications(",
        1,
    )[0]
    for marker in [
        "queued_client = session.query(Clients).filter(",
        "if not queued_client:",
        "session.delete(queued_entry)",
        "return None",
        "def promote_next_queue_entry(",
        "queued_notification = create_reservation_from_queue(session, next_in_queue, post, stats=stats)",
    ]:
        if marker not in queue_transfer_block:
            raise AssertionError(f"queue transfer orphan-client guard missing {marker}")

    delete_client_block = text.split("def process_delete_client_phone(message):", 1)[1].split(
        "# Возможность установить клиенту статус рабочего",
        1,
    )[0]
    forbidden = [
        "session.query(Reservations)",
        "deleted_reservations_count",
        ".delete()",
    ]
    for marker in forbidden:
        if marker in delete_client_block:
            raise AssertionError(f"client delete must use safe cleanup helper, not {marker}")


def test_removed_delivery_entries_clear_collection_retry_rows():
    text = MAIN.read_text(encoding="utf-8")
    for marker in [
        "delivery_collection_items_removed",
        "def delete_delivery_collection_items_for_delivery_entries(session, delivery_entries, stats=None):",
        "DeliveryCollectionItem.delivery_id.in_(delivery_ids)",
        "delete_delivery_collection_items_for_delivery_entries(session, entries_to_remove, stats)",
    ]:
        if marker not in text:
            raise AssertionError(f"delivery cleanup collection row marker missing {marker}")

    cleanup_block = text.split("def cleanup_or_refresh_for_delivery_by_phone(session, phone):", 1)[1].split(
        "def remove_unprocessed_reservation_from_cart",
        1,
    )[0]
    assert_marker_order(
        cleanup_block,
        "delete_delivery_collection_items_for_delivery_entries(session, entries_to_remove, stats)",
        "session.delete(delivery_entry)",
        "removed delivery entries must clear collection retry rows before deleting for_delivery rows",
    )


def test_channel_delete_happens_only_after_delivery_cleanup():
    text = MAIN.read_text(encoding="utf-8")
    required = [
        "def delete_delivered_channel_post_message",
        "bot.delete_message(chat_id=CHANNEL_ID",
        "Товар уехал в доставку",
        "Deleted delivered channel post",
        "cleanup_in_delivery_records",
        "delete_delivered_channel_post_message(post, source=source)",
        'disable_reason="Товар уехал в доставку"',
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


def test_delivery_cart_merge_works_without_for_delivery_rows():
    main_text = MAIN.read_text(encoding="utf-8")
    db_init_text = (ROOT / "db" / "__init__.py").read_text(encoding="utf-8")
    model_text = (ROOT / "db" / "delivery_cart_merges.py").read_text(encoding="utf-8")

    for marker in [
        "class DeliveryCartMerge(AbstractModel):",
        '__tablename__ = "delivery_cart_merges"',
        'UniqueConstraint("extra_phone", name="uq_delivery_cart_merges_extra_phone")',
        "main_phone = mapped_column(String, nullable=False)",
        "extra_phone = mapped_column(String, nullable=False)",
    ]:
        if marker not in model_text:
            raise AssertionError(f"delivery cart merge model marker missing {marker}")

    for marker in [
        "from .delivery_cart_merges import DeliveryCartMerge",
        '"027_delivery_cart_merges"',
        "def ensure_delivery_cart_merges():",
        "INSERT INTO delivery_cart_merges (main_phone, extra_phone, merge_order, created_at)",
        "child.merged_into_delivery_id IS NOT NULL",
    ]:
        if marker not in db_init_text:
            raise AssertionError(f"delivery cart merge migration marker missing {marker}")

    for marker in [
        "def find_client_by_phone_input(session, raw_phone):",
        "def merge_or_unmerge_delivery_carts(session, main_phone, extra_phone_text):",
        "def unmerge_delivery_carts(session, main_phone, extra_phone):",
        "def apply_persistent_cart_merges_for_phone(session, phone):",
        "apply_merges=True",
        "apply_persistent_cart_merges_for_phone(session, delivery_entry.phone)",
        "apply_persistent_cart_merges_for_phone(session, root_entry.phone)",
        "Корзины разъединены.",
        "Когда основная корзина попадёт в список сборки, дополнительная тоже будет добавлена.",
        "Если введёте уже объединённый номер, корзины разъединятся.",
        'required_fields=("main_phone",)',
        "Данные объединения потерялись. Нажмите «Объединить корзины» заново.",
        "upsert_for_delivery_entry(",
        "apply_merges=False",
        "def get_delivery_sheet_phone(",
        "get_delivery_sheet_phone(session, item=item)",
        "find_delivery_cart_merge_by_extra_phone(session, normalized)",
    ]:
        if marker not in main_text:
            raise AssertionError(f"delivery cart merge flow marker missing {marker}")

    extra_block = main_text.split("def handle_delivery_cart_merge_extra_input(", 1)[1].split(
        "@bot.message_handler",
        1,
    )[0]
    assert_marker_order(
        extra_block,
        "get_temp_user_data_or_reset(",
        "merge_or_unmerge_delivery_carts(",
        "delivery cart merge extra step must keep main phone before merge/unmerge",
    )
    if "find_delivery_entry_by_phone_input(session, text)" in main_text.split(
        "def handle_delivery_cart_merge_main_input(",
        1,
    )[1].split("def handle_delivery_cart_merge_extra_input(", 1)[0]:
        raise AssertionError("main cart merge must look up clients, not only for_delivery rows")


def test_delivery_collection_pauses_reserved_group_flow():
    text = MAIN.read_text(encoding="utf-8")
    for marker in [
        "RESERVED_GROUP_FLOW_STATE_KEY = 0",
        "def activate_reserved_group_delivery_pause():",
        "def is_reserved_group_delivery_paused():",
        "def send_delivery_reserved_group_snapshot():",
        "safe_send_message(bot, TARGET_GROUP_ID, \"Брони на доставку\", logger=logger)",
        "f\"Id товара: {item['post_id']}\"",
        "f\"Выложил: {item.get('author') or 'неизвестно'}\"",
        '"author": format_post_author(post)',
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


def test_audit_can_collect_delivery_only():
    main_text = MAIN.read_text(encoding="utf-8")
    keyboard_text = (ROOT / "bot" / "keyboard.py").read_text(encoding="utf-8")

    for marker in [
        'DELIVERY_ROLES = {"admin", "supreme_leader"}',
        'DELIVERY_COLLECTION_ROLES = DELIVERY_ROLES | {"audit"}',
        'if not require_role(message, DELIVERY_COLLECTION_ROLES):',
        'if not has_role(call.from_user.id, DELIVERY_COLLECTION_ROLES):',
    ]:
        if marker not in main_text:
            raise AssertionError(f"audit delivery collection marker missing {marker}")

    if 'collect_delivery = KeyboardButton("🧺 Собрать доставку")' not in keyboard_text:
        raise AssertionError("audit menu must expose collect delivery button")

    audit_menu = keyboard_text.split("def audit_main_menu():", 1)[1].split("def admin_main_menu():", 1)[0]
    if "collect_delivery" not in audit_menu:
        raise AssertionError("collect delivery button must be inside audit menu")


def test_delivery_collection_claim_markers():
    main_text = MAIN.read_text(encoding="utf-8")
    db_init_text = (ROOT / "db" / "__init__.py").read_text(encoding="utf-8")
    for_delivery_text = (ROOT / "db" / "for_delivery.py").read_text(encoding="utf-8")

    for marker in [
        "collector_user_id = mapped_column(BIGINT, nullable=True)",
        "collector_name = mapped_column(String, nullable=True)",
        "collection_started_at = mapped_column(DateTime, nullable=True)",
        "collector_sheet_added_at = mapped_column(DateTime, nullable=True)",
        "collector_sheet_row = mapped_column(Integer, nullable=True)",
        "collector_sheet_error = mapped_column(Text, nullable=True)",
    ]:
        if marker not in for_delivery_text:
            raise AssertionError(f"delivery collection claim model marker missing {marker}")

    for marker in [
        '"008_for_delivery_collection_claim"',
        'add_column_if_missing("for_delivery", "collector_user_id"',
        'add_column_if_missing("for_delivery", "collector_name"',
        'add_column_if_missing("for_delivery", "collection_started_at"',
        '"011_delivery_collector_sheet_tracking"',
        'add_column_if_missing("for_delivery", "collector_sheet_added_at"',
        'add_column_if_missing("for_delivery", "collector_sheet_row"',
        'add_column_if_missing("for_delivery", "collector_sheet_error"',
    ]:
        if marker not in db_init_text:
            raise AssertionError(f"delivery collection claim migration marker missing {marker}")

    for marker in [
        "def claim_delivery_collection_entry(",
        "def release_delivery_collection_entry(",
        "def build_delivery_collection_button_text(",
        "👷 {row.collector_name}",
        'callback_data=f"collect_delivery_{row.id}_{page}"',
        'callback_data_startswith(call, ("collect_delivery_release_", "collect_release_"))',
        'callback_data=f"collect_release_{delivery_id}_{page}"',
        'callback_prefix = (',
        'if callback_data_startswith(call, "collect_release_")',
        'else "collect_delivery_release_"',
        'and not callback_data_startswith(call, "collect_release_")',
        "Эту корзину сейчас собирает",
        "collector_user_id=call.from_user.id",
        "add_delivery_collector_name_to_sheet(delivery_id)",
        "move_unplaced_to_missing=False",
    ]:
        if marker not in main_text:
            raise AssertionError(f"delivery collection claim marker missing {marker}")


def test_delivery_collection_item_and_missing_markers():
    main_text = MAIN.read_text(encoding="utf-8")
    db_init_text = (ROOT / "db" / "__init__.py").read_text(encoding="utf-8")
    collection_item_text = (ROOT / "db" / "delivery_collection_items.py").read_text(encoding="utf-8")
    missing_item_text = (ROOT / "db" / "missing_delivery_items.py").read_text(encoding="utf-8")
    sheets_text = (ROOT / "services" / "google_sheets_delivery.py").read_text(encoding="utf-8")
    requirements_text = (ROOT / "requirements.txt").read_text(encoding="utf-8")

    for marker in [
        "class DeliveryCollectionItem",
        "__tablename__ = \"delivery_collection_items\"",
        'SAMARA_TZ = ZoneInfo("Europe/Samara")',
        "default=local_now_naive",
        "uq_delivery_collection_items_reservation_unit",
        "sheet_amount_before",
        "sheet_amount_after",
        "sheet_error",
        "collector_sheet_added_at",
        "collector_sheet_row",
        "collector_sheet_error",
    ]:
        if marker not in collection_item_text:
            raise AssertionError(f"delivery collection item model marker missing {marker}")

    for marker in [
        "class MissingDeliveryItem",
        "__tablename__ = \"missing_delivery_items\"",
        'SAMARA_TZ = ZoneInfo("Europe/Samara")',
        "default=local_now_naive",
        "status = mapped_column(String, nullable=False, default=\"missing\")",
        "resolved_by_user_id",
    ]:
        if marker not in missing_item_text:
            raise AssertionError(f"missing delivery item model marker missing {marker}")

    for marker in [
        '"010_delivery_collection_items_and_missing"',
        '"021_delivery_collection_post_id_indexes"',
        '"023_delivery_collection_item_collector_sheet_tracking"',
        '"024_backfill_completed_delivery_collection_item_collector_sheet_tracking"',
        "ensure_delivery_collection_items_and_missing",
        "ensure_delivery_collection_post_id_indexes",
        "ensure_delivery_collection_item_collector_sheet_tracking",
        "backfill_completed_delivery_collection_item_collector_sheet_tracking",
        "DeliveryCollectionItem",
        "MissingDeliveryItem",
        "ix_delivery_collection_items_post_id",
        "ix_missing_delivery_items_post_id",
        'add_column_if_missing("delivery_collection_items", "collector_sheet_added_at"',
        'add_column_if_missing("delivery_collection_items", "collector_sheet_row"',
        'add_column_if_missing("delivery_collection_items", "collector_sheet_error"',
        "ix_delivery_collection_items_collector_sheet_added_at",
        '"AND NOT EXISTS ("',
        '"SELECT 1 FROM for_delivery fd WHERE fd.id = delivery_collection_items.delivery_id"',
        '"AND fd.collector_sheet_added_at IS NOT NULL"',
    ]:
        if marker not in db_init_text:
            raise AssertionError(f"delivery collection item migration marker missing {marker}")

    for marker in [
        "GOOGLE_SHEETS_WORKSHEET",
        "GOOGLE_SHEETS_PHONE_COLUMN",
        "GOOGLE_SHEETS_AMOUNT_COLUMN",
        "GOOGLE_SHEETS_COLLECTOR_COLUMN",
        "warnings.filterwarnings(",
        "google\\.api_core\\._python_version_support",
        "valueRenderOption=\"FORMULA\"",
        "DELIVERY_OPERATION_MARKER_PREFIX",
        "def build_delivery_operation_marker(",
        "def delivery_formula_has_marker(",
        "re.escape(normalized_id)",
        "re.search(marker_pattern",
        "def build_delivery_amount_update_value(",
        "def write_delivery_collector_name(",
        "collector_range",
        "GOOGLE_SHEETS_HTTP_TIMEOUT_SECONDS",
        "AuthorizedHttp(credentials, http=httplib2.Http(timeout=timeout_seconds))",
        'return f"{formula_text}+{amount_to_add}{marker_suffix}"',
        'return f"={amount_to_add}{marker_suffix}"',
        "already_added",
        "values().update(",
        "amount_after = amount_before + amount",
    ]:
        if marker not in sheets_text:
            raise AssertionError(f"google sheets delivery marker missing {marker}")

    marker_check_block = sheets_text.split("def delivery_formula_has_marker(", 1)[1].split(
        "def get_delivery_sheet_config(",
        1,
    )[0]
    if "normalized_id in str(formula_value" in marker_check_block:
        raise AssertionError("google sheets operation markers must not use substring matching")

    for marker in [
        "google-api-python-client",
        "google-auth",
        "google-auth-httplib2",
    ]:
        if marker not in requirements_text:
            raise AssertionError(f"google sheets requirement missing {marker}")

    for marker in [
        "📦 Список пропажи",
        "✅ Положил",
        "def build_delivery_collection_finish_keyboard(",
        "def mark_delivery_collection_item_placed(",
        "add_collection_item_amount_to_sheet",
        "def get_delivery_sheet_phone(",
        "add_delivery_collector_name_to_sheet",
        "update_delivery_collection_items_collector",
        "mark_delivery_collection_items_collector_sheet_result",
        "get_pending_delivery_collector_sheet_delivery_ids",
        "get_pending_delivery_collector_sheet_item_ids",
        "collector_item_ids = get_pending_delivery_collector_sheet_item_ids(exclude_delivery_ids=delivery_ids)",
        "DeliveryCollectionItem.collector_sheet_added_at == None",
        "build_delivery_sheet_operation_id",
        "Unexpected delivery collector Sheets update failed delivery_id=%s",
        "Unexpected delivery item Sheets update failed item_id=%s",
        "delivery_sheet_sync_lock",
        "def acquire_delivery_sheet_db_lock():",
        "DELIVERY_SHEET_LOCK_KEY",
        "def retry_pending_delivery_sheet_items(",
        "def start_delivery_sheet_retry_worker(",
        "start_delivery_sheet_retry_worker()",
        "def mark_temp_fulfilled_in_delivery_for_items(",
        "temp_item.quantity = delivery_collection_items_quantity(placed_items)",
        "temp_item.price = delivery_collection_items_amount(placed_items)",
        "def finish_delivery_collection(",
        "move_unplaced_to_missing=True",
        "def show_missing_delivery_list(",
        "def return_missing_item_to_channel(",
        "def publish_returned_missing_post_to_channel(",
        "def build_post_from_missing_delivery_item(item):",
        "post = build_post_from_missing_delivery_item(item)",
        "session.add(post)",
        "session.flush()",
        "channel_ok, message_id, error_text = publish_returned_missing_post_to_channel(post)",
        "session.rollback()",
        "post.message_id = message_id",
        "post.is_sent = True",
        "def return_missing_item_to_cart(",
        'def ensure_missing_delivery_item_snapshot(session, item, reason="missing_delivery_return_to_cart"):',
        'reason=reason',
        'reason="missing_delivery_return_to_channel"',
        "session.add(Temp_Fulfilled(",
        "ensure_missing_delivery_item_snapshot(session, item)",
        "📢 Вернуть на канал",
        "🛒 Вернуть в корзину клиента",
    ]:
        if marker not in main_text:
            raise AssertionError(f"delivery collection missing workflow marker missing {marker}")

    collector_sheet_block = main_text.split("def add_delivery_collector_name_to_sheet(", 1)[1].split(
        "def get_pending_delivery_collector_sheet_delivery_ids(",
        1,
    )[0]
    if collector_sheet_block.count("Session(bind=engine, expire_on_commit=False)") < 2:
        raise AssertionError("collector sheet update must return non-expired delivery entry objects")
    for marker in [
        "session.query(DeliveryCollectionItem).filter(",
        "DeliveryCollectionItem.delivery_id == delivery_id",
        "DeliveryCollectionItem.collector_name != None",
        "mark_delivery_collection_items_collector_sheet_result(session, delivery_id, result=result)",
        "mark_delivery_collection_items_collector_sheet_result(session, delivery_id, sheet_error=sheet_error)",
    ]:
        if marker not in collector_sheet_block:
            raise AssertionError(f"collector sheet durable retry marker missing {marker}")

    collector_retry_block = main_text.split("def retry_pending_delivery_sheet_items(", 1)[1].split(
        "def delivery_sheet_retry_loop(",
        1,
    )[0]
    for marker in [
        "collector_item_ids = get_pending_delivery_collector_sheet_item_ids(exclude_delivery_ids=delivery_ids)",
        "for item_id in collector_item_ids:",
        "DeliveryCollectionItem.delivery_id",
        "add_delivery_collector_name_to_sheet(delivery_id)",
        "len(delivery_ids) + len(collector_item_ids)",
    ]:
        if marker not in collector_retry_block:
            raise AssertionError(f"collector sheet retry from collection items marker missing {marker}")
    assert_marker_order(
        collector_retry_block,
        "delivery_ids = get_pending_delivery_collector_sheet_delivery_ids()",
        "collector_item_ids = get_pending_delivery_collector_sheet_item_ids(exclude_delivery_ids=delivery_ids)",
        "collector sheet item retry must exclude active delivery rows already selected",
    )

    item_sheet_block = main_text.split("def add_collection_item_amount_to_sheet(", 1)[1].split(
        "def get_pending_delivery_sheet_item_ids(",
        1,
    )[0]
    if item_sheet_block.count("Session(bind=engine, expire_on_commit=False)") < 2:
        raise AssertionError("delivery item sheet update must return non-expired collection item objects")
    if "get_delivery_sheet_phone(session, item=item)" not in item_sheet_block:
        raise AssertionError("delivery item sheet update must write amounts to the main cart phone")
    if "phone = item.phone" in item_sheet_block:
        raise AssertionError("delivery item sheet update must not use extra cart phone")

    snapshot_block = main_text.split("def get_or_create_delivery_collection_items(", 1)[1].split(
        "def build_delivery_collection_item_caption(",
        1,
    )[0]
    for marker in [
        "def delivery_collection_cart_item_key(cart_item):",
        "def find_delivery_collection_items_by_keys(session, keys):",
        "def apply_delivery_collection_entry_to_item(item, delivery_entry):",
        "def build_delivery_collection_item_from_cart(delivery_entry, cart_item):",
        "tuple_(",
        "DeliveryCollectionItem.reservation_id",
        "DeliveryCollectionItem.unit_index",
    ]:
        if marker not in main_text:
            raise AssertionError(f"delivery collection snapshot helper marker missing {marker}")

    for marker in [
        "existing_items = session.query(DeliveryCollectionItem).filter(",
        "if existing_items:",
        "return sort_delivery_collection_items(existing_items)",
        "cart_items = get_delivery_entry_cart_items(session, delivery_entry)",
        "existing_by_key = find_delivery_collection_items_by_keys(session, list(cart_items_by_key))",
        "apply_delivery_collection_entry_to_item(item, delivery_entry)",
        "item.delivery_id = delivery_entry.id",
        "with session.begin_nested():",
        "except IntegrityError:",
        "item = find_delivery_collection_items_by_keys(session, [key]).get(key)",
    ]:
        if marker not in snapshot_block:
            raise AssertionError(f"delivery collection snapshot freeze marker missing {marker}")
    assert_marker_order(
        snapshot_block,
        "return sort_delivery_collection_items(existing_items)",
        "cart_items = get_delivery_entry_cart_items(session, delivery_entry)",
        "delivery collection snapshot must be reused before reading the current cart",
    )
    if "elif not item.placed_at:" in snapshot_block or "existing.get(key)" in snapshot_block:
        raise AssertionError("delivery collection snapshot items must not be refreshed from the current cart")
    if "item.delivery_id not in delivery_ids" in snapshot_block:
        raise AssertionError("delivery collection snapshot must not append leftover items from merged carts")

    item_message_block = main_text.split("def send_delivery_collection_item_message(", 1)[1].split(
        "def acquire_delivery_sheet_db_lock(",
        1,
    )[0]
    for marker in [
        "except Exception as exc:",
        "Delivery collection item primary send failed item_id=%s post_id=%s, falling back to text: %s",
        "safe_send_message(",
        "caption,",
        "reply_markup=markup",
        "return sent_message, False",
        "raise",
    ]:
        if marker not in item_message_block:
            raise AssertionError(f"delivery collection item send fallback marker missing {marker}")

    for function_name, end_marker in [
        ("acquire_delivery_sheet_db_lock", "def release_delivery_sheet_db_lock("),
        ("acquire_delivery_collection_report_db_lock", "def release_delivery_collection_report_db_lock("),
    ]:
        lock_block = main_text.split(f"def {function_name}():", 1)[1].split(end_marker, 1)[0]
        for marker in [
            "connection = engine.connect()",
            "try:",
            "connection.execute(",
            "except Exception:",
            "connection.close()",
            "raise",
            "return connection",
        ]:
            if marker not in lock_block:
                raise AssertionError(f"{function_name} must close connection on lock acquire failure: {marker}")
        assert_marker_order(lock_block, "connection = engine.connect()", "try:", f"{function_name} must wrap lock acquire")
        assert_marker_order(lock_block, "except Exception:", "connection.close()", f"{function_name} must close failed lock connection")
        assert_marker_order(lock_block, "connection.close()", "raise", f"{function_name} must re-raise after closing")
        assert_marker_order(lock_block, "raise", "return connection", f"{function_name} must return only after successful lock")
    assert_marker_order(
        item_message_block,
        "sent_message = safe_send_message(",
        "return sent_message, False",
        "delivery collection item fallback text message must be saved with its inline keyboard",
    )

    placed_callback_block = main_text.split("def mark_delivery_collection_item_placed(", 1)[1].split(
        "def move_for_delivery_to_in_delivery(",
        1,
    )[0]
    assert_marker_order(
        placed_callback_block,
        'safe_answer_callback_query(call.id, "Положено. Записываю сумму в Google Sheets.")',
        "add_collection_item_amount_to_sheet(item_id)",
        "delivery item placed callback must answer before Google Sheets update",
    )
    for marker in [
        'safe_answer_callback_query(call.id, "Сборка уже завершена или удалена.", show_alert=True)',
        "if delivery_entry.collector_user_id",
        "ForDelivery.collector_user_id == call.from_user.id",
        "item.delivery_id = delivery_entry.id",
    ]:
        if marker not in placed_callback_block:
            raise AssertionError(f"delivery placed callback stale delivery guard missing {marker}")
    assert_marker_order(
        placed_callback_block,
        "ForDelivery.collector_user_id == call.from_user.id",
        'safe_answer_callback_query(call.id, "Сборка уже завершена или удалена.", show_alert=True)',
        "placed callback must rebind live cart before treating collection as deleted",
    )
    assert_marker_order(
        placed_callback_block,
        'safe_answer_callback_query(call.id, "Сборка уже завершена или удалена.", show_alert=True)',
        "item.placed_at = now_local()",
        "stale delivery placed callback must stop before marking item placed",
    )

    keyboard_block = main_text.split("def build_delivery_collection_keyboard(", 1)[1].split(
        "def show_delivery_collection_list(",
        1,
    )[0]
    for marker in [
        "deduplicate_for_delivery_entries(session)",
        "ForDelivery.merged_into_delivery_id == None",
        "sync_delivery_entry_total_sum(session, row)",
    ]:
        if marker not in keyboard_block:
            raise AssertionError(f"delivery collection list marker missing {marker}")

    dedupe_block = main_text.split("def deduplicate_for_delivery_entries(", 1)[1].split(
        "@bot.message_handler(func=lambda message: message.text == \"👨‍🦯 Засунуть в доставку\")",
        1,
    )[0]
    for marker in [
        "transfer_delivery_collection_items(session, discarded.id, keeper.id)",
        "session.delete(discarded)",
        "entry.collector_user_id and not existing.collector_user_id",
    ]:
        if marker not in dedupe_block:
            raise AssertionError(f"delivery dedupe must preserve live collection items: missing {marker}")
    assert_marker_order(
        dedupe_block,
        "transfer_delivery_collection_items(session, discarded.id, keeper.id)",
        "session.delete(discarded)",
        "dedupe must rebind collection items before deleting a duplicate cart",
    )

    continue_block = main_text.split("def continue_delivery_collection(", 1)[1].split(
        "@bot.message_handler",
        1,
    )[0]
    if "build_delivery_collection_finish_keyboard(delivery_id, page=0)" not in continue_block:
        raise AssertionError("continue delivery collection must restore the finish keyboard")

    delivery_client_block = main_text.split("def show_delivery_collection_client(", 1)[1].split(
        "def build_delivery_collection_finish_keyboard(",
        1,
    )[0]
    for marker in [
        "add_delivery_collector_name_to_sheet(delivery_id)",
        "safe_send_message(bot, call.message.chat.id, header, logger=logger)",
        "start_delivery_collection_report_to_group(report_context, report_items)",
    ]:
        assert_marker_order(
            delivery_client_block,
            "safe_answer_callback_query(call.id)",
            marker,
            "delivery collection client must answer callback before slow work",
        )

    collected_block = main_text.split("def mark_delivery_collected(", 1)[1].split(
        "@bot.callback_query_handler(func=lambda call: callback_data_startswith(call, \"delivery_finish_confirm_\"))",
        1,
    )[0]
    assert_marker_order(
        collected_block,
        "safe_answer_callback_query(call.id)",
        "finish_delivery_collection(",
        "finish collection callback must answer before DB move",
    )

    finish_collection_block = main_text.split("def finish_delivery_collection(", 1)[1].split(
        "@bot.callback_query_handler(func=lambda call: callback_data_startswith(call, \"delivery_collected_\"))",
        1,
    )[0]
    for marker in [
        "placed_for_reservation = placed_by_reservation.get(reservation_id)",
        "mark_temp_fulfilled_in_delivery_for_items(session, reservation, placed_for_reservation)",
        "delete_temp_fulfilled_for_reservation(session, reservation)",
        "mark_delivery_collection_items_collector_sheet_result(",
        "Ожидает повторной записи Google Sheets",
    ]:
        if marker not in finish_collection_block:
            raise AssertionError(f"partial delivery finish marker missing {marker}")
    assert_marker_order(
        finish_collection_block,
        "mark_delivery_collection_items_collector_sheet_result(",
        "session.delete(delivery_entry)",
        "pending collector sheet retry must be preserved before deleting active delivery row",
    )

    confirm_finish_block = main_text.split("def confirm_finish_delivery_with_missing(", 1)[1].split(
        "@bot.callback_query_handler(func=lambda call: callback_data_startswith(call, \"delivery_finish_continue_\"))",
        1,
    )[0]
    assert_marker_order(
        confirm_finish_block,
        "safe_answer_callback_query(call.id)",
        "finish_delivery_collection(",
        "confirm finish callback must answer before DB move",
    )

    stale_manual_sum_block = main_text.split("def handle_delivery_collected_sum(", 1)[1].split(
        "@bot.message_handler(func=lambda message: message.text == \"✅ Подтвердить доставку\")",
        1,
    )[0]
    for marker in [
        "clear_user_state(message.chat.id)",
        "Старый способ завершения доставки больше не используется",
        "show_delivery_collection_list(message.chat.id, message_id=source_message_id, page=0)",
    ]:
        if marker not in stale_manual_sum_block:
            raise AssertionError(f"stale manual delivery completion guard missing {marker}")
    for forbidden in [
        "parse_delivery_total_input(",
        "move_for_delivery_to_in_delivery(",
        "start_reserved_group_resume_flush_if_delivery_done(",
    ]:
        if forbidden in stale_manual_sum_block:
            raise AssertionError(f"stale manual delivery completion must not use old move flow: {forbidden}")

    missing_list_block = main_text.split("def show_missing_delivery_list(", 1)[1].split(
        "def build_missing_delivery_item_text(",
        1,
    )[0]
    for marker in [
        "edited_message = safe_edit_message_text",
        "if edited_message is not None:",
        "safe_send_message(bot, chat_id, text, logger=logger, reply_markup=keyboard)",
    ]:
        if marker not in missing_list_block:
            raise AssertionError(f"missing delivery list photo fallback marker missing {marker}")

    missing_item_block = main_text.split("def show_missing_delivery_item(", 1)[1].split(
        "def publish_returned_missing_post_to_channel(",
        1,
    )[0]
    for marker in [
        'safe_send_message(bot, chat_id, "Товар из списка пропажи не найден.", logger=logger)',
        "try:",
        "send_photo_or_text(bot, chat_id, photo, text, reply_markup=keyboard)",
        "Missing delivery item display failed item_id=%s chat_id=%s: %s",
        "Не удалось показать товар из списка пропажи. Попробуйте ещё раз.",
    ]:
        if marker not in missing_item_block:
            raise AssertionError(f"missing delivery item safe display marker missing {marker}")
    if "bot.send_message(chat_id, \"Товар из списка пропажи не найден.\")" in missing_item_block:
        raise AssertionError("missing delivery item display must use safe_send_message for not-found")

    missing_callback_checks = [
        (
            "def paginate_missing_delivery(",
            "def show_missing_delivery_item_handler(",
            "show_missing_delivery_list(",
            "missing delivery page callback",
        ),
        (
            "def show_missing_delivery_item_handler(",
            "def choose_missing_delivery_return_target(",
            "show_missing_delivery_item(",
            "missing delivery item callback",
        ),
        (
            "def choose_missing_delivery_return_target(",
            "def return_missing_delivery_to_channel_handler(",
            "safe_send_message(bot, call.message.chat.id, \"Куда вернуть найденный товар?\"",
            "missing delivery found callback",
        ),
        (
            "def return_missing_delivery_to_channel_handler(",
            "def return_missing_delivery_to_cart_handler(",
            "return_missing_item_to_channel(",
            "missing delivery return-channel callback",
        ),
        (
            "def return_missing_delivery_to_cart_handler(",
            "@bot.callback_query_handler(\n    func=lambda call: call.data == \"edit_address\"",
            "return_missing_item_to_cart(",
            "missing delivery return-cart callback",
        ),
    ]
    for start, end, slow_marker, label in missing_callback_checks:
        callback_block = main_text.split(start, 1)[1].split(end, 1)[0]
        assert_marker_order(
            callback_block,
            "safe_answer_callback_query(call.id)",
            slow_marker,
            f"{label} must answer before slow work",
        )


def test_delivery_sheet_amount_updates_are_additive_and_idempotent():
    sheets_path = ROOT / "services" / "google_sheets_delivery.py"
    sheets_tree = ast.parse(sheets_path.read_text(encoding="utf-8"))
    wanted_functions = {
        "normalize_delivery_operation_id",
        "build_delivery_operation_marker",
        "delivery_formula_has_marker",
        "build_delivery_amount_update_value",
    }
    function_nodes = [
        node
        for node in sheets_tree.body
        if isinstance(node, ast.FunctionDef) and node.name in wanted_functions
    ]
    namespace = {"re": re}
    exec(
        compile(ast.Module(body=function_nodes, type_ignores=[]), str(sheets_path), "exec"),
        namespace,
    )
    build_delivery_amount_update_value = namespace["build_delivery_amount_update_value"]
    delivery_formula_has_marker = namespace["delivery_formula_has_marker"]

    operation_id = "delivery_item_123"
    if build_delivery_amount_update_value("", 0, 1500, operation_id) != '=1500+N("delivery_item_123")':
        raise AssertionError("empty delivery sheet amount cell must be initialized as an additive formula")
    if build_delivery_amount_update_value("", 300, 1500, operation_id) != '=300+1500+N("delivery_item_123")':
        raise AssertionError("plain delivery sheet amount cells must preserve the existing amount")
    if build_delivery_amount_update_value("=300+200", 500, 1500, operation_id) != '=300+200+1500+N("delivery_item_123")':
        raise AssertionError("formula delivery sheet amount cells must be appended to, not overwritten")

    formula = '=300+200+1500+N("delivery_item_123")'
    if not delivery_formula_has_marker(formula, operation_id):
        raise AssertionError("delivery sheet retry must detect an already applied operation marker")
    if delivery_formula_has_marker('=300+N("delivery_item_1234")', operation_id):
        raise AssertionError("delivery sheet operation marker matching must not use substring matching")


def test_core_declared_indexes_migration_markers():
    db_init_text = (ROOT / "db" / "__init__.py").read_text(encoding="utf-8")
    for marker in [
        '"022_core_declared_indexes"',
        "def ensure_core_declared_indexes():",
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
        "CREATE INDEX IF NOT EXISTS ix_temp_fulfilled_delivery_flags",
        "ON temp_fulfilled (user_id, in_delivery, defect, skidka)",
        "CREATE INDEX IF NOT EXISTS ix_temp_reservations_user_post ON temp_reservations (user_id, post_id)",
        "CREATE INDEX IF NOT EXISTS ix_temp_reservations_post_fulfilled",
        "ON temp_reservations (post_id, temp_fulfilled)",
    ]:
        if marker not in db_init_text:
            raise AssertionError(f"core declared index migration marker missing {marker}")


def test_delivery_collection_report_group_markers():
    main_text = MAIN.read_text(encoding="utf-8")

    for marker in [
        'DELIVERY_COLLECTION_REPORT_GROUP_ID = env_int("DELIVERY_COLLECTION_REPORT_GROUP_ID", -1004453060578)',
        'DELIVERY_COLLECTION_REPORT_SEND_INTERVAL_SECONDS = env_float(',
        '"DELIVERY_COLLECTION_REPORT_SEND_INTERVAL_SECONDS", 5, minimum=0',
        'DELIVERY_COLLECTION_REPORT_LOCK_KEY = env_int("DELIVERY_COLLECTION_REPORT_LOCK_KEY", 4453060578)',
        "delivery_collection_report_lock = threading.Lock()",
        "delivery_collection_report_target_group_id = DELIVERY_COLLECTION_REPORT_GROUP_ID",
        "def build_delivery_collection_report_items_from_collection_items(",
        "def get_delivery_collection_report_collector_name(session, collector_user_id):",
        "session.query(Clients).filter(Clients.user_id == collector_user_id).first()",
        "def group_delivery_collection_report_items(",
        "def build_delivery_collection_report_header(",
        "def build_delivery_collection_report_item_caption(",
        "def build_delivery_collection_report_footer(",
        "🧺 Начало сборки доставки",
        "✅ Конец корзины",
        "Номер телефона клиента: {context['phone'] or 'не указан'}",
        "Кто собирает: {context['collector_name'] or 'неизвестно'}",
        "def telegram_migrate_to_chat_id(exc):",
        "migrate_to_chat_id = parameters.get(\"migrate_to_chat_id\")",
        "def update_delivery_collection_report_group_id(migrate_to_chat_id):",
        "global delivery_collection_report_target_group_id",
        "def send_delivery_collection_report_message(text):",
        "def send_delivery_collection_report_photo_or_text(photo, text, reply_markup=None):",
        "def acquire_delivery_collection_report_db_lock():",
        "SELECT pg_advisory_lock(:lock_key)",
        "def release_delivery_collection_report_db_lock(connection):",
        "SELECT pg_advisory_unlock(:lock_key)",
        "is_transient_telegram_error(exc) and attempt < attempts",
        "Delivery collection report header send failed attempt=%s/%s, retrying in %ss: %s",
        "Delivery collection report item send failed attempt=%s/%s, retrying in %ss: %s",
        "DELIVERY_COLLECTION_REPORT_SEND_INTERVAL_SECONDS * attempt",
        "def send_delivery_collection_report_to_group(",
        "with delivery_collection_report_lock:",
        "db_lock_connection = acquire_delivery_collection_report_db_lock()",
        "release_delivery_collection_report_db_lock(db_lock_connection)",
        "send_delivery_collection_report_message(header)",
        "send_delivery_collection_report_item_to_group(context, item)",
        "build_delivery_collection_report_footer(context, grouped_items)",
        "send_delivery_collection_report_photo_or_text(current_item.get(\"photo\"), caption)",
        "Сумма товара: {item['total_price']} ₽",
        "Количество: {item['quantity']}",
        "report_context = build_delivery_collection_report_context(session, delivery_entry)",
        "report_items = build_delivery_collection_report_items_from_collection_items(items)",
        "def start_delivery_collection_report_to_group(context, items):",
        "start_delivery_collection_report_to_group(report_context, report_items)",
        'return "✅ Нашли"',
        'return "❌ Не нашли"',
        'InlineKeyboardButton("НАШЛИ!", callback_data=f"delivery_report_found_{unplaced_item_ids[0]}")',
        "def notify_client_after_delivery_collection(notice):",
        "Сборка вашей доставки завершена.",
        "Этот товар во время сборки мы не нашли.",
        "Этот товар нашли и добавили в вашу корзину.",
        "def recover_missing_delivery_collection_items(",
        "MISSING_DELIVERY_STATUS_FOUND",
        'callback_data_startswith(call, "delivery_report_found_")',
        "has_role(call.from_user.id, ADMIN_ROLES)",
        "Кнопка доступна только администратору и создателю.",
    ]:
        if marker not in main_text:
            raise AssertionError(f"delivery collection report group marker missing {marker}")

    collection_client_block = main_text.split("def show_delivery_collection_client(", 1)[1].split(
        "def build_delivery_collection_finish_keyboard(",
        1,
    )[0]
    if "get_delivery_entry_full_cart_report_items(session, delivery_entry)" in collection_client_block:
        raise AssertionError("delivery collection group report must use current collection items, not the full cart")


def test_defect_pagination_and_menu_markers():
    main_text = MAIN.read_text(encoding="utf-8")
    keyboard_text = (ROOT / "bot" / "keyboard.py").read_text(encoding="utf-8")

    if 'KeyboardButton(" Ревизия")' in keyboard_text:
        raise AssertionError("revision keyboard button must not contain a leading space")
    if 'KeyboardButton("Ревизия")' not in keyboard_text:
        raise AssertionError("revision keyboard button is missing")

    for marker in [
        "DEFECT_ITEM_PAGE_SIZE = 8",
        "DEFECTIVE_RESERVATION_PAGE_SIZE = 8",
        "def build_user_defect_items_keyboard(user_id, page=0):",
        "callback_data=f\"defect_items_page_{page + 1}\"",
        "def paginate_user_defect_items(call):",
        "def user_can_select_defective_item(user_id, item_id):",
        "def send_defective_reservation_page(chat_id, page=0, message_id=None):",
        "callback_data=f\"defective_res_page_{page + 1}\"",
        "def paginate_defective_reservations(call):",
        "and callback_data_startswith(call, \"select_\")",
        "def handle_defective_action(call):",
        "safe_answer_callback_query(call.id, \"Некорректная кнопка.\", show_alert=True)",
    ]:
        if marker not in main_text:
            raise AssertionError(f"defect pagination marker missing {marker}")

    defect_action_block = main_text.split("def handle_defect_action(call, item_id):", 1)[1].split(
        "def request_discount_amount(call, item_id):",
        1,
    )[0]
    for marker in [
        "with_for_update().first()",
        "if item.defect or item.skidka:",
        'safe_answer_callback_query(call.id, "Этот товар уже обработан.", show_alert=True)',
    ]:
        if marker not in defect_action_block:
            raise AssertionError(f"defect action idempotency marker missing {marker}")

    discount_request_block = main_text.split("def request_discount_amount(call, item_id):", 1)[1].split(
        "@bot.message_handler(",
        1,
    )[0]
    for marker in [
        "if item.defect or item.skidka:",
        'safe_answer_callback_query(call.id, "Этот товар уже обработан.", show_alert=True)',
    ]:
        if marker not in discount_request_block:
            raise AssertionError(f"discount request idempotency marker missing {marker}")
    for marker in [
        "prompt_message = safe_send_message(",
        "if not prompt_message:",
        "Не удалось отправить запрос суммы. Попробуйте ещё раз.",
    ]:
        if marker not in discount_request_block:
            raise AssertionError(f"discount request prompt marker missing {marker}")
    assert_marker_order(
        discount_request_block,
        "prompt_message = safe_send_message(",
        "set_user_state(call.from_user.id",
        "discount request state must be set only after prompt is sent",
    )

    discount_amount_block = main_text.split("def handle_discount_amount(message):", 1)[1].split(
        "@bot.callback_query_handler(",
        1,
    )[0]
    for marker in [
        "if item.defect or item.skidka:",
        'safe_send_message(bot, admin_id, "Этот товар уже обработан.", logger=logger)',
        "clear_user_state(admin_id)",
        "discount_message = safe_send_message(",
        "if not discount_message:",
        "Не удалось отправить предложение скидки клиенту. Попробуйте позже.",
        "clear_user_state(client.user_id)",
    ]:
        if marker not in discount_amount_block:
            raise AssertionError(f"discount amount state marker missing {marker}")
    assert_marker_order(
        discount_amount_block,
        "discount_message = safe_send_message(",
        "set_user_state(\n            client.user_id,",
        "client discount state must be set only after the offer message is sent",
    )

    discount_confirmation_block = main_text.split("def handle_discount_confirmation(call):", 1)[1].split(
        "def contact_client(call, user_id):",
        1,
    )[0]
    for marker in [
        'state.get("action") != "confirm_discount"',
        "with_for_update().first()",
        "if item.defect or item.skidka:",
        'safe_answer_callback_query(call.id, "Этот товар уже обработан.", show_alert=True)',
        "clear_user_state(user_id)",
    ]:
        if marker not in discount_confirmation_block:
            raise AssertionError(f"discount confirmation idempotency marker missing {marker}")


def test_statistic_api_security_and_query_scope():
    statistic_text = (ROOT / "statistic_api.py").read_text(encoding="utf-8")
    for marker in [
        "def is_authorized(self):",
        "Authorization",
        "return False",
        "WHERE created_at >= %s AND created_at < %s",
        "created_at::time <> TIME '00:00:00'",
        "def handle_error(self, request, client_address):",
        "def do_CONNECT(self):",
        'database_url = os.environ.get("DATABASE_URL", "").strip()',
        "if database_url:",
        "psycopg2.connect(database_url)",
        "host=get_required_env(\"DB_HOST\")",
        "def parse_cors_origins(value):",
        "def same_loopback_origin(left, right):",
        "def allowed_cors_origin(request_origin, configured_origins):",
        "allowed_cors_origin(self.headers.get(\"Origin\", \"\").strip(), configured_origins)",
    ]:
        if marker not in statistic_text:
            raise AssertionError(f"statistic API hardening marker missing {marker}")
    for forbidden in ["parse_qs", "query_token"]:
        if forbidden in statistic_text:
            raise AssertionError(f"statistic API must not accept URL token marker {forbidden}")


def test_statistic_api_cors_origin_matching():
    import statistic_api

    origins = statistic_api.parse_cors_origins("http://127.0.0.1:8765, http://example.test")
    if origins != ["http://127.0.0.1:8765", "http://example.test"]:
        raise AssertionError("statistic API CORS origins must parse comma-separated values")
    if statistic_api.allowed_cors_origin("http://127.0.0.1:8765", origins) != "http://127.0.0.1:8765":
        raise AssertionError("statistic API CORS must echo an explicitly allowed request origin")
    if statistic_api.allowed_cors_origin("http://localhost:8765", origins) != "http://localhost:8765":
        raise AssertionError("statistic API CORS must allow equivalent localhost preview origin")
    if statistic_api.allowed_cors_origin("http://localhost:9999", origins):
        raise AssertionError("statistic API CORS must not allow a different localhost port")
    if statistic_api.allowed_cors_origin("http://evil.test", origins):
        raise AssertionError("statistic API CORS must reject unconfigured origins")
    if statistic_api.allowed_cors_origin("http://evil.test", ["*"]) != "*":
        raise AssertionError("statistic API CORS must support wildcard origins")


def test_statistic_api_database_url_connection():
    import statistic_api

    previous_env = {name: os.environ.get(name) for name in ("DATABASE_URL", "DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD")}
    original_connect = statistic_api.psycopg2.connect
    calls = []

    class FakeConnection:
        def set_session(self, **kwargs):
            calls.append(("set_session", kwargs))

    def fake_connect(*args, **kwargs):
        calls.append(("connect", args, kwargs))
        return FakeConnection()

    try:
        os.environ["DATABASE_URL"] = "postgresql://user:pass@example.invalid:5432/final_bot"
        for name in ("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD"):
            os.environ.pop(name, None)
        statistic_api.psycopg2.connect = fake_connect

        connection = statistic_api.get_database_connection()
        if not isinstance(connection, FakeConnection):
            raise AssertionError("statistic API must return the created database connection")
        if calls[0] != ("connect", (os.environ["DATABASE_URL"],), {}):
            raise AssertionError("statistic API must prefer DATABASE_URL for database connections")
        if ("set_session", {"readonly": True, "autocommit": False}) not in calls:
            raise AssertionError("statistic API database connection must remain read-only")
    finally:
        statistic_api.psycopg2.connect = original_connect
        for name, value in previous_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_button_control_connection_noise_markers():
    button_text = (ROOT / "button_control_api.py").read_text(encoding="utf-8")
    for marker in [
        "def do_CONNECT(self):",
        "method_not_allowed",
        "def handle_error(self, request, client_address):",
        "BrokenPipeError",
        "ConnectionResetError",
    ]:
        if marker not in button_text:
            raise AssertionError(f"button control connection hardening marker missing {marker}")

    control_page_block = button_text.split("function renderBinding() {", 1)[1].split(
        "async function request(",
        1,
    )[0]
    for marker in [
        "bindingText.textContent = \"\";",
        "document.createElement(\"code\")",
        "code.textContent = bound;",
        "bindingText.appendChild(code);",
    ]:
        if marker not in control_page_block:
            raise AssertionError(f"button control page safe binding marker missing {marker}")
    if "bindingText.innerHTML" in control_page_block:
        raise AssertionError("button control page must not render localStorage binding with innerHTML")


def test_auxiliary_systemd_services_are_hardened():
    for service_name in ("final_bot_statistic_api.service", "final_bot_button_control.service"):
        service_text = (ROOT / "deploy" / service_name).read_text(encoding="utf-8")
        for marker in [
            "NoNewPrivileges=true",
            "PrivateTmp=true",
            "ProtectSystem=full",
            "ProtectHome=true",
        ]:
            if marker not in service_text:
                raise AssertionError(f"{service_name} hardening marker missing {marker}")


def test_post_stats_app_keeps_last_snapshot_on_fetch_error():
    activity_path = (
        ROOT / "android_post_stats" / "app" / "src" / "main" / "java" /
        "com" / "megaskidka" / "poststats" / "MainActivity.java"
    )
    web_path = ROOT / "android_post_stats" / "web_preview" / "index.html"
    gradle_path = ROOT / "android_post_stats" / "app" / "build.gradle.kts"
    if not activity_path.exists() or not web_path.exists():
        return

    activity_text = activity_path.read_text(encoding="utf-8")
    web_text = web_path.read_text(encoding="utf-8")
    gradle_text = gradle_path.read_text(encoding="utf-8") if gradle_path.exists() else ""

    for marker in [
        "handler.post(this::renderFetchFailure)",
        "private void renderFetchFailure()",
        "HttpURLConnection connection = null;",
        "finally {",
        "connection.disconnect();",
        "try (BufferedReader reader = new BufferedReader",
        "private static final int FIRST_UPDATE_HOUR = 9;",
        "private static final int LAST_UPDATE_HOUR = 17;",
        "next.set(Calendar.HOUR_OF_DAY, hour + 1);",
        "if (currentSnapshot != null) {",
        "renderCurrentPeriod();",
        "renderError(\"Ошибка загрузки\");",
    ]:
        if marker not in activity_text:
            raise AssertionError(f"Android post stats stale-data marker missing {marker}")

    for marker in [
        "const FETCH_TIMEOUT_MS = 10000;",
        "const controller = new AbortController();",
        "window.setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS)",
        "signal: controller.signal",
        "window.clearTimeout(timeoutId)",
        "let refreshTimeoutId = null;",
        "window.clearTimeout(refreshTimeoutId)",
        "refreshTimeoutId = window.setTimeout(refresh, delay);",
        "if (currentSnapshot) {",
        "renderCurrentPeriod();",
        "renderMessage(\"Ошибка загрузки\", true);",
    ]:
        if marker not in web_text:
            raise AssertionError(f"web preview stale-data marker missing {marker}")

    for forbidden in ("REFRESH_INTERVAL_MS", "STATISTIC_REFRESH_SECONDS", "statisticRefreshSeconds", ".toLong() * 1000L"):
        if forbidden in activity_text or forbidden in gradle_text:
            raise AssertionError(f"Android post stats must not use stale refresh build field: {forbidden}")

    for marker in [
        "val copyDebugApk by tasks.registering",
        'val debugApk = layout.buildDirectory.file("outputs/apk/debug/app-debug.apk")',
        'val targetApk = layout.projectDirectory.file("../post-statistics.apk")',
        "inputs.file(debugApk)",
        "outputs.file(targetApk)",
        "debugApk.get().asFile.copyTo(targetApk.asFile, overwrite = true)",
        'tasks.matching { it.name == "assembleDebug" }.configureEach',
        "finalizedBy(copyDebugApk)",
    ]:
        if marker not in gradle_text:
            raise AssertionError(f"Android post stats APK copy marker missing {marker}")


def test_api_integer_env_parsing_is_tolerant():
    import statistic_api
    import button_control_api

    previous_statistic_value = os.environ.get("STATISTIC_API_REFRESH_SECONDS")
    previous_button_value = os.environ.get("FINAL_BOT_BUTTON_PORT")
    try:
        os.environ["STATISTIC_API_REFRESH_SECONDS"] = "broken"
        os.environ["FINAL_BOT_BUTTON_PORT"] = "broken"
        if statistic_api.get_int_env("STATISTIC_API_REFRESH_SECONDS", 3600) != 3600:
            raise AssertionError("statistic API must tolerate invalid integer env values")
        if button_control_api.get_int_env("FINAL_BOT_BUTTON_PORT", 8092) != 8092:
            raise AssertionError("button control API must tolerate invalid integer env values")
    finally:
        if previous_statistic_value is None:
            os.environ.pop("STATISTIC_API_REFRESH_SECONDS", None)
        else:
            os.environ["STATISTIC_API_REFRESH_SECONDS"] = previous_statistic_value
        if previous_button_value is None:
            os.environ.pop("FINAL_BOT_BUTTON_PORT", None)
        else:
            os.environ["FINAL_BOT_BUTTON_PORT"] = previous_button_value

    button_text = (ROOT / "button_control_api.py").read_text(encoding="utf-8")
    statistic_text = (ROOT / "statistic_api.py").read_text(encoding="utf-8")
    if 'max(0, parse_int_env("FINAL_BOT_BUTTON_RESTART_COOLDOWN_SECONDS", 30))' not in button_text:
        raise AssertionError("button control restart cooldown must tolerate invalid env values")
    if 'port=get_int_env("DB_PORT", None)' not in statistic_text:
        raise AssertionError("statistic API database port must tolerate invalid env values")
    if "port=int(port)" in statistic_text:
        raise AssertionError("statistic API database port must not use direct int parsing")


def test_core_env_number_parsing_is_tolerant_by_construction():
    main_text = MAIN.read_text(encoding="utf-8")
    db_text = (ROOT / "db" / "db.py").read_text(encoding="utf-8")
    database_config_text = (ROOT / "database" / "config.py").read_text(encoding="utf-8")
    sheets_text = (ROOT / "services" / "google_sheets_delivery.py").read_text(encoding="utf-8")

    for marker in [
        "def env_int(",
        "def env_float(",
        'TELEGRAM_STARTUP_RETRY_SECONDS = env_float("TELEGRAM_STARTUP_RETRY_SECONDS", 5, minimum=0)',
        'TELEGRAM_API_MAX_RETRIES = env_int("TELEGRAM_API_MAX_RETRIES", 3, minimum=1)',
        'DELIVERY_SHEET_RETRY_BATCH_SIZE = env_int("DELIVERY_SHEET_RETRY_BATCH_SIZE", 25, minimum=1)',
        'PHOENIX_BROADCAST_DELAY_SECONDS = env_float("PHOENIX_BROADCAST_DELAY_SECONDS", 1.5, minimum=0)',
    ]:
        if marker not in main_text:
            raise AssertionError(f"main env numeric tolerance marker missing {marker}")

    if "int(os.environ.get(" in main_text or "float(os.environ.get(" in main_text:
        raise AssertionError("main.py must not parse optional numeric env values directly")

    for marker in [
        "def _optional_int(value):",
        "port=_optional_int(db_port)",
        'port=_optional_int(database_config.get("port"))',
    ]:
        if marker not in db_text:
            raise AssertionError(f"database URL port tolerance marker missing {marker}")
    for forbidden in ["port=int(db_port)", "port=int(database_config"]:
        if forbidden in db_text:
            raise AssertionError(f"database URL port must not use direct int parsing: {forbidden}")

    for marker in [
        "def _get_optional_int(name: str, default: int) -> int:",
        'raise RuntimeError(f"Invalid integer environment variable: {name}") from exc',
        'protected_user_id = _get_optional_int("PROTECTED_USER_ID", ADMIN_USER_ID)',
    ]:
        if marker not in database_config_text:
            raise AssertionError(f"database config numeric tolerance marker missing {marker}")
    if 'protected_user_id = int(os.environ.get("PROTECTED_USER_ID"' in database_config_text:
        raise AssertionError("PROTECTED_USER_ID must not use direct int parsing")

    for marker in [
        "def get_int_env(name, default, minimum=None):",
        '"first_data_row": get_int_env("GOOGLE_SHEETS_FIRST_DATA_ROW", 2, minimum=1)',
        '"GOOGLE_SHEETS_HTTP_TIMEOUT_SECONDS"',
        "DEFAULT_GOOGLE_SHEETS_HTTP_TIMEOUT_SECONDS",
        "minimum=1",
    ]:
        if marker not in sheets_text:
            raise AssertionError(f"Google Sheets env numeric tolerance marker missing {marker}")
    if 'int(os.environ.get("GOOGLE_SHEETS_FIRST_DATA_ROW"' in sheets_text:
        raise AssertionError("Google Sheets first_data_row must not use direct int parsing")


def test_env_example_documents_runtime_options():
    env_example_text = (ROOT / ".env.example").read_text(encoding="utf-8")
    markers = [
        "# LOG_LEVEL=INFO",
        "# TELEGRAM_PROXY_URL=",
        "# TELEGRAM_PROXY_SCHEME=socks5",
        "# TELEGRAM_PROXY_HOST=",
        "# TELEGRAM_PROXY_PORT=",
        "# TELEGRAM_PROXY_USERNAME=",
        "# TELEGRAM_PROXY_PASSWORD=",
        "# TELEGRAM_STARTUP_RETRY_SECONDS=5",
        "# TELEGRAM_STARTUP_MAX_RETRY_SECONDS=60",
        "# TELEGRAM_API_RETRY_ON_ERROR=true",
        "# TELEGRAM_API_MAX_RETRIES=3",
        "# TELEGRAM_API_RETRY_TIMEOUT_SECONDS=2",
        "# TELEGRAM_POLLING_TIMEOUT_SECONDS=15",
        "# TELEGRAM_LONG_POLLING_TIMEOUT_SECONDS=30",
        "# STATISTIC_API_TOKEN=",
        "# STATISTIC_API_HOST=127.0.0.1",
        "# STATISTIC_API_PORT=8091",
        "# STATISTIC_API_REFRESH_SECONDS=3600",
        "# STATISTIC_API_CORS_ORIGIN=http://127.0.0.1:8765,http://localhost:8765",
        "# FINAL_BOT_BUTTON_TOKEN=",
        "# FINAL_BOT_BUTTON_HOST=0.0.0.0",
        "# FINAL_BOT_BUTTON_PORT=8092",
        "# FINAL_BOT_SERVICE_NAME=final_bot.service",
        "# FINAL_BOT_BUTTON_RESTART_COOLDOWN_SECONDS=30",
        "# GOOGLE_SHEETS_SPREADSHEET_ID=1RQ4Xx8iH1R_A_Y5OrNS4R0c5dPW5R5T89oBwotLxm0s",
        "# GOOGLE_SHEETS_WORKSHEET=База",
        "# GOOGLE_SHEETS_PHONE_COLUMN=A",
        "# GOOGLE_SHEETS_AMOUNT_COLUMN=C",
        "# GOOGLE_SHEETS_COLLECTOR_COLUMN=I",
        "# GOOGLE_SHEETS_FIRST_DATA_ROW=2",
        "# GOOGLE_SHEETS_HTTP_TIMEOUT_SECONDS=20",
        "# GOOGLE_SERVICE_ACCOUNT_FILE=/opt/final_bot/google_service_account.json",
        "# DELIVERY_SHEET_RETRY_SECONDS=60",
        "# DELIVERY_SHEET_RETRY_BATCH_SIZE=25",
        "# DELIVERY_SHEET_LOCK_KEY=4453060579",
        "# DELIVERY_COLLECTION_REPORT_GROUP_ID=-1004453060578",
        "# DELIVERY_COLLECTION_REPORT_SEND_INTERVAL_SECONDS=5",
        "# DELIVERY_COLLECTION_REPORT_LOCK_KEY=4453060578",
        "# PHOENIX_BROADCAST_DELAY_SECONDS=1.5",
        "# PHOENIX_BROADCAST_BATCH_SIZE=50",
        "# PHOENIX_BROADCAST_BATCH_PAUSE_SECONDS=10",
    ]
    for marker in markers:
        if marker not in env_example_text:
            raise AssertionError(f".env.example runtime option marker missing {marker}")


def test_button_restart_agent_zip_matches_sources():
    agent_dir = ROOT / "tools" / "button_restart_agent"
    agent_text = (agent_dir / "button_restart_agent.py").read_text(encoding="utf-8")
    if "\nfrom pynput import keyboard" in agent_text:
        raise AssertionError("button restart agent must not import pynput at module import time")
    for marker in [
        "import tempfile",
        "fallback_log_path = Path(tempfile.gettempdir()) / \"button_restart_agent.log\"",
        "for path in (LOG_PATH, fallback_log_path):",
        "except OSError:",
        "return False",
        "def load_keyboard_module():",
        "Missing dependency pynput.",
        "keyboard_module.Listener(on_press=on_press, on_release=on_release)",
    ]:
        if marker not in agent_text:
            raise AssertionError(f"button restart agent dependency marker missing {marker}")
    log_block = agent_text.split("def log(message):", 1)[1].split("\n\ndef load_config():", 1)[0]
    if "\n        pass" in log_block:
        raise AssertionError("button restart agent log must not silently swallow logging failures")

    installer_text = (agent_dir / "install_windows.ps1").read_text(encoding="utf-8")
    windows_agent_text = (agent_dir / "windows_button_agent.ps1").read_text(encoding="utf-8")
    for marker in [
        '$ErrorActionPreference = "Stop"',
        "function Get-Win32LastErrorText",
        "[System.Runtime.InteropServices.Marshal]::GetLastWin32Error()",
        "keyboard hook type compile failed:",
        "keyboard hook failed: $(Get-Win32LastErrorText)",
        "message loop failed:",
        "keyboard hook cleanup failed:",
    ]:
        if marker not in windows_agent_text:
            raise AssertionError(f"windows button agent diagnostics marker missing {marker}")

    for marker in [
        "$targetConfigPath = Join-Path $targetDir \"button_restart_agent.config.json\"",
        "if (-not (Test-Path $targetConfigPath))",
        "$sourceConfigPath = Join-Path $sourceDir \"button_restart_agent.config.example.json\"",
        "Copy-Item $sourceConfigPath $targetConfigPath -Force",
    ]:
        if marker not in installer_text:
            raise AssertionError(f"button restart installer config fallback marker missing {marker}")

    for script_name in ("install_macos.command", "run_macos.command"):
        macos_script_text = (agent_dir / script_name).read_text(encoding="utf-8")
        for marker in [
            'PYTHON_BIN="$(command -v python3)"',
            '[[ -z "$PYTHON_BIN" ]]',
            "INSTALL_ERROR_LOG",
            "button_restart_agent.log",
            "\"$PYTHON_BIN\" -m pip install --user -r requirements.txt >> \"$INSTALL_ERROR_LOG\" 2>&1",
            "from pynput import keyboard",
            "pynput is not importable",
            "exit 1",
        ]:
            if marker not in macos_script_text:
                raise AssertionError(f"{script_name} dependency diagnostics marker missing {marker}")
        if "python3 -m pip install --user -r requirements.txt >/dev/null 2>&1" in macos_script_text:
            raise AssertionError(f"{script_name} must not hide dependency install errors")
        if "/usr/bin/python3" in macos_script_text:
            raise AssertionError(f"{script_name} must not hardcode a different Python than the dependency installer")

    install_macos_text = (agent_dir / "install_macos.command").read_text(encoding="utf-8")
    run_macos_text = (agent_dir / "run_macos.command").read_text(encoding="utf-8")
    if "<string>${PYTHON_BIN}</string>" not in install_macos_text:
        raise AssertionError("macOS LaunchAgent must use the same Python that installed dependencies")
    if '"$PYTHON_BIN" button_restart_agent.py >/dev/null 2>&1 &' not in run_macos_text:
        raise AssertionError("macOS manual runner must use the same Python that installed dependencies")

    zip_path = agent_dir / "button-restart-agent.zip"
    if not zip_path.exists():
        return

    with ZipFile(zip_path) as archive:
        archive_names = set(archive.namelist())
        if "button_restart_agent.config.json" in archive_names:
            raise AssertionError("button restart zip must not contain private config")
        for required_name in (
            "button_restart_agent.config.example.json",
            "windows_button_agent.ps1",
        ):
            if required_name not in archive_names:
                raise AssertionError(f"button restart zip must contain {required_name}")
        for name in archive_names:
            if name.endswith("/"):
                continue
            source_path = agent_dir / name
            if not source_path.exists():
                raise AssertionError(f"button restart zip contains missing source {name}")
            if archive.read(name) != source_path.read_bytes():
                raise AssertionError(f"button restart zip is stale for {name}")


def test_button_restart_agent_config_is_tolerant():
    agent_path = ROOT / "tools" / "button_restart_agent" / "button_restart_agent.py"
    spec = importlib.util.spec_from_file_location("button_restart_agent_smoke", agent_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        module.CONFIG_PATH = tmp_path / "button_restart_agent.config.json"
        module.LOG_PATH = tmp_path / "button_restart_agent.log"

        module.CONFIG_PATH.write_text("{broken json", encoding="utf-8")
        config = module.load_config()
        if config.get("token") != "put-token-here":
            raise AssertionError("button restart agent must fall back to defaults for broken config JSON")
        if "config JSON parse failed" not in module.LOG_PATH.read_text(encoding="utf-8"):
            raise AssertionError("button restart agent must log broken config JSON")

        module.config = {"cooldown_seconds": "not-a-number"}
        if module.get_cooldown_seconds() != 30:
            raise AssertionError("button restart agent must tolerate invalid cooldown_seconds")


def test_client_menu_hides_orders_in_delivery():
    keyboard_text = (ROOT / "bot" / "keyboard.py").read_text(encoding="utf-8")
    client_menu = keyboard_text.split("def client_main_menu():", 1)[1].split("def worker_main_menu():", 1)[0]
    if "🚗 Заказы в доставке" in client_menu:
        raise AssertionError("client menu must not show orders in delivery button")


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
        "SELECT post_id AS id FROM delivery_collection_items WHERE post_id > 0",
        "SELECT post_id AS id FROM missing_delivery_items WHERE post_id > 0",
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
        "ForDelivery.id == DeliveryCollectionItem.delivery_id",
        "MissingDeliveryItem.status == MISSING_DELIVERY_STATUS_MISSING",
    ]:
        if marker not in main_text:
            raise AssertionError(f"post active-link marker missing {marker}")

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
        "def build_item_list_caption(description, price, quantity, created_at, post_id=None, author=None, show_quantity=True):",
        "Id товара: {post_id}",
        "f\"Id товара: {item.post_id}\"",
        "for unit_index in range(quantity):",
        '"show_quantity": False',
        "def build_delivery_collection_item_caption(item):",
    ]:
        if marker not in main_text:
            raise AssertionError(f"post id display marker missing {marker}")


def test_post_creation_state_updates_are_atomic():
    main_text = MAIN.read_text(encoding="utf-8")
    session_store_text = (ROOT / "services" / "session_store.py").read_text(encoding="utf-8")
    bot_session_text = (ROOT / "db" / "bot_session.py").read_text(encoding="utf-8")

    for marker in [
        "def update_bucket_dict(user_id, bucket_name, updates=None, delete_keys=None):",
        ".with_for_update()",
        "def update_dict(self, key, updates=None, delete_keys=None):",
        "latest = self._parent.update_dict(self._key, {key: value})",
    ]:
        if marker not in session_store_text + bot_session_text:
            raise AssertionError(f"atomic session update marker missing {marker}")

    for marker in [
        "def get_temp_post_payload(user_id):",
        "def get_temp_post_id_or_reset(user_id):",
        "photos = getattr(message, \"photo\", None) or []",
        "if not photos:",
        "temp_post_data.update_dict(message.chat.id, {\"photo\": photos[-1].file_id})",
        "temp_post_data.update_dict(chat_id, {\"price\": text})",
        "temp_post_data.update_dict(chat_id, {\"description\": description})",
        "data = dict(temp_post_data.update_dict(chat_id, {\"quantity\": quantity}))",
        "Сначала отправьте фото товара.",
    ]:
        if marker not in main_text:
            raise AssertionError(f"post creation state marker missing {marker}")

    forbidden = [
        'temp_post_data[message.chat.id]["photo"]',
        'temp_post_data[chat_id]["price"]',
        'temp_post_data[chat_id]["description"]',
        'temp_post_data[chat_id]["quantity"]',
        'temp_post_data[user_id]["post_id"]',
    ]
    for marker in forbidden:
        if marker in main_text:
            raise AssertionError(f"post flow must not use non-atomic temp_post_data access: {marker}")


def test_temp_user_data_flows_reset_when_memory_is_missing():
    main_text = MAIN.read_text(encoding="utf-8")
    for marker in [
        "def get_temp_user_data_or_reset(user_id, required_fields=None, notice_chat_id=None, notice_text=None):",
        "Temporary user flow data missing",
        "temp_user_data.pop(user_id, None)",
        "clear_user_state(user_id)",
    ]:
        if marker not in main_text:
            raise AssertionError(f"temp user data reset helper marker missing {marker}")

    guarded_blocks = {
        "def handle_defective_sum_entry(message):": [
            'required_fields=("user_id",)',
            "flow_data[\"user_id\"]",
            "Сессия оформления брака потерялась",
        ],
        "def handle_reservation_selection(call):": [
            'required_fields=("defective_sum", "user_id")',
            "flow_data[\"defective_sum\"]",
            "flow_data[\"user_id\"]",
        ],
        "def handle_data_editing(call):": [
            'required_fields=("name", "phone", "address", "final_sum", "delivery_cutoff_at")',
            "Данные доставки потерялись",
        ],
        "def handle_new_address(message):": [
            'required_fields=("name", "phone", "final_sum", "delivery_cutoff_at")',
            "flow_data[\"address\"] = new_address",
        ],
        "def handle_new_phone(message):": [
            'required_fields=("name", "phone", "address", "final_sum", "delivery_cutoff_at")',
            "flow_data[\"phone\"] = new_phone",
        ],
        "def handle_confirmation(call):": [
            'required_fields=("name", "phone", "address", "final_sum", "delivery_cutoff_at")',
            "Данные доставки потерялись. Нажмите согласие на доставку и заполните данные заново.",
        ],
        "def handle_delivery_cart_merge_extra_input(message):": [
            'required_fields=("main_phone",)',
            "Данные объединения потерялись. Нажмите «Объединить корзины» заново.",
        ],
    }
    for signature, markers in guarded_blocks.items():
        block = main_text.split(signature, 1)[1].split("\n@bot.", 1)[0]
        if "get_temp_user_data_or_reset(" not in block:
            raise AssertionError(f"temp user data guard missing in {signature}")
        for marker in markers:
            if marker not in block:
                raise AssertionError(f"temp user data guard marker missing in {signature}: {marker}")

    forbidden = [
        'temp_user_data[message.chat.id]["user_id"]',
        'temp_user_data[message.chat.id]["defective_sum"]',
        'temp_user_data[call.message.chat.id]["defective_sum"]',
        'temp_user_data[call.message.chat.id]["user_id"]',
        'temp_user_data[user_id].get("name"',
        'temp_user_data[user_id].get("phone"',
        'temp_user_data[user_id].get("address"',
        'temp_user_data[user_id].get("final_sum"',
        'temp_user_data[user_id].get("delivery_cutoff_at"',
    ]
    for marker in forbidden:
        if marker in main_text:
            raise AssertionError(f"state flow must not read temp_user_data directly: {marker}")


def test_telegram_startup_preflight_markers():
    main_text = MAIN.read_text(encoding="utf-8")
    telegram_safe_text = (ROOT / "services" / "telegram_safe.py").read_text(encoding="utf-8")
    for marker in [
        "TELEGRAM_STARTUP_RETRY_SECONDS",
        "TELEGRAM_STARTUP_MAX_RETRY_SECONDS",
        "TELEGRAM_API_RETRY_ON_ERROR",
        "TELEGRAM_API_MAX_RETRIES",
        "TELEGRAM_API_RETRY_TIMEOUT_SECONDS",
        "TELEGRAM_POLLING_TIMEOUT_SECONDS",
        "TELEGRAM_LONG_POLLING_TIMEOUT_SECONDS",
        "def configure_telegram_api_retries():",
        "apihelper.RETRY_ON_ERROR = TELEGRAM_API_RETRY_ON_ERROR",
        "class TelegramPollingExceptionHandler",
        "class TelegramTokenRedactingFormatter",
        "sanitize_telegram_error_text(super().format(record))",
        "log_handler.setFormatter(TelegramTokenRedactingFormatter(LOG_FORMAT))",
        "is_transient_telegram_error(exception)",
        "sanitize_telegram_error_text(exception)",
        "exception_handler=TelegramPollingExceptionHandler()",
        "def wait_for_telegram_api():",
        "username = bot.user.username",
        "if not is_transient_telegram_error(exc):",
        'logger.exception("Telegram API preflight failed with non-transient error")',
        "Telegram API is unavailable before polling",
        "sanitize_telegram_error_text(exc)",
        "wait_for_telegram_api()",
        "timeout=TELEGRAM_POLLING_TIMEOUT_SECONDS",
        "long_polling_timeout=TELEGRAM_LONG_POLLING_TIMEOUT_SECONDS",
        'logger.warning(\n                    "Bot polling transient failure: %s. Retrying in %s seconds."',
        'logger.exception("Bot polling failed unexpectedly. Retrying in %s seconds.", retry_delay)',
    ]:
        if marker not in main_text:
            raise AssertionError(f"telegram startup preflight marker missing {marker}")

    for marker in [
        "TELEGRAM_BOT_URL_TOKEN_RE",
        "TELEGRAM_TOKEN_RE",
        "def sanitize_telegram_error_text(",
    ]:
        if marker not in telegram_safe_text:
            raise AssertionError(f"telegram token redaction marker missing {marker}")


def test_telegram_error_text_redacts_tokens():
    raw = (
        "HTTPSConnectionPool(host='api.telegram.org', port=443): "
        "Max retries exceeded with url: /bot123456789:AAAbbb_CCC-dddEEEfffGGGhhhIIIjjj/getUpdates"
    )
    redacted = sanitize_telegram_error_text(raw)
    if "123456789:AAAbbb" in redacted or "/bot123456789:" in redacted:
        raise AssertionError("telegram token was not redacted")
    if "bot<telegram-token>" not in redacted:
        raise AssertionError("telegram token redaction marker missing from sanitized text")


def test_telegram_bad_gateway_is_transient():
    class TelegramLikeError(Exception):
        def __str__(self):
            return (
                "A request to the Telegram API was unsuccessful. "
                "Error code: 502. Description: Bad Gateway"
            )

    if not is_transient_telegram_error(TelegramLikeError()):
        raise AssertionError("Telegram 502 Bad Gateway must be treated as a transient error")


def _call_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return f"{node.value.id}.{node.attr}"
    return ""


def _contains_exception_detail(node):
    exception_names = {"e", "exc", "exception"}
    for child in ast.walk(node):
        if isinstance(child, ast.FormattedValue):
            value = child.value
            if isinstance(value, ast.Name) and value.id in exception_names:
                return True
            if (
                isinstance(value, ast.Call)
                and _call_name(value.func) == "str"
                and value.args
                and isinstance(value.args[0], ast.Name)
                and value.args[0].id in exception_names
            ):
                return True
        if (
            isinstance(child, ast.Call)
            and _call_name(child.func) == "str"
            and child.args
            and isinstance(child.args[0], ast.Name)
            and child.args[0].id in exception_names
        ):
            return True
    return False


def test_user_facing_errors_do_not_expose_exception_details():
    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
    user_facing_calls = {
        "bot.send_message",
        "safe_send_message",
        "safe_answer_callback_query",
        "notify_publish_status",
    }
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _call_name(node.func) not in user_facing_calls:
            continue
        values = list(node.args) + [keyword.value for keyword in node.keywords]
        if any(_contains_exception_detail(value) for value in values):
            offenders.append(node.lineno)

    if offenders:
        lines = ", ".join(str(line) for line in offenders[:20])
        raise AssertionError(f"user-facing messages expose exception details on lines: {lines}")

    posts_text = (ROOT / "db" / "posts.py").read_text(encoding="utf-8")
    delete_block = posts_text.split("def delete_row(post_id: int):", 1)[1].split(
        "@staticmethod",
        1,
    )[0]
    for forbidden in [
        "str(e)",
        "str(exc)",
        "{e}",
        "{exc}",
    ]:
        if forbidden in delete_block:
            raise AssertionError(f"post delete user-facing error must not expose exception details: {forbidden}")
    for marker in [
        "logger.exception(\"Post photo delete failed",
        "logger.exception(\"Post row delete failed",
        "session.rollback()",
        "return False, \"Ошибка при удалении фотографии.\"",
        "return False, \"Ошибка при удалении поста.\"",
    ]:
        if marker not in delete_block:
            raise AssertionError(f"post delete safe error marker missing: {marker}")


def test_exception_details_are_not_printed_directly():
    paths = [
        MAIN,
        ROOT / "statistic_api.py",
        ROOT / "button_control_api.py",
        *sorted((ROOT / "db").glob("*.py")),
        *sorted((ROOT / "handlers").glob("*.py")),
        *sorted((ROOT / "services").glob("*.py")),
    ]
    offenders = []
    for path in paths:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _call_name(node.func) != "print":
                continue
            source = ast.get_source_segment(text, node) or ""
            lowered = source.lower()
            if any(marker in source for marker in ("{e}", "{ex}", "{exc}")) or "ошибка" in lowered or "exception" in lowered:
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")

    if offenders:
        raise AssertionError("exception details must use logger, not print: " + ", ".join(offenders[:20]))


def test_runtime_code_does_not_print_directly():
    paths = [
        MAIN,
        ROOT / "statistic_api.py",
        ROOT / "button_control_api.py",
        *sorted((ROOT / "db").glob("*.py")),
        *sorted((ROOT / "handlers").glob("*.py")),
        *sorted((ROOT / "services").glob("*.py")),
    ]
    offenders = []
    for path in paths:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _call_name(node.func) == "print":
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")

    if offenders:
        raise AssertionError("runtime code must use logger instead of print: " + ", ".join(offenders[:20]))


def test_runtime_code_preserves_exception_tracebacks():
    paths = [
        MAIN,
        ROOT / "statistic_api.py",
        ROOT / "button_control_api.py",
        *sorted((ROOT / "db").glob("*.py")),
        *sorted((ROOT / "handlers").glob("*.py")),
        *sorted((ROOT / "services").glob("*.py")),
    ]
    offenders = []
    for path in paths:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Name):
                if node.exc.id in {"e", "exc", "exception"}:
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
            if (
                isinstance(node, ast.Raise)
                and isinstance(node.exc, ast.Call)
                and _call_name(node.exc.func) == "Exception"
            ):
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")

    if offenders:
        raise AssertionError(
            "use bare raise or a specific chained exception to preserve traceback: "
            + ", ".join(offenders[:20])
        )


def test_main_does_not_swallow_exceptions_silently():
    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        if node.type is None:
            continue
        exception_name = _call_name(node.type)
        if exception_name != "Exception":
            continue
        if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
            offenders.append(node.lineno)

    if offenders:
        lines = ", ".join(str(line) for line in offenders)
        raise AssertionError(f"main.py silently swallows Exception on lines: {lines}")


def test_exception_handlers_do_not_call_telegram_directly():
    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        for child in ast.walk(node):
            if not isinstance(child, ast.Call):
                continue
            name = _call_name(child.func)
            if name.startswith("bot.") or name.startswith("apihelper."):
                offenders.append(f"{child.lineno}:{name}")

    if offenders:
        raise AssertionError("exception handlers must use safe Telegram wrappers: " + ", ".join(offenders[:20]))


def test_state_membership_checks_are_safe_for_dict_states():
    main_text = MAIN.read_text(encoding="utf-8")
    if "def user_state_in(chat_id, expected_states):" not in main_text:
        raise AssertionError("safe user state membership helper missing")
    if "return isinstance(state, str) and state in expected_states" not in main_text:
        raise AssertionError("safe user state membership helper must ignore dict states")
    if "get_user_state(message.chat.id) in {" in main_text:
        raise AssertionError("message handlers must not compare possibly-dict state with set membership directly")


def test_auto_fulfill_avoids_lock_contention_markers():
    main_text = MAIN.read_text(encoding="utf-8")
    for marker in [
        "reservation_auto_fulfill_lock = threading.Lock()",
        "if not reservation_auto_fulfill_lock.acquire(blocking=False):",
        "Auto-fulfill skipped: another auto-fulfill run is active",
        ").with_for_update(skip_locked=True).all()",
        "reservation_auto_fulfill_lock.release()",
        "with session.no_autoflush:",
    ]:
        if marker not in main_text:
            raise AssertionError(f"auto-fulfill lock contention marker missing {marker}")


def test_reservation_creation_retries_transient_database_errors():
    main_text = MAIN.read_text(encoding="utf-8")
    for marker in [
        'RESERVATION_DB_RETRY_ATTEMPTS = env_int("RESERVATION_DB_RETRY_ATTEMPTS", 5, minimum=1)',
        'TRANSIENT_DATABASE_SQLSTATES = {"40001", "40P01", "55P03"}',
        "def database_error_sqlstate(exc):",
        "def is_transient_database_error(exc):",
        "def sleep_before_database_retry(attempt):",
        "for attempt in range(1, RESERVATION_DB_RETRY_ATTEMPTS + 1):",
        "with_for_update(nowait=True).first()",
        "Reservation transient database error user_id=%s post_id=%s attempt=%s/%s: %s",
        "sleep_before_database_retry(attempt)",
        "Сервер занят. Попробуйте забронировать ещё раз через несколько секунд.",
    ]:
        if marker not in main_text:
            raise AssertionError(f"reservation database retry marker missing {marker}")

    reservation_block = main_text.split("def handle_reservation(call):", 1)[1].split(
        "# Получение бронирования пользователя",
        1,
    )[0]
    if "if reservation_committed:" not in reservation_block:
        raise AssertionError("reservation retry must keep the committed guard")
    assert_marker_order(
        reservation_block,
        "if reservation_committed:",
        "if is_transient_database_error(exc) and attempt < RESERVATION_DB_RETRY_ATTEMPTS:",
        "reservation retry after commit guard",
    )
    if "continue" not in reservation_block:
        raise AssertionError("reservation transient database errors must continue to the next attempt")


class RaisingBot:
    def __init__(self, exc):
        self.exc = exc

    def delete_message(self, **kwargs):
        raise self.exc


def test_telegram_safe_helpers():
    telegram_safe_text = (ROOT / "services" / "telegram_safe.py").read_text(encoding="utf-8")
    for marker in [
        "def edit_message_media_with_retries(",
        "Telegram edit_message_media",
        "lambda: bot.edit_message_media(**kwargs)",
    ]:
        if marker not in telegram_safe_text:
            raise AssertionError(f"telegram media edit retry helper marker missing {marker}")

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


def test_media_edits_use_retry_wrapper():
    main_text = MAIN.read_text(encoding="utf-8")
    for marker in [
        "edit_message_media_with_retries(",
        "media=InputMediaPhoto(media=post.photo, caption=caption)",
        "media=InputMediaPhoto(photo, caption=text)",
        "media=InputMediaAnimation(gif, caption=text)",
    ]:
        if marker not in main_text:
            raise AssertionError(f"media edit retry marker missing {marker}")

    if "bot.edit_message_media(" in main_text:
        raise AssertionError("main.py must use edit_message_media_with_retries instead of bot.edit_message_media")

    order_details_block = main_text.split("def order_details(call):", 1)[1].split(
        "def cancel_unfulfilled_reservation(",
        1,
    )[0]
    for marker in [
        "callback_answered = False",
        "callback_answered = True",
        "if is_message_not_modified_error(e):",
        "Order details were already up to date",
        "Не удалось открыть заказ. Попробуйте ещё раз.",
    ]:
        if marker not in order_details_block:
            raise AssertionError(f"order details media edit no-op marker missing {marker}")


def test_admin_cart_display_uses_safe_telegram_sends():
    main_text = MAIN.read_text(encoding="utf-8")
    clients_text = (ROOT / "db" / "clients.py").read_text(encoding="utf-8")
    for marker in [
        "def get_row_by_phone_digits(phone_digits):",
        "if len(phone_digits) != 4:",
        "return []",
    ]:
        if marker not in clients_text:
            raise AssertionError(f"client phone-digits validation marker missing {marker}")

    defective_phone_block = main_text.split("def search_user_for_defective(message):", 1)[1].split(
        "# Обработка действия (подтверждения или отмены)",
        1,
    )[0]
    for marker in [
        "if not last_digits.isdigit() or len(last_digits) != 4:",
        "Введите корректные последние 4 цифры номера телефона (4 цифры).",
        "return",
    ]:
        if marker not in defective_phone_block:
            raise AssertionError(f"defective phone search validation marker missing {marker}")

    phone_digits_block = main_text.split("def show_cart_by_last_phone_digits(", 1)[1].split(
        "def deduplicate_clients_by_full_phone(",
        1,
    )[0]
    for marker in [
        'safe_send_message(\n            bot,\n            message.chat.id,\n            "Пользователи с такими последними цифрами номера телефона не найдены."',
        "Найдено несколько клиентов с такими последними цифрами. Выберите нужного:",
        "logger=logger",
    ]:
        if marker not in phone_digits_block:
            raise AssertionError(f"admin cart phone search safe-send marker missing {marker}")

    show_cart_block = main_text.split("def show_cart_for_client(", 1)[1].split(
        "def build_cart_item_caption(",
        1,
    )[0]
    for marker in [
        "safe_send_message(",
        "Корзина пользователя {client.name} пуста.",
        "logger=logger",
    ]:
        if marker not in show_cart_block:
            raise AssertionError(f"admin cart header safe-send marker missing {marker}")
    if "bot.send_message(" in show_cart_block:
        raise AssertionError("show_cart_for_client must use safe_send_message")

    cart_content_block = main_text.split("def send_cart_content(", 1)[1].split(
        "def handle_admin_remove_cart_item(",
        1,
    )[0]
    for marker in [
        "try:",
        "send_photo_or_text(bot, chat_id, post.photo, caption, reply_markup=item_markup)",
        "Admin cart item send failed chat_id=%s reservation_id=%s post_id=%s: %s",
        "Не удалось отправить фото товара. Подробности записаны в лог.",
        "safe_send_message(bot, chat_id, \"Выберите действие:\", reply_markup=markup, logger=logger)",
    ]:
        if marker not in cart_content_block:
            raise AssertionError(f"admin cart content safe-send marker missing {marker}")
    if "bot.send_message(" in cart_content_block:
        raise AssertionError("send_cart_content must use safe_send_message")


def test_channel_and_reserved_group_edits_use_retry_wrappers():
    main_text = MAIN.read_text(encoding="utf-8")

    delivered_delete_block = main_text.split("def delete_delivered_channel_post_message(", 1)[1].split(
        "def build_reservation_stats_text(",
        1,
    )[0]
    for marker in [
        "call_telegram_with_retries(",
        "Telegram delivered channel post delete",
        'telegram_error_contains(exc, "message to delete not found")',
    ]:
        if marker not in delivered_delete_block:
            raise AssertionError(f"delivered channel delete retry marker missing {marker}")

    live_stats_block = main_text.split("def update_live_reservation_stats(", 1)[1].split(
        "@bot.callback_query_handler(func=lambda call: callback_data_startswith(call, \"reservation_stats_close_\"))",
        1,
    )[0]
    for marker in [
        "Telegram live reservation stats edit",
        "safe_send_message(bot, message.chat.id, \"У вас нет прав доступа к этой функции.\"",
        "if not sent_message:",
    ]:
        if marker not in live_stats_block:
            raise AssertionError(f"live reservation stats safe Telegram marker missing {marker}")

    close_stats_block = main_text.split("def close_live_reservation_stats(", 1)[1].split(
        "@bot.message_handler",
        1,
    )[0]
    for marker in [
        "Telegram live reservation stats close edit",
        "chat_id = callback_message_chat_id(call)",
        "message_id = getattr(getattr(call, \"message\", None), \"message_id\", None)",
        "Live reservation stats close skipped: missing callback message",
        "safe_send_message(",
        "Главное меню",
    ]:
        if marker not in close_stats_block:
            raise AssertionError(f"live reservation stats close marker missing {marker}")

    reserved_group_block = main_text.split("def edit_reserved_group_message(", 1)[1].split(
        "def send_reserved_group_message(",
        1,
    )[0]
    for marker in [
        "Telegram reserved group caption edit",
        "Telegram reserved group text edit",
        "Telegram reserved group fallback text edit",
        "call_telegram_with_retries(",
    ]:
        if marker not in reserved_group_block:
            raise AssertionError(f"reserved group edit retry marker missing {marker}")


def test_delivery_archive_document_send_is_safe():
    main_text = MAIN.read_text(encoding="utf-8")
    telegram_safe_text = (ROOT / "services" / "telegram_safe.py").read_text(encoding="utf-8")

    for marker in [
        "def safe_send_document(",
        "Telegram send_document",
        "lambda: bot.send_document(chat_id=chat_id, document=document, **kwargs)",
    ]:
        if marker not in telegram_safe_text:
            raise AssertionError(f"safe send_document helper marker missing {marker}")

    archive_block = main_text.split("def archive_delivery_to_excel(", 1)[1].split(
        "@bot.callback_query_handler(func=lambda call: call.data in [\"archive_delivery_clear_yes\", \"archive_delivery_clear_no\"])",
        1,
    )[0]
    for marker in [
        "sent_archive = safe_send_document(bot, delivery_archive, document, logger=logger)",
        "if not sent_archive:",
        "Записи in_delivery не очищены.",
        "safe_send_message(",
        "Архив доставок отправлен в канал. Очистить таблицу in_delivery?",
    ]:
        if marker not in archive_block:
            raise AssertionError(f"delivery archive safe document marker missing {marker}")
    if "bot.send_document(" in main_text:
        raise AssertionError("main.py must use safe_send_document instead of bot.send_document")


def test_my_reservations_uses_safe_telegram_sends():
    main_text = MAIN.read_text(encoding="utf-8")
    block = main_text.split("def show_reservations(message):", 1)[1].split(
        "# Хэндлер для обработки нажатий на заказ",
        1,
    )[0]
    for marker in [
        "safe_send_message(",
        "safe_send_photo(bot, user_id, post.photo, caption=caption, logger=logger)",
        "if sent_photo:",
        "if sent_message:",
        "Не удалось показать фотографию товара.",
        "У вас пока нет заказов.",
    ]:
        if marker not in block:
            raise AssertionError(f"my reservations safe-send marker missing {marker}")
    for forbidden in [
        "bot.send_photo(",
        "bot.send_message(",
    ]:
        if forbidden in block:
            raise AssertionError(f"my reservations must use safe wrappers instead of {forbidden}")


def test_direct_send_photo_is_limited_to_channel_retry_loop():
    main_text = MAIN.read_text(encoding="utf-8")
    tree = ast.parse(main_text)
    offenders = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for child in ast.walk(node):
            if isinstance(child, ast.Call) and _call_name(child.func) == "bot.send_photo":
                if node.name != "send_channel_post_with_retries":
                    offenders.append(f"{node.name}:{child.lineno}")

    if offenders:
        raise AssertionError("bot.send_photo must use safe_send_photo outside channel retry loop: " + ", ".join(offenders))

    channel_send_block = main_text.split("def send_channel_post_with_retries(", 1)[1].split(
        "def publish_unsent_posts_to_channel(",
        1,
    )[0]
    for marker in [
        "for attempt in range(1, CHANNEL_POST_SEND_ATTEMPTS + 1):",
        "if not post.photo:",
        "return bot.send_message(",
        "return bot.send_photo(",
        "telegram_retry_after_seconds(exc)",
        "is_transient_telegram_error(exc)",
    ]:
        if marker not in channel_send_block:
            raise AssertionError(f"channel send retry marker missing {marker}")


def test_direct_telegram_calls_are_limited_to_retry_helpers():
    main_text = MAIN.read_text(encoding="utf-8")
    tree = ast.parse(main_text)
    direct_method_names = {
        "send_message",
        "send_photo",
        "edit_message_text",
        "edit_message_caption",
        "delete_message",
        "send_document",
        "send_animation",
    }
    allowed_functions = {
        "edit_channel_post_caption",
        "edit_channel_post_text",
        "delete_delivered_channel_post_message",
        "update_live_reservation_stats",
        "close_live_reservation_stats",
        "edit_reserved_group_message",
        "send_channel_post_with_retries",
        "send_delivery_offer_message",
        "send_delivery_collection_report_message",
        "edit_or_send_text_page",
    }
    offenders = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for child in ast.walk(node):
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and child.func.attr in direct_method_names
            ):
                if node.name not in allowed_functions:
                    offenders.append(f"{node.name}:{child.lineno}:{_call_name(child.func)}")

    if offenders:
        raise AssertionError("direct Telegram calls must stay inside retry helpers: " + ", ".join(offenders[:30]))


def test_access_guard_helpers_use_safe_send_message():
    main_text = MAIN.read_text(encoding="utf-8")
    for signature, expected_text in [
        ("def require_role(message, roles):", "У вас недостаточно прав для этой команды."),
        ("def require_creator(message):", "Эта команда доступна только создателю."),
    ]:
        block = main_text.split(signature, 1)[1].split("\n\ndef ", 1)[0]
        if "safe_send_message(" not in block or expected_text not in block or "logger=logger" not in block:
            raise AssertionError(f"access guard helper must use safe_send_message: {signature}")
        if "bot.send_message(" in block:
            raise AssertionError(f"access guard helper must not call bot.send_message directly: {signature}")


def test_common_user_flows_use_safe_send_message():
    main_text = MAIN.read_text(encoding="utf-8")
    tree = ast.parse(main_text)
    target_functions = {
        "notify_publish_status",
        "send_daily_reservation_stats_report",
        "list_unsent_posts",
        "handle_start",
        "my_orders",
        "send_broadcast",
        "handle_registration",
        "handle_name_registration",
        "handle_phone_registration",
        "confirm_phone_registration",
        "complete_registration",
        "handle_enqueue",
        "go_back_to_menu",
        "show_delivery_orders",
        "manage_user",
        "show_role_controls",
        "process_user_input",
        "manage_clients",
        "defective_order",
        "ask_defective_confirmation",
        "search_user_for_defective",
        "handle_defective_action",
        "handle_defective_sum_entry",
        "handle_reservation_selection",
        "send_all_reserved_to_group",
        "handle_clear_cart",
        "request_phone_last_digits",
        "handle_delivery_management",
        "show_delivery_clients_summary",
        "handle_phone_input",
        "handle_admin_remove_cart_item",
        "handle_clear_processed",
        "handle_clear_full_cart",
        "handle_clear_without_channel_return",
        "delete_client_by_phone",
        "process_delete_client_phone",
        "handle_set_role",
        "get_temp_post_id_or_reset",
        "create_new_post",
        "handle_photo",
        "handle_post_details",
        "manage_posts",
        "edit_post",
        "edit_post_price",
        "edit_post_description",
        "edit_post_quantity",
        "go_back",
        "send_new_posts_to_channel",
        "start_phoenix_broadcast",
        "handle_address_input",
        "handle_empty_delivery_command",
        "send_delivery_candidates_summary",
        "push_in_delivery",
        "process_numbers",
        "handle_new_address",
        "handle_new_phone",
        "handle_confirmation",
        "send_queue_transfer_notifications",
        "send_delivery_reserved_group_snapshot",
        "collect_delivery",
        "handle_delivery_collected_sum",
        "confirm_delivery",
        "audit_menu",
        "manage_audit_posts",
        "answer_manual_audit_disabled",
        "show_posts_by_date",
        "cancel_audit",
        "request_defect_reason",
        "handle_defect_reason",
        "handle_defect_action",
        "request_discount_amount",
        "handle_discount_amount",
        "handle_discount_confirmation",
        "contact_client",
    }
    found = set()
    offenders = []
    missing_safe_wrapper = []
    lines = main_text.splitlines()

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name not in target_functions:
            continue
        found.add(node.name)
        block = "\n".join(lines[node.lineno - 1 : node.end_lineno])
        if "safe_send_message(" not in block:
            missing_safe_wrapper.append(node.name)
        for child in ast.walk(node):
            if isinstance(child, ast.Call) and _call_name(child.func) == "bot.send_message":
                offenders.append(f"{node.name}:{child.lineno}")

    missing_functions = target_functions - found
    if missing_functions:
        raise AssertionError("common user flow functions missing: " + ", ".join(sorted(missing_functions)))
    if missing_safe_wrapper:
        raise AssertionError("common user flows must use safe_send_message: " + ", ".join(sorted(missing_safe_wrapper)))
    if offenders:
        raise AssertionError("common user flows must not call bot.send_message directly: " + ", ".join(offenders))


def test_silent_blacklist_markers():
    main_text = MAIN.read_text(encoding="utf-8")
    db_init_text = (ROOT / "db" / "__init__.py").read_text(encoding="utf-8")
    black_list_text = (ROOT / "db" / "black_list.py").read_text(encoding="utf-8")
    handler_text = (ROOT / "handlers" / "black_list.py").read_text(encoding="utf-8")

    for marker in [
        "silent_block = mapped_column(Boolean",
        "def set_silent_block",
        "def is_silent_blocked",
    ]:
        if marker not in black_list_text:
            raise AssertionError(f"silent blacklist model marker missing {marker}")

    for marker in [
        '"007_black_list_silent_block"',
        'add_column_if_missing("black_list", "silent_block"',
        "ix_black_list_silent_block",
    ]:
        if marker not in db_init_text:
            raise AssertionError(f"silent blacklist migration marker missing {marker}")

    if "def is_user_silent_blocked" not in handler_text:
        raise AssertionError("silent blacklist handler helper missing")

    for marker in [
        "def ignore_silent_blocked_message",
        "def ignore_silent_blocked_callback",
        "def handle_reservation(call):",
        "def my_orders(message):",
        "def cancel_reservation(call):",
        "def handle_delivery_response_callback(call):",
        "def defect(message):",
    ]:
        if marker not in main_text:
            raise AssertionError(f"silent blacklist main marker missing {marker}")

    for handler_signature in [
        "def handle_reservation(",
        "def show_my_orders(",
        "def my_orders(",
        "def paginate_orders(",
        "def cancel_reservation(",
        "def handle_enqueue(",
        "def handle_delivery_response_callback(",
        "def handle_address_input(",
        "def handle_confirmation(",
        "def defect(",
    ]:
        block = main_text.split(handler_signature, 1)[1].split("\n@bot.", 1)[0]
        if "ignore_silent_blocked_" not in block:
            raise AssertionError(f"silent blacklist guard missing in {handler_signature}")


def test_no_post_stats_api_tokens_are_hardcoded():
    secret_patterns = [
        re.compile(r"POST_STATS_API_TOKEN\s*=\s*['\"][A-Za-z0-9_-]{30,}['\"]"),
        re.compile(r"STATISTIC_API_TOKEN\s*=\s*['\"][A-Za-z0-9_-]{30,}['\"]"),
    ]
    skipped_dirs = {".git", ".gradle", "build", "__pycache__"}

    for path in ROOT.glob("**/*"):
        if not path.is_file():
            continue
        if skipped_dirs.intersection(path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if any(pattern.search(text) for pattern in secret_patterns):
            raise AssertionError(f"hardcoded post statistics API token found in {path.relative_to(ROOT)}")


def test_inline_callback_data_stays_within_telegram_limit():
    text = MAIN.read_text(encoding="utf-8")
    tree = ast.parse(text)
    max_dynamic_part_bytes = 20

    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and ast.unparse(node.func).endswith("InlineKeyboardButton")
        ):
            continue

        callback_node = None
        for keyword in node.keywords:
            if keyword.arg == "callback_data":
                callback_node = keyword.value
                break
        if callback_node is None:
            continue

        if isinstance(callback_node, ast.Constant) and isinstance(callback_node.value, str):
            estimated_bytes = len(callback_node.value.encode("utf-8"))
        elif isinstance(callback_node, ast.JoinedStr):
            estimated_bytes = 0
            for part in callback_node.values:
                if isinstance(part, ast.Constant) and isinstance(part.value, str):
                    estimated_bytes += len(part.value.encode("utf-8"))
                elif isinstance(part, ast.FormattedValue):
                    estimated_bytes += max_dynamic_part_bytes
                else:
                    raise AssertionError(
                        f"unsupported callback_data f-string part at line {node.lineno}"
                    )
        else:
            callback_source = ast.get_source_segment(text, callback_node) or ast.unparse(callback_node)
            raise AssertionError(f"callback_data length is not statically bounded at line {node.lineno}: {callback_source}")

        if estimated_bytes > 64:
            callback_source = ast.get_source_segment(text, callback_node) or ast.unparse(callback_node)
            raise AssertionError(
                f"callback_data can exceed Telegram 64-byte limit at line {node.lineno}: "
                f"{estimated_bytes} bytes for {callback_source}"
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
    test_cancel_reservation_returns_stock_and_refreshes_channel()
    test_callback_answers_are_safe()
    test_delivery_confirmation_callback_answers_before_early_returns()
    test_role_search_input_parsing_is_safe()
    test_delivery_phone_next_step_requires_text()
    test_state_text_handlers_reject_non_text_messages()
    test_phoenix_broadcast_markers()
    test_reservation_stats_markers()
    test_delivery_broadcast_campaign_markers()
    test_channel_post_auto_publish_markers()
    test_post_management_only_shows_unpublished_posts()
    test_order_pagination_recovers_missing_messages()
    test_client_update_for_work_handles_errors_safely()
    test_init_db_runs_on_clean_sqlite_database()
    test_post_dates_use_samara_and_shift_sunday()
    test_reservation_creation_is_atomic_and_missing_posts_do_not_queue()
    test_post_delete_and_zero_quantity_are_safe()
    test_delivery_move_and_archive_are_loss_safe()
    test_revision_excludes_linked_posts_and_logs_work()
    test_statistic_excludes_revision_posts_from_created_posts()
    test_delivery_cleanup_schedule_markers()
    test_midnight_posts_are_snapshotted_before_delete()
    test_cart_clear_processed_is_available()
    test_client_delete_uses_safe_cart_cleanup()
    test_removed_delivery_entries_clear_collection_retry_rows()
    test_channel_delete_happens_only_after_delivery_cleanup()
    test_delivery_clients_summary_markers()
    test_delivery_cart_merge_works_without_for_delivery_rows()
    test_delivery_collection_pauses_reserved_group_flow()
    test_audit_can_collect_delivery_only()
    test_delivery_collection_claim_markers()
    test_delivery_collection_item_and_missing_markers()
    test_delivery_sheet_amount_updates_are_additive_and_idempotent()
    test_core_declared_indexes_migration_markers()
    test_delivery_collection_report_group_markers()
    test_defect_pagination_and_menu_markers()
    test_statistic_api_security_and_query_scope()
    test_statistic_api_cors_origin_matching()
    test_statistic_api_database_url_connection()
    test_button_control_connection_noise_markers()
    test_auxiliary_systemd_services_are_hardened()
    test_button_restart_agent_zip_matches_sources()
    test_api_integer_env_parsing_is_tolerant()
    test_core_env_number_parsing_is_tolerant_by_construction()
    test_env_example_documents_runtime_options()
    test_button_restart_agent_config_is_tolerant()
    test_client_menu_hides_orders_in_delivery()
    test_post_id_labels_for_new_posts_and_delivery_collection()
    test_post_creation_state_updates_are_atomic()
    test_temp_user_data_flows_reset_when_memory_is_missing()
    test_telegram_startup_preflight_markers()
    test_state_membership_checks_are_safe_for_dict_states()
    test_auto_fulfill_avoids_lock_contention_markers()
    test_telegram_safe_helpers()
    test_media_edits_use_retry_wrapper()
    test_admin_cart_display_uses_safe_telegram_sends()
    test_channel_and_reserved_group_edits_use_retry_wrappers()
    test_delivery_archive_document_send_is_safe()
    test_my_reservations_uses_safe_telegram_sends()
    test_direct_send_photo_is_limited_to_channel_retry_loop()
    test_direct_telegram_calls_are_limited_to_retry_helpers()
    test_access_guard_helpers_use_safe_send_message()
    test_common_user_flows_use_safe_send_message()
    test_telegram_error_text_redacts_tokens()
    test_telegram_bad_gateway_is_transient()
    test_user_facing_errors_do_not_expose_exception_details()
    test_exception_details_are_not_printed_directly()
    test_runtime_code_does_not_print_directly()
    test_runtime_code_preserves_exception_tracebacks()
    test_main_does_not_swallow_exceptions_silently()
    test_exception_handlers_do_not_call_telegram_directly()
    test_silent_blacklist_markers()
    test_reservation_creation_retries_transient_database_errors()
    test_no_post_stats_api_tokens_are_hardcoded()
    test_inline_callback_data_stays_within_telegram_limit()
    print("smoke checks ok")


if __name__ == "__main__":
    main()
