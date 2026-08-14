"""Pure Step 9.1 candidate eligibility over one atomic market snapshot."""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from ..domain.models import InstrumentType
from ..execution_cost.config import execution_fee_config
from ..execution_cost.engine import estimate_execution_cost
from ..execution_cost.models import (
    ExecutionCostRequest,
    ExecutionCostStatus,
    ExecutionFeeConfig,
    ExecutionSide,
    FeeStatus,
)
from ..hedge_economics.engine import calculate_hedge_economics
from ..hedge_economics.models import (
    CarryStatus,
    HedgeEconomicsRequest,
    HedgeEconomicsStatus,
    OpenInterestContext,
)
from ..market.models import (
    ContractStructure,
    ExecutableBookView,
    ExecutableMarketSnapshot,
    MarketConnectionStatus,
)
from .models import (
    CandidateBuilderResult,
    CandidateBuilderStatus,
    CandidateExclusionReason,
    HedgeCandidate,
    HedgeOptimizationInput,
    NormalizedBookReference,
)


COMPARABLE_COST_EXCLUSIONS = {
    CandidateExclusionReason.FEE_UNCONFIGURED,
    CandidateExclusionReason.HOLDING_HORIZON_UNAVAILABLE,
    CandidateExclusionReason.FUNDING_DATA_UNAVAILABLE,
    CandidateExclusionReason.ECONOMICS_INCOMPLETE,
}


def build_hedge_candidates(
    request: HedgeOptimizationInput,
    snapshot: ExecutableMarketSnapshot,
    fee_config: ExecutionFeeConfig = execution_fee_config,
) -> CandidateBuilderResult:
    """Build eligibility only; never rank, allocate, or mutate desk state."""

    result_flags: list[str] = []
    if request.market_snapshot_version != snapshot.snapshot_version:
        result_flags.append("MARKET_SNAPSHOT_ADVANCED_SINCE_RISK_ASSESSMENT")

    if request.working_order_conflict:
        return _terminal_result(
            request,
            snapshot,
            CandidateBuilderStatus.OPTIMIZATION_BLOCKED_WORKING_ORDER_CONFLICT,
            result_flags + ["WORKING_ORDER_CONFLICT"],
        )
    if request.working_order_overhedge:
        return _terminal_result(
            request,
            snapshot,
            CandidateBuilderStatus.OPTIMIZATION_BLOCKED_WORKING_ORDER_OVERHEDGE,
            result_flags + ["WORKING_ORDER_OVERHEDGE"],
        )
    if request.remaining_hedge_requirement_btc == 0:
        return _terminal_result(
            request,
            snapshot,
            CandidateBuilderStatus.NO_HEDGE_REQUIRED,
            result_flags,
            full_requirement_possible=True,
        )

    assert request.side is not None
    candidates = tuple(
        _build_candidate(request, snapshot, market, fee_config)
        for market in snapshot.markets
    )
    eligible = tuple(candidate for candidate in candidates if candidate.eligible)
    excluded = tuple(candidate for candidate in candidates if not candidate.eligible)
    total_depth = sum(
        (candidate.max_executable_quantity_btc for candidate in eligible),
        Decimal("0"),
    )
    if eligible:
        status = CandidateBuilderStatus.READY
    elif excluded and all(
        candidate.exclusion_reason in COMPARABLE_COST_EXCLUSIONS
        for candidate in excluded
    ):
        status = CandidateBuilderStatus.OPTIMIZATION_DATA_UNAVAILABLE
        result_flags.append("NO_COMPARABLE_COST_CANDIDATES")
    else:
        status = CandidateBuilderStatus.NO_ELIGIBLE_CANDIDATES
        result_flags.append("NO_ELIGIBLE_EXECUTION_CANDIDATES")

    for candidate in candidates:
        result_flags.extend(candidate.data_quality_flags)
    return CandidateBuilderResult(
        optimization_id=request.optimization_id,
        status=status,
        required_hedge_delta_btc=request.remaining_hedge_requirement_btc,
        side=request.side,
        expected_holding_seconds=request.expected_holding_seconds,
        desk_state_version=request.desk_state_version,
        risk_assessment_id=request.risk_assessment_id,
        market_snapshot_version=snapshot.snapshot_version,
        market_snapshot_captured_at=snapshot.captured_at,
        eligible_candidates=eligible,
        excluded_candidates=excluded,
        total_eligible_depth_btc=total_depth,
        full_requirement_possible=(
            total_depth >= abs(request.remaining_hedge_requirement_btc)
        ),
        data_quality_flags=_unique(result_flags),
    )


