"""Step 9.2 deterministic marginal-cost allocation over normalized L2 books."""

from __future__ import annotations

import heapq
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Optional

from ..domain.models import InstrumentType
from ..execution_cost.config import execution_fee_config
from ..execution_cost.engine import (
    estimate_execution_cost,
    marginal_execution_cost_usd_per_btc,
)
from ..execution_cost.models import (
    ExecutionCostRequest,
    ExecutionCostStatus,
    ExecutionFeeConfig,
    ExecutionSide,
)
from ..hedge_economics.engine import calculate_hedge_economics
from ..hedge_economics.models import (
    CarryStatus,
    HedgeEconomicsRequest,
    HedgeEconomicsStatus,
)
from ..market.models import (
    ContractStructure,
    ExecutableBookView,
    ExecutableMarketSnapshot,
    InstrumentRules,
    MarketConnectionStatus,
)
from .models import (
    CandidateBuilderResult,
    CandidateBuilderStatus,
    CandidateExclusionFact,
    FundingApplicability,
    HedgeCandidate,
    HedgeLeg,
    HedgeOptimizationInput,
    HedgePlan,
    HedgePlanExplanationData,
    HedgePlanStatus,
    MarginalSelectionFact,
)


BASIS_POINTS = Decimal("10000")
ALLOCATOR_METHOD = "GREEDY_MARGINAL_L2_V1"


@dataclass(frozen=True)
class _LiquiditySlice:
    price: Decimal
    native_quantity: Decimal
    btc_quantity: Decimal
    expected_cost_usd_per_btc: Decimal


@dataclass(frozen=True)
class _MarginalSegment:
    candidate_id: str
    index: int
    activation_segment: bool
    slices: tuple[_LiquiditySlice, ...]

    @property
    def native_quantity(self) -> Decimal:
        return sum(
            (item.native_quantity for item in self.slices), Decimal("0")
        )

    @property
    def btc_quantity(self) -> Decimal:
        return sum((item.btc_quantity for item in self.slices), Decimal("0"))

    @property
    def expected_cost_usd_per_btc(self) -> Decimal:
        total = sum(
            (
                item.btc_quantity * item.expected_cost_usd_per_btc
                for item in self.slices
            ),
            Decimal("0"),
        )
        return total / self.btc_quantity


