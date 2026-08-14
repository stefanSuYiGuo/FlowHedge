from datetime import datetime, timezone
from decimal import Decimal

import pytest

from backend.demo import (
    DemoStateError,
    DemoTradingService,
    HedgeAllocationError,
    HedgeFillError,
)
from backend.domain.accounting import apply_client_trade
from backend.domain.models import (
    ClientSide,
    ClientTrade,
    DeskState,
    EventType,
    HedgeOrderStatus,
    InstrumentType,
)
from backend.domain.validation import (
    RFQBelowMinimumNotional,
    validate_client_rfq_notional,
)


def flat_state() -> DeskState:
    return DeskState(
        version=0,
        as_of=datetime.now(timezone.utc),
        spot_inventory_btc=Decimal("0"),
        derivative_delta_btc=Decimal("0"),
        total_delta_btc=Decimal("0"),
    )


def trade(side: ClientSide, quantity: str, trade_id: str = "trade-test") -> ClientTrade:
    return ClientTrade(
        client_trade_id=trade_id,
        rfq_id="rfq-test",
        quote_id="quote-test",
        client_id="INST-TEST",
        instrument_id="BTC-USD",
        client_side=side,
        quantity_btc=Decimal(quantity),
        trade_price_usd=Decimal("118087"),
        traded_at=datetime.now(timezone.utc),
    )


def test_rfq_notional_must_be_strictly_greater_than_500k() -> None:
    assert validate_client_rfq_notional(
        Decimal("5"), Decimal("118000")
    ) == Decimal("590000")

    with pytest.raises(RFQBelowMinimumNotional):
        validate_client_rfq_notional(Decimal("4"), Decimal("118000"))

    with pytest.raises(RFQBelowMinimumNotional):
        validate_client_rfq_notional(Decimal("5"), Decimal("100000"))


def test_client_buy_reduces_desk_spot_inventory_and_delta() -> None:
    updated = apply_client_trade(flat_state(), trade(ClientSide.BUY, "5"))

    assert updated.version == 1
    assert updated.spot_inventory_btc == Decimal("-5")
    assert updated.derivative_delta_btc == Decimal("0")
    assert updated.total_delta_btc == Decimal("-5")


def test_client_sell_increases_desk_spot_inventory_and_delta() -> None:
    updated = apply_client_trade(flat_state(), trade(ClientSide.SELL, "5"))

    assert updated.spot_inventory_btc == Decimal("5")
    assert updated.derivative_delta_btc == Decimal("0")
    assert updated.total_delta_btc == Decimal("5")


def test_desk_state_rejects_unreconciled_total_delta() -> None:
    with pytest.raises(ValueError, match="total_delta_btc must equal"):
        DeskState(
            version=0,
            as_of=datetime.now(timezone.utc),
            spot_inventory_btc=Decimal("-5"),
            derivative_delta_btc=Decimal("2"),
            total_delta_btc=Decimal("-5"),
        )


def test_fixed_scenario_is_auto_accepted_and_updates_position_once() -> None:
    service = DemoTradingService()
    result = service.run_fixed_client_trade()

    assert result.replayed is False
    assert result.rfq.validated_notional_usd == Decimal("590000")
    assert result.quote.pricing_source == "FIXED_STEP_2_FIXTURE"
    assert result.client_trade.client_side is ClientSide.BUY
    assert result.desk_state_before.total_delta_btc == Decimal("0")
    assert result.desk_state_after.spot_inventory_btc == Decimal("-5")
    assert result.desk_state_after.total_delta_btc == Decimal("-5")
    assert [event.event_type for event in result.events] == [
        EventType.RFQ_RECEIVED,
        EventType.RFQ_VALIDATED,
        EventType.QUOTE_GENERATED,
        EventType.QUOTE_ACCEPTED,
        EventType.CLIENT_FILL,
        EventType.POSITION_UPDATED,
    ]

    replay = service.run_fixed_client_trade()
    assert replay.replayed is True
    assert service.desk_state.version == 1
    assert service.desk_state.total_delta_btc == Decimal("-5")
    assert len(service.events) == 6


def test_reset_clears_the_booked_scenario_and_event_ledger() -> None:
    service = DemoTradingService()
    service.run_fixed_client_trade()
    service.create_manual_hedge_orders(
        Decimal("3"), Decimal("2"), "batch-to-reset"
    )

    reset_state = service.reset()

    assert reset_state.version == 0
    assert reset_state.total_delta_btc == Decimal("0")
    assert service.saved_result is None
    assert service.events == []
    assert service.processed_trade_ids == set()
    assert service.hedge_orders == {}
    assert service.hedge_fills == []
    assert service.processed_fill_results == {}


def test_hedge_orders_require_client_trade_and_exact_demo_target() -> None:
    service = DemoTradingService()

    with pytest.raises(DemoStateError, match="book the fixed client trade"):
        service.create_manual_hedge_orders(
            Decimal("3"), Decimal("2"), "batch-before-client-trade"
        )

    service.run_fixed_client_trade()

    with pytest.raises(HedgeAllocationError, match="must equal"):
        service.create_manual_hedge_orders(
            Decimal("3"), Decimal("1"), "batch-under-hedged"
        )
    with pytest.raises(HedgeAllocationError, match="cannot be negative"):
        service.create_manual_hedge_orders(
            Decimal("-1"), Decimal("6"), "batch-negative"
        )


