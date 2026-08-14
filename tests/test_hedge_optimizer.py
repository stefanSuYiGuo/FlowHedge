from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from backend.domain.models import InstrumentType
from backend.execution_cost.engine import estimate_execution_cost
from backend.execution_cost.models import (
    ExecutionCostRequest,
    ExecutionFeeConfig,
    ExecutionFeeEntry,
    ExecutionSide,
)
from backend.hedge_optimizer.allocator import allocate_hedge
from backend.hedge_optimizer.candidate_builder import build_hedge_candidates
from backend.hedge_optimizer.models import (
    HedgeOptimizationInput,
    HedgePlanStatus,
)
from backend.hedge_optimizer.service import HedgeOptimizerService
from backend.hedge_economics.engine import calculate_hedge_economics
from backend.hedge_economics.models import HedgeEconomicsRequest
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


NOW = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
VERSION = 77


def run(coroutine):
    return asyncio.run(coroutine)


def rules(
    venue: MarketVenue,
    instrument_type: InstrumentType,
    *,
    quantity_step: str = "0.01",
    quantity_min: str = "0.01",
    contract_multiplier: str = "1",
) -> InstrumentRules:
    is_spot = instrument_type is InstrumentType.SPOT
    return InstrumentRules(
        venue=venue,
        symbol="BTC-USD",
        venue_symbol=f"{venue.value}-{'SPOT' if is_spot else 'PERP'}",
        instrument_type=instrument_type,
        base_asset="BTC",
        quote_asset="USD",
        price_increment=Decimal("0.01"),
        quantity_increment=Decimal(quantity_step),
        quantity_min=Decimal(quantity_min),
        price_precision=2,
        quantity_precision=4,
        status="LIVE",
        eligible_for_execution=True,
        contract_structure=(
            ContractStructure.SPOT if is_spot else ContractStructure.LINEAR
        ),
        contract_multiplier=Decimal(contract_multiplier),
        contract_value_currency=None if is_spot else "BTC",
        native_quantity_unit="BTC" if is_spot else "CONTRACTS",
        settlement_asset="USD",
        usd_conversion_rate=Decimal("1"),
        received_at=NOW,
    )


def market(
    venue: MarketVenue,
    instrument_type: InstrumentType,
    *,
    bids: tuple[tuple[str, str], ...],
    asks: tuple[tuple[str, str], ...],
    funding_rate: str = "0",
    status: MarketConnectionStatus = MarketConnectionStatus.LIVE,
    basis_bps: str = "0",
    open_interest: str = "10000",
    quantity_step: str = "0.01",
    quantity_min: str = "0.01",
    contract_multiplier: str = "1",
) -> ExecutableBookView:
    instrument = rules(
        venue,
        instrument_type,
        quantity_step=quantity_step,
        quantity_min=quantity_min,
        contract_multiplier=contract_multiplier,
    )
    _, book = normalized_books_from_levels(
        rules=instrument,
        bids=tuple((Decimal(price), Decimal(quantity)) for price, quantity in bids),
        asks=tuple((Decimal(price), Decimal(quantity)) for price, quantity in asks),
        exchange_timestamp=NOW,
        received_at=NOW,
    )
    context = None
    if instrument_type is InstrumentType.PERPETUAL:
        oi = Decimal(open_interest)
        context = DerivativeMarketContext(
            venue=venue,
            symbol="BTC-USD",
            venue_symbol=instrument.venue_symbol,
            mark_price=Decimal("100000"),
            index_price=Decimal("100000"),
            current_funding_rate=Decimal(funding_rate),
            predicted_funding_rate=Decimal(funding_rate),
            next_funding_time=NOW + timedelta(hours=1),
            funding_interval_seconds=8 * 60 * 60,
            open_interest=oi,
            open_interest_unit="CONTRACTS",
            open_interest_btc_equivalent=oi,
            open_interest_usd=oi * Decimal("100000"),
            funding_captured_at=NOW,
            open_interest_captured_at=NOW,
            received_at=NOW,
            source="TEST",
            basis_bps=Decimal(basis_bps),
            basis_reference_price_usd=Decimal("100000"),
            basis_captured_at=NOW,
        )
    live = status is MarketConnectionStatus.LIVE
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
        instrument=instrument,
        derivatives=context,
        book_data_age_ms=0,
        derivative_data_age_ms=0 if context else None,
        derivative_data_stale=False if context else None,
        funding_data_age_ms=0 if context else None,
        funding_data_stale=False if context else None,
        eligible=live,
        exclusion_reason=None if live else f"FEED_{status.value}",
        as_of=NOW,
    )


