"""Stateful Step 9.3 advisory planning and trader acceptance workflow."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Optional

from ..config import DemoDeskConfig, demo_desk_config
from ..demo import (
    DemoStateError,
    DemoTradingService,
    HedgeAllocationError,
    demo_service,
)
from ..domain.models import EventType, HedgeOrderBatchResult, InstrumentType
from ..hedge_optimizer.models import (
    HedgeOptimizationInput,
    HedgePlan,
    HedgePlanStatus,
    OptimizationMode,
)
from ..hedge_optimizer.service import HedgeOptimizerService, hedge_optimizer_service
from ..market.models import (
    ExecutableBookView,
    ExecutableMarketSnapshot,
    MarketConnectionStatus,
)
from ..risk.models import RiskAssessment, RiskBand
from ..risk.service import RiskService, risk_service
from .models import AdvisoryHedgeRecommendation, AdvisoryLifecycleStatus


class AdvisoryPlanStateError(ValueError):
    """Raised when a plan decision no longer matches the advisory lifecycle."""


class AdvisoryPlanExecutionError(ValueError):
    """Raised when an advisory plan cannot safely become working orders."""


class AdvisoryHedgeService:
    """Connect RiskPolicy to the optimizer without making execution automatic."""

    def __init__(
        self,
        risk: RiskService,
        optimizer: HedgeOptimizerService,
        trading: DemoTradingService,
        *,
        expected_holding_seconds: Optional[int] = None,
        demo_config: Optional[DemoDeskConfig] = None,
    ) -> None:
        self.risk = risk
        self.optimizer = optimizer
        self.trading = trading
        self.expected_holding_seconds = expected_holding_seconds
        self.demo_config = demo_config
        self._lock: Optional[asyncio.Lock] = None
        self._lock_loop: Optional[asyncio.AbstractEventLoop] = None
        self.reset()

    def reset(self) -> None:
        self._current_plan: Optional[HedgePlan] = None
        self._current_material_key: Optional[tuple[object, ...]] = None
        self._current_market_signature: Optional[tuple[object, ...]] = None
        self._rejected_material_key: Optional[tuple[object, ...]] = None
        self._rejected_plan: Optional[HedgePlan] = None
        self._accepted_batches: dict[str, HedgeOrderBatchResult] = {}
        self._sequence = 0

    async def recommendation(
        self,
        assessment: RiskAssessment,
    ) -> AdvisoryHedgeRecommendation:
        async with self._active_lock():
            return await self._recommendation_locked(assessment)

    async def accept(self, plan_id: str) -> HedgeOrderBatchResult:
        """Validate once more, then create idempotent working orders only."""

        async with self._active_lock():
            previous = self._accepted_batches.get(plan_id)
            if previous is not None:
                return previous.model_copy(update={"replayed": True})

            assessment = await self.risk.assess()
            recommendation = await self._recommendation_locked(assessment)
            plan = recommendation.plan
            if (
                plan is None
                or plan.plan_id != plan_id
                or not recommendation.can_use_system_plan
            ):
                raise AdvisoryPlanStateError(
                    "the advisory HedgePlan is stale, blocked, or no longer current"
                )
            if assessment.desk_state_version != plan.desk_state_version:
                raise AdvisoryPlanStateError(
                    "the advisory HedgePlan was generated for a different DeskState"
                )
            if assessment.working_order_conflict:
                raise AdvisoryPlanExecutionError(
                    "working-order conflict blocks advisory plan acceptance"
                )
            if assessment.working_order_overhedge:
                raise AdvisoryPlanExecutionError(
                    "working-order overhedge blocks advisory plan acceptance"
                )

            snapshot = await self.optimizer.store.executable_snapshot("BTC")
            if not self._referenced_markets_eligible(plan, snapshot):
                self._mark_stale(plan, "REFERENCED_MARKET_NO_LONGER_ELIGIBLE")
                self._clear_current()
                raise AdvisoryPlanStateError(
                    "a referenced venue is stale, disconnected, or ineligible"
                )

            self.trading.record_system_event(
                EventType.HEDGE_PLAN_ACCEPTED,
                aggregate_id=plan.plan_id,
                correlation_id=plan.optimization_id,
                payload={
                    "plan_id": plan.plan_id,
                    "desk_state_version": plan.desk_state_version,
                    "allocated_hedge_delta_btc": plan.allocated_hedge_delta_btc,
                    "leg_count": len(plan.legs),
                },
            )
            try:
                batch = self.trading.create_advisory_hedge_orders(plan)
            except (DemoStateError, HedgeAllocationError) as error:
                self._mark_stale(plan, "DESK_STATE_CHANGED_DURING_ACCEPTANCE")
                self._clear_current()
                raise AdvisoryPlanStateError(str(error)) from error
            self.trading.record_system_event(
                EventType.HEDGE_PLAN_EXECUTION_STARTED,
                aggregate_id=plan.plan_id,
                correlation_id=plan.optimization_id,
                payload={
                    "plan_id": plan.plan_id,
                    "batch_id": batch.batch_id,
                    "hedge_order_ids": tuple(
                        order.hedge_order_id for order in batch.orders
                    ),
                    "execution_mode": "SIMULATED_WORKING_ORDERS",
                    "fills_created": 0,
                },
            )
            self._accepted_batches[plan.plan_id] = batch
            return batch

    async def reject(self, plan_id: str) -> AdvisoryHedgeRecommendation:
        """Record Manual Override and suppress this plan until exposure changes."""

        async with self._active_lock():
            assessment = await self.risk.assess()
            recommendation = await self._recommendation_locked(assessment)
            plan = recommendation.plan
            if plan is None or plan.plan_id != plan_id:
                raise AdvisoryPlanStateError(
                    "the advisory HedgePlan is no longer current"
                )
            material_key = self._material_key(assessment)
            self.trading.record_system_event(
                EventType.HEDGE_PLAN_REJECTED,
                aggregate_id=plan.plan_id,
                correlation_id=plan.optimization_id,
                payload={
                    "plan_id": plan.plan_id,
                    "decision": "MANUAL_OVERRIDE",
                },
            )
            self._rejected_material_key = material_key
            self._rejected_plan = plan
            self._clear_current()
            return self._view(
                AdvisoryLifecycleStatus.REJECTED,
                plan=plan,
                reasons=("TRADER_SELECTED_MANUAL_OVERRIDE",),
            )

    async def _recommendation_locked(
        self,
        assessment: RiskAssessment,
    ) -> AdvisoryHedgeRecommendation:
        material_key = self._material_key(assessment)
        actionable = self._is_actionable(assessment)

        if self._current_plan is not None and (
            self._current_material_key != material_key or not actionable
        ):
            reason = (
                "ADVISORY_NO_LONGER_REQUIRED"
                if not actionable
                else "MATERIAL_DESK_OR_RISK_STATE_CHANGED"
            )
            self._mark_stale(self._current_plan, reason)
            self._clear_current()

        if self._rejected_material_key is not None and (
            self._rejected_material_key != material_key
        ):
            self._rejected_material_key = None
            self._rejected_plan = None

        if assessment.auto_hedge_required:
            if self._current_plan is not None:
                self._mark_stale(
                    self._current_plan,
                    "AUTO_HEDGE_REQUIRED_ADVISORY_HANDOFF_ENDED",
                )
                self._clear_current()
            return self._view(
                AdvisoryLifecycleStatus.AUTO_HANDOFF_PENDING,
                reasons=("AUTO_HEDGE_REQUIRED_STEP_9_4_NOT_ACTIVE",),
            )
        if not actionable:
            reasons = (
                ("RISK_ASSESSMENT_UNAVAILABLE",)
                if assessment.risk_band is RiskBand.UNAVAILABLE
                else ("NO_ADVISORY_HEDGE_REQUIRED",)
            )
            return self._view(AdvisoryLifecycleStatus.NOT_REQUIRED, reasons=reasons)
        if self._rejected_material_key == material_key:
            return self._view(
                AdvisoryLifecycleStatus.REJECTED,
                plan=self._rejected_plan,
                reasons=("TRADER_SELECTED_MANUAL_OVERRIDE",),
            )

        snapshot = await self.optimizer.store.executable_snapshot("BTC")
        market_signature = self._market_signature(snapshot)
        if (
            self._current_plan is not None
            and self._current_market_signature != market_signature
        ):
            self._mark_stale(
                self._current_plan,
                "MARKET_ELIGIBILITY_OR_DATA_QUALITY_CHANGED",
            )
            self._clear_current()

        if self._current_plan is None:
            self._sequence += 1
            request = HedgeOptimizationInput.from_risk_assessment(
                assessment,
                optimization_id=(
                    f"advisory-d{assessment.desk_state_version}-"
                    f"p{assessment.policy_version.lower()}-{self._sequence:04}"
                ),
                expected_holding_seconds=self.expected_holding_seconds,
                mode=OptimizationMode.ADVISORY,
            )
            plan = await self.optimizer.optimize(request, base_asset="BTC")
            self._current_plan = plan
            self._current_material_key = material_key
            self._current_market_signature = market_signature
            self.trading.record_system_event(
                EventType.HEDGE_PLAN_GENERATED,
                aggregate_id=plan.plan_id,
                correlation_id=plan.optimization_id,
                payload={
                    "plan_id": plan.plan_id,
                    "mode": plan.mode,
                    "status": plan.status,
                    "risk_band": assessment.risk_band,
                    "target_delta_btc": plan.target_delta_btc,
                    "requested_hedge_delta_btc": plan.requested_hedge_delta_btc,
                    "allocated_hedge_delta_btc": plan.allocated_hedge_delta_btc,
                    "residual_unallocated_delta_btc": (
                        plan.residual_unallocated_delta_btc
                    ),
                    "market_snapshot_version": plan.market_snapshot_version,
                },
            )

        assert self._current_plan is not None
        return self._plan_view(self._current_plan)

    def _plan_view(self, plan: HedgePlan) -> AdvisoryHedgeRecommendation:
        if plan.status is HedgePlanStatus.FULLY_FEASIBLE:
            status = AdvisoryLifecycleStatus.AVAILABLE
        elif plan.status is HedgePlanStatus.PARTIALLY_FEASIBLE:
            status = AdvisoryLifecycleStatus.PARTIALLY_FEASIBLE
        elif plan.status is HedgePlanStatus.OPTIMIZATION_BLOCKED:
            status = AdvisoryLifecycleStatus.BLOCKED
        elif plan.status is HedgePlanStatus.NO_FEASIBLE_HEDGE:
            status = AdvisoryLifecycleStatus.NO_FEASIBLE_HEDGE
        else:
            status = AdvisoryLifecycleStatus.NOT_REQUIRED
        return self._view(
            status,
            plan=plan,
            reasons=plan.data_quality_flags,
            can_use=(
                plan.status
                in {
                    HedgePlanStatus.FULLY_FEASIBLE,
                    HedgePlanStatus.PARTIALLY_FEASIBLE,
                }
                and bool(plan.legs)
            ),
        )

    def _view(
        self,
        status: AdvisoryLifecycleStatus,
        *,
        plan: Optional[HedgePlan] = None,
        reasons: tuple[str, ...] = (),
        can_use: bool = False,
    ) -> AdvisoryHedgeRecommendation:
        return AdvisoryHedgeRecommendation(
            lifecycle_status=status,
            plan=plan,
            can_use_system_plan=can_use,
            reason_codes=tuple(dict.fromkeys(reasons)),
            expected_holding_seconds=self.expected_holding_seconds,
            holding_horizon_status=(
                "CONFIGURED"
                if self.expected_holding_seconds is not None
                else "UNAVAILABLE_SPOT_ONLY"
            ),
            demo_taker_fee_bps=(
                self.demo_config.taker_fee_bps
                if self.demo_config is not None
                else None
            ),
            economics_assumption_label=(
                self.demo_config.assumption_label
                if self.demo_config is not None
                else None
            ),
            fee_disclaimer=(
                self.demo_config.fee_disclaimer
                if self.demo_config is not None
                else None
            ),
        )

    @staticmethod
    def _is_actionable(assessment: RiskAssessment) -> bool:
        remaining = assessment.advisory_remaining_hedge_requirement_btc
        return (
            assessment.risk_band in {RiskBand.YELLOW, RiskBand.RED}
            and remaining is not None
            and remaining != Decimal("0")
        )

    @staticmethod
    def _material_key(assessment: RiskAssessment) -> tuple[object, ...]:
        remaining = assessment.advisory_remaining_hedge_requirement_btc
        direction = 0 if remaining is None else 1 if remaining > 0 else -1 if remaining < 0 else 0
        return (
            assessment.policy_version,
            assessment.desk_state_version,
            assessment.risk_band,
            assessment.action,
            direction,
            assessment.working_order_conflict,
            assessment.working_order_overhedge,
        )

    @staticmethod
    def _market_signature(
        snapshot: ExecutableMarketSnapshot,
    ) -> tuple[object, ...]:
        return tuple(
            sorted(
                (
                    market.venue.value,
                    market.instrument_type.value,
                    market.connection.status.value,
                    market.eligible,
                    market.exclusion_reason,
                    market.book is not None,
                    market.instrument is not None,
                    (
                        market.instrument.eligible_for_execution
                        if market.instrument is not None
                        else False
                    ),
                    market.funding_data_stale,
                    (
                        market.derivatives is not None
                        and (
                            market.derivatives.current_funding_rate is not None
                            or market.derivatives.predicted_funding_rate is not None
                        )
                    ),
                )
                for market in snapshot.markets
            )
        )

    @staticmethod
    def _instrument_id(market: ExecutableBookView) -> str:
        if market.instrument is not None:
            return market.instrument.venue_symbol
        if market.book is not None:
            return market.book.venue_symbol
        return f"{market.symbol}:{market.instrument_type.value}"

    @classmethod
    def _referenced_markets_eligible(
        cls,
        plan: HedgePlan,
        snapshot: ExecutableMarketSnapshot,
    ) -> bool:
        for leg in plan.legs:
            market = next(
                (
                    item
                    for item in snapshot.markets
                    if item.venue is leg.venue
                    and item.instrument_type is leg.instrument_type
                    and cls._instrument_id(item) == leg.instrument_id
                ),
                None,
            )
            if (
                market is None
                or market.connection.status is not MarketConnectionStatus.LIVE
                or not market.eligible
                or market.book is None
                or market.instrument is None
                or not market.instrument.eligible_for_execution
            ):
                return False
            levels = market.book.asks if leg.side.value == "BUY" else market.book.bids
            if not levels:
                return False
        return True

    def _mark_stale(self, plan: HedgePlan, reason: str) -> None:
        self.trading.record_system_event(
            EventType.HEDGE_PLAN_STALE,
            aggregate_id=plan.plan_id,
            correlation_id=plan.optimization_id,
            payload={
                "plan_id": plan.plan_id,
                "reason": reason,
                "generated_desk_state_version": plan.desk_state_version,
                "current_desk_state_version": self.trading.desk_state.version,
            },
        )

    def _clear_current(self) -> None:
        self._current_plan = None
        self._current_material_key = None
        self._current_market_signature = None

    def _active_lock(self) -> asyncio.Lock:
        """Keep one lock per runtime loop (tests use isolated asyncio.run loops)."""

        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:
            self._lock = asyncio.Lock()
            self._lock_loop = loop
        return self._lock


advisory_hedge_service = AdvisoryHedgeService(
    risk_service,
    hedge_optimizer_service,
    demo_service,
    expected_holding_seconds=(
        demo_desk_config.default_expected_hedge_horizon_seconds
    ),
    demo_config=demo_desk_config,
)
