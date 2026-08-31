from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from backend.domain.models import (
    ClientSide,
    ClientTrade,
    DemoScenarioResult,
    DeskState,
    HedgeFill,
    HedgeSide,
    InstrumentType,
    MarketSnapshot,
    PricingLiquidityLeg,
    PricingResult,
    PricingStatus,
    Quote,
    QuoteStatus,
    RFQ,
    RFQStatus,
)
from backend.pnl import (
    AttributionStatus,
    PERP_VALUATION_FLAG,
    PnLInputError,
    PnLStatus,
    PositionValuationStatus,
    calculate_pnl,
)


NOW = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)


def scenario(
    *,
    trade_id: str,
    side: ClientSide,
    quantity: str,
    trade_price: str,
    reference_mid: str = "100",
    seconds: int = 0,
    with_pricing_result: bool = True,
) -> DemoScenarioResult:
    traded_at = NOW + timedelta(seconds=seconds)
    quantity_btc = Decimal(quantity)
    reference = Decimal(reference_mid)
    trade = ClientTrade(
        client_trade_id=trade_id,
        rfq_id=f"rfq-{trade_id}",
        quote_id=f"quote-{trade_id}",
        client_id="INST-TEST",
        instrument_id="BTC-USD",
        client_side=side,
        quantity_btc=quantity_btc,
        trade_price_usd=Decimal(trade_price),
        traded_at=traded_at,
    )
    snapshot = MarketSnapshot(
        market_snapshot_id=f"snapshot-{trade_id}",
        version=1,
        captured_at=traded_at,
        base_asset="BTC",
        quote_currency="USD",
        reference_price_usd=reference,
        observations=(),
    )
    pricing_result = None
    if with_pricing_result:
        leg = PricingLiquidityLeg(
            venue="KRAKEN",
            instrument_id="BTC-USD",
            instrument_type=InstrumentType.SPOT,
            quantity_btc=quantity_btc,
            execution_vwap_usd=reference,
            executed_notional_usd=reference * quantity_btc,
            expected_taker_fee_bps=Decimal("2"),
            expected_fee_usd=reference * quantity_btc * Decimal("2") / Decimal("10000"),
            usd_conversion_rate=Decimal("1"),
            usd_conversion_assumption="USD_NATIVE",
        )
        pricing_result = PricingResult(
            pricing_result_id=f"pricing-{trade_id}",
            request_id=f"request-{trade_id}",
            rfq_id=f"rfq-{trade_id}",
            model_version="TEST_L2_V1",
            status=PricingStatus.OK,
            client_side=side,
            requested_quantity_btc=quantity_btc,
            priced_quantity_btc=quantity_btc,
            unpriced_quantity_btc=Decimal("0"),
            market_snapshot_version=1,
            snapshot_captured_at=traded_at,
            reference_mid_usd=reference,
            reference_source="TEST_REFERENCE",
            executable_replacement_vwap_usd=reference,
            executed_notional_usd=reference * quantity_btc,
            expected_market_impact_bps=Decimal("0"),
            expected_market_impact_usd=Decimal("0"),
            expected_fee_bps=Decimal("2"),
            expected_fee_usd=leg.expected_fee_usd,
            client_margin_bps=Decimal("5"),
            client_margin_usd=reference * quantity_btc * Decimal("5") / Decimal("10000"),
            rounding_adjustment_usd=Decimal("0"),
            expected_gross_edge_usd=Decimal("1"),
            final_quote_price_usd=Decimal(trade_price),
            client_price_increment_usd=Decimal("0.1"),
            quote_validity_seconds=5,
            assumption_label="TEST",
            economics_disclosure="EXPECTED ONLY",
            liquidity_legs=(leg,),
        )
    flat_state = DeskState(
        version=0,
        as_of=traded_at,
        spot_inventory_btc=Decimal("0"),
        derivative_delta_btc=Decimal("0"),
        total_delta_btc=Decimal("0"),
    )
    return DemoScenarioResult(
        replayed=False,
        market_snapshot=snapshot,
        rfq=RFQ(
            rfq_id=f"rfq-{trade_id}",
            client_id="INST-TEST",
            instrument_id="BTC-USD",
            client_side=side,
            quantity_btc=quantity_btc,
            received_at=traded_at,
            status=RFQStatus.FILLED,
            validation_market_snapshot_id=snapshot.market_snapshot_id,
            validation_reference_price_usd=reference,
            validated_notional_usd=reference * quantity_btc,
        ),
        quote=Quote(
            quote_id=f"quote-{trade_id}",
            rfq_id=f"rfq-{trade_id}",
            revision=1,
            quoted_price_usd=Decimal(trade_price),
            quantity_btc=quantity_btc,
            created_at=traded_at,
            expires_at=traded_at + timedelta(seconds=5),
            status=QuoteStatus.ACCEPTED,
            market_snapshot_id=snapshot.market_snapshot_id,
            desk_state_version=0,
            pricing_source="TEST",
            pricing_result_id=(
                pricing_result.pricing_result_id if pricing_result is not None else None
            ),
        ),
        client_trade=trade,
        desk_state_before=flat_state,
        desk_state_after=flat_state,
        events=(),
        pricing_result=pricing_result,
    )


