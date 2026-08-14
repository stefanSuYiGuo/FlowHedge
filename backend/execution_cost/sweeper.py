"""Deterministic normalized L2 book sweeping."""

from __future__ import annotations

from decimal import Decimal

from ..market.models import ExecutableOrderBook
from .models import (
    BookSweepResult,
    ExecutionSide,
    SimulatedExecutionFill,
)


def sweep_executable_book(
    book: ExecutableOrderBook,
    side: ExecutionSide,
    quantity_btc: Decimal,
) -> BookSweepResult:
    """Sweep asks for BUY or bids for SELL, partially consuming the last level."""

    if quantity_btc <= 0:
        raise ValueError("requested BTC-equivalent quantity must be positive")

    levels = book.asks if side is ExecutionSide.BUY else book.bids
    remaining = quantity_btc
    fills: list[SimulatedExecutionFill] = []
    filled = Decimal("0")
    notional = Decimal("0")

    for level in levels:
        if remaining == 0:
            break
        fill_quantity = min(remaining, level.quantity_btc_equivalent)
        fills.append(
            SimulatedExecutionFill(
                price=level.price,
                quantity_btc=fill_quantity,
            )
        )
        filled += fill_quantity
        notional += level.price * fill_quantity
        remaining -= fill_quantity

    return BookSweepResult(
        requested_quantity_btc=quantity_btc,
        filled_quantity_btc=filled,
        unfilled_quantity_btc=remaining,
        execution_vwap=notional / filled if filled > 0 else None,
        executed_notional_quote=notional,
        fully_executable=remaining == 0,
        fills=tuple(fills),
    )
