"""Atomic Step 8A → Step 8B orchestration for standalone candidates."""

from __future__ import annotations

from typing import Optional

from ..execution_cost.config import execution_fee_config
from ..execution_cost.engine import estimate_execution_cost
from ..execution_cost.models import ExecutionCostRequest, ExecutionFeeConfig
from ..market.models import ExecutableBookView, ExecutableMarketSnapshot
from ..market.service import market_state_store
from ..market.store import InMemoryMarketStateStore
from .engine import calculate_hedge_economics
from .models import (
    HedgeEconomicsCandidateRequest,
    HedgeEconomicsComparisonRequest,
    HedgeEconomicsComparisonResult,
    HedgeEconomicsRequest,
    HedgeEconomicsResult,
)


class HedgeEconomicsService:
    """Evaluate economics without ranking candidates or mutating desk state."""

    def __init__(
        self,
        store: InMemoryMarketStateStore,
        fee_config: ExecutionFeeConfig = execution_fee_config,
    ) -> None:
        self.store = store
        self.fee_config = fee_config

    async def estimate(
        self, request: HedgeEconomicsCandidateRequest
    ) -> HedgeEconomicsResult:
        snapshot = await self.store.executable_snapshot()
        execution_request = ExecutionCostRequest(
            request_id=f"{request.request_id}:execution",
            venue=request.venue,
            instrument_id=request.instrument_id,
            instrument_type=request.instrument_type,
            side=request.side,
            quantity_btc_equivalent=request.quantity_btc_equivalent,
            market_snapshot_version=request.market_snapshot_version,
            requested_at=request.requested_at,
        )
        execution = estimate_execution_cost(
            execution_request,
            snapshot,
            self.fee_config,
        )
        market = _find_execution_market(execution_request, snapshot)
        return calculate_hedge_economics(
            HedgeEconomicsRequest(
                request_id=request.request_id,
                execution_cost_result_id=execution.result_id,
                expected_holding_seconds=request.expected_holding_seconds,
                market_snapshot_version=execution.market_snapshot_version,
                requested_at=request.requested_at,
            ),
            execution,
            market,
        )

    async def compare(
        self, request: HedgeEconomicsComparisonRequest
    ) -> HedgeEconomicsComparisonResult:
        """Evaluate every registered market against one immutable snapshot."""

        snapshot = await self.store.executable_snapshot(request.base_asset)
        results: list[HedgeEconomicsResult] = []
        for market in snapshot.markets:
            instrument_id = (
                market.instrument.venue_symbol
                if market.instrument is not None
                else market.book.venue_symbol
                if market.book is not None
                else market.symbol
            )
            candidate_id = (
                f"{request.request_id}:{market.venue.value}:"
                f"{market.instrument_type.value}"
            )
            execution_request = ExecutionCostRequest(
                request_id=f"{candidate_id}:execution",
                venue=market.venue,
                instrument_id=instrument_id,
                instrument_type=market.instrument_type,
                side=request.side,
                quantity_btc_equivalent=request.quantity_btc_equivalent,
                market_snapshot_version=request.market_snapshot_version,
                requested_at=request.requested_at,
            )
            execution = estimate_execution_cost(
                execution_request,
                snapshot,
                self.fee_config,
            )
            results.append(
                calculate_hedge_economics(
                    HedgeEconomicsRequest(
                        request_id=candidate_id,
                        execution_cost_result_id=execution.result_id,
                        expected_holding_seconds=request.expected_holding_seconds,
                        market_snapshot_version=execution.market_snapshot_version,
                        requested_at=request.requested_at,
                    ),
                    execution,
                    market,
                )
            )
        return HedgeEconomicsComparisonResult(
            comparison_id=(
                f"hedge-economics-{request.request_id}-"
                f"v{snapshot.snapshot_version}"
            ),
            request_id=request.request_id,
            side=request.side,
            requested_quantity_btc=request.quantity_btc_equivalent,
            expected_holding_seconds=request.expected_holding_seconds,
            base_asset=request.base_asset.upper(),
            market_snapshot_version=snapshot.snapshot_version,
            snapshot_captured_at=snapshot.captured_at,
            results=tuple(results),
        )


def _find_execution_market(
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


hedge_economics_service = HedgeEconomicsService(market_state_store)
