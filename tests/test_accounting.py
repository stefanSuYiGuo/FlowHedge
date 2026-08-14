from datetime import datetime, timezone
from decimal import Decimal

import pytest

from backend.demo import DemoTradingService
from backend.domain.accounting import apply_client_trade
from backend.domain.models import ClientSide, ClientTrade, DeskState, EventType
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
