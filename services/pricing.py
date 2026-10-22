from decimal import Decimal, ROUND_HALF_UP

AUDIT_MIN_PRICE = 100
AUDIT_MIN_DISCOUNT = 50
AUDIT_PRICE_STEP = 50


def calculate_audit_price(price: int) -> int:
    price_value = Decimal(int(price or 0))
    discount = max(price_value * Decimal("0.10"), Decimal(AUDIT_MIN_DISCOUNT))
    discounted_price = price_value - discount
    rounded_price = (discounted_price / Decimal(AUDIT_PRICE_STEP)).quantize(
        Decimal("1"),
        rounding=ROUND_HALF_UP,
    ) * Decimal(AUDIT_PRICE_STEP)
    return max(AUDIT_MIN_PRICE, int(rounded_price))