def allocate_hedge(
    request: HedgeOptimizationInput,
    candidates: CandidateBuilderResult,
    snapshot: ExecutableMarketSnapshot,
    fee_config: ExecutionFeeConfig = execution_fee_config,
) -> HedgePlan:
    """Allocate RiskPolicy's remaining quantity without creating orders.

    The reachable-next-segment greedy method is optimal for this v1 model because
    it has no fixed order costs or cross-venue coupling, while L2 execution costs
    are monotonic and both taker fees and modeled funding are linear in notional.
    """

    input_errors = _input_consistency_errors(request, candidates, snapshot)
    if input_errors:
        return _empty_plan(
            request,
            candidates,
            snapshot,
            HedgePlanStatus.OPTIMIZATION_BLOCKED,
            input_errors,
        )
    if candidates.status is CandidateBuilderStatus.NO_HEDGE_REQUIRED:
        return _empty_plan(
            request,
            candidates,
            snapshot,
            HedgePlanStatus.NO_HEDGE_REQUIRED,
            list(candidates.data_quality_flags),
            fully_feasible=True,
        )
    if candidates.status in {
        CandidateBuilderStatus.OPTIMIZATION_BLOCKED_WORKING_ORDER_CONFLICT,
        CandidateBuilderStatus.OPTIMIZATION_BLOCKED_WORKING_ORDER_OVERHEDGE,
    }:
        return _empty_plan(
            request,
            candidates,
            snapshot,
            HedgePlanStatus.OPTIMIZATION_BLOCKED,
            list(candidates.data_quality_flags),
        )
    if candidates.status is not CandidateBuilderStatus.READY:
        return _empty_plan(
            request,
            candidates,
            snapshot,
            HedgePlanStatus.NO_FEASIBLE_HEDGE,
            list(candidates.data_quality_flags),
        )

    market_by_candidate: dict[str, ExecutableBookView] = {}
    candidate_by_id = {
        candidate.candidate_id: candidate
        for candidate in candidates.eligible_candidates
    }
    curves: dict[str, tuple[_MarginalSegment, ...]] = {}
    allocation_flags: list[str] = list(candidates.data_quality_flags)
    for candidate in candidates.eligible_candidates:
        market = _find_market(candidate, snapshot)
        if market is None:
            allocation_flags.append(
                f"ALLOCATOR_MARKET_UNAVAILABLE:{candidate.candidate_id}"
            )
            continue
        curve = _build_marginal_curve(candidate, market, fee_config)
        if not curve:
            allocation_flags.append(
                f"NO_VALID_MARGINAL_SEGMENTS:{candidate.candidate_id}"
            )
            continue
        market_by_candidate[candidate.candidate_id] = market
        curves[candidate.candidate_id] = curve

    selected: dict[str, list[_LiquiditySlice]] = {}
    selection_facts: list[MarginalSelectionFact] = []
    remaining = abs(request.remaining_hedge_requirement_btc)
    heap: list[tuple[Decimal, str, str, str, int, _MarginalSegment]] = []
    for candidate_id, curve in curves.items():
        candidate = candidate_by_id[candidate_id]
        _push_segment(heap, candidate, curve[0])

    while heap and remaining > 0:
        _, _, _, candidate_id, index, segment = heapq.heappop(heap)
        candidate = candidate_by_id[candidate_id]
        market = market_by_candidate[candidate_id]
        selected_slices = _select_segment_quantity(segment, remaining, market)
        if not selected_slices:
            continue
        selected_btc = sum(
            (item.btc_quantity for item in selected_slices), Decimal("0")
        )
        if selected_btc <= 0 or selected_btc > remaining:
            continue
        selected.setdefault(candidate_id, []).extend(selected_slices)
        remaining -= selected_btc
        selection_facts.append(
            MarginalSelectionFact(
                sequence=len(selection_facts) + 1,
                candidate_id=candidate_id,
                venue=candidate.venue,
                instrument_type=candidate.instrument_type,
                quantity_btc=selected_btc,
                expected_marginal_cost_usd_per_btc=(
                    sum(
                        (
                            item.btc_quantity
                            * item.expected_cost_usd_per_btc
                            for item in selected_slices
                        ),
                        Decimal("0"),
                    )
                    / selected_btc
                ),
            )
        )
        segment_fully_consumed = selected_btc == segment.btc_quantity
        next_index = index + 1
        if segment_fully_consumed and next_index < len(curves[candidate_id]):
            _push_segment(
                heap,
                candidate,
                curves[candidate_id][next_index],
            )

    legs: list[HedgeLeg] = []
    valid_candidate_ids: set[str] = set()
    for candidate_id in sorted(selected):
        candidate = candidate_by_id[candidate_id]
        market = market_by_candidate[candidate_id]
        leg = _build_final_leg(
            request,
            candidate,
            market,
            tuple(selected[candidate_id]),
            snapshot,
            fee_config,
        )
        if leg is None:
            allocation_flags.append(
                f"FINAL_LEG_ECONOMICS_UNAVAILABLE:{candidate_id}"
            )
            continue
        legs.append(leg)
        valid_candidate_ids.add(candidate_id)

    allocated_abs = sum((leg.quantity_btc for leg in legs), Decimal("0"))
    direction = (
        Decimal("1")
        if request.remaining_hedge_requirement_btc > 0
        else Decimal("-1")
    )
    allocated = direction * allocated_abs
    residual = request.remaining_hedge_requirement_btc - allocated
    if residual == 0:
        status = HedgePlanStatus.FULLY_FEASIBLE
    elif allocated_abs > 0:
        status = HedgePlanStatus.PARTIALLY_FEASIBLE
        allocation_flags.append("RESIDUAL_HEDGE_REQUIREMENT_UNALLOCATED")
    else:
        status = HedgePlanStatus.NO_FEASIBLE_HEDGE
        allocation_flags.append("NO_VALID_FINAL_HEDGE_LEGS")

    total_cost_usd = (
        sum((leg.expected_total_cost_usd for leg in legs), Decimal("0"))
        if legs
        else None
    )
    total_notional_usd = sum(
        (leg.expected_notional_usd for leg in legs), Decimal("0")
    )
    total_cost_bps = (
        total_cost_usd / total_notional_usd * BASIS_POINTS
        if total_cost_usd is not None and total_notional_usd > 0
        else None
    )
    projected_delta = (
        request.actual_delta_btc
        + request.qualifying_working_order_delta_btc
        + allocated
    )
    explanation = _explanation(
        candidates,
        tuple(
            fact
            for fact in selection_facts
            if fact.candidate_id in valid_candidate_ids
        ),
        residual,
    )
    return HedgePlan(
        plan_id=_plan_id(request, snapshot),
        optimization_id=request.optimization_id,
        mode=request.mode,
        status=status,
        generated_at=request.requested_at,
        desk_state_version=request.desk_state_version,
        risk_assessment_id=request.risk_assessment_id,
        market_snapshot_version=snapshot.snapshot_version,
        actual_delta_btc=request.actual_delta_btc,
        target_delta_btc=request.target_delta_btc,
        qualifying_working_order_delta_btc=(
            request.qualifying_working_order_delta_btc
        ),
        requested_hedge_delta_btc=request.remaining_hedge_requirement_btc,
        allocated_hedge_delta_btc=allocated,
        residual_unallocated_delta_btc=residual,
        expected_holding_seconds=request.expected_holding_seconds,
        legs=tuple(legs),
        total_expected_cost_usd=total_cost_usd,
        total_expected_cost_bps=total_cost_bps,
        projected_delta_btc=projected_delta,
        projected_delta_notional_usd=(
            projected_delta * request.reference_price_usd
            if request.reference_price_usd is not None
            else None
        ),
        fully_feasible=residual == 0,
        data_quality_flags=_unique(allocation_flags),
        explanation_data=explanation,
    )


