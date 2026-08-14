from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from backend.domain.models import DeskState, InstrumentType
from backend.execution_cost.models import (
    ExecutionFeeConfig,
    ExecutionFeeEntry,
    ExecutionSide,
)
from backend.hedge_optimizer.candidate_builder import build_hedge_candidates
from backend.hedge_optimizer.models import (
    CandidateBuilderStatus,
    CandidateExclusionReason,
    HedgeOptimizationInput,
)
from backend.hedge_optimizer.service import HedgeCandidateBuilderService
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
from backend.risk.models import RiskReferencePrice
from backend.risk.policy import RiskPolicy


NOW = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
SNAPSHOT_VERSION = 42


def run(coroutine):
    return asyncio.run(coroutine)


def instrument_rules(
    venue: MarketVenue,
    instrument_type: InstrumentType,
    *,
    executable: bool = True,
    quantity_step: str = "0.01",
    quantity_min: str = "0.05",
) -> InstrumentRules:
    is_spot = instrument_type is InstrumentType.SPOT
    venue_symbol = f"{venue.value}-BTC-{'SPOT' if is_spot else 'PERP'}"
    return InstrumentRules(
        venue=venue,
        symbol="BTC-USD",
        venue_symbol=venue_symbol,
        instrument_type=instrument_type,
        base_asset="BTC",
        quote_asset="USD",
        price_increment=Decimal("0.1"),
        quantity_increment=Decimal(quantity_step),
        quantity_min=Decimal(quantity_min),
        price_precision=1,
        quantity_precision=2,
        status="LIVE" if executable else "OFFLINE",
        eligible_for_execution=executable,
        contract_structure=(
            ContractStructure.SPOT if is_spot else ContractStructure.LINEAR
        ),
        contract_multiplier=Decimal("1"),
        contract_value_currency=None if is_spot else "BTC",
        native_quantity_unit="BTC" if is_spot else "CONTRACTS",
        settlement_asset="USD",
        usd_conversion_rate=Decimal("1"),
        received_at=NOW,
    )


def derivative_context(
    venue: MarketVenue,
    venue_symbol: str,
    *,
    funding_available: bool = True,
    basis_bps: Decimal = Decimal("25"),
    open_interest: Decimal = Decimal("12000"),
) -> DerivativeMarketContext:
    return DerivativeMarketContext(
        venue=venue,
        symbol="BTC-USD",
        venue_symbol=venue_symbol,
        mark_price=Decimal("100050"),
        index_price=Decimal("100000"),
        current_funding_rate=(
            Decimal("0.0002") if funding_available else None
        ),
        predicted_funding_rate=(
            Decimal("0.0001") if funding_available else None
        ),
        next_funding_time=NOW + timedelta(hours=1),
        funding_interval_seconds=8 * 60 * 60,
        open_interest=open_interest,
        open_interest_unit="CONTRACTS",
        open_interest_btc_equivalent=open_interest,
        open_interest_usd=open_interest * Decimal("100000"),
        funding_captured_at=NOW - timedelta(seconds=1),
        open_interest_captured_at=NOW - timedelta(seconds=2),
        received_at=NOW - timedelta(seconds=1),
        source="TEST",
        basis_bps=basis_bps,
        basis_reference_price_usd=Decimal("100000"),
        basis_captured_at=NOW - timedelta(seconds=1),
    )


def market(
    venue: MarketVenue,
    instrument_type: InstrumentType,
    *,
    depth: str = "10",
    status: MarketConnectionStatus = MarketConnectionStatus.LIVE,
    eligible: bool = True,
    exclusion_reason: str | None = None,
    include_book: bool = True,
    include_instrument: bool = True,
    funding_available: bool = True,
    instrument_executable: bool = True,
    basis_bps: Decimal = Decimal("25"),
    open_interest: Decimal = Decimal("12000"),
    quantity_step: str = "0.01",
    quantity_min: str = "0.05",
) -> ExecutableBookView:
    rules = instrument_rules(
        venue,
        instrument_type,
        executable=instrument_executable,
        quantity_step=quantity_step,
        quantity_min=quantity_min,
    )
    level_quantity = Decimal(depth)
    book = (
        ExecutableOrderBook(
            venue=venue,
            symbol="BTC-USD",
            venue_symbol=rules.venue_symbol,
            instrument_type=instrument_type,
            max_levels=200,
            bids=(
                ExecutableMarketLevel(
                    price=Decimal("99900"),
                    quantity_btc_equivalent=level_quantity,
                    source_quantity=level_quantity,
                    source_quantity_unit=rules.native_quantity_unit,
                ),
            ),
            asks=(
                ExecutableMarketLevel(
                    price=Decimal("100100"),
                    quantity_btc_equivalent=level_quantity,
                    source_quantity=level_quantity,
                    source_quantity_unit=rules.native_quantity_unit,
                ),
            ),
            exchange_timestamp=NOW,
            received_at=NOW,
            source_sequence=101,
        )
        if include_book
        else None
    )
    derivatives = (
        derivative_context(
            venue,
            rules.venue_symbol,
            funding_available=funding_available,
            basis_bps=basis_bps,
            open_interest=open_interest,
        )
        if instrument_type is InstrumentType.PERPETUAL
        else None
    )
    return ExecutableBookView(
        venue=venue,
        symbol="BTC-USD",
        instrument_type=instrument_type,
        connection=MarketConnectionState(
            feed_id=f"{venue.value}-{instrument_type.value}",
            venue=venue,
            status=status,
            endpoint="public",
            connected_at=NOW,
            last_message_at=NOW,
            last_book_update_at=NOW,
        ),
        book=book,
        instrument=rules if include_instrument else None,
        derivatives=derivatives,
        book_data_age_ms=0 if book is not None else None,
        derivative_data_age_ms=1000 if derivatives is not None else None,
        derivative_data_stale=False if derivatives is not None else None,
        funding_data_age_ms=1000 if derivatives is not None else None,
        funding_data_stale=False if derivatives is not None else None,
        eligible=eligible,
        exclusion_reason=exclusion_reason,
        as_of=NOW,
    )


