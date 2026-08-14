"""Snapshot-aware Step 8A service used by future optimizer code."""

from __future__ import annotations

from ..market.store import InMemoryMarketStateStore
from ..market.service import market_state_store
from .config import execution_fee_config
from .engine import estimate_execution_cost
from .models import (
    ExecutionCostComparisonRequest,
    ExecutionCostComparisonResult,
    ExecutionCostRequest,
    ExecutionCostResult,
    ExecutionFeeConfig,
)


class ExecutionCostService:
    def __init__(
        self,
        store: InMemoryMarketStateStore,
        fee_config: ExecutionFeeConfig = execution_fee_config,
    ) -> None:
        self.store = store
        self.fee_config = fee_config

    async def estimate(
        self, request: ExecutionCostRequest
    ) -> ExecutionCostResult:
        snapshot = await self.store.executable_snapshot()
        return estimate_execution_cost(request, snapshot, self.fee_config)

    async def compare(
        self, request: ExecutionCostComparisonRequest
    ) -> ExecutionCostComparisonResult:
        """Evaluate each registered market independently on one atomic snapshot."""

        snapshot = await self.store.executable_snapshot(request.base_asset)
        results = tuple(
            estimate_execution_cost(
                ExecutionCostRequest(
                    request_id=(
                        f"{request.request_id}:{market.venue.value}:"
                        f"{market.instrument_type.value}"
                    ),
                    venue=market.venue,
                    instrument_id=(
                        market.instrument.venue_symbol
                        if market.instrument is not None
                        else market.book.venue_symbol
                        if market.book is not None
                        else market.symbol
                    ),
                    instrument_type=market.instrument_type,
                    side=request.side,
                    quantity_btc_equivalent=request.quantity_btc_equivalent,
                    market_snapshot_version=request.market_snapshot_version,
                    requested_at=request.requested_at,
                ),
                snapshot,
                self.fee_config,
            )
            for market in snapshot.markets
        )
        return ExecutionCostComparisonResult(
            comparison_id=(
                f"comparison-{request.request_id}-v{snapshot.snapshot_version}"
            ),
            request_id=request.request_id,
            side=request.side,
            requested_quantity_btc=request.quantity_btc_equivalent,
            base_asset=request.base_asset.upper(),
            market_snapshot_version=snapshot.snapshot_version,
            snapshot_captured_at=snapshot.captured_at,
            results=results,
        )


execution_cost_service = ExecutionCostService(market_state_store)