def snapshot(markets: tuple[ExecutableBookView, ...]) -> ExecutableMarketSnapshot:
    return ExecutableMarketSnapshot(
        snapshot_version=VERSION,
        captured_at=NOW,
        base_asset="BTC",
        markets=markets,
    )


def fees_for(markets: tuple[ExecutableBookView, ...]) -> ExecutionFeeConfig:
    return ExecutionFeeConfig(
        entries=tuple(
            ExecutionFeeEntry(
                venue=item.venue,
                instrument_type=item.instrument_type,
                fee_bps=Decimal("0"),
                assumption_label="TEST_ZERO_FEE",
            )
            for item in markets
        )
    )


def request(
    requirement: str,
    *,
    actual: str | None = None,
    working: str = "0",
    horizon: int = 2 * 60 * 60,
) -> HedgeOptimizationInput:
    required = Decimal(requirement)
    actual_delta = (
        Decimal(actual)
        if actual is not None
        else Decimal("-20")
        if required >= 0
        else Decimal("20")
    )
    working_delta = Decimal(working)
    return HedgeOptimizationInput(
        optimization_id="optimizer-acceptance",
        actual_delta_btc=actual_delta,
        target_delta_btc=actual_delta + working_delta + required,
        remaining_hedge_requirement_btc=required,
        qualifying_working_order_delta_btc=working_delta,
        reference_price_usd=Decimal("100000"),
        expected_holding_seconds=horizon,
        desk_state_version=12,
        risk_assessment_id="risk-12",
        market_snapshot_version=VERSION,
        requested_at=NOW,
    )


def optimize(
    markets: tuple[ExecutableBookView, ...],
    requirement: str,
    *,
    optimization_request: HedgeOptimizationInput | None = None,
):
    state = snapshot(markets)
    fee_config = fees_for(markets)
    normalized_request = optimization_request or request(requirement)
    candidates = build_hedge_candidates(normalized_request, state, fee_config)
    return allocate_hedge(normalized_request, candidates, state, fee_config)


def leg_quantities(plan):
    return {
        (leg.venue, leg.instrument_type): leg.quantity_btc for leg in plan.legs
    }


def test_one_clearly_cheapest_market_receives_all_quantity() -> None:
    cheap = market(
        MarketVenue.OKX,
        InstrumentType.SPOT,
        bids=(("99999", "10"),),
        asks=(("100001", "10"),),
    )
    expensive = market(
        MarketVenue.COINBASE,
        InstrumentType.SPOT,
        bids=(("99950", "10"),),
        asks=(("100050", "10"),),
    )
    plan = optimize((cheap, expensive), "5")

    assert plan.status is HedgePlanStatus.FULLY_FEASIBLE
    assert leg_quantities(plan) == {
        (MarketVenue.OKX, InstrumentType.SPOT): Decimal("5")
    }


def test_configured_taker_fee_is_part_of_marginal_selection() -> None:
    tight_but_high_fee = market(
        MarketVenue.OKX,
        InstrumentType.SPOT,
        bids=(("99999", "10"),),
        asks=(("100001", "10"),),
    )
    wider_but_zero_fee = market(
        MarketVenue.COINBASE,
        InstrumentType.SPOT,
        bids=(("99990", "10"),),
        asks=(("100010", "10"),),
    )
    markets = (tight_but_high_fee, wider_but_zero_fee)
    state = snapshot(markets)
    fee_config = ExecutionFeeConfig(
        entries=(
            ExecutionFeeEntry(
                venue=MarketVenue.OKX,
                instrument_type=InstrumentType.SPOT,
                fee_bps=Decimal("10"),
                assumption_label="HIGH_FEE",
            ),
            ExecutionFeeEntry(
                venue=MarketVenue.COINBASE,
                instrument_type=InstrumentType.SPOT,
                fee_bps=Decimal("0"),
                assumption_label="ZERO_FEE",
            ),
        )
    )
    normalized_request = request("5")
    candidates = build_hedge_candidates(
        normalized_request,
        state,
        fee_config,
    )
    plan = allocate_hedge(
        normalized_request,
        candidates,
        state,
        fee_config,
    )

    assert len(plan.legs) == 1
    assert plan.legs[0].venue is MarketVenue.COINBASE