def six_markets(*, depth: str = "10") -> tuple[ExecutableBookView, ...]:
    return tuple(
        market(venue, instrument_type, depth=depth)
        for venue in MarketVenue
        for instrument_type in InstrumentType
    )


def snapshot(
    markets: tuple[ExecutableBookView, ...] | None = None,
) -> ExecutableMarketSnapshot:
    return ExecutableMarketSnapshot(
        snapshot_version=SNAPSHOT_VERSION,
        captured_at=NOW,
        base_asset="BTC",
        markets=markets if markets is not None else six_markets(),
    )


def configured_fees(
    *,
    exclude: tuple[tuple[MarketVenue, InstrumentType], ...] = (),
) -> ExecutionFeeConfig:
    return ExecutionFeeConfig(
        entries=tuple(
            ExecutionFeeEntry(
                venue=venue,
                instrument_type=instrument_type,
                fee_bps=Decimal("1"),
                assumption_label="TEST_TAKER_FEE",
            )
            for venue in MarketVenue
            for instrument_type in InstrumentType
            if (venue, instrument_type) not in exclude
        )
    )


def optimization_input(
    requirement: str = "18",
    *,
    horizon: int | None = 2 * 60 * 60,
    conflict: bool = False,
    overhedge: bool = False,
) -> HedgeOptimizationInput:
    remaining = Decimal(requirement)
    actual = Decimal("-20") if remaining >= 0 else Decimal("20")
    return HedgeOptimizationInput(
        optimization_id="opt-acceptance",
        actual_delta_btc=actual,
        target_delta_btc=actual + remaining,
        remaining_hedge_requirement_btc=remaining,
        expected_holding_seconds=horizon,
        desk_state_version=7,
        risk_assessment_id="risk-acceptance",
        market_snapshot_version=SNAPSHOT_VERSION,
        requested_at=NOW,
        working_order_conflict=conflict,
        working_order_overhedge=overhedge,
    )


def build(
    request: HedgeOptimizationInput | None = None,
    markets: tuple[ExecutableBookView, ...] | None = None,
    fees: ExecutionFeeConfig | None = None,
):
    return build_hedge_candidates(
        request or optimization_input(),
        snapshot(markets),
        fees or configured_fees(),
    )


def replace_market(
    markets: tuple[ExecutableBookView, ...],
    replacement: ExecutableBookView,
) -> tuple[ExecutableBookView, ...]:
    return tuple(
        replacement
        if item.venue is replacement.venue
        and item.instrument_type is replacement.instrument_type
        else item
        for item in markets
    )


def candidate_for(result, venue: MarketVenue, instrument_type: InstrumentType):
    return next(
        candidate
        for candidate in result.eligible_candidates + result.excluded_candidates
        if candidate.venue is venue
        and candidate.instrument_type is instrument_type
    )


def test_all_six_healthy_markets_are_considered_on_one_snapshot() -> None:
    result = build()

    assert result.status is CandidateBuilderStatus.READY
    assert len(result.eligible_candidates) == 6
    assert len(result.excluded_candidates) == 0
    assert {candidate.market_snapshot_version for candidate in result.eligible_candidates} == {
        SNAPSHOT_VERSION
    }
    assert all(candidate.normalized_book_reference for candidate in result.eligible_candidates)
    assert not hasattr(result, "hedge_plan")
    assert not hasattr(result, "allocations")