def _build_marginal_curve(
    candidate: HedgeCandidate,
    market: ExecutableBookView,
    fee_config: ExecutionFeeConfig,
) -> tuple[_MarginalSegment, ...]:
    book = market.book
    instrument = market.instrument
    fee = fee_config.taker_fee_for(candidate.venue, candidate.instrument_type)
    if (
        book is None
        or instrument is None
        or fee is None
        or not market.eligible
        or market.connection.status is not MarketConnectionStatus.LIVE
    ):
        return ()
    levels = book.asks if candidate.side is ExecutionSide.BUY else book.bids
    arrival_mid = (book.bids[0].price + book.asks[0].price) / Decimal("2")
    carry_bps = candidate.expected_funding_cost_bps or Decimal("0")
    raw_slices: list[_LiquiditySlice] = []
    for level in levels:
        native_capacity = _floor_to_increment(
            level.source_quantity,
            instrument.quantity_increment,
        )
        if native_capacity <= 0:
            continue
        btc_capacity = instrument.quantity_to_btc_equivalent(
            native_capacity,
            price=level.price,
        )
        if btc_capacity <= 0:
            continue
        immediate_per_btc = marginal_execution_cost_usd_per_btc(
            side=candidate.side,
            execution_price=level.price,
            arrival_mid=arrival_mid,
            usd_conversion_rate=instrument.usd_conversion_rate,
            taker_fee_bps=fee.fee_bps,
        )
        carry_per_btc = (
            level.price
            * instrument.usd_conversion_rate
            * carry_bps
            / BASIS_POINTS
        )
        raw_slices.append(
            _LiquiditySlice(
                price=level.price,
                native_quantity=native_capacity,
                btc_quantity=btc_capacity,
                expected_cost_usd_per_btc=immediate_per_btc + carry_per_btc,
            )
        )
    if not raw_slices:
        return ()

    minimum_native = _ceil_to_increment(
        instrument.quantity_min,
        instrument.quantity_increment,
    )
    activation_parts: list[_LiquiditySlice] = []
    remaining_minimum = minimum_native
    residual_slices: list[_LiquiditySlice] = []
    for item in raw_slices:
        if remaining_minimum > 0:
            take_native = min(item.native_quantity, remaining_minimum)
            activation_parts.append(_resize_slice(item, take_native, instrument))
            remaining_minimum -= take_native
            leftover_native = item.native_quantity - take_native
            if leftover_native > 0:
                residual_slices.append(
                    _resize_slice(item, leftover_native, instrument)
                )
        else:
            residual_slices.append(item)
    if remaining_minimum > 0:
        return ()

    segments = [
        _MarginalSegment(
            candidate_id=candidate.candidate_id,
            index=0,
            activation_segment=True,
            slices=tuple(activation_parts),
        )
    ]
    segments.extend(
        _MarginalSegment(
            candidate_id=candidate.candidate_id,
            index=index,
            activation_segment=False,
            slices=(item,),
        )
        for index, item in enumerate(residual_slices, start=1)
    )
    return tuple(segments)