def hedge_fill(
    *,
    fill_id: str,
    side: HedgeSide,
    quantity: str,
    price: str,
    seconds: int,
    instrument_type: InstrumentType = InstrumentType.SPOT,
    venue: str = "KRAKEN",
    instrument_id: str | None = None,
    fee: str | None = "0",
    arrival_mid: str | None = None,
    expected_vwap: str | None = None,
) -> HedgeFill:
    return HedgeFill(
        hedge_fill_id=fill_id,
        hedge_order_id=f"order-{fill_id}",
        instrument_id=instrument_id
        or ("BTC-USD" if instrument_type is InstrumentType.SPOT else "BTC-PERP"),
        instrument_type=instrument_type,
        side=side,
        quantity_btc=Decimal(quantity),
        fill_price_usd=Decimal(price),
        filled_at=NOW + timedelta(seconds=seconds),
        execution_source="TEST_L2",
        venue=venue,
        arrival_mid_usd=(Decimal(arrival_mid) if arrival_mid is not None else None),
        expected_vwap_usd=(
            Decimal(expected_vwap) if expected_vwap is not None else None
        ),
        fee_usd=(Decimal(fee) if fee is not None else None),
    )


def calculate(
    scenarios: tuple[DemoScenarioResult, ...] = (),
    fills: tuple[HedgeFill, ...] = (),
    *,
    spot_mark: str | None = "100",
    perp_marks: dict[tuple[str, str], Decimal] | None = None,
):
    return calculate_pnl(
        completed_scenarios=scenarios,
        hedge_fills=fills,
        spot_mark_usd=(Decimal(spot_mark) if spot_mark is not None else None),
        perp_marks=perp_marks or {},
        as_of=NOW + timedelta(minutes=1),
        desk_state_version=17,
        market_snapshot_version=42,
    )


def test_empty_session_is_zero_and_reconciled() -> None:
    result = calculate()

    assert result.status is PnLStatus.COMPLETE
    assert result.total_desk_pnl_usd == Decimal("0")
    assert result.reconciliation_difference_usd == Decimal("0")
    assert result.reconciled is True
    assert result.positions[0].valuation_status is PositionValuationStatus.FLAT


def test_spot_average_cost_handles_add_partial_close_and_crossing_zero() -> None:
    # +10 @ 100, +10 @ 110 => average 105; sell 6 @ 120; sell 17 @ 90
    # closes the remaining 14 long and opens 3 short at 90.
    scenarios = (
        scenario(
            trade_id="buy-10",
            side=ClientSide.SELL,
            quantity="10",
            trade_price="100",
            seconds=1,
        ),
        scenario(
            trade_id="buy-10-second",
            side=ClientSide.SELL,
            quantity="10",
            trade_price="110",
            seconds=2,
        ),
        scenario(
            trade_id="sell-6",
            side=ClientSide.BUY,
            quantity="6",
            trade_price="120",
            seconds=3,
        ),
        scenario(
            trade_id="cross-17",
            side=ClientSide.BUY,
            quantity="17",
            trade_price="90",
            seconds=4,
        ),
    )

    result = calculate(scenarios, spot_mark="80")
    spot = result.positions[0]

    assert spot.signed_quantity_btc == Decimal("-3")
    assert spot.average_entry_price_usd == Decimal("90")
    assert spot.gross_realized_pnl_usd == Decimal("-120")
    assert spot.unrealized_mtm_usd == Decimal("30")
    assert result.total_desk_pnl_usd == Decimal("-90")
    assert result.reconciliation_difference_usd == Decimal("0")
    assert result.reconciled is True


