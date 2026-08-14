"""Pure Step 8A immediate execution-cost calculation."""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from ..market.models import (
    ExecutableBookView,
    ExecutableMarketSnapshot,
    MarketConnectionStatus,
)
from .config import execution_fee_config
from .models import (
    ExecutionCostRequest,
    ExecutionCostResult,
    ExecutionCostStatus,
    ExecutionFeeConfig,
    ExecutionSide,
    FeeStatus,
)
from .sweeper import sweep_executable_book


BASIS_POINTS = Decimal("10000")
USD_IDENTITY_ASSUMPTION = "USD_IDENTITY"


def estimate_execution_cost(
    request: ExecutionCostRequest,
    snapshot: ExecutableMarketSnapshot,
    fee_config: ExecutionFeeConfig = execution_fee_config,
) -> ExecutionCostResult:
    """Estimate one standalone candidate without changing any trading state."""

    market = _find_market(request, snapshot)
    if request.market_snapshot_version is not None and (
        request.market_snapshot_version != snapshot.snapshot_version
    ):
        return _empty_result(
            request,
            snapshot,
            market,
            fee_config,
            status=ExecutionCostStatus.INVALID_REQUEST,
            reason="SNAPSHOT_VERSION_MISMATCH",
        )
    if market is None:
        return _empty_result(
            request,
            snapshot,
            None,
            fee_config,
            status=ExecutionCostStatus.MARKET_UNAVAILABLE,
            reason="MARKET_NOT_REGISTERED",
        )
    if (
        market.connection.status is MarketConnectionStatus.STALE
        or market.exclusion_reason == "FEED_STALE"
    ):
        return _empty_result(
            request,
            snapshot,
            market,
            fee_config,
            status=ExecutionCostStatus.MARKET_STALE,
            reason=market.exclusion_reason or "FEED_STALE",
        )
    if not market.eligible or market.book is None or market.instrument is None:
        return _empty_result(
            request,
            snapshot,
            market,
            fee_config,
            status=ExecutionCostStatus.MARKET_UNAVAILABLE,
            reason=market.exclusion_reason or "EXECUTION_INPUT_UNAVAILABLE",
        )

    book = market.book
    instrument = market.instrument
    best_bid = book.bids[0].price
    best_ask = book.asks[0].price
    arrival_mid = (best_bid + best_ask) / Decimal("2")
    sweep = sweep_executable_book(
        book,
        request.side,
        request.quantity_btc_equivalent,
    )
    if sweep.execution_vwap is None:
        return _empty_result(
            request,
            snapshot,
            market,
            fee_config,
            status=ExecutionCostStatus.MARKET_UNAVAILABLE,
            reason="NO_EXECUTABLE_LEVELS",
        )

    if request.side is ExecutionSide.BUY:
        spread_cost_bps = (best_ask - arrival_mid) / arrival_mid * BASIS_POINTS
        depth_impact_bps = (
            (sweep.execution_vwap - best_ask) / arrival_mid * BASIS_POINTS
        )
        total_price_cost_bps = (
            (sweep.execution_vwap - arrival_mid) / arrival_mid * BASIS_POINTS
        )
    else:
        spread_cost_bps = (arrival_mid - best_bid) / arrival_mid * BASIS_POINTS
        depth_impact_bps = (
            (best_bid - sweep.execution_vwap) / arrival_mid * BASIS_POINTS
        )
        total_price_cost_bps = (
            (arrival_mid - sweep.execution_vwap) / arrival_mid * BASIS_POINTS
        )

    executed_notional_usd = (
        sweep.executed_notional_quote * instrument.usd_conversion_rate
    )
    price_cost_usd = (
        executed_notional_usd * total_price_cost_bps / BASIS_POINTS
    )
    fee_entry = fee_config.taker_fee_for(request.venue, request.instrument_type)
    if fee_entry is None:
        fee_status = FeeStatus.UNCONFIGURED
        taker_fee_bps = None
        fee_usd = None
        all_in_bps = None
        all_in_usd = None
        fee_assumption_label = None
    else:
        fee_status = FeeStatus.CONFIGURED
        taker_fee_bps = fee_entry.fee_bps
        fee_usd = executed_notional_usd * taker_fee_bps / BASIS_POINTS
        all_in_bps = total_price_cost_bps + taker_fee_bps
        all_in_usd = price_cost_usd + fee_usd
        fee_assumption_label = fee_entry.assumption_label

    status = (
        ExecutionCostStatus.OK
        if sweep.fully_executable
        else ExecutionCostStatus.INSUFFICIENT_LIQUIDITY
    )
    return ExecutionCostResult(
        result_id=_result_id(request, snapshot),
        request_id=request.request_id,
        venue=request.venue,
        instrument_id=request.instrument_id,
        instrument_type=request.instrument_type,
        side=request.side,
        market_snapshot_version=snapshot.snapshot_version,
        snapshot_captured_at=snapshot.captured_at,
        book_captured_at=book.received_at,
        requested_quantity_btc=request.quantity_btc_equivalent,
        filled_quantity_btc=sweep.filled_quantity_btc,
        unfilled_quantity_btc=sweep.unfilled_quantity_btc,
        fully_executable=sweep.fully_executable,
        status=status,
        status_reason=(
            None if sweep.fully_executable else "KNOWN_BOOK_DEPTH_EXHAUSTED"
        ),
        best_bid=best_bid,
        best_ask=best_ask,
        arrival_mid=arrival_mid,
        execution_vwap=sweep.execution_vwap,
        quote_currency=instrument.quote_asset,
        usd_conversion_rate=instrument.usd_conversion_rate,
        usd_conversion_assumption=(
            instrument.usd_conversion_assumption
            or (
                USD_IDENTITY_ASSUMPTION
                if instrument.quote_asset == "USD"
                else "CONFIGURED_QUOTE_TO_USD_RATE"
            )
        ),
        executed_notional_quote=sweep.executed_notional_quote,
        executed_notional_usd=executed_notional_usd,
        spread_cost_bps=spread_cost_bps,
        depth_impact_bps=depth_impact_bps,
        total_price_cost_bps=total_price_cost_bps,
        price_cost_usd=price_cost_usd,
        taker_fee_bps=taker_fee_bps,
        fee_usd=fee_usd,
        fee_status=fee_status,
        fee_assumption_label=fee_assumption_label,
        all_in_immediate_cost_bps=all_in_bps,
        all_in_immediate_cost_usd=all_in_usd,
        fills=sweep.fills,
    )