def _select_segment_quantity(
    segment: _MarginalSegment,
    remaining_btc: Decimal,
    market: ExecutableBookView,
) -> tuple[_LiquiditySlice, ...]:
    instrument = market.instrument
    if instrument is None:
        return ()
    if segment.btc_quantity <= remaining_btc:
        return segment.slices
    if segment.activation_segment:
        return ()
    item = segment.slices[0]
    desired_native = _btc_to_native(
        remaining_btc,
        instrument,
        price=item.price,
    )
    selected_native = min(
        item.native_quantity,
        _floor_to_increment(desired_native, instrument.quantity_increment),
    )
    if selected_native <= 0:
        return ()
    selected = _resize_slice(item, selected_native, instrument)
    if selected.btc_quantity > remaining_btc:
        selected_native -= instrument.quantity_increment
        if selected_native <= 0:
            return ()
        selected = _resize_slice(item, selected_native, instrument)
    return (selected,)


def _build_final_leg(
    request: HedgeOptimizationInput,
    candidate: HedgeCandidate,
    market: ExecutableBookView,
    selected: tuple[_LiquiditySlice, ...],
    snapshot: ExecutableMarketSnapshot,
    fee_config: ExecutionFeeConfig,
) -> Optional[HedgeLeg]:
    instrument = market.instrument
    if instrument is None or request.side is None:
        return None
    quantity_btc = sum((item.btc_quantity for item in selected), Decimal("0"))
    native_quantity = sum(
        (item.native_quantity for item in selected), Decimal("0")
    )
    if (
        quantity_btc <= 0
        or native_quantity < instrument.quantity_min
        or not _is_increment_multiple(
            native_quantity, instrument.quantity_increment
        )
    ):
        return None
    execution = estimate_execution_cost(
        ExecutionCostRequest(
            request_id=f"{candidate.candidate_id}:final-leg",
            venue=candidate.venue,
            instrument_id=candidate.instrument_id,
            instrument_type=candidate.instrument_type,
            side=request.side,
            quantity_btc_equivalent=quantity_btc,
            market_snapshot_version=snapshot.snapshot_version,
            requested_at=request.requested_at,
        ),
        snapshot,
        fee_config,
    )
    if (
        execution.status is not ExecutionCostStatus.OK
        or not execution.fully_executable
        or execution.execution_vwap is None
        or execution.executed_notional_usd is None
        or execution.all_in_immediate_cost_bps is None
        or execution.all_in_immediate_cost_usd is None
    ):
        return None

    funding_applicability = FundingApplicability.NOT_APPLICABLE
    funding_bps = Decimal("0")
    funding_usd = Decimal("0")
    total_bps = execution.all_in_immediate_cost_bps
    total_usd = execution.all_in_immediate_cost_usd
    quality_flags = list(candidate.data_quality_flags)
    if candidate.instrument_type is InstrumentType.PERPETUAL:
        if request.expected_holding_seconds is None:
            return None
        economics = calculate_hedge_economics(
            HedgeEconomicsRequest(
                request_id=f"{candidate.candidate_id}:final-economics",
                execution_cost_result_id=execution.result_id,
                expected_holding_seconds=request.expected_holding_seconds,
                market_snapshot_version=snapshot.snapshot_version,
                requested_at=request.requested_at,
            ),
            execution,
            market,
        )
        if (
            economics.carry_status is not CarryStatus.COMPLETE
            or economics.economics_status is not HedgeEconomicsStatus.COMPLETE
            or economics.expected_funding_cost_bps is None
            or economics.expected_funding_cost_usd is None
            or economics.expected_total_hedge_cost_bps is None
            or economics.expected_total_hedge_cost_usd is None
        ):
            return None
        funding_applicability = FundingApplicability.APPLIED
        funding_bps = economics.expected_funding_cost_bps
        funding_usd = economics.expected_funding_cost_usd
        total_bps = economics.expected_total_hedge_cost_bps
        total_usd = economics.expected_total_hedge_cost_usd
        quality_flags.extend(economics.data_quality_flags)

    return HedgeLeg(
        leg_id=(
            f"leg-{request.optimization_id}-{candidate.venue.value}-"
            f"{candidate.instrument_type.value}"
        ),
        candidate_id=candidate.candidate_id,
        venue=candidate.venue,
        instrument_id=candidate.instrument_id,
        instrument_type=candidate.instrument_type,
        side=request.side,
        quantity_btc=quantity_btc,
        native_quantity=native_quantity,
        native_quantity_unit=instrument.native_quantity_unit,
        expected_vwap=execution.execution_vwap,
        expected_notional_usd=execution.executed_notional_usd,
        expected_immediate_cost_bps=execution.all_in_immediate_cost_bps,
        expected_immediate_cost_usd=execution.all_in_immediate_cost_usd,
        funding_applicability=funding_applicability,
        expected_funding_cost_bps=funding_bps,
        expected_funding_cost_usd=funding_usd,
        expected_total_cost_bps=total_bps,
        expected_total_cost_usd=total_usd,
        entry_basis_bps=candidate.entry_basis_bps,
        open_interest_context=candidate.open_interest_context,
        market_snapshot_version=snapshot.snapshot_version,
        expected_fills=execution.fills,
        data_quality_flags=_unique(quality_flags),
    )