@pytest.mark.parametrize(
    ("status", "expected_reason"),
    [
        (MarketConnectionStatus.STALE, CandidateExclusionReason.MARKET_STALE),
        (
            MarketConnectionStatus.DISCONNECTED,
            CandidateExclusionReason.MARKET_DISCONNECTED,
        ),
    ],
)
def test_unhealthy_market_is_excluded_while_others_remain(
    status: MarketConnectionStatus,
    expected_reason: CandidateExclusionReason,
) -> None:
    changed = market(
        MarketVenue.KRAKEN,
        InstrumentType.SPOT,
        status=status,
        eligible=False,
        exclusion_reason=f"FEED_{status.value}",
    )
    result = build(markets=replace_market(six_markets(), changed))

    assert len(result.eligible_candidates) == 5
    assert candidate_for(result, MarketVenue.KRAKEN, InstrumentType.SPOT).exclusion_reason is expected_reason


@pytest.mark.parametrize("instrument_type", list(InstrumentType))
def test_partial_spot_or_perp_depth_remains_eligible(
    instrument_type: InstrumentType,
) -> None:
    changed = market(MarketVenue.OKX, instrument_type, depth="6")
    result = build(markets=replace_market(six_markets(), changed))
    candidate = candidate_for(result, MarketVenue.OKX, instrument_type)

    assert candidate.eligible is True
    assert candidate.max_executable_quantity_btc == Decimal("6")
    assert candidate.fully_executable_for_full_requirement is False
    assert "PARTIAL_EXECUTABLE_DEPTH" in candidate.data_quality_flags


def test_no_executable_depth_is_excluded() -> None:
    changed = market(
        MarketVenue.OKX,
        InstrumentType.SPOT,
        include_book=False,
        eligible=False,
        exclusion_reason="EXECUTABLE_BOOK_UNAVAILABLE",
    )
    result = build(markets=replace_market(six_markets(), changed))

    assert candidate_for(result, MarketVenue.OKX, InstrumentType.SPOT).exclusion_reason is CandidateExclusionReason.NO_EXECUTABLE_DEPTH


def test_unconfigured_fee_is_an_economics_incomplete_exclusion() -> None:
    missing = (MarketVenue.COINBASE, InstrumentType.SPOT)
    result = build(fees=configured_fees(exclude=(missing,)))
    candidate = candidate_for(result, *missing)

    assert candidate.eligible is False
    assert candidate.exclusion_reason is CandidateExclusionReason.FEE_UNCONFIGURED
    assert candidate.immediate_economics_available is False
    assert "ECONOMICS_INCOMPLETE" in candidate.data_quality_flags
    assert candidate.immediate_cost_reference is not None


def test_every_unconfigured_fee_returns_clear_data_failure() -> None:
    result = build(fees=ExecutionFeeConfig())

    assert result.status is CandidateBuilderStatus.OPTIMIZATION_DATA_UNAVAILABLE
    assert len(result.excluded_candidates) == 6
    assert "NO_COMPARABLE_COST_CANDIDATES" in result.data_quality_flags


def test_perp_funding_unavailable_when_needed_is_excluded() -> None:
    changed = market(
        MarketVenue.KRAKEN,
        InstrumentType.PERPETUAL,
        funding_available=False,
    )
    result = build(markets=replace_market(six_markets(), changed))
    candidate = candidate_for(result, MarketVenue.KRAKEN, InstrumentType.PERPETUAL)

    assert candidate.exclusion_reason is CandidateExclusionReason.FUNDING_DATA_UNAVAILABLE
    assert candidate.immediate_economics_available is True
    assert candidate.carry_economics_available is False
    assert "FUNDING_RATE_UNAVAILABLE" in candidate.data_quality_flags


def test_missing_horizon_degrades_to_spot_only_candidates() -> None:
    result = build(request=optimization_input(horizon=None))

    assert {candidate.instrument_type for candidate in result.eligible_candidates} == {
        InstrumentType.SPOT
    }
    assert len(result.eligible_candidates) == 3
    assert all(
        candidate.exclusion_reason
        is CandidateExclusionReason.HOLDING_HORIZON_UNAVAILABLE
        for candidate in result.excluded_candidates
    )


@pytest.mark.parametrize(
    ("requirement", "expected_side"),
    [("18", ExecutionSide.BUY), ("-18", ExecutionSide.SELL)],
)
def test_signed_requirement_determines_normalized_side(
    requirement: str,
    expected_side: ExecutionSide,
) -> None:
    result = build(request=optimization_input(requirement))

    assert result.side is expected_side
    assert all(candidate.side is expected_side for candidate in result.eligible_candidates)
    assert all(candidate.requested_requirement_btc == Decimal("18") for candidate in result.eligible_candidates)


