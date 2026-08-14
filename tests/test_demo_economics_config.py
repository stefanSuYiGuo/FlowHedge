from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from backend.advisory.service import advisory_hedge_service
from backend.config import demo_desk_config
from backend.domain.models import InstrumentType
from backend.execution_cost.config import execution_fee_config
from backend.execution_cost.engine import estimate_execution_cost
from backend.execution_cost.models import (
    ExecutionCostRequest,
    ExecutionSide,
    FeeStatus,
)
from backend.hedge_optimizer.allocator import allocate_hedge
from backend.hedge_optimizer.candidate_builder import build_hedge_candidates
from backend.hedge_optimizer.models import (
    CandidateExclusionReason,
    HedgeOptimizationInput,
    HedgePlanStatus,
)
from backend.market.book import normalized_books_from_levels
from backend.market.models import (
    ContractStructure,
    DerivativeMarketContext,
    ExecutableBookView,
    ExecutableMarketSnapshot,
    InstrumentRules,
    MarketConnectionState,
    MarketConnectionStatus,
    MarketVenue,
)


NOW = datetime(2026, 8, 14, 20, 0, tzinfo=timezone.utc)
VERSION = 301


def rules(
    venue: MarketVenue,
    instrument_type: InstrumentType,
) -> InstrumentRules:
    spot = instrument_type is InstrumentType.SPOT
    return InstrumentRules(
        venue=venue,
        symbol="BTC-USD",
        venue_symbol=f"{venue.value}-{'SPOT' if spot else 'PERP'}",
        instrument_type=instrument_type,
        base_asset="BTC",
        quote_asset="USD",
        price_increment=Decimal("0.01"),
        quantity_increment=Decimal("0.01"),
        quantity_min=Decimal("0.01"),
        price_precision=2,
        quantity_precision=2,
        status="LIVE",
        eligible_for_execution=True,
        contract_structure=(
            ContractStructure.SPOT if spot else ContractStructure.LINEAR
        ),
        contract_multiplier=Decimal("1"),
        contract_value_currency=None if spot else "BTC",
        native_quantity_unit="BTC" if spot else "CONTRACTS",
        settlement_asset="USD",
        received_at=NOW,
    )


def market(
    venue: MarketVenue,
    instrument_type: InstrumentType,
    *,
    bid: str = "99990",
    ask: str = "100010",
    depth: str = "25",
    funding_rate: Decimal | None = Decimal("0"),
    funding_stale: bool = False,
) -> ExecutableBookView:
    instrument = rules(venue, instrument_type)
    _, book = normalized_books_from_levels(
        rules=instrument,
        bids=((Decimal(bid), Decimal(depth)),),
        asks=((Decimal(ask), Decimal(depth)),),
        exchange_timestamp=NOW,
        received_at=NOW,
    )
    derivatives = None
    if instrument_type is InstrumentType.PERPETUAL:
        derivatives = DerivativeMarketContext(
            venue=venue,
            symbol="BTC-USD",
            venue_symbol=instrument.venue_symbol,
            mark_price=Decimal("100000"),
            index_price=Decimal("100000"),
            current_funding_rate=funding_rate,
            predicted_funding_rate=funding_rate,
            next_funding_time=NOW + timedelta(hours=1),
            funding_interval_seconds=8 * 60 * 60,
            open_interest=Decimal("10000"),
            open_interest_unit="CONTRACTS",
            open_interest_btc_equivalent=Decimal("10000"),
            open_interest_usd=Decimal("1000000000"),
            funding_captured_at=NOW,
            open_interest_captured_at=NOW,
            received_at=NOW,
            source="TEST_RUNTIME_STYLE",
            basis_bps=Decimal("3"),
            basis_reference_price_usd=Decimal("100000"),
            basis_captured_at=NOW,
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
            connected_at=NOW,
            last_message_at=NOW,
            last_book_update_at=NOW,
        ),
        book=book,
        instrument=instrument,
        derivatives=derivatives,
        book_data_age_ms=0,
        derivative_data_age_ms=0 if derivatives else None,
        derivative_data_stale=False if derivatives else None,
        funding_data_age_ms=0 if derivatives else None,
        funding_data_stale=funding_stale if derivatives else None,
        eligible=True,
        as_of=NOW,
    )


def snapshot(
    markets: tuple[ExecutableBookView, ...],
) -> ExecutableMarketSnapshot:
    return ExecutableMarketSnapshot(
        snapshot_version=VERSION,
        captured_at=NOW,
        base_asset="BTC",
        markets=markets,
    )


def optimization_request(
    *,
    horizon: int | None = None,
) -> HedgeOptimizationInput:
    return HedgeOptimizationInput(
        optimization_id=f"demo-economics-{horizon}",
        actual_delta_btc=Decimal("-20"),
        target_delta_btc=Decimal("-10"),
        remaining_hedge_requirement_btc=Decimal("10"),
        reference_price_usd=Decimal("100000"),
        expected_holding_seconds=(
            horizon
            if horizon is not None
            else demo_desk_config.default_expected_hedge_horizon_seconds
        ),
        desk_state_version=1,
        risk_assessment_id="risk-demo-economics",
        market_snapshot_version=VERSION,
        requested_at=NOW,
    )


