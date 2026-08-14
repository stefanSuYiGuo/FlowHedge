from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from backend.domain.models import InstrumentType
from backend.execution_cost.models import (
    ExecutionCostResult,
    ExecutionCostStatus,
    ExecutionFeeConfig,
    ExecutionFeeEntry,
    ExecutionSide,
    FeeStatus,
    SimulatedExecutionFill,
)
from backend.hedge_economics.engine import calculate_hedge_economics
from backend.hedge_economics.models import (
    CarryStatus,
    FundingProjectionMethod,
    FundingRateSource,
    HedgeEconomicsComparisonRequest,
    HedgeEconomicsRequest,
    HedgeEconomicsStatus,
)
from backend.hedge_economics.service import HedgeEconomicsService
from backend.market.book import normalized_books_from_levels
from backend.market.models import (
    ContractStructure,
    DerivativeMarketContext,
    ExecutableBookView,
    ExecutableMarketLevel,
    ExecutableMarketSnapshot,
    ExecutableOrderBook,
    InstrumentRules,
    MarketConnectionState,
    MarketConnectionStatus,
    MarketVenue,
)
from backend.market.store import InMemoryMarketStateStore


ENTRY = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
SNAPSHOT_TIME = ENTRY - timedelta(milliseconds=50)
CONTEXT_TIME = ENTRY - timedelta(seconds=2)
BASIS_TIME = ENTRY - timedelta(seconds=1)
FUNDING_TIME = ENTRY - timedelta(seconds=3)
BPS = Decimal("10000")


def run(coroutine):
    return asyncio.run(coroutine)


def instrument_rules(
    *,
    venue: MarketVenue = MarketVenue.OKX,
    instrument_type: InstrumentType = InstrumentType.PERPETUAL,
) -> InstrumentRules:
    is_spot = instrument_type is InstrumentType.SPOT
    venue_symbol = "BTC-USDT" if is_spot else "BTC-USDT-SWAP"
    return InstrumentRules(
        venue=venue,
        symbol="BTC-USD",
        venue_symbol=venue_symbol,
        instrument_type=instrument_type,
        base_asset="BTC",
        quote_asset="USDT" if venue is MarketVenue.OKX else "USD",
        price_increment=Decimal("0.1"),
        quantity_increment=Decimal("0.01"),
        quantity_min=Decimal("0.01"),
        price_precision=1,
        quantity_precision=2,
        status="LIVE",
        contract_structure=(
            ContractStructure.SPOT if is_spot else ContractStructure.LINEAR
        ),
        contract_multiplier=Decimal("1"),
        contract_value_currency=None if is_spot else "BTC",
        native_quantity_unit="BTC" if is_spot else "CONTRACTS",
        settlement_asset="USDT" if venue is MarketVenue.OKX else "USD",
        usd_conversion_rate=Decimal("1"),
        received_at=CONTEXT_TIME,
    )


