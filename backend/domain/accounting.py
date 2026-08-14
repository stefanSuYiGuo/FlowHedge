"""Pure accounting functions for client fills."""

from __future__ import annotations

from decimal import Decimal

from .models import ClientSide, ClientTrade, DeskState


def signed_client_spot_change(
    client_side: ClientSide, quantity_btc: Decimal
) -> Decimal:
    """Translate a client-side spot trade into the desk's signed inventory change."""

    if quantity_btc <= 0:
        raise ValueError("quantity_btc must be positive")
    return -quantity_btc if client_side is ClientSide.BUY else quantity_btc


def apply_client_trade(state: DeskState, trade: ClientTrade) -> DeskState:
    """Return a new desk state after applying one immutable client spot fill."""

    spot_change = signed_client_spot_change(trade.client_side, trade.quantity_btc)
    new_spot_inventory = state.spot_inventory_btc + spot_change
    new_total_delta = new_spot_inventory + state.derivative_delta_btc

    return DeskState(
        version=state.version + 1,
        as_of=trade.traded_at,
        spot_inventory_btc=new_spot_inventory,
        derivative_delta_btc=state.derivative_delta_btc,
        total_delta_btc=new_total_delta,
        open_hedge_order_ids=state.open_hedge_order_ids,
        working_order_delta_btc=state.working_order_delta_btc,
    )
