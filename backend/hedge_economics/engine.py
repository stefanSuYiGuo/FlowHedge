"""Pure Step 8B Spot/Perpetual holding-economics calculations."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional

from ..domain.models import InstrumentType
from ..execution_cost.models import (
    ExecutionCostResult,
    ExecutionCostStatus,
    ExecutionSide,
    FeeStatus,
)
from ..market.models import DerivativeMarketContext, ExecutableBookView
from .models import (
    BasisTreatment,
    CarryStatus,
    FundingProjectionMethod,
    FundingRateSource,
    HedgeEconomicsRequest,
    HedgeEconomicsResult,
    HedgeEconomicsStatus,
    OpenInterestContext,
    ProjectedFundingEvent,
)


BASIS_POINTS = Decimal("10000")
BASIS_REFERENCE = "NORMALIZED_USD_SPOT_REFERENCE"
EXCLUDED_COST_COMPONENTS = (
    "CAPITAL_CHARGE",
    "MARGIN_FUNDING_COST",
    "SPOT_FINANCING_COST",
    "BORROW_COST",
    "CUSTODY_COST",
    "EXPECTED_BASIS_CONVERGENCE",
    "FUTURE_UNWIND_EXECUTION_COST",
    "OI_MONETARY_PENALTY",
    "VOLATILITY_MONETARY_PENALTY",
    "VENUE_CREDIT_RISK_PENALTY",
)


def calculate_hedge_economics(
    request: HedgeEconomicsRequest,
    execution: ExecutionCostResult,
    market: Optional[ExecutableBookView],
) -> HedgeEconomicsResult:
    """Add v1 carry context to one Step 8A result without selecting a hedge."""

    expected_exit_time = request.requested_at + timedelta(
        seconds=request.expected_holding_seconds
    )
    flags: list[str] = []
    if execution.fee_status is FeeStatus.UNCONFIGURED:
        flags.append("IMMEDIATE_TAKER_FEE_UNCONFIGURED")
    if not execution.fully_executable:
        flags.append("PARTIAL_EXECUTION")

    context = market.derivatives if market is not None else None
    if market is not None and market.derivative_data_stale is True:
        flags.append("DERIVATIVE_CONTEXT_STALE")
    open_interest = _open_interest_context(context)
    if context is not None and context.basis_bps is not None:
        flags.append("BASIS_CONTEXT_ONLY")
    if open_interest is not None:
        flags.append("OPEN_INTEREST_CONTEXT_ONLY")
    base = _base_result_fields(
        request,
        execution,
        market,
        expected_exit_time,
        open_interest,
        flags,
    )

    if request.execution_cost_result_id != execution.result_id:
        return HedgeEconomicsResult(
            **base,
            carry_status=CarryStatus.NOT_EVALUATED,
            economics_status=HedgeEconomicsStatus.INVALID_REQUEST,
            funding_rate_source=FundingRateSource.UNAVAILABLE,
            funding_projection_degraded=False,
            funding_projection_method=FundingProjectionMethod.UNAVAILABLE,
            modeled_funding_event_count=0,
            modeled_funding_events=(),
            data_quality_flags=tuple(flags + ["EXECUTION_RESULT_ID_MISMATCH"]),
        )
    if request.market_snapshot_version != execution.market_snapshot_version:
        return HedgeEconomicsResult(
            **base,
            carry_status=CarryStatus.NOT_EVALUATED,
            economics_status=HedgeEconomicsStatus.INVALID_REQUEST,
            funding_rate_source=FundingRateSource.UNAVAILABLE,
            funding_projection_degraded=False,
            funding_projection_method=FundingProjectionMethod.UNAVAILABLE,
            modeled_funding_event_count=0,
            modeled_funding_events=(),
            data_quality_flags=tuple(flags + ["SNAPSHOT_VERSION_MISMATCH"]),
        )
    if market is not None and not _market_matches_execution(market, execution):
        return HedgeEconomicsResult(
            **base,
            carry_status=CarryStatus.NOT_EVALUATED,
            economics_status=HedgeEconomicsStatus.INVALID_REQUEST,
            funding_rate_source=FundingRateSource.UNAVAILABLE,
            funding_projection_degraded=False,
            funding_projection_method=FundingProjectionMethod.UNAVAILABLE,
            modeled_funding_event_count=0,
            modeled_funding_events=(),
            data_quality_flags=tuple(flags + ["MARKET_IDENTITY_MISMATCH"]),
        )

    execution_usable = execution.status in {
        ExecutionCostStatus.OK,
        ExecutionCostStatus.INSUFFICIENT_LIQUIDITY,
    } and execution.filled_quantity_btc > 0
    if not execution_usable:
        return HedgeEconomicsResult(
            **base,
            carry_status=CarryStatus.NOT_EVALUATED,
            economics_status=HedgeEconomicsStatus.EXECUTION_UNAVAILABLE,
            funding_rate_source=FundingRateSource.UNAVAILABLE,
            funding_projection_degraded=False,
            funding_projection_method=FundingProjectionMethod.UNAVAILABLE,
            modeled_funding_event_count=0,
            modeled_funding_events=(),
            data_quality_flags=tuple(flags + ["EXECUTION_COST_UNAVAILABLE"]),
        )

    if execution.instrument_type is InstrumentType.SPOT:
        flags.append("SPOT_CARRY_ZERO_V1")
        expected_total_bps = execution.all_in_immediate_cost_bps
        expected_total_usd = execution.all_in_immediate_cost_usd
        return HedgeEconomicsResult(
            **base,
            carry_status=CarryStatus.COMPLETE,
            economics_status=_complete_status(execution, CarryStatus.COMPLETE),
            funding_rate_source=FundingRateSource.NOT_REQUIRED,
            funding_projection_degraded=False,
            funding_projection_method=FundingProjectionMethod.NONE,
            modeled_funding_event_count=0,
            modeled_funding_events=(),
            expected_funding_cost_bps=Decimal("0"),
            expected_funding_cost_usd=Decimal("0"),
            expected_carry_cost_bps=Decimal("0"),
            expected_carry_cost_usd=Decimal("0"),
            expected_total_hedge_cost_bps=expected_total_bps,
            expected_total_hedge_cost_usd=expected_total_usd,
            data_quality_flags=tuple(flags),
        )

    if market is None or context is None:
        return _carry_unavailable(
            base,
            flags,
            "DERIVATIVE_CONTEXT_UNAVAILABLE",
        )

    event_times, schedule_complete = _project_funding_event_times(
        entry_time=request.requested_at,
        expected_exit_time=expected_exit_time,
        next_funding_time=context.next_funding_time,
        funding_interval_seconds=context.funding_interval_seconds,
    )
    if not schedule_complete:
        return _carry_unavailable(base, flags, "FUNDING_SCHEDULE_UNAVAILABLE")
    if not event_times:
        flags.append("NO_FUNDING_EVENTS_IN_HORIZON")
        return HedgeEconomicsResult(
            **base,
            carry_status=CarryStatus.COMPLETE,
            economics_status=_complete_status(execution, CarryStatus.COMPLETE),
            funding_rate_source=FundingRateSource.NOT_REQUIRED,
            funding_projection_degraded=False,
            funding_projection_method=FundingProjectionMethod.NONE,
            modeled_funding_event_count=0,
            modeled_funding_events=(),
            expected_funding_cost_bps=Decimal("0"),
            expected_funding_cost_usd=Decimal("0"),
            expected_carry_cost_bps=Decimal("0"),
            expected_carry_cost_usd=Decimal("0"),
            expected_total_hedge_cost_bps=execution.all_in_immediate_cost_bps,
            expected_total_hedge_cost_usd=execution.all_in_immediate_cost_usd,
            data_quality_flags=tuple(flags),
        )

    if market.derivative_data_stale is True:
        return _carry_unavailable(base, flags, "FUNDING_DATA_STALE")

    if context.funding_captured_at is None:
        return _carry_unavailable(base, flags, "FUNDING_TIMESTAMP_UNAVAILABLE")
    if market.funding_data_stale is True:
        return _carry_unavailable(base, flags, "FUNDING_DATA_STALE")

    if context.predicted_funding_rate is not None:
        funding_rate = context.predicted_funding_rate
        funding_source = FundingRateSource.PREDICTED
        projection_degraded = False
    elif context.current_funding_rate is not None:
        funding_rate = context.current_funding_rate
        funding_source = FundingRateSource.CURRENT
        projection_degraded = True
        flags.append("CURRENT_FUNDING_RATE_FALLBACK")
    else:
        return _carry_unavailable(base, flags, "FUNDING_RATE_UNAVAILABLE")

    projection_method = FundingProjectionMethod.SINGLE_EVENT
    if len(event_times) > 1:
        projection_method = FundingProjectionMethod.FLAT_RATE_EXTRAPOLATION
        projection_degraded = True
        flags.append("FLAT_RATE_EXTRAPOLATION")

    hedge_notional_usd = execution.executed_notional_usd
    if hedge_notional_usd is None or hedge_notional_usd <= 0:
        return _carry_unavailable(base, flags, "HEDGE_NOTIONAL_UNAVAILABLE")

    position_sign = (
        Decimal("1") if execution.side is ExecutionSide.BUY else Decimal("-1")
    )
    event_cost = hedge_notional_usd * funding_rate * position_sign
    modeled_events = tuple(
        ProjectedFundingEvent(
            event_time=event_time,
            funding_rate=funding_rate,
            expected_cost_usd=event_cost,
        )
        for event_time in event_times
    )
    expected_funding_usd = event_cost * Decimal(len(modeled_events))
    expected_funding_bps = (
        expected_funding_usd / hedge_notional_usd * BASIS_POINTS
    )
    total_bps = (
        execution.all_in_immediate_cost_bps + expected_funding_bps
        if execution.all_in_immediate_cost_bps is not None
        else None
    )
    total_usd = (
        execution.all_in_immediate_cost_usd + expected_funding_usd
        if execution.all_in_immediate_cost_usd is not None
        else None
    )
    return HedgeEconomicsResult(
        **base,
        carry_status=CarryStatus.COMPLETE,
        economics_status=_complete_status(execution, CarryStatus.COMPLETE),
        funding_rate_used=funding_rate,
        funding_rate_source=funding_source,
        funding_projection_degraded=projection_degraded,
        funding_projection_method=projection_method,
        modeled_funding_event_count=len(modeled_events),
        modeled_funding_events=modeled_events,
        expected_funding_cost_bps=expected_funding_bps,
        expected_funding_cost_usd=expected_funding_usd,
        expected_carry_cost_bps=expected_funding_bps,
        expected_carry_cost_usd=expected_funding_usd,
        expected_total_hedge_cost_bps=total_bps,
        expected_total_hedge_cost_usd=total_usd,
        data_quality_flags=tuple(flags),
    )


def _project_funding_event_times(
    *,
    entry_time: datetime,
    expected_exit_time: datetime,
    next_funding_time: Optional[datetime],
    funding_interval_seconds: Optional[int],
) -> tuple[tuple[datetime, ...], bool]:
    """Project discrete events using entry-exclusive, exit-inclusive boundaries."""

    if next_funding_time is None or next_funding_time.tzinfo is None:
        return (), False
    cursor = next_funding_time
    try:
        if cursor <= entry_time:
            if funding_interval_seconds is None:
                return (), False
            elapsed = (entry_time - cursor).total_seconds()
            steps = int(elapsed // funding_interval_seconds) + 1
            cursor += timedelta(seconds=steps * funding_interval_seconds)
        if cursor > expected_exit_time:
            return (), True
    except TypeError:
        return (), False

    events = [cursor]
    if funding_interval_seconds is None:
        return (tuple(events), cursor == expected_exit_time)
    while True:
        cursor += timedelta(seconds=funding_interval_seconds)
        if cursor > expected_exit_time:
            break
        events.append(cursor)
    return tuple(events), True


def _carry_unavailable(
    base: dict[str, object],
    flags: list[str],
    reason: str,
) -> HedgeEconomicsResult:
    return HedgeEconomicsResult(
        **base,
        carry_status=CarryStatus.UNAVAILABLE,
        economics_status=HedgeEconomicsStatus.CARRY_UNAVAILABLE,
        funding_rate_source=FundingRateSource.UNAVAILABLE,
        funding_projection_degraded=True,
        funding_projection_method=FundingProjectionMethod.UNAVAILABLE,
        modeled_funding_event_count=0,
        modeled_funding_events=(),
        data_quality_flags=tuple(flags + [reason]),
    )


def _base_result_fields(
    request: HedgeEconomicsRequest,
    execution: ExecutionCostResult,
    market: Optional[ExecutableBookView],
    expected_exit_time: datetime,
    open_interest: Optional[OpenInterestContext],
    flags: list[str],
) -> dict[str, object]:
    context = market.derivatives if market is not None else None
    return {
        "result_id": f"economics-{request.request_id}-{execution.result_id}",
        "request_id": request.request_id,
        "execution_cost_result_id": execution.result_id,
        "venue": execution.venue,
        "instrument_id": execution.instrument_id,
        "instrument_type": execution.instrument_type,
        "side": execution.side,
        "requested_quantity_btc": execution.requested_quantity_btc,
        "quantity_btc": execution.filled_quantity_btc,
        "unfilled_quantity_btc": execution.unfilled_quantity_btc,
        "fully_executable": execution.fully_executable,
        "execution_status": execution.status,
        "expected_holding_seconds": request.expected_holding_seconds,
        "entry_time": request.requested_at,
        "expected_exit_time": expected_exit_time,
        "immediate_price_cost_bps": execution.total_price_cost_bps,
        "immediate_price_cost_usd": execution.price_cost_usd,
        "immediate_execution_cost_bps": execution.all_in_immediate_cost_bps,
        "immediate_execution_cost_usd": execution.all_in_immediate_cost_usd,
        "immediate_fee_status": execution.fee_status,
        "entry_basis_bps": context.basis_bps if context else None,
        "basis_reference": (
            BASIS_REFERENCE if context and context.basis_bps is not None else None
        ),
        "basis_reference_price_usd": (
            context.basis_reference_price_usd if context else None
        ),
        "basis_captured_at": context.basis_captured_at if context else None,
        "basis_treatment": BasisTreatment.CONTEXT_ONLY,
        "open_interest_context": open_interest,
        "market_snapshot_version": execution.market_snapshot_version,
        "snapshot_captured_at": execution.snapshot_captured_at,
        "book_captured_at": execution.book_captured_at,
        "derivative_context_captured_at": context.received_at if context else None,
        "funding_captured_at": context.funding_captured_at if context else None,
        "excluded_cost_components": EXCLUDED_COST_COMPONENTS,
    }


def _complete_status(
    execution: ExecutionCostResult,
    carry_status: CarryStatus,
) -> HedgeEconomicsStatus:
    if carry_status is CarryStatus.UNAVAILABLE:
        return HedgeEconomicsStatus.CARRY_UNAVAILABLE
    if execution.all_in_immediate_cost_bps is None:
        return HedgeEconomicsStatus.INCOMPLETE_IMMEDIATE_COST
    if not execution.fully_executable:
        return HedgeEconomicsStatus.PARTIAL_EXECUTION
    return HedgeEconomicsStatus.COMPLETE


def _market_matches_execution(
    market: ExecutableBookView,
    execution: ExecutionCostResult,
) -> bool:
    identifiers = {
        market.symbol.upper(),
        market.book.venue_symbol.upper() if market.book else "",
        market.instrument.venue_symbol.upper() if market.instrument else "",
    }
    return (
        market.venue is execution.venue
        and market.instrument_type is execution.instrument_type
        and execution.instrument_id.upper() in identifiers
    )


def _open_interest_context(
    context: Optional[DerivativeMarketContext],
) -> Optional[OpenInterestContext]:
    if context is None or all(
        value is None
        for value in (
            context.open_interest,
            context.open_interest_btc_equivalent,
            context.open_interest_usd,
        )
    ):
        return None
    return OpenInterestContext(
        open_interest=context.open_interest,
        open_interest_unit=context.open_interest_unit,
        open_interest_btc_equivalent=context.open_interest_btc_equivalent,
        open_interest_usd=context.open_interest_usd,
        captured_at=context.open_interest_captured_at,
    )