def _build_candidate(
    request: HedgeOptimizationInput,
    snapshot: ExecutableMarketSnapshot,
    market: ExecutableBookView,
    fee_config: ExecutionFeeConfig,
) -> HedgeCandidate:
    assert request.side is not None
    required_quantity = abs(request.remaining_hedge_requirement_btc)
    instrument_id = _instrument_id(market)
    candidate_id = (
        f"candidate-{request.optimization_id}-v{snapshot.snapshot_version}-"
        f"{market.venue.value}-{market.instrument_type.value}"
    )
    fee_entry = fee_config.taker_fee_for(market.venue, market.instrument_type)
    fee_status = FeeStatus.CONFIGURED if fee_entry else FeeStatus.UNCONFIGURED
    context_fields = _context_fields(market)

    preliminary_reason = _market_exclusion_reason(market)
    if preliminary_reason is not None:
        return HedgeCandidate(
            candidate_id=candidate_id,
            venue=market.venue,
            instrument_id=instrument_id,
            instrument_type=market.instrument_type,
            side=request.side,
            requested_requirement_btc=required_quantity,
            eligible=False,
            exclusion_reason=preliminary_reason,
            market_snapshot_version=snapshot.snapshot_version,
            fee_status=fee_status,
            data_quality_flags=(preliminary_reason.value,),
            **context_fields,
        )

    book = market.book
    instrument = market.instrument
    assert book is not None and instrument is not None
    levels = book.asks if request.side is ExecutionSide.BUY else book.bids
    if not levels:
        return _excluded_with_market_context(
            request,
            snapshot,
            market,
            candidate_id,
            fee_status,
            CandidateExclusionReason.NO_EXECUTABLE_DEPTH,
            context_fields,
        )
    if not _market_inputs_match(market):
        return _excluded_with_market_context(
            request,
            snapshot,
            market,
            candidate_id,
            fee_status,
            CandidateExclusionReason.INVALID_BOOK,
            context_fields,
        )

    reference_price = levels[0].price
    try:
        btc_step = instrument.quantity_to_btc_equivalent(
            instrument.quantity_increment,
            price=reference_price,
        )
        btc_minimum = instrument.quantity_to_btc_equivalent(
            instrument.quantity_min,
            price=reference_price,
        )
    except (ArithmeticError, ValueError):
        return _excluded_with_market_context(
            request,
            snapshot,
            market,
            candidate_id,
            fee_status,
            CandidateExclusionReason.QUANTITY_NORMALIZATION_UNAVAILABLE,
            context_fields,
        )
    max_depth = sum(
        (level.quantity_btc_equivalent for level in levels), Decimal("0")
    )
    normalized_book_reference = NormalizedBookReference(
        market_snapshot_version=snapshot.snapshot_version,
        venue=market.venue,
        instrument_id=instrument_id,
        instrument_type=market.instrument_type,
        side=request.side,
        source_sequence=book.source_sequence,
        captured_at=book.received_at,
    )
    quantity_fields = {
        "book_captured_at": book.received_at,
        "max_executable_quantity_btc": max_depth,
        "native_quantity_unit": instrument.native_quantity_unit,
        "contract_multiplier": instrument.contract_multiplier,
        "native_quantity_step": instrument.quantity_increment,
        "btc_equivalent_quantity_step": btc_step,
        "minimum_order_quantity": instrument.quantity_min,
        "minimum_order_quantity_btc_equivalent": btc_minimum,
        "normalized_book_reference": normalized_book_reference,
    }
    quantity_flags: list[str] = []
    if instrument.contract_structure is ContractStructure.INVERSE:
        quantity_flags.append("BTC_EQUIVALENT_STEP_PRICE_DEPENDENT")
    if max_depth <= 0 or max_depth < btc_minimum:
        return _excluded_with_market_context(
            request,
            snapshot,
            market,
            candidate_id,
            fee_status,
            CandidateExclusionReason.NO_EXECUTABLE_DEPTH,
            context_fields,
            quantity_fields=quantity_fields,
            extra_flags=quantity_flags,
        )
    if required_quantity < btc_minimum:
        return _excluded_with_market_context(
            request,
            snapshot,
            market,
            candidate_id,
            fee_status,
            CandidateExclusionReason.REQUIREMENT_BELOW_MINIMUM_ORDER_SIZE,
            context_fields,
            quantity_fields=quantity_fields,
            extra_flags=quantity_flags,
        )

    execution = estimate_execution_cost(
        ExecutionCostRequest(
            request_id=f"{candidate_id}:execution",
            venue=market.venue,
            instrument_id=instrument_id,
            instrument_type=market.instrument_type,
            side=request.side,
            quantity_btc_equivalent=required_quantity,
            market_snapshot_version=snapshot.snapshot_version,
            requested_at=request.requested_at,
        ),
        snapshot,
        fee_config,
    )
    execution_usable = execution.status in {
        ExecutionCostStatus.OK,
        ExecutionCostStatus.INSUFFICIENT_LIQUIDITY,
    } and execution.filled_quantity_btc > 0
    if not execution_usable:
        return _excluded_with_market_context(
            request,
            snapshot,
            market,
            candidate_id,
            execution.fee_status,
            _execution_exclusion_reason(execution.status),
            context_fields,
            quantity_fields=quantity_fields,
            immediate_cost_reference=execution.result_id,
            extra_flags=quantity_flags
            + ([execution.status_reason] if execution.status_reason else []),
        )
    if execution.fee_status is FeeStatus.UNCONFIGURED:
        return _excluded_with_market_context(
            request,
            snapshot,
            market,
            candidate_id,
            execution.fee_status,
            CandidateExclusionReason.FEE_UNCONFIGURED,
            context_fields,
            quantity_fields=quantity_fields,
            immediate_cost_reference=execution.result_id,
            extra_flags=quantity_flags + ["ECONOMICS_INCOMPLETE"],
        )

    immediate_available = (
        execution.all_in_immediate_cost_bps is not None
        and execution.all_in_immediate_cost_usd is not None
    )
    if not immediate_available:
        return _excluded_with_market_context(
            request,
            snapshot,
            market,
            candidate_id,
            execution.fee_status,
            CandidateExclusionReason.ECONOMICS_INCOMPLETE,
            context_fields,
            quantity_fields=quantity_fields,
            immediate_cost_reference=execution.result_id,
            extra_flags=quantity_flags + ["IMMEDIATE_ECONOMICS_UNAVAILABLE"],
        )

    economics_reference: Optional[str] = None
    expected_funding_cost_bps: Optional[Decimal] = Decimal("0")
    economics_flags: list[str] = []
    if market.instrument_type is InstrumentType.PERPETUAL:
        if request.expected_holding_seconds is None:
            return _excluded_with_market_context(
                request,
                snapshot,
                market,
                candidate_id,
                execution.fee_status,
                CandidateExclusionReason.HOLDING_HORIZON_UNAVAILABLE,
                context_fields,
                quantity_fields=quantity_fields,
                immediate_cost_reference=execution.result_id,
                immediate_economics_available=True,
                extra_flags=quantity_flags,
            )
        economics = calculate_hedge_economics(
            HedgeEconomicsRequest(
                request_id=f"{candidate_id}:economics",
                execution_cost_result_id=execution.result_id,
                expected_holding_seconds=request.expected_holding_seconds,
                market_snapshot_version=snapshot.snapshot_version,
                requested_at=request.requested_at,
            ),
            execution,
            market,
        )
        economics_reference = economics.result_id
        expected_funding_cost_bps = economics.expected_funding_cost_bps
        economics_flags.extend(economics.data_quality_flags)
        if economics.carry_status is CarryStatus.UNAVAILABLE:
            return _excluded_with_market_context(
                request,
                snapshot,
                market,
                candidate_id,
                execution.fee_status,
                CandidateExclusionReason.FUNDING_DATA_UNAVAILABLE,
                context_fields,
                quantity_fields=quantity_fields,
                immediate_cost_reference=execution.result_id,
                hedge_economics_reference=economics.result_id,
                immediate_economics_available=True,
                extra_flags=quantity_flags + economics_flags,
            )
        if economics.economics_status not in {
            HedgeEconomicsStatus.COMPLETE,
            HedgeEconomicsStatus.PARTIAL_EXECUTION,
        } or economics.expected_total_hedge_cost_bps is None:
            return _excluded_with_market_context(
                request,
                snapshot,
                market,
                candidate_id,
                execution.fee_status,
                CandidateExclusionReason.ECONOMICS_INCOMPLETE,
                context_fields,
                quantity_fields=quantity_fields,
                immediate_cost_reference=execution.result_id,
                hedge_economics_reference=economics.result_id,
                immediate_economics_available=True,
                extra_flags=quantity_flags + economics_flags,
            )

    if not execution.fully_executable:
        quantity_flags.append("PARTIAL_EXECUTABLE_DEPTH")
    return HedgeCandidate(
        candidate_id=candidate_id,
        venue=market.venue,
        instrument_id=instrument_id,
        instrument_type=market.instrument_type,
        side=request.side,
        requested_requirement_btc=required_quantity,
        eligible=True,
        market_snapshot_version=snapshot.snapshot_version,
        fully_executable_for_full_requirement=execution.fully_executable,
        immediate_economics_available=True,
        carry_economics_available=True,
        fee_status=execution.fee_status,
        immediate_cost_reference=execution.result_id,
        hedge_economics_reference=economics_reference,
        expected_funding_cost_bps=expected_funding_cost_bps,
        data_quality_flags=_unique(quantity_flags + economics_flags),
        **context_fields,
        **quantity_fields,
    )