def test_zero_requirement_returns_no_hedge_without_building_candidates() -> None:
    result = build(request=optimization_input("0"))

    assert result.status is CandidateBuilderStatus.NO_HEDGE_REQUIRED
    assert result.side is None
    assert result.eligible_candidates == result.excluded_candidates == ()
    assert result.full_requirement_possible is True


@pytest.mark.parametrize(
    ("conflict", "overhedge", "expected_status"),
    [
        (
            True,
            False,
            CandidateBuilderStatus.OPTIMIZATION_BLOCKED_WORKING_ORDER_CONFLICT,
        ),
        (
            False,
            True,
            CandidateBuilderStatus.OPTIMIZATION_BLOCKED_WORKING_ORDER_OVERHEDGE,
        ),
    ],
)
def test_risk_policy_working_order_safety_gate_blocks_optimization(
    conflict: bool,
    overhedge: bool,
    expected_status: CandidateBuilderStatus,
) -> None:
    result = build(
        request=optimization_input("0" if overhedge else "18", conflict=conflict, overhedge=overhedge)
    )

    assert result.status is expected_status
    assert result.eligible_candidates == result.excluded_candidates == ()


def test_risk_assessment_factory_does_not_subtract_working_orders_twice() -> None:
    desk = DeskState(
        version=3,
        as_of=NOW,
        spot_inventory_btc=Decimal("-12"),
        derivative_delta_btc=Decimal("0"),
        total_delta_btc=Decimal("-12"),
        working_order_delta_btc=Decimal("1.25"),
    )
    reference = RiskReferencePrice(
        asset="BTC",
        price_usd=Decimal("100000"),
        captured_at=NOW,
        source="TEST",
        market_snapshot_version=SNAPSHOT_VERSION,
        eligible=True,
        degraded=False,
    )
    assessment = RiskPolicy().evaluate(desk, reference, assessed_at=NOW)
    request = HedgeOptimizationInput.from_risk_assessment(
        assessment,
        optimization_id="from-risk",
        expected_holding_seconds=3600,
    )

    assert assessment.advisory_gross_required_hedge_delta_btc == Decimal("2")
    assert request.remaining_hedge_requirement_btc == Decimal("0.75")
    assert request.side is ExecutionSide.BUY


def test_candidate_retains_normalized_execution_constraints() -> None:
    changed = market(
        MarketVenue.COINBASE,
        InstrumentType.PERPETUAL,
        quantity_step="0.02",
        quantity_min="0.10",
    )
    result = build(markets=replace_market(six_markets(), changed))
    candidate = candidate_for(result, MarketVenue.COINBASE, InstrumentType.PERPETUAL)

    assert candidate.native_quantity_unit == "CONTRACTS"
    assert candidate.native_quantity_step == Decimal("0.02")
    assert candidate.btc_equivalent_quantity_step == Decimal("0.02")
    assert candidate.minimum_order_quantity == Decimal("0.10")
    assert candidate.minimum_order_quantity_btc_equivalent == Decimal("0.10")
    assert candidate.contract_multiplier == Decimal("1")


def test_basis_and_open_interest_are_context_only() -> None:
    extreme = market(
        MarketVenue.OKX,
        InstrumentType.PERPETUAL,
        basis_bps=Decimal("5000"),
        open_interest=Decimal("999999999"),
    )
    result = build(markets=replace_market(six_markets(), extreme))
    candidate = candidate_for(result, MarketVenue.OKX, InstrumentType.PERPETUAL)

    assert candidate.eligible is True
    assert candidate.entry_basis_bps == Decimal("5000")
    assert candidate.open_interest_context is not None
    assert candidate.open_interest_context.open_interest == Decimal("999999999")


@pytest.mark.parametrize(
    ("depth", "possible"),
    [("2", False), ("4", True)],
)
def test_combined_eligible_depth_determines_full_requirement_possibility(
    depth: str,
    possible: bool,
) -> None:
    result = build(markets=six_markets(depth=depth))

    assert result.total_eligible_depth_btc == Decimal(depth) * Decimal("6")
    assert result.full_requirement_possible is possible


class StaticSnapshotStore:
    def __init__(self, value: ExecutableMarketSnapshot) -> None:
        self.value = value
        self.calls = 0

    async def executable_snapshot(
        self, base_asset: str | None = None
    ) -> ExecutableMarketSnapshot:
        assert base_asset == "BTC"
        self.calls += 1
        return self.value


def test_service_uses_one_atomic_snapshot_and_returns_structured_output() -> None:
    store = StaticSnapshotStore(snapshot())
    service = HedgeCandidateBuilderService(store, configured_fees())  # type: ignore[arg-type]

    result = run(service.build(optimization_input()))

    assert store.calls == 1
    assert result.status is CandidateBuilderStatus.READY
    assert result.market_snapshot_version == SNAPSHOT_VERSION