def execution_result(
    *,
    side: ExecutionSide = ExecutionSide.BUY,
    instrument_type: InstrumentType = InstrumentType.PERPETUAL,
    requested_quantity: Decimal = Decimal("8"),
    filled_quantity: Decimal = Decimal("8"),
    fee_configured: bool = True,
    venue: MarketVenue = MarketVenue.OKX,
) -> ExecutionCostResult:
    unfilled = requested_quantity - filled_quantity
    notional = filled_quantity * Decimal("100000")
    partial = unfilled > 0
    fee_bps = Decimal("0.5") if fee_configured else None
    price_bps = Decimal("1.5")
    all_in_bps = price_bps + fee_bps if fee_bps is not None else None
    instrument_id = (
        "BTC-USDT" if instrument_type is InstrumentType.SPOT else "BTC-USDT-SWAP"
    )
    return ExecutionCostResult(
        result_id="cost-acceptance-v17",
        request_id="execution-acceptance",
        venue=venue,
        instrument_id=instrument_id,
        instrument_type=instrument_type,
        side=side,
        market_snapshot_version=17,
        snapshot_captured_at=SNAPSHOT_TIME,
        book_captured_at=SNAPSHOT_TIME - timedelta(milliseconds=10),
        requested_quantity_btc=requested_quantity,
        filled_quantity_btc=filled_quantity,
        unfilled_quantity_btc=unfilled,
        fully_executable=not partial,
        status=(
            ExecutionCostStatus.INSUFFICIENT_LIQUIDITY
            if partial
            else ExecutionCostStatus.OK
        ),
        status_reason="KNOWN_BOOK_DEPTH_EXHAUSTED" if partial else None,
        best_bid=Decimal("99990"),
        best_ask=Decimal("100010"),
        arrival_mid=Decimal("100000"),
        execution_vwap=Decimal("100000"),
        quote_currency="USDT",
        usd_conversion_rate=Decimal("1"),
        usd_conversion_assumption="STABLECOIN_USD_PARITY_DEMO_ASSUMPTION",
        executed_notional_quote=notional,
        executed_notional_usd=notional,
        spread_cost_bps=Decimal("1"),
        depth_impact_bps=Decimal("0.5"),
        total_price_cost_bps=price_bps,
        price_cost_usd=notional * price_bps / BPS,
        taker_fee_bps=fee_bps,
        fee_usd=(notional * fee_bps / BPS if fee_bps is not None else None),
        fee_status=(FeeStatus.CONFIGURED if fee_configured else FeeStatus.UNCONFIGURED),
        fee_assumption_label="TEST_TAKER_FEE" if fee_configured else None,
        all_in_immediate_cost_bps=all_in_bps,
        all_in_immediate_cost_usd=(
            notional * all_in_bps / BPS if all_in_bps is not None else None
        ),
        fills=(
            SimulatedExecutionFill(
                price=Decimal("100000"), quantity_btc=filled_quantity
            ),
        ),
    )


def derivative_context(
    *,
    current_rate: Decimal | None = Decimal("0.0002"),
    predicted_rate: Decimal | None = Decimal("0.0001"),
    next_funding_time: datetime | None = ENTRY + timedelta(hours=2),
    interval_seconds: int | None = 8 * 60 * 60,
    basis_bps: Decimal | None = Decimal("25"),
    open_interest: Decimal | None = Decimal("12000"),
) -> DerivativeMarketContext:
    return DerivativeMarketContext(
        venue=MarketVenue.OKX,
        symbol="BTC-USD",
        venue_symbol="BTC-USDT-SWAP",
        mark_price=Decimal("100250"),
        index_price=Decimal("100200"),
        current_funding_rate=current_rate,
        predicted_funding_rate=predicted_rate,
        next_funding_time=next_funding_time,
        funding_interval_seconds=interval_seconds,
        open_interest=open_interest,
        open_interest_unit="CONTRACTS" if open_interest is not None else None,
        open_interest_btc_equivalent=(
            Decimal("12000") if open_interest is not None else None
        ),
        open_interest_usd=(
            Decimal("1203000000") if open_interest is not None else None
        ),
        mark_price_captured_at=CONTEXT_TIME,
        index_price_captured_at=CONTEXT_TIME,
        funding_captured_at=FUNDING_TIME,
        open_interest_captured_at=CONTEXT_TIME,
        received_at=CONTEXT_TIME,
        source="TEST_NORMALIZED_CONTEXT",
        basis_bps=basis_bps,
        basis_reference_price_usd=(
            Decimal("100000") if basis_bps is not None else None
        ),
        basis_captured_at=BASIS_TIME if basis_bps is not None else None,
    )


def market_view(
    *,
    context: DerivativeMarketContext | None = None,
    stale: bool = False,
    funding_stale: bool = False,
) -> ExecutableBookView:
    rules = instrument_rules()
    return ExecutableBookView(
        venue=rules.venue,
        symbol=rules.symbol,
        instrument_type=rules.instrument_type,
        connection=MarketConnectionState(
            feed_id="okx-public",
            venue=rules.venue,
            status=MarketConnectionStatus.LIVE,
            endpoint="public",
        ),
        book=None,
        instrument=rules,
        derivatives=context or derivative_context(),
        derivative_data_age_ms=31000 if stale else 2000,
        derivative_data_stale=stale,
        funding_data_age_ms=31000 if funding_stale else 3000,
        funding_data_stale=funding_stale,
        eligible=True,
        as_of=ENTRY,
    )