def _market_exclusion_reason(
    market: ExecutableBookView,
) -> Optional[CandidateExclusionReason]:
    status = market.connection.status
    if status is MarketConnectionStatus.STALE:
        return CandidateExclusionReason.MARKET_STALE
    if status is not MarketConnectionStatus.LIVE:
        return CandidateExclusionReason.MARKET_DISCONNECTED
    if market.instrument is None:
        return CandidateExclusionReason.INSTRUMENT_METADATA_UNAVAILABLE
    if not market.instrument.eligible_for_execution:
        return CandidateExclusionReason.INSTRUMENT_NOT_EXECUTABLE
    if market.book is None:
        return CandidateExclusionReason.NO_EXECUTABLE_DEPTH
    if market.exclusion_reason is not None or not market.eligible:
        if market.exclusion_reason == "INSTRUMENT_METADATA_UNAVAILABLE":
            return CandidateExclusionReason.INSTRUMENT_METADATA_UNAVAILABLE
        if market.exclusion_reason in {
            "EXECUTABLE_BOOK_UNAVAILABLE",
            "BOOK_UNAVAILABLE",
        }:
            return CandidateExclusionReason.NO_EXECUTABLE_DEPTH
        return CandidateExclusionReason.MARKET_UNAVAILABLE
    return None