def test_full_close_clears_average_entry_and_realizes_pnl() -> None:
    scenarios = (
        scenario(
            trade_id="open-long",
            side=ClientSide.SELL,
            quantity="2",
            trade_price="100",
            seconds=1,
        ),
        scenario(
            trade_id="close-long",
            side=ClientSide.BUY,
            quantity="2",
            trade_price="105",
            seconds=2,
        ),
    )

    result = calculate(scenarios, spot_mark=None)
    spot = result.positions[0]

    assert spot.signed_quantity_btc == 0
    assert spot.average_entry_price_usd is None
    assert spot.gross_realized_pnl_usd == Decimal("10")
    assert spot.unrealized_mtm_usd == 0
    assert spot.valuation_status is PositionValuationStatus.FLAT
    assert result.total_desk_pnl_usd == Decimal("10")


def test_spot_client_and_hedge_economics_reconcile_and_attribute() -> None:
    scenarios = (
        scenario(
            trade_id="client-buy",
            side=ClientSide.BUY,
            quantity="10",
            trade_price="110",
            reference_mid="100",
            seconds=1,
        ),
        scenario(
            trade_id="client-sell",
            side=ClientSide.SELL,
            quantity="8",
            trade_price="90",
            reference_mid="100",
            seconds=3,
        ),
    )
    fills = (
        hedge_fill(
            fill_id="spot-buy",
            side=HedgeSide.BUY,
            quantity="4",
            price="100",
            seconds=2,
            fee="0.08",
            arrival_mid="98",
            expected_vwap="99",
        ),
    )

    result = calculate(scenarios, fills, spot_mark="95")

    assert result.gross_realized_pnl_usd == Decimal("160")
    assert result.trading_fees_usd == Decimal("0.08")
    assert result.net_realized_pnl_usd == Decimal("159.92")
    assert result.spot_unrealized_mtm_usd == Decimal("10")
    assert result.total_desk_pnl_usd == Decimal("169.92")
    assert result.client_spread_capture_usd == Decimal("180")
    assert result.hedge_slippage_vs_expected_usd == Decimal("4")
    assert result.hedge_implementation_shortfall_usd == Decimal("8")
    assert result.inventory_market_movement_usd == Decimal("-2.00")
    assert (
        result.client_spread_capture_usd
        - result.hedge_implementation_shortfall_usd
        - result.trading_fees_usd
        + result.inventory_market_movement_usd
        == result.total_desk_pnl_usd
    )
    assert result.attribution_status is AttributionStatus.COMPLETE
    assert result.reconciled is True


def test_perpetual_buckets_are_isolated_by_venue_and_instrument() -> None:
    fills = (
        hedge_fill(
            fill_id="okx-long",
            side=HedgeSide.LONG,
            quantity="2",
            price="100",
            seconds=1,
            instrument_type=InstrumentType.PERPETUAL,
            venue="OKX",
            instrument_id="BTC-USDT-SWAP",
            arrival_mid="100",
            expected_vwap="100",
        ),
        hedge_fill(
            fill_id="kraken-short",
            side=HedgeSide.SHORT,
            quantity="1",
            price="105",
            seconds=2,
            instrument_type=InstrumentType.PERPETUAL,
            venue="KRAKEN",
            instrument_id="PF_XBTUSD",
            arrival_mid="105",
            expected_vwap="105",
        ),
    )

    result = calculate(
        fills=fills,
        perp_marks={
            ("OKX", "BTC-USDT-SWAP"): Decimal("110"),
            ("KRAKEN", "PF_XBTUSD"): Decimal("100"),
        },
    )

    assert result.perp_unrealized_mtm_usd == Decimal("25")
    assert result.total_desk_pnl_usd == Decimal("25")
    assert len(result.positions) == 3
    perp_positions = {
        (item.venue, item.instrument_id): item for item in result.positions[1:]
    }
    assert perp_positions[("OKX", "BTC-USDT-SWAP")].signed_quantity_btc == 2
    assert perp_positions[("KRAKEN", "PF_XBTUSD")].signed_quantity_btc == -1
    assert PERP_VALUATION_FLAG in result.data_quality_flags
    assert all(
        PERP_VALUATION_FLAG in item.data_quality_flags
        for item in perp_positions.values()
    )


def test_missing_actual_fee_fails_closed_instead_of_assuming_zero() -> None:
    fill = hedge_fill(
        fill_id="legacy-fill",
        side=HedgeSide.BUY,
        quantity="1",
        price="100",
        seconds=1,
        fee=None,
        arrival_mid="100",
        expected_vwap="100",
    )

    result = calculate(fills=(fill,), spot_mark="101")

    assert result.status is PnLStatus.PARTIAL
    assert result.trading_fees_usd is None
    assert result.net_realized_pnl_usd is None
    assert result.total_desk_pnl_usd is None
    assert result.reconciled is False
    assert "ACTUAL_HEDGE_FEE_UNAVAILABLE" in result.data_quality_flags