def economics_request(
    execution: ExecutionCostResult,
    *,
    horizon_seconds: int = 3 * 60 * 60,
) -> HedgeEconomicsRequest:
    return HedgeEconomicsRequest(
        request_id="economics-acceptance",
        execution_cost_result_id=execution.result_id,
        expected_holding_seconds=horizon_seconds,
        market_snapshot_version=execution.market_snapshot_version,
        requested_at=ENTRY,
    )


def calculate(
    *,
    execution: ExecutionCostResult | None = None,
    context: DerivativeMarketContext | None = None,
    stale: bool = False,
    funding_stale: bool = False,
    horizon_seconds: int = 3 * 60 * 60,
):
    execution = execution or execution_result()
    return calculate_hedge_economics(
        economics_request(execution, horizon_seconds=horizon_seconds),
        execution,
        market_view(
            context=context,
            stale=stale,
            funding_stale=funding_stale,
        ),
    )


def test_spot_has_zero_carry_and_preserves_configured_immediate_total() -> None:
    execution = execution_result(instrument_type=InstrumentType.SPOT)
    result = calculate_hedge_economics(
        economics_request(execution), execution, None
    )

    assert result.carry_status is CarryStatus.COMPLETE
    assert result.funding_rate_source is FundingRateSource.NOT_REQUIRED
    assert result.expected_funding_cost_bps == 0
    assert result.expected_carry_cost_usd == 0
    assert result.expected_total_hedge_cost_bps == execution.all_in_immediate_cost_bps
    assert result.expected_total_hedge_cost_usd == execution.all_in_immediate_cost_usd


def test_spot_with_unconfigured_fee_preserves_incomplete_total() -> None:
    execution = execution_result(
        instrument_type=InstrumentType.SPOT,
        fee_configured=False,
    )
    result = calculate_hedge_economics(
        economics_request(execution), execution, None
    )

    assert result.carry_status is CarryStatus.COMPLETE
    assert result.economics_status is HedgeEconomicsStatus.INCOMPLETE_IMMEDIATE_COST
    assert result.expected_carry_cost_bps == 0
    assert result.expected_total_hedge_cost_bps is None
    assert "IMMEDIATE_TAKER_FEE_UNCONFIGURED" in result.data_quality_flags


@pytest.mark.parametrize(
    ("side", "rate", "expected_bps", "expected_usd"),
    [
        (ExecutionSide.BUY, Decimal("0.0001"), Decimal("1"), Decimal("80")),
        (ExecutionSide.SELL, Decimal("0.0001"), Decimal("-1"), Decimal("-80")),
        (ExecutionSide.BUY, Decimal("-0.0001"), Decimal("-1"), Decimal("-80")),
        (ExecutionSide.SELL, Decimal("-0.0001"), Decimal("1"), Decimal("80")),
    ],
)
def test_funding_direction_uses_normalized_long_pays_short_convention(
    side: ExecutionSide,
    rate: Decimal,
    expected_bps: Decimal,
    expected_usd: Decimal,
) -> None:
    execution = execution_result(side=side)
    result = calculate(
        execution=execution,
        context=derivative_context(predicted_rate=rate),
    )

    assert result.expected_funding_cost_bps == expected_bps
    assert result.expected_funding_cost_usd == expected_usd
    assert result.expected_total_hedge_cost_bps == Decimal("2") + expected_bps


def test_funding_event_outside_horizon_is_known_zero() -> None:
    result = calculate(horizon_seconds=60 * 60)

    assert result.carry_status is CarryStatus.COMPLETE
    assert result.modeled_funding_event_count == 0
    assert result.funding_projection_method is FundingProjectionMethod.NONE
    assert result.expected_funding_cost_bps == 0
    assert "NO_FUNDING_EVENTS_IN_HORIZON" in result.data_quality_flags


def test_one_funding_event_inside_horizon_uses_predicted_rate() -> None:
    result = calculate()

    assert result.modeled_funding_event_count == 1
    assert result.modeled_funding_events[0].event_time == ENTRY + timedelta(hours=2)
    assert result.funding_rate_source is FundingRateSource.PREDICTED
    assert result.funding_rate_used == Decimal("0.0001")
    assert result.funding_projection_degraded is False
    assert result.funding_projection_method is FundingProjectionMethod.SINGLE_EVENT


