from decimal import Decimal, ROUND_HALF_UP


def calculate_audit_price(price: int) -> int:
    price_value = Decimal(int(price or 0))
    discount = max(price_value * Decimal("0.10"), Decimal("50"))
    discounted_price = price_value - discount
    rounded_price = (discounted_price / Decimal("50")).quantize(
        Decimal("1"),
        rounding=ROUND_HALF_UP,
    ) * Decimal("50")
    return max(50, int(rounded_price))