def test_missing_open_position_mark_withholds_total() -> None:
    client_trade = scenario(
        trade_id="unmarked-spot",
        side=ClientSide.SELL,
        quantity="2",
        trade_price="100",
    )
    perp_fill = hedge_fill(
        fill_id="unmarked-perp",
        side=HedgeSide.SHORT,
        quantity="1",
        price="100",
        seconds=1,
        instrument_type=InstrumentType.PERPETUAL,
        venue="OKX",
        instrument_id="BTC-USDT-SWAP",
        arrival_mid="100",
        expected_vwap="100",
    )

    result = calculate(
        scenarios=(client_trade,),
        fills=(perp_fill,),
        spot_mark=None,
        perp_marks={},
    )

    assert result.status is PnLStatus.PARTIAL
    assert result.spot_unrealized_mtm_usd is None
    assert result.perp_unrealized_mtm_usd is None
    assert result.total_desk_pnl_usd is None
    assert "OPEN_SPOT_MARK_UNAVAILABLE" in result.data_quality_flags
    assert any(
        flag.startswith("OPEN_PERP_MARK_UNAVAILABLE")
        for flag in result.data_quality_flags
    )


def test_missing_execution_benchmark_only_makes_attribution_partial() -> None:
    fill = hedge_fill(
        fill_id="no-benchmarks",
        side=HedgeSide.BUY,
        quantity="1",
        price="100",
        seconds=1,
        fee="0.02",
    )

    result = calculate(fills=(fill,), spot_mark="101")

    assert result.total_desk_pnl_usd == Decimal("0.98")
    assert result.reconciled is True
    assert result.status is PnLStatus.PARTIAL
    assert result.hedge_slippage_vs_expected_usd is None
    assert result.hedge_implementation_shortfall_usd is None
    assert result.inventory_market_movement_usd is None
    assert result.attribution_status is AttributionStatus.PARTIAL
    assert any(
        flag.startswith("HEDGE_ARRIVAL_MID_UNAVAILABLE")
        for flag in result.data_quality_flags
    )


def test_missing_expected_vwap_marks_attribution_partial_without_changing_pnl() -> None:
    fill = hedge_fill(
        fill_id="arrival-only",
        side=HedgeSide.BUY,
        quantity="1",
        price="100",
        seconds=1,
        fee="0.02",
        arrival_mid="99",
    )

    result = calculate(fills=(fill,), spot_mark="101")

    assert result.total_desk_pnl_usd == Decimal("0.98")
    assert result.hedge_implementation_shortfall_usd == Decimal("1")
    assert result.hedge_slippage_vs_expected_usd is None
    assert result.status is PnLStatus.PARTIAL
    assert result.attribution_status is AttributionStatus.PARTIAL
    assert result.inventory_market_movement_usd is None


def test_legacy_client_trade_uses_snapshot_reference_with_quality_flag() -> None:
    legacy = scenario(
        trade_id="legacy-client",
        side=ClientSide.BUY,
        quantity="3",
        trade_price="102",
        reference_mid="100",
        with_pricing_result=False,
    )

    result = calculate(scenarios=(legacy,), spot_mark="100")

    assert result.client_spread_capture_usd == Decimal("6")
    assert (
        "LEGACY_CLIENT_REFERENCE_FALLBACK:legacy-client"
        in result.data_quality_flags
    )


def test_replayed_duplicate_ids_are_idempotent_but_conflicts_are_rejected() -> None:
    original = scenario(
        trade_id="same-id",
        side=ClientSide.SELL,
        quantity="1",
        trade_price="100",
    )
    replay = original.model_copy(update={"replayed": True})

    result = calculate(scenarios=(original, replay), spot_mark="110")
    assert result.positions[0].signed_quantity_btc == Decimal("1")
    assert result.total_desk_pnl_usd == Decimal("10")

    conflict = scenario(
        trade_id="same-id",
        side=ClientSide.SELL,
        quantity="2",
        trade_price="100",
    )
    with pytest.raises(PnLInputError, match="conflicting client trade id"):
        calculate(scenarios=(original, conflict), spot_mark="110")

    conflicting_reference = scenario(
        trade_id="same-id",
        side=ClientSide.SELL,
        quantity="1",
        trade_price="100",
        reference_mid="99",
    )
    with pytest.raises(PnLInputError, match="conflicting client trade id"):
        calculate(scenarios=(original, conflicting_reference), spot_mark="110")
