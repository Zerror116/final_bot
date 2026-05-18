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


def test_reservation_sum_queries_have_explicit_from():
    text = (ROOT / "handlers" / "reservations_manage.py").read_text(encoding="utf-8")
    marker = ").select_from(\n            Reservations\n        ).join("
    if text.count(marker) < 2:
        raise AssertionError("reservation sum queries must select_from Reservations before join")
    if "func.coalesce(Reservations.old_price, Posts.price)" not in text:
        raise AssertionError("reservation sums must use reservation old_price")


def test_order_amount_uses_reserved_price():
    text = MAIN.read_text(encoding="utf-8")
    required = [
        "def calculate_order_amount(order, post):",
        "unit_price = order.old_price if order.old_price is not None else post.price",
        "(func.coalesce(Reservations.old_price, Posts.price) * Reservations.quantity)",
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
        "build_channel_post_markup(post)",
        "if post.quantity > 0:",
        "Channel post caption update failed",
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


def test_channel_post_auto_publish_markers():
    text = MAIN.read_text(encoding="utf-8")
    for marker in [
        'AUTO_CHANNEL_POST_HOURS = {10, 12, 14, 16}',
        'SAMARA_TZ = ZoneInfo("Europe/Samara")',
        "channel_post_publish_lock = threading.Lock()",
        "def publish_unsent_posts_to_channel(",
        "Отправка постов в канал уже выполняется",
        "def should_auto_publish_channel_posts(value):",
        "value.minute == 0",
        "def start_channel_post_auto_publish_worker():",
        "start_channel_post_auto_publish_worker()",
    ]:
        if marker not in text:
            raise AssertionError(f"channel post auto publish marker missing {marker}")


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
        "missing_reservations = [",
        "Перенос остановлен: у части броней удалены карточки товаров",
        "archive_delivery_clear_yes",
        "InDelivery.clear_table()",
    ]:
        if marker not in text:
            raise AssertionError(f"delivery loss-safety marker missing {marker}")


def test_cart_clear_processed_is_available():
    text = MAIN.read_text(encoding="utf-8")
    for marker in [
        "Расформировать обработанные",
        "def clear_processed(user_id):",
        "Reservations.is_fulfilled == True",
        "deleted_count = session.query(Reservations).filter(",
    ]:
        if marker not in text:
            raise AssertionError(f"cart clear processed marker missing {marker}")
    if "Обработанные товары удаляются только через" in text:
        raise AssertionError("cart clear processed warning must not be shown")


def test_sold_out_channel_delete_keeps_reserved_group():
    text = MAIN.read_text(encoding="utf-8")
    required = [
        "def delete_channel_post_if_fully_processed",
        "bot.delete_message(chat_id=CHANNEL_ID",
        "Reservations.is_fulfilled == False",
        "fulfilled_post_ids.add(reservation.post_id)",
        "delete_channel_post_if_fully_processed(post_id)",
    ]
    for marker in required:
        if marker not in text:
            raise AssertionError(f"sold-out channel deletion missing {marker}")

    helper = text.split("def delete_channel_post_if_fully_processed", 1)[1].split("def build_telegram_proxy_url", 1)[0]
    if "TARGET_GROUP_ID" in helper:
        raise AssertionError("sold-out channel deletion must not touch reserved group messages")


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
    test_reservation_sum_queries_have_explicit_from()
    test_order_amount_uses_reserved_price()
    test_delivery_cutoff_markers()
    test_reserved_group_flow_markers()
    test_reservation_auto_fulfill_uses_local_time()
    test_channel_post_updates_are_centralized()
    test_callback_answers_are_safe()
    test_phoenix_broadcast_markers()
    test_channel_post_auto_publish_markers()
    test_reservation_creation_is_atomic_and_missing_posts_do_not_queue()
    test_post_delete_and_zero_quantity_are_safe()
    test_delivery_move_and_archive_are_loss_safe()
    test_cart_clear_processed_is_available()
    test_sold_out_channel_delete_keeps_reserved_group()
    test_telegram_safe_helpers()
    print("smoke checks ok")


if __name__ == "__main__":
    main()