def test_cheapest_partial_depth_splits_with_next_market() -> None:
    okx = market(
        MarketVenue.OKX,
        InstrumentType.SPOT,
        bids=(("99999", "20"),),
        asks=(("100001", "3"), ("100100", "20")),
    )
    coinbase = market(
        MarketVenue.COINBASE,
        InstrumentType.SPOT,
        bids=(("99990", "20"),),
        asks=(("100010", "20"),),
    )
    plan = optimize((okx, coinbase), "8")

    assert leg_quantities(plan) == {
        (MarketVenue.COINBASE, InstrumentType.SPOT): Decimal("5"),
        (MarketVenue.OKX, InstrumentType.SPOT): Decimal("3"),
    }


def test_three_market_split_beats_every_single_full_market_execution() -> None:
    markets = (
        market(
            MarketVenue.KRAKEN,
            InstrumentType.SPOT,
            bids=(("99999", "20"),),
            asks=(("100001", "2"), ("101000", "20")),
        ),
        market(
            MarketVenue.COINBASE,
            InstrumentType.SPOT,
            bids=(("99998", "20"),),
            asks=(("100002", "3"), ("101000", "20")),
        ),
        market(
            MarketVenue.OKX,
            InstrumentType.SPOT,
            bids=(("99997", "20"),),
            asks=(("100003", "4"), ("101000", "20")),
        ),
    )
    state = snapshot(markets)
    fee_config = fees_for(markets)
    plan = optimize(markets, "9")
    single_market_costs = []
    for item in markets:
        result = estimate_execution_cost(
            ExecutionCostRequest(
                request_id=f"single-{item.venue.value}",
                venue=item.venue,
                instrument_id=item.instrument.venue_symbol,
                instrument_type=item.instrument_type,
                side=ExecutionSide.BUY,
                quantity_btc_equivalent=Decimal("9"),
                market_snapshot_version=VERSION,
                requested_at=NOW,
            ),
            state,
            fee_config,
        )
        single_market_costs.append(result.all_in_immediate_cost_usd)

    assert len(plan.legs) == 3
    assert set(leg_quantities(plan).values()) == {
        Decimal("2"),
        Decimal("3"),
        Decimal("4"),
    }
    assert all(
        plan.total_expected_cost_usd < cost
        for cost in single_market_costs
        if cost is not None
    )


def test_positive_funding_makes_long_perp_less_attractive_than_spot() -> None:
    spot = market(
        MarketVenue.COINBASE,
        InstrumentType.SPOT,
        bids=(("99990", "10"),),
        asks=(("100010", "10"),),
    )
    perp = market(
        MarketVenue.OKX,
        InstrumentType.PERPETUAL,
        bids=(("99995", "10"),),
        asks=(("100005", "10"),),
        funding_rate="0.0002",
    )
    plan = optimize((spot, perp), "5")

    assert len(plan.legs) == 1
    assert plan.legs[0].instrument_type is InstrumentType.SPOT


def test_negative_funding_credit_can_make_long_perp_more_attractive() -> None:
    spot = market(
        MarketVenue.COINBASE,
        InstrumentType.SPOT,
        bids=(("99990", "10"),),
        asks=(("100010", "10"),),
    )
    perp = market(
        MarketVenue.OKX,
        InstrumentType.PERPETUAL,
        bids=(("99950", "10"),),
        asks=(("100050", "10"),),
        funding_rate="-0.001",
    )
    plan = optimize((spot, perp), "5")

    assert len(plan.legs) == 1
    assert plan.legs[0].instrument_type is InstrumentType.PERPETUAL
    assert plan.legs[0].expected_funding_cost_usd < 0


