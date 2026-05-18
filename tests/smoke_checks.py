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
    test_delivery_cutoff_markers()
    test_reserved_group_flow_markers()
    test_reservation_auto_fulfill_uses_local_time()
    test_channel_post_updates_are_centralized()
    test_sold_out_channel_delete_keeps_reserved_group()
    test_telegram_safe_helpers()
    print("smoke checks ok")


if __name__ == "__main__":
    main()
