"""Institutional client-flow validation rules."""

from __future__ import annotations

from decimal import Decimal


MINIMUM_CLIENT_RFQ_NOTIONAL_USD = Decimal("500000")


class RFQBelowMinimumNotional(ValueError):
    """Raised when an RFQ is not strictly above the institutional threshold."""


def calculate_notional_usd(
    quantity_btc: Decimal, reference_price_usd: Decimal
) -> Decimal:
    if quantity_btc <= 0:
        raise ValueError("quantity_btc must be positive")
    if reference_price_usd <= 0:
        raise ValueError("reference_price_usd must be positive")
    return quantity_btc * reference_price_usd


def validate_client_rfq_notional(
    quantity_btc: Decimal, reference_price_usd: Decimal
) -> Decimal:
    """Return notional when it is strictly greater than USD 500,000."""

    notional_usd = calculate_notional_usd(quantity_btc, reference_price_usd)
    if notional_usd <= MINIMUM_CLIENT_RFQ_NOTIONAL_USD:
        raise RFQBelowMinimumNotional(
            "client RFQ notional must be strictly greater than USD 500,000"
        )
    return notional_usd