def test_sell_hedge_uses_bid_liquidity_symmetrically() -> None:
    cheap = market(
        MarketVenue.KRAKEN,
        InstrumentType.SPOT,
        bids=(("99999", "10"),),
        asks=(("100001", "10"),),
    )
    expensive = market(
        MarketVenue.OKX,
        InstrumentType.SPOT,
        bids=(("99950", "10"),),
        asks=(("100050", "10"),),
    )
    plan = optimize((cheap, expensive), "-4")

    assert plan.allocated_hedge_delta_btc == Decimal("-4")
    assert len(plan.legs) == 1
    assert plan.legs[0].venue is MarketVenue.KRAKEN
    assert plan.legs[0].side is ExecutionSide.SELL


def test_candidate_depth_is_never_exceeded() -> None:
    shallow = market(
        MarketVenue.OKX,
        InstrumentType.SPOT,
        bids=(("99999", "2"),),
        asks=(("100001", "2"),),
    )
    deep = market(
        MarketVenue.COINBASE,
        InstrumentType.SPOT,
        bids=(("99990", "10"),),
        asks=(("100010", "10"),),
    )
    plan = optimize((shallow, deep), "6")

    assert leg_quantities(plan)[
        (MarketVenue.OKX, InstrumentType.SPOT)
    ] == Decimal("2")


def test_insufficient_combined_depth_is_partially_feasible() -> None:
    markets = (
        market(
            MarketVenue.KRAKEN,
            InstrumentType.SPOT,
            bids=(("99999", "1"),),
            asks=(("100001", "1"),),
        ),
        market(
            MarketVenue.COINBASE,
            InstrumentType.SPOT,
            bids=(("99998", "1"),),
            asks=(("100002", "1"),),
        ),
    )
    plan = optimize(markets, "3")

    assert plan.status is HedgePlanStatus.PARTIALLY_FEASIBLE
    assert plan.allocated_hedge_delta_btc == Decimal("2")
    assert plan.residual_unallocated_delta_btc == Decimal("1")
    assert plan.explanation_data.residual_reason is not None


def test_no_valid_candidates_returns_no_feasible_hedge() -> None:
    stale = market(
        MarketVenue.KRAKEN,
        InstrumentType.SPOT,
        bids=(("99999", "10"),),
        asks=(("100001", "10"),),
        status=MarketConnectionStatus.STALE,
    )
    plan = optimize((stale,), "3")

    assert plan.status is HedgePlanStatus.NO_FEASIBLE_HEDGE
    assert plan.legs == ()
    assert plan.allocated_hedge_delta_btc == 0


def test_working_order_conflict_returns_optimization_blocked() -> None:
    spot = market(
        MarketVenue.KRAKEN,
        InstrumentType.SPOT,
        bids=(("99999", "10"),),
        asks=(("100001", "10"),),
    )
    state = snapshot((spot,))
    fee_config = fees_for((spot,))
    normalized_request = request("3").model_copy(
        update={"working_order_conflict": True}
    )
    candidates = build_hedge_candidates(
        normalized_request,
        state,
        fee_config,
    )
    plan = allocate_hedge(
        normalized_request,
        candidates,
        state,
        fee_config,
    )

    assert plan.status is HedgePlanStatus.OPTIMIZATION_BLOCKED
    assert plan.legs == ()


def test_stale_candidate_never_receives_an_allocation() -> None:
    stale = market(
        MarketVenue.KRAKEN,
        InstrumentType.SPOT,
        bids=(("99999.9", "10"),),
        asks=(("100000.1", "10"),),
        status=MarketConnectionStatus.STALE,
    )
    live = market(
        MarketVenue.COINBASE,
        InstrumentType.SPOT,
        bids=(("99950", "10"),),
        asks=(("100050", "10"),),
    )
    plan = optimize((stale, live), "3")

    assert {leg.venue for leg in plan.legs} == {MarketVenue.COINBASE}
    assert {
        fact.venue for fact in plan.explanation_data.excluded_candidate_facts
    } == {MarketVenue.KRAKEN}