def test_multiple_events_use_explicit_flat_rate_extrapolation() -> None:
    context = derivative_context(
        next_funding_time=ENTRY + timedelta(hours=1),
        interval_seconds=2 * 60 * 60,
    )
    result = calculate(context=context, horizon_seconds=5 * 60 * 60)

    assert result.modeled_funding_event_count == 3
    assert [event.event_time for event in result.modeled_funding_events] == [
        ENTRY + timedelta(hours=1),
        ENTRY + timedelta(hours=3),
        ENTRY + timedelta(hours=5),
    ]
    assert result.expected_funding_cost_bps == Decimal("3")
    assert result.funding_projection_degraded is True
    assert (
        result.funding_projection_method
        is FundingProjectionMethod.FLAT_RATE_EXTRAPOLATION
    )
    assert "FLAT_RATE_EXTRAPOLATION" in result.data_quality_flags


def test_exit_boundary_is_entry_exclusive_and_exit_inclusive() -> None:
    just_before = calculate(horizon_seconds=2 * 60 * 60 - 1)
    exactly_at = calculate(horizon_seconds=2 * 60 * 60)
    context_at_entry = derivative_context(
        next_funding_time=ENTRY,
        interval_seconds=2 * 60 * 60,
    )
    entry_boundary = calculate(
        context=context_at_entry,
        horizon_seconds=2 * 60 * 60,
    )

    assert just_before.modeled_funding_event_count == 0
    assert exactly_at.modeled_funding_event_count == 1
    assert entry_boundary.modeled_funding_event_count == 1
    assert entry_boundary.modeled_funding_events[0].event_time == ENTRY + timedelta(hours=2)


def test_current_rate_fallback_is_explicitly_degraded() -> None:
    result = calculate(
        context=derivative_context(
            predicted_rate=None,
            current_rate=Decimal("0.0003"),
        )
    )

    assert result.funding_rate_source is FundingRateSource.CURRENT
    assert result.funding_rate_used == Decimal("0.0003")
    assert result.funding_projection_degraded is True
    assert "CURRENT_FUNDING_RATE_FALLBACK" in result.data_quality_flags


def test_missing_rate_with_event_is_unavailable_not_zero() -> None:
    result = calculate(
        context=derivative_context(predicted_rate=None, current_rate=None)
    )

    assert result.carry_status is CarryStatus.UNAVAILABLE
    assert result.economics_status is HedgeEconomicsStatus.CARRY_UNAVAILABLE
    assert result.expected_funding_cost_bps is None
    assert result.expected_carry_cost_usd is None
    assert result.expected_total_hedge_cost_bps is None
    assert "FUNDING_RATE_UNAVAILABLE" in result.data_quality_flags


def test_partial_execution_funds_only_filled_quantity_and_preserves_shortfall() -> None:
    execution = execution_result(
        requested_quantity=Decimal("20"),
        filled_quantity=Decimal("13"),
    )
    result = calculate(execution=execution)

    assert result.quantity_btc == Decimal("13")
    assert result.unfilled_quantity_btc == Decimal("7")
    assert result.fully_executable is False
    assert result.economics_status is HedgeEconomicsStatus.PARTIAL_EXECUTION
    assert result.expected_funding_cost_usd == Decimal("130")


def test_basis_is_preserved_as_context_and_does_not_change_cost() -> None:
    narrow = calculate(context=derivative_context(basis_bps=Decimal("25")))
    wide = calculate(context=derivative_context(basis_bps=Decimal("900")))

    assert narrow.entry_basis_bps == Decimal("25")
    assert narrow.basis_reference_price_usd == Decimal("100000")
    assert narrow.basis_captured_at == BASIS_TIME
    assert narrow.expected_total_hedge_cost_bps == wide.expected_total_hedge_cost_bps
    assert "BASIS_CONTEXT_ONLY" in narrow.data_quality_flags


def test_open_interest_is_preserved_as_context_and_has_no_monetary_penalty() -> None:
    with_oi = calculate(context=derivative_context(open_interest=Decimal("12000")))
    without_oi = calculate(context=derivative_context(open_interest=None))

    assert with_oi.open_interest_context is not None
    assert with_oi.open_interest_context.open_interest == Decimal("12000")
    assert with_oi.open_interest_context.open_interest_btc_equivalent == Decimal("12000")
    assert with_oi.open_interest_context.captured_at == CONTEXT_TIME
    assert with_oi.expected_total_hedge_cost_bps == without_oi.expected_total_hedge_cost_bps
    assert "OPEN_INTEREST_CONTEXT_ONLY" in with_oi.data_quality_flags
    assert "OI_MONETARY_PENALTY" in with_oi.excluded_cost_components