def test_creating_mixed_hedge_orders_changes_working_but_not_actual_delta() -> None:
    service = DemoTradingService()
    service.run_fixed_client_trade()

    result = service.create_manual_hedge_orders(
        Decimal("3"), Decimal("2"), "batch-mixed"
    )

    assert result.replayed is False
    assert result.required_hedge_delta_btc == Decimal("5")
    assert result.demo_target_total_delta_btc == Decimal("0")
    assert result.desk_state_before.version == 1
    assert result.desk_state_after.version == 2
    assert result.desk_state_after.spot_inventory_btc == Decimal("-5")
    assert result.desk_state_after.derivative_delta_btc == Decimal("0")
    assert result.desk_state_after.total_delta_btc == Decimal("-5")
    assert result.desk_state_after.working_order_delta_btc == Decimal("5")
    assert (
        result.desk_state_after.total_delta_btc
        + result.desk_state_after.working_order_delta_btc
        == Decimal("0")
    )
    assert [order.instrument_type for order in result.orders] == [
        InstrumentType.SPOT,
        InstrumentType.PERPETUAL,
    ]
    assert all(order.status is HedgeOrderStatus.OPEN for order in result.orders)
    assert [event.event_type for event in result.events] == [
        EventType.HEDGE_ORDER_CREATED,
        EventType.HEDGE_ORDER_CREATED,
        EventType.POSITION_UPDATED,
    ]

    replay = service.create_manual_hedge_orders(
        Decimal("3"), Decimal("2"), "batch-mixed"
    )
    assert replay.replayed is True
    assert service.desk_state.version == 2
    assert len(service.events) == 9


def test_mixed_partial_and_remaining_fills_reconcile_positions() -> None:
    service = DemoTradingService()
    service.run_fixed_client_trade()
    batch = service.create_manual_hedge_orders(
        Decimal("3"), Decimal("2"), "batch-mixed-fills"
    )
    spot_order, perp_order = batch.orders

    first_spot = service.simulate_hedge_fill(
        spot_order.hedge_order_id, Decimal("1.5"), "fill-spot-half"
    )
    assert first_spot.desk_state_after.spot_inventory_btc == Decimal("-3.5")
    assert first_spot.desk_state_after.derivative_delta_btc == Decimal("0")
    assert first_spot.desk_state_after.total_delta_btc == Decimal("-3.5")
    assert first_spot.desk_state_after.working_order_delta_btc == Decimal("3.5")
    assert first_spot.order.status is HedgeOrderStatus.PARTIALLY_FILLED

    first_perp = service.simulate_hedge_fill(
        perp_order.hedge_order_id, Decimal("1"), "fill-perp-half"
    )
    assert first_perp.desk_state_after.spot_inventory_btc == Decimal("-3.5")
    assert first_perp.desk_state_after.derivative_delta_btc == Decimal("1")
    assert first_perp.desk_state_after.total_delta_btc == Decimal("-2.5")
    assert first_perp.desk_state_after.working_order_delta_btc == Decimal("2.5")
    assert (
        first_perp.desk_state_after.total_delta_btc
        + first_perp.desk_state_after.working_order_delta_btc
        == Decimal("0")
    )

    service.simulate_hedge_fill(
        spot_order.hedge_order_id, Decimal("1.5"), "fill-spot-rest"
    )
    completed = service.simulate_hedge_fill(
        perp_order.hedge_order_id, Decimal("1"), "fill-perp-rest"
    )
    assert completed.desk_state_after.version == 6
    assert completed.desk_state_after.spot_inventory_btc == Decimal("-2")
    assert completed.desk_state_after.derivative_delta_btc == Decimal("2")
    assert completed.desk_state_after.total_delta_btc == Decimal("0")
    assert completed.desk_state_after.working_order_delta_btc == Decimal("0")
    assert completed.desk_state_after.open_hedge_order_ids == ()
    assert all(
        order.status is HedgeOrderStatus.FILLED
        for order in service.hedge_orders.values()
    )


@pytest.mark.parametrize(
    ("spot_quantity", "perp_quantity", "expected_spot", "expected_derivative"),
    [
        ("5", "0", "0", "0"),
        ("0", "5", "-5", "5"),
    ],
)
def test_full_spot_and_full_perp_allocations_neutralize_delta(
    spot_quantity: str,
    perp_quantity: str,
    expected_spot: str,
    expected_derivative: str,
) -> None:
    service = DemoTradingService()
    service.run_fixed_client_trade()
    batch = service.create_manual_hedge_orders(
        Decimal(spot_quantity), Decimal(perp_quantity), f"batch-{spot_quantity}-{perp_quantity}"
    )
    order = batch.orders[0]

    result = service.simulate_hedge_fill(
        order.hedge_order_id, order.quantity_btc, f"fill-{order.hedge_order_id}"
    )

    assert result.desk_state_after.spot_inventory_btc == Decimal(expected_spot)
    assert result.desk_state_after.derivative_delta_btc == Decimal(
        expected_derivative
    )
    assert result.desk_state_after.total_delta_btc == Decimal("0")


def test_fill_is_idempotent_and_overfill_is_rejected_without_mutation() -> None:
    service = DemoTradingService()
    service.run_fixed_client_trade()
    order = service.create_manual_hedge_orders(
        Decimal("5"), Decimal("0"), "batch-idempotent"
    ).orders[0]

    first = service.simulate_hedge_fill(
        order.hedge_order_id, Decimal("2"), "fill-idempotent"
    )
    event_count = len(service.events)
    replay = service.simulate_hedge_fill(
        order.hedge_order_id, Decimal("2"), "fill-idempotent"
    )
    assert replay.replayed is True
    assert service.desk_state == first.desk_state_after
    assert len(service.events) == event_count

    with pytest.raises(HedgeFillError, match="exceeds remaining"):
        service.simulate_hedge_fill(
            order.hedge_order_id, Decimal("4"), "fill-overfill"
        )
    assert service.desk_state == first.desk_state_after
    assert service.hedge_orders[order.hedge_order_id].remaining_quantity_btc == Decimal(
        "3"
    )