def test_contract_steps_are_respected_and_round_down_without_overhedge() -> None:
    perp = market(
        MarketVenue.OKX,
        InstrumentType.PERPETUAL,
        bids=(("99999", "100"),),
        asks=(("100001", "100"),),
        quantity_step="1",
        quantity_min="1",
        contract_multiplier="0.1",
    )
    plan = optimize((perp,), "0.35")

    assert plan.status is HedgePlanStatus.PARTIALLY_FEASIBLE
    assert plan.legs[0].native_quantity == Decimal("3")
    assert plan.legs[0].quantity_btc == Decimal("0.3")
    assert plan.allocated_hedge_delta_btc == Decimal("0.3")
    assert plan.residual_unallocated_delta_btc == Decimal("0.05")
    assert plan.allocated_hedge_delta_btc < plan.requested_hedge_delta_btc


def test_requirement_below_minimum_never_rounds_up_into_overhedge() -> None:
    spot = market(
        MarketVenue.OKX,
        InstrumentType.SPOT,
        bids=(("99999", "10"),),
        asks=(("100001", "10"),),
        quantity_step="0.1",
        quantity_min="0.1",
    )
    plan = optimize((spot,), "0.05")

    assert plan.status is HedgePlanStatus.NO_FEASIBLE_HEDGE
    assert plan.allocated_hedge_delta_btc == 0
    assert plan.residual_unallocated_delta_btc == Decimal("0.05")


def test_final_spot_leg_economics_reconcile_with_step_8a() -> None:
    spot = market(
        MarketVenue.OKX,
        InstrumentType.SPOT,
        bids=(("99999", "10"),),
        asks=(("100001", "2"), ("100010", "8")),
    )
    state = snapshot((spot,))
    fee_config = fees_for((spot,))
    plan = optimize((spot,), "5")
    leg = plan.legs[0]
    expected = estimate_execution_cost(
        ExecutionCostRequest(
            request_id="reconcile",
            venue=leg.venue,
            instrument_id=leg.instrument_id,
            instrument_type=leg.instrument_type,
            side=leg.side,
            quantity_btc_equivalent=leg.quantity_btc,
            market_snapshot_version=VERSION,
            requested_at=NOW,
        ),
        state,
        fee_config,
    )

    assert leg.expected_vwap == expected.execution_vwap
    assert leg.expected_immediate_cost_usd == expected.all_in_immediate_cost_usd
    assert leg.expected_fills == expected.fills


def test_final_perp_leg_economics_reconcile_with_step_8b() -> None:
    perp = market(
        MarketVenue.OKX,
        InstrumentType.PERPETUAL,
        bids=(("99999", "10"),),
        asks=(("100001", "10"),),
        funding_rate="0.0001",
    )
    state = snapshot((perp,))
    fee_config = fees_for((perp,))
    plan = optimize((perp,), "4")
    leg = plan.legs[0]
    execution = estimate_execution_cost(
        ExecutionCostRequest(
            request_id="perp-reconcile",
            venue=leg.venue,
            instrument_id=leg.instrument_id,
            instrument_type=leg.instrument_type,
            side=leg.side,
            quantity_btc_equivalent=leg.quantity_btc,
            market_snapshot_version=VERSION,
            requested_at=NOW,
        ),
        state,
        fee_config,
    )
    economics = calculate_hedge_economics(
        HedgeEconomicsRequest(
            request_id="perp-reconcile",
            execution_cost_result_id=execution.result_id,
            expected_holding_seconds=2 * 60 * 60,
            market_snapshot_version=VERSION,
            requested_at=NOW,
        ),
        execution,
        perp,
    )

    assert leg.expected_funding_cost_usd == economics.expected_funding_cost_usd
    assert leg.expected_total_cost_bps == economics.expected_total_hedge_cost_bps
    assert leg.expected_total_cost_usd == economics.expected_total_hedge_cost_usd


def test_total_plan_cost_equals_sum_of_exact_leg_costs() -> None:
    first = market(
        MarketVenue.OKX,
        InstrumentType.SPOT,
        bids=(("99999", "2"),),
        asks=(("100001", "2"),),
    )
    second = market(
        MarketVenue.COINBASE,
        InstrumentType.SPOT,
        bids=(("99998", "3"),),
        asks=(("100002", "3"),),
    )
    plan = optimize((first, second), "5")

    assert plan.total_expected_cost_usd == sum(
        (leg.expected_total_cost_usd for leg in plan.legs), Decimal("0")
    )