def test_stale_funding_with_event_is_unavailable() -> None:
    result = calculate(funding_stale=True)

    assert result.carry_status is CarryStatus.UNAVAILABLE
    assert result.expected_funding_cost_usd is None
    assert "FUNDING_DATA_STALE" in result.data_quality_flags


def test_funding_rate_without_observation_timestamp_is_unavailable() -> None:
    context = derivative_context().model_copy(update={"funding_captured_at": None})
    result = calculate(context=context)

    assert result.carry_status is CarryStatus.UNAVAILABLE
    assert "FUNDING_TIMESTAMP_UNAVAILABLE" in result.data_quality_flags


def test_stale_context_with_no_event_keeps_known_zero_but_discloses_staleness() -> None:
    result = calculate(stale=True, horizon_seconds=60 * 60)

    assert result.carry_status is CarryStatus.COMPLETE
    assert result.expected_funding_cost_usd == 0
    assert "DERIVATIVE_CONTEXT_STALE" in result.data_quality_flags


def test_snapshot_and_source_timestamps_are_preserved() -> None:
    result = calculate()

    assert result.market_snapshot_version == 17
    assert result.snapshot_captured_at == SNAPSHOT_TIME
    assert result.book_captured_at == SNAPSHOT_TIME - timedelta(milliseconds=10)
    assert result.derivative_context_captured_at == CONTEXT_TIME
    assert result.funding_captured_at == FUNDING_TIME
    assert result.entry_time == ENTRY
    assert result.expected_exit_time == ENTRY + timedelta(hours=3)


def test_missing_schedule_with_funding_horizon_is_unavailable() -> None:
    result = calculate(
        context=derivative_context(next_funding_time=None, interval_seconds=None)
    )

    assert result.carry_status is CarryStatus.UNAVAILABLE
    assert "FUNDING_SCHEDULE_UNAVAILABLE" in result.data_quality_flags


def executable_market(
    *,
    venue: MarketVenue,
    instrument_type: InstrumentType,
    context: DerivativeMarketContext | None = None,
) -> ExecutableBookView:
    rules = instrument_rules(venue=venue, instrument_type=instrument_type)
    bids = (
        ExecutableMarketLevel(
            price=Decimal("99990"),
            quantity_btc_equivalent=Decimal("20"),
            source_quantity=Decimal("20"),
            source_quantity_unit=rules.native_quantity_unit,
        ),
    )
    asks = (
        ExecutableMarketLevel(
            price=Decimal("100010"),
            quantity_btc_equivalent=Decimal("20"),
            source_quantity=Decimal("20"),
            source_quantity_unit=rules.native_quantity_unit,
        ),
    )
    book = ExecutableOrderBook(
        venue=venue,
        symbol="BTC-USD",
        venue_symbol=rules.venue_symbol,
        instrument_type=instrument_type,
        max_levels=200,
        bids=bids,
        asks=asks,
        exchange_timestamp=SNAPSHOT_TIME,
        received_at=SNAPSHOT_TIME,
    )
    return ExecutableBookView(
        venue=venue,
        symbol="BTC-USD",
        instrument_type=instrument_type,
        connection=MarketConnectionState(
            feed_id=f"{venue.value}-{instrument_type.value}",
            venue=venue,
            status=MarketConnectionStatus.LIVE,
            endpoint="public",
        ),
        book=book,
        instrument=rules,
        derivatives=context,
        book_data_age_ms=0,
        derivative_data_age_ms=0 if context is not None else None,
        derivative_data_stale=False if context is not None else None,
        eligible=True,
        as_of=ENTRY,
    )


class StaticSnapshotStore:
    def __init__(self, snapshot: ExecutableMarketSnapshot) -> None:
        self.snapshot = snapshot

    async def executable_snapshot(
        self, base_asset: str | None = None
    ) -> ExecutableMarketSnapshot:
        assert base_asset in (None, "BTC")
        return self.snapshot