def test_demo_economics_defaults_and_all_six_fee_mappings() -> None:
    assert demo_desk_config.taker_fee_bps == Decimal("2.0")
    assert demo_desk_config.default_expected_hedge_horizon_seconds == 14400
    assert advisory_hedge_service.expected_holding_seconds == 14400
    assert advisory_hedge_service.demo_config is demo_desk_config
    assert len(execution_fee_config.entries) == 6

    for venue in MarketVenue:
        for instrument_type in InstrumentType:
            fee = execution_fee_config.taker_fee_for(venue, instrument_type)
            assert fee is not None
            assert fee.fee_bps == Decimal("2.0")
            assert "DEMO DESK ASSUMPTION" in fee.assumption_label
            assert "NOT ACTUAL OSL" in fee.assumption_label


def test_demo_fee_is_added_exactly_once_to_execution_cost() -> None:
    spot = market(MarketVenue.KRAKEN, InstrumentType.SPOT)
    state = snapshot((spot,))
    result = estimate_execution_cost(
        ExecutionCostRequest(
            request_id="demo-fee-once",
            venue=MarketVenue.KRAKEN,
            instrument_id=spot.instrument.venue_symbol,
            instrument_type=InstrumentType.SPOT,
            side=ExecutionSide.BUY,
            quantity_btc_equivalent=Decimal("5"),
            market_snapshot_version=VERSION,
            requested_at=NOW,
        ),
        state,
        execution_fee_config,
    )

    assert result.fee_status is FeeStatus.CONFIGURED
    assert result.taker_fee_bps == Decimal("2.0")
    assert result.fee_usd == result.executed_notional_usd * Decimal("2") / Decimal("10000")
    assert result.all_in_immediate_cost_usd == result.price_cost_usd + result.fee_usd


def test_healthy_spot_and_perpetual_candidates_use_runtime_assumptions() -> None:
    markets = (
        market(MarketVenue.KRAKEN, InstrumentType.SPOT),
        market(MarketVenue.OKX, InstrumentType.PERPETUAL),
    )
    request = optimization_request()
    candidates = build_hedge_candidates(
        request,
        snapshot(markets),
        execution_fee_config,
    )

    assert candidates.expected_holding_seconds == 14400
    assert len(candidates.eligible_candidates) == 2
    assert {candidate.instrument_type for candidate in candidates.eligible_candidates} == {
        InstrumentType.SPOT,
        InstrumentType.PERPETUAL,
    }


def test_explicit_horizon_override_is_preserved() -> None:
    request = optimization_request(horizon=3600)
    candidates = build_hedge_candidates(
        request,
        snapshot((market(MarketVenue.KRAKEN, InstrumentType.SPOT),)),
        execution_fee_config,
    )
    assert request.expected_holding_seconds == 3600
    assert candidates.expected_holding_seconds == 3600


def test_missing_funding_still_excludes_perpetual_candidate() -> None:
    perp = market(
        MarketVenue.OKX,
        InstrumentType.PERPETUAL,
        funding_rate=None,
    )
    candidates = build_hedge_candidates(
        optimization_request(),
        snapshot((perp,)),
        execution_fee_config,
    )

    assert candidates.eligible_candidates == ()
    assert candidates.excluded_candidates[0].exclusion_reason is (
        CandidateExclusionReason.FUNDING_DATA_UNAVAILABLE
    )


def test_runtime_style_config_produces_reconciled_feasible_plan() -> None:
    markets = (
        market(
            MarketVenue.KRAKEN,
            InstrumentType.SPOT,
            ask="100020",
        ),
        market(
            MarketVenue.OKX,
            InstrumentType.PERPETUAL,
            ask="100001",
            funding_rate=Decimal("-0.0001"),
        ),
    )
    state = snapshot(markets)
    request = optimization_request()
    candidates = build_hedge_candidates(request, state, execution_fee_config)
    plan = allocate_hedge(request, candidates, state, execution_fee_config)

    assert plan.status is HedgePlanStatus.FULLY_FEASIBLE
    assert plan.expected_holding_seconds == 14400
    assert plan.allocated_hedge_delta_btc == Decimal("10")
    assert plan.residual_unallocated_delta_btc == 0
    assert plan.total_expected_cost_usd == sum(
        (leg.expected_total_cost_usd for leg in plan.legs),
        Decimal("0"),
    )
    assert any(
        leg.instrument_type is InstrumentType.PERPETUAL for leg in plan.legs
    )
    for leg in plan.legs:
        assert leg.expected_total_cost_usd == (
            leg.expected_immediate_cost_usd + leg.expected_funding_cost_usd
        )
