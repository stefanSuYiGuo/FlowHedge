"""Snapshot-aware orchestration for the Step 9.1 Candidate Builder."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from ..execution_cost.config import execution_fee_config
from ..execution_cost.models import ExecutionFeeConfig
from ..market.service import market_state_store
from ..market.store import InMemoryMarketStateStore
from ..risk.models import RiskAssessment
from .allocator import allocate_hedge
from .candidate_builder import build_hedge_candidates
from .models import (
    CandidateBuilderResult,
    HedgeOptimizationInput,
    HedgePlan,
    OptimizationMode,
)


class HedgeCandidateBuilderService:
    def __init__(
        self,
        store: InMemoryMarketStateStore,
        fee_config: ExecutionFeeConfig = execution_fee_config,
    ) -> None:
        self.store = store
        self.fee_config = fee_config

    async def build(
        self,
        request: HedgeOptimizationInput,
        *,
        base_asset: str = "BTC",
    ) -> CandidateBuilderResult:
        snapshot = await self.store.executable_snapshot(base_asset)
        return build_hedge_candidates(request, snapshot, self.fee_config)

    async def build_for_risk_assessment(
        self,
        assessment: RiskAssessment,
        *,
        optimization_id: str,
        expected_holding_seconds: Optional[int],
        mode: OptimizationMode = OptimizationMode.ADVISORY,
        requested_at: Optional[datetime] = None,
        base_asset: str = "BTC",
    ) -> CandidateBuilderResult:
        request = HedgeOptimizationInput.from_risk_assessment(
            assessment,
            optimization_id=optimization_id,
            expected_holding_seconds=expected_holding_seconds,
            mode=mode,
            requested_at=requested_at,
        )
        return await self.build(request, base_asset=base_asset)


hedge_candidate_builder_service = HedgeCandidateBuilderService(market_state_store)


class HedgeOptimizerService:
    """Run Steps 9.1 and 9.2 on one atomic snapshot without execution."""

    def __init__(
        self,
        store: InMemoryMarketStateStore,
        fee_config: ExecutionFeeConfig = execution_fee_config,
    ) -> None:
        self.store = store
        self.fee_config = fee_config

    async def optimize(
        self,
        request: HedgeOptimizationInput,
        *,
        base_asset: str = "BTC",
    ) -> HedgePlan:
        snapshot = await self.store.executable_snapshot(base_asset)
        candidates = build_hedge_candidates(
            request,
            snapshot,
            self.fee_config,
        )
        return allocate_hedge(
            request,
            candidates,
            snapshot,
            self.fee_config,
        )


hedge_optimizer_service = HedgeOptimizerService(market_state_store)