def _find_market(
    candidate: HedgeCandidate,
    snapshot: ExecutableMarketSnapshot,
) -> Optional[ExecutableBookView]:
    normalized_id = candidate.instrument_id.upper()
    return next(
        (
            market
            for market in snapshot.markets
            if market.venue is candidate.venue
            and market.instrument_type is candidate.instrument_type
            and normalized_id
            in {
                market.symbol.upper(),
                market.book.venue_symbol.upper() if market.book else "",
                market.instrument.venue_symbol.upper()
                if market.instrument
                else "",
            }
        ),
        None,
    )


def _push_segment(
    heap: list[tuple[Decimal, str, str, str, int, _MarginalSegment]],
    candidate: HedgeCandidate,
    segment: _MarginalSegment,
) -> None:
    heapq.heappush(
        heap,
        (
            segment.expected_cost_usd_per_btc,
            candidate.venue.value,
            candidate.instrument_type.value,
            candidate.candidate_id,
            segment.index,
            segment,
        ),
    )


def _resize_slice(
    item: _LiquiditySlice,
    native_quantity: Decimal,
    instrument: InstrumentRules,
) -> _LiquiditySlice:
    return _LiquiditySlice(
        price=item.price,
        native_quantity=native_quantity,
        btc_quantity=instrument.quantity_to_btc_equivalent(
            native_quantity,
            price=item.price,
        ),
        expected_cost_usd_per_btc=item.expected_cost_usd_per_btc,
    )


def _btc_to_native(
    quantity_btc: Decimal,
    instrument: InstrumentRules,
    *,
    price: Decimal,
) -> Decimal:
    if instrument.instrument_type is InstrumentType.SPOT:
        return quantity_btc
    if instrument.contract_structure is ContractStructure.INVERSE:
        return quantity_btc * price / instrument.contract_multiplier
    return quantity_btc / instrument.contract_multiplier


def _floor_to_increment(quantity: Decimal, increment: Decimal) -> Decimal:
    return (
        (quantity / increment).to_integral_value(rounding=ROUND_FLOOR)
        * increment
    )


def _ceil_to_increment(quantity: Decimal, increment: Decimal) -> Decimal:
    return (
        (quantity / increment).to_integral_value(rounding=ROUND_CEILING)
        * increment
    )