def test_batch_service_evaluates_every_candidate_on_one_snapshot_without_ranking() -> None:
    spot = executable_market(
        venue=MarketVenue.KRAKEN,
        instrument_type=InstrumentType.SPOT,
    )
    perp = executable_market(
        venue=MarketVenue.OKX,
        instrument_type=InstrumentType.PERPETUAL,
        context=derivative_context(),
    )
    snapshot = ExecutableMarketSnapshot(
        snapshot_version=42,
        captured_at=SNAPSHOT_TIME,
        base_asset="BTC",
        markets=(spot, perp),
    )
    fees = ExecutionFeeConfig(
        entries=(
            ExecutionFeeEntry(
                venue=MarketVenue.KRAKEN,
                instrument_type=InstrumentType.SPOT,
                fee_bps=Decimal("1"),
                assumption_label="TEST",
            ),
            ExecutionFeeEntry(
                venue=MarketVenue.OKX,
                instrument_type=InstrumentType.PERPETUAL,
                fee_bps=Decimal("1"),
                assumption_label="TEST",
            ),
        )
    )
    service = HedgeEconomicsService(StaticSnapshotStore(snapshot), fees)  # type: ignore[arg-type]
    response = run(
        service.compare(
            HedgeEconomicsComparisonRequest(
                request_id="batch",
                side=ExecutionSide.BUY,
                quantity_btc_equivalent=Decimal("8"),
                expected_holding_seconds=3 * 60 * 60,
                base_asset="BTC",
                requested_at=ENTRY,
            )
        )
    )

    assert len(response.results) == 2
    assert {result.instrument_type for result in response.results} == {
        InstrumentType.SPOT,
        InstrumentType.PERPETUAL,
    }
    assert all(result.market_snapshot_version == 42 for result in response.results)
    assert response.snapshot_captured_at == SNAPSHOT_TIME
    assert not hasattr(response, "optimal_hedge")


def test_atomic_executable_snapshot_calculates_normalized_basis() -> None:
    async def exercise():
        store = InMemoryMarketStateStore()
        await store.register_feed(
            "kraken-spot",
            MarketVenue.KRAKEN,
            "public",
            (("BTC-USD", InstrumentType.SPOT),),
        )
        await store.register_feed(
            "okx-perp",
            MarketVenue.OKX,
            "public",
            (("BTC-USD", InstrumentType.PERPETUAL),),
        )
        await store.update_connection(
            "kraken-spot", status=MarketConnectionStatus.LIVE
        )
        await store.update_connection("okx-perp", status=MarketConnectionStatus.LIVE)

        spot_rules = instrument_rules(
            venue=MarketVenue.KRAKEN,
            instrument_type=InstrumentType.SPOT,
        )
        perp_rules = instrument_rules(
            venue=MarketVenue.OKX,
            instrument_type=InstrumentType.PERPETUAL,
        )
        await store.replace_instrument(spot_rules)
        await store.replace_instrument(perp_rules)
        for rules, bid, ask in (
            (spot_rules, Decimal("99990"), Decimal("100010")),
            (perp_rules, Decimal("101990"), Decimal("102010")),
        ):
            display, executable = normalized_books_from_levels(
                rules=rules,
                bids=((bid, Decimal("20")),),
                asks=((ask, Decimal("20")),),
                exchange_timestamp=ENTRY,
                received_at=datetime.now(timezone.utc),
            )
            await store.replace_books(display, executable)
        await store.replace_derivative_context(
            DerivativeMarketContext(
                venue=MarketVenue.OKX,
                symbol="BTC-USD",
                venue_symbol="BTC-USDT-SWAP",
                mark_price=Decimal("102000"),
                received_at=datetime.now(timezone.utc),
                source="TEST",
            )
        )
        return await store.executable_snapshot("BTC")

    snapshot = run(exercise())
    perp = next(
        market
        for market in snapshot.markets
        if market.instrument_type is InstrumentType.PERPETUAL
    )

    assert perp.derivatives is not None
    assert perp.derivatives.basis_reference_price_usd == Decimal("100000")
    assert perp.derivatives.basis_bps == Decimal("200")
    assert perp.derivatives.basis_captured_at == snapshot.captured_at
