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


def test_delivery_broadcast_campaign_markers():
    main_text = MAIN.read_text(encoding="utf-8")
    db_init_text = (ROOT / "db" / "__init__.py").read_text(encoding="utf-8")
    campaign_text = (ROOT / "db" / "delivery_broadcast_campaigns.py").read_text(encoding="utf-8")
    recipient_text = (ROOT / "db" / "delivery_broadcast_recipients.py").read_text(encoding="utf-8")
    reservation_text = (ROOT / "db" / "reservations.py").read_text(encoding="utf-8")
    for_delivery_text = (ROOT / "db" / "for_delivery.py").read_text(encoding="utf-8")

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
        '"002_delivery_broadcast_campaigns"',
        '"003_delivery_cutoff_and_fulfilled_at"',
        "ensure_delivery_broadcast_campaigns",
        "ensure_delivery_cutoff_and_fulfilled_at",
    ]:
        if marker not in db_init_text:
            raise AssertionError(f"delivery broadcast migration marker missing {marker}")

    if "fulfilled_at" not in reservation_text:
        raise AssertionError("reservations fulfilled_at missing")
    if "delivery_cutoff_at" not in for_delivery_text:
        raise AssertionError("for_delivery delivery_cutoff_at missing")

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


def test_safe_cart_release_markers():
    text = MAIN.read_text(encoding="utf-8")
    for marker in [
        "def release_reservation_safely",
        "def release_reservations_for_users",
        "adjust_temp_fulfilled_for_released_reservation",
        "TempReservations.post_id == reservation.post_id",
        "post.quantity += reservation.quantity",
        "update_channel_post_message(Posts.get_row_by_id(channel_post_id))",
        "Товар передан следующему клиенту из очереди.",
        "Корзина клиента расформирована безопасно.",
        "Обработанные товары расформированы безопасно.",
    ]:
        if marker not in text:
            raise AssertionError(f"safe cart release marker missing {marker}")


def main():
    test_audit_prices()
    test_delivery_callbacks_are_namespaced()
    test_manual_audit_old_flow_removed()
    test_telegram_safe_helpers()
    test_delivery_broadcast_campaign_markers()
    test_safe_cart_release_markers()
    print("smoke checks ok")


if __name__ == "__main__":
    main()