def _market_inputs_match(market: ExecutableBookView) -> bool:
    book = market.book
    instrument = market.instrument
    if book is None or instrument is None:
        return False
    return (
        book.venue is market.venue
        and instrument.venue is market.venue
        and book.symbol == market.symbol == instrument.symbol
        and book.instrument_type is market.instrument_type
        and instrument.instrument_type is market.instrument_type
        and book.venue_symbol == instrument.venue_symbol
        and instrument.base_asset.upper() == "BTC"
    )


def _execution_exclusion_reason(
    status: ExecutionCostStatus,
) -> CandidateExclusionReason:
    if status is ExecutionCostStatus.MARKET_STALE:
        return CandidateExclusionReason.MARKET_STALE
    if status is ExecutionCostStatus.MARKET_UNAVAILABLE:
        return CandidateExclusionReason.MARKET_UNAVAILABLE
    if status is ExecutionCostStatus.INVALID_REQUEST:
        return CandidateExclusionReason.INVALID_BOOK
    return CandidateExclusionReason.NO_EXECUTABLE_DEPTH


def _instrument_id(market: ExecutableBookView) -> str:
    if market.instrument is not None:
        return market.instrument.venue_symbol
    if market.book is not None:
        return market.book.venue_symbol
    return f"{market.symbol}:{market.instrument_type.value}"