def _is_increment_multiple(quantity: Decimal, increment: Decimal) -> bool:
    return quantity == _floor_to_increment(quantity, increment)


def _input_consistency_errors(
    request: HedgeOptimizationInput,
    candidates: CandidateBuilderResult,
    snapshot: ExecutableMarketSnapshot,
) -> list[str]:
    errors: list[str] = []
    if candidates.optimization_id != request.optimization_id:
        errors.append("OPTIMIZATION_ID_MISMATCH")
    if candidates.required_hedge_delta_btc != request.remaining_hedge_requirement_btc:
        errors.append("HEDGE_REQUIREMENT_MISMATCH")
    if candidates.side is not request.side:
        errors.append("HEDGE_SIDE_MISMATCH")
    if candidates.desk_state_version != request.desk_state_version:
        errors.append("DESK_STATE_VERSION_MISMATCH")
    if candidates.risk_assessment_id != request.risk_assessment_id:
        errors.append("RISK_ASSESSMENT_ID_MISMATCH")
    if candidates.expected_holding_seconds != request.expected_holding_seconds:
        errors.append("EXPECTED_HOLDING_HORIZON_MISMATCH")
    if candidates.market_snapshot_version != snapshot.snapshot_version:
        errors.append("MARKET_SNAPSHOT_VERSION_MISMATCH")
    return errors


def _empty_plan(
    request: HedgeOptimizationInput,
    candidates: CandidateBuilderResult,
    snapshot: ExecutableMarketSnapshot,
    status: HedgePlanStatus,
    flags: list[str],
    *,
    fully_feasible: bool = False,
) -> HedgePlan:
    residual = (
        Decimal("0")
        if status is HedgePlanStatus.NO_HEDGE_REQUIRED
        else request.remaining_hedge_requirement_btc
    )
    projected = (
        request.actual_delta_btc + request.qualifying_working_order_delta_btc
    )
    return HedgePlan(
        plan_id=_plan_id(request, snapshot),
        optimization_id=request.optimization_id,
        mode=request.mode,
        status=status,
        generated_at=request.requested_at,
        desk_state_version=request.desk_state_version,
        risk_assessment_id=request.risk_assessment_id,
        market_snapshot_version=snapshot.snapshot_version,
        actual_delta_btc=request.actual_delta_btc,
        target_delta_btc=request.target_delta_btc,
        qualifying_working_order_delta_btc=(
            request.qualifying_working_order_delta_btc
        ),
        requested_hedge_delta_btc=request.remaining_hedge_requirement_btc,
        allocated_hedge_delta_btc=Decimal("0"),
        residual_unallocated_delta_btc=residual,
        expected_holding_seconds=request.expected_holding_seconds,
        legs=(),
        projected_delta_btc=projected,
        projected_delta_notional_usd=(
            projected * request.reference_price_usd
            if request.reference_price_usd is not None
            else None
        ),
        fully_feasible=fully_feasible,
        data_quality_flags=_unique(flags),
        explanation_data=_explanation(candidates, (), residual),
    )


def _explanation(
    candidates: CandidateBuilderResult,
    selection_facts: tuple[MarginalSelectionFact, ...],
    residual: Decimal,
) -> HedgePlanExplanationData:
    exclusions = tuple(
        CandidateExclusionFact(
            candidate_id=candidate.candidate_id,
            venue=candidate.venue,
            instrument_type=candidate.instrument_type,
            reason=candidate.exclusion_reason,
        )
        for candidate in candidates.excluded_candidates
        if candidate.exclusion_reason is not None
    )
    return HedgePlanExplanationData(
        allocator_method=ALLOCATOR_METHOD,
        selection_facts=selection_facts,
        excluded_candidate_facts=exclusions,
        residual_reason=(
            "INSUFFICIENT_LEGITIMATE_LIQUIDITY_OR_QUANTITY_GRANULARITY"
            if residual != 0
            else None
        ),
    )


def _plan_id(
    request: HedgeOptimizationInput,
    snapshot: ExecutableMarketSnapshot,
) -> str:
    return (
        f"plan-{request.optimization_id}-d{request.desk_state_version}-"
        f"m{snapshot.snapshot_version}"
    )


def _unique(values: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))
