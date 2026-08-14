import asyncio

import pytest
from fastapi import HTTPException

from backend.main import (
    ManualHedgeOrderRequest,
    SimulatedHedgeFillRequest,
    create_manual_hedge_orders,
    get_desk_state,
    get_hedge_fills,
    get_hedge_orders,
    reset_demo,
    run_fixed_client_trade,
    simulate_hedge_fill,
)


def run(coroutine):
    return asyncio.run(coroutine)


def test_step_4_api_order_and_fill_lifecycle() -> None:
    run(reset_demo())

    with pytest.raises(HTTPException) as premature:
        run(
            create_manual_hedge_orders(
                ManualHedgeOrderRequest(
                    batch_id="api-step4",
                    spot_quantity_btc="3",
                    perp_quantity_btc="2",
                )
            )
        )
    assert premature.value.status_code == 409

    client_trade = run(run_fixed_client_trade())
    assert str(client_trade.desk_state_after.total_delta_btc) == "-5"

    order_batch = run(
        create_manual_hedge_orders(
            ManualHedgeOrderRequest(
                batch_id="api-step4",
                spot_quantity_btc="3",
                perp_quantity_btc="2",
            )
        )
    )
    assert order_batch.desk_state_after.total_delta_btc == -5
    assert order_batch.desk_state_after.working_order_delta_btc == 5
    assert len(order_batch.orders) == 2

    spot_order_id = order_batch.orders[0].hedge_order_id
    partial_fill = run(
        simulate_hedge_fill(
            spot_order_id,
            SimulatedHedgeFillRequest(
                hedge_fill_id="api-spot-half", quantity_btc="1.5"
            ),
        )
    )
    assert partial_fill.order.status == "PARTIALLY_FILLED"
    assert partial_fill.desk_state_after.spot_inventory_btc == -3.5
    assert partial_fill.desk_state_after.derivative_delta_btc == 0
    assert partial_fill.desk_state_after.total_delta_btc == -3.5
    assert partial_fill.desk_state_after.working_order_delta_btc == 3.5

    replay = run(
        simulate_hedge_fill(
            spot_order_id,
            SimulatedHedgeFillRequest(
                hedge_fill_id="api-spot-half", quantity_btc="1.5"
            ),
        )
    )
    assert replay.replayed is True

    with pytest.raises(HTTPException) as overfill:
        run(
            simulate_hedge_fill(
                spot_order_id,
                SimulatedHedgeFillRequest(
                    hedge_fill_id="api-overfill", quantity_btc="2"
                ),
            )
        )
    assert overfill.value.status_code == 422

    assert len(run(get_hedge_orders())) == 2
    assert len(run(get_hedge_fills())) == 1
    assert run(get_desk_state()).version == 3

    run(reset_demo())