def _context_fields(market: ExecutableBookView) -> dict[str, object]:
    context = market.derivatives
    open_interest: Optional[OpenInterestContext] = None
    if context is not None and any(
        value is not None
        for value in (
            context.open_interest,
            context.open_interest_btc_equivalent,
            context.open_interest_usd,
        )
    ):
        open_interest = OpenInterestContext(
            open_interest=context.open_interest,
            open_interest_unit=context.open_interest_unit,
            open_interest_btc_equivalent=context.open_interest_btc_equivalent,
            open_interest_usd=context.open_interest_usd,
            captured_at=context.open_interest_captured_at,
        )
    return {
        "entry_basis_bps": context.basis_bps if context else None,
        "open_interest_context": open_interest,
    }


def _excluded_with_market_context(
    request: HedgeOptimizationInput,
    snapshot: ExecutableMarketSnapshot,
    market: ExecutableBookView,
    candidate_id: str,
    fee_status: FeeStatus,
    reason: CandidateExclusionReason,
    context_fields: dict[str, object],
    *,
    quantity_fields: Optional[dict[str, object]] = None,
    immediate_cost_reference: Optional[str] = None,
    hedge_economics_reference: Optional[str] = None,
    immediate_economics_available: bool = False,
    extra_flags: Optional[list[str]] = None,
) -> HedgeCandidate:
    assert request.side is not None
    return HedgeCandidate(
        candidate_id=candidate_id,
        venue=market.venue,
        instrument_id=_instrument_id(market),
        instrument_type=market.instrument_type,
        side=request.side,
        requested_requirement_btc=abs(request.remaining_hedge_requirement_btc),
        eligible=False,
        exclusion_reason=reason,
        market_snapshot_version=snapshot.snapshot_version,
        fee_status=fee_status,
        immediate_cost_reference=immediate_cost_reference,
        hedge_economics_reference=hedge_economics_reference,
        immediate_economics_available=immediate_economics_available,
        data_quality_flags=_unique([reason.value] + (extra_flags or [])),
        **context_fields,
        **(quantity_fields or {}),
    )


def _terminal_result(
    request: HedgeOptimizationInput,
    snapshot: ExecutableMarketSnapshot,
    status: CandidateBuilderStatus,
    flags: list[str],
    *,
    full_requirement_possible: bool = False,
) -> CandidateBuilderResult:
    return CandidateBuilderResult(
        optimization_id=request.optimization_id,
        status=status,
        required_hedge_delta_btc=request.remaining_hedge_requirement_btc,
        side=request.side,
        expected_holding_seconds=request.expected_holding_seconds,
        desk_state_version=request.desk_state_version,
        risk_assessment_id=request.risk_assessment_id,
        market_snapshot_version=snapshot.snapshot_version,
        market_snapshot_captured_at=snapshot.captured_at,
        eligible_candidates=(),
        excluded_candidates=(),
        total_eligible_depth_btc=Decimal("0"),
        full_requirement_possible=full_requirement_possible,
        data_quality_flags=_unique(flags),
    )


def _unique(values: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))