def test_basis_and_oi_are_explanatory_and_do_not_change_allocation() -> None:
    normal = market(
        MarketVenue.OKX,
        InstrumentType.PERPETUAL,
        bids=(("99999", "10"),),
        asks=(("100001", "10"),),
        basis_bps="0",
        open_interest="1",
    )
    extreme = market(
        MarketVenue.OKX,
        InstrumentType.PERPETUAL,
        bids=(("99999", "10"),),
        asks=(("100001", "10"),),
        basis_bps="9000",
        open_interest="999999999",
    )
    normal_plan = optimize((normal,), "4")
    extreme_plan = optimize((extreme,), "4")

    assert normal_plan.allocated_hedge_delta_btc == extreme_plan.allocated_hedge_delta_btc
    assert normal_plan.total_expected_cost_usd == extreme_plan.total_expected_cost_usd
    assert extreme_plan.legs[0].entry_basis_bps == Decimal("9000")
    assert extreme_plan.legs[0].open_interest_context.open_interest == Decimal(
        "999999999"
    )


def test_working_orders_are_not_subtracted_again_and_projection_includes_them() -> None:
    spot = market(
        MarketVenue.KRAKEN,
        InstrumentType.SPOT,
        bids=(("99999", "10"),),
        asks=(("100001", "10"),),
    )
    normalized_request = request("0.75", actual="-12", working="1.25")
    plan = optimize(
        (spot,),
        "0.75",
        optimization_request=normalized_request,
    )

    assert plan.requested_hedge_delta_btc == Decimal("0.75")
    assert plan.allocated_hedge_delta_btc == Decimal("0.75")
    assert plan.projected_delta_btc == Decimal("-10")
    assert plan.projected_delta_notional_usd == Decimal("-1000000")
    assert plan.target_delta_btc == Decimal("-10")


def test_plan_references_exact_desk_risk_and_market_versions() -> None:
    spot = market(
        MarketVenue.KRAKEN,
        InstrumentType.SPOT,
        bids=(("99999", "10"),),
        asks=(("100001", "10"),),
    )
    plan = optimize((spot,), "2")

    assert plan.desk_state_version == 12
    assert plan.risk_assessment_id == "risk-12"
    assert plan.market_snapshot_version == VERSION
    assert all(leg.market_snapshot_version == VERSION for leg in plan.legs)


def test_repeated_identical_inputs_produce_deterministic_plan() -> None:
    markets = (
        market(
            MarketVenue.KRAKEN,
            InstrumentType.SPOT,
            bids=(("99999", "10"),),
            asks=(("100001", "10"),),
        ),
        market(
            MarketVenue.COINBASE,
            InstrumentType.SPOT,
            bids=(("99999", "10"),),
            asks=(("100001", "10"),),
        ),
    )

    assert optimize(markets, "5").model_dump() == optimize(
        markets, "5"
    ).model_dump()


def test_explanation_data_is_structured_from_actual_selection() -> None:
    cheap = market(
        MarketVenue.OKX,
        InstrumentType.SPOT,
        bids=(("99999", "2"),),
        asks=(("100001", "2"),),
    )
    next_market = market(
        MarketVenue.COINBASE,
        InstrumentType.SPOT,
        bids=(("99998", "3"),),
        asks=(("100002", "3"),),
    )
    plan = optimize((cheap, next_market), "5")

    assert plan.explanation_data.allocator_method == "GREEDY_MARGINAL_L2_V1"
    assert plan.explanation_data.selection_facts
    assert all(
        fact.reason_code == "LOWEST_AVAILABLE_MARGINAL_ECONOMICS"
        for fact in plan.explanation_data.selection_facts
    )


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


def test_optimizer_service_runs_candidate_builder_and_allocator_on_one_snapshot() -> None:
    spot = market(
        MarketVenue.KRAKEN,
        InstrumentType.SPOT,
        bids=(("99999", "10"),),
        asks=(("100001", "10"),),
    )
    state = snapshot((spot,))
    store = StaticSnapshotStore(state)
    service = HedgeOptimizerService(store, fees_for((spot,)))  # type: ignore[arg-type]

    plan = run(service.optimize(request("3")))

    assert store.calls == 1
    assert plan.status is HedgePlanStatus.FULLY_FEASIBLE
    assert plan.allocated_hedge_delta_btc == Decimal("3")