def _find_market(
    request: ExecutionCostRequest,
    snapshot: ExecutableMarketSnapshot,
) -> Optional[ExecutableBookView]:
    normalized_id = request.instrument_id.upper()
    return next(
        (
            market
            for market in snapshot.markets
            if market.venue is request.venue
            and market.instrument_type is request.instrument_type
            and normalized_id
            in {
                market.symbol.upper(),
                market.book.venue_symbol.upper() if market.book else "",
                market.instrument.venue_symbol.upper() if market.instrument else "",
            }
        ),
        None,
    )


def _empty_result(
    request: ExecutionCostRequest,
    snapshot: ExecutableMarketSnapshot,
    market: Optional[ExecutableBookView],
    fee_config: ExecutionFeeConfig,
    *,
    status: ExecutionCostStatus,
    reason: str,
) -> ExecutionCostResult:
    fee_entry = fee_config.taker_fee_for(request.venue, request.instrument_type)
    instrument = market.instrument if market else None
    return ExecutionCostResult(
        result_id=_result_id(request, snapshot),
        request_id=request.request_id,
        venue=request.venue,
        instrument_id=request.instrument_id,
        instrument_type=request.instrument_type,
        side=request.side,
        market_snapshot_version=snapshot.snapshot_version,
        snapshot_captured_at=snapshot.captured_at,
        book_captured_at=market.book.received_at if market and market.book else None,
        requested_quantity_btc=request.quantity_btc_equivalent,
        filled_quantity_btc=Decimal("0"),
        unfilled_quantity_btc=request.quantity_btc_equivalent,
        fully_executable=False,
        status=status,
        status_reason=reason,
        quote_currency=instrument.quote_asset if instrument else None,
        usd_conversion_rate=instrument.usd_conversion_rate if instrument else None,
        usd_conversion_assumption=(
            instrument.usd_conversion_assumption
            or (USD_IDENTITY_ASSUMPTION if instrument and instrument.quote_asset == "USD" else None)
            if instrument
            else None
        ),
        taker_fee_bps=fee_entry.fee_bps if fee_entry else None,
        fee_status=(FeeStatus.CONFIGURED if fee_entry else FeeStatus.UNCONFIGURED),
        fee_assumption_label=fee_entry.assumption_label if fee_entry else None,
        fills=(),
    )


def _result_id(
    request: ExecutionCostRequest, snapshot: ExecutableMarketSnapshot
) -> str:
    return (
        f"cost-{request.request_id}-v{snapshot.snapshot_version}-"
        f"{request.venue.value}-{request.instrument_type.value}"
    )
