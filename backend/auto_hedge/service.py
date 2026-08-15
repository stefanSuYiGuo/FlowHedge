"""Step 9.4 hard-limit controller using the existing optimizer and fill ledger."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Callable, Optional

from ..config import demo_desk_config
from ..demo import (
    DemoStateError,
    DemoTradingService,
    HedgeAllocationError,
    HedgeFillError,
    WORKING_HEDGE_ORDER_STATUSES,
    demo_service,
    utc_now,
)
from ..domain.accounting import signed_hedge_delta
from ..domain.models import (
    Event,
    EventType,
    HedgeOrder,
    HedgeOrderOrigin,
)
from ..hedge_optimizer.models import (
    HedgeOptimizationInput,
    HedgePlanStatus,
    OptimizationMode,
)
from ..hedge_optimizer.service import HedgeOptimizerService, hedge_optimizer_service
from ..market.models import (
    ExecutableBookView,
    ExecutableMarketSnapshot,
    MarketConnectionStatus,
)
from ..risk.models import RiskAssessment
from ..risk.service import RiskService, risk_service
from .models import AutoHedgeIntervention, AutoHedgeInterventionStatus


TERMINAL_INTERVENTION_STATUSES = {
    AutoHedgeInterventionStatus.COMPLETE,
    AutoHedgeInterventionStatus.CANCELLED,
}


class AutoHedgeController:
    """Own one idempotent automatic intervention per hard-limit breach."""

    def __init__(
        self,
        risk: RiskService,
        optimizer: HedgeOptimizerService,
        trading: DemoTradingService,
        *,
        expected_holding_seconds: int = (
            demo_desk_config.default_expected_hedge_horizon_seconds
        ),
        fill_interval_seconds: Decimal = Decimal("0.75"),
        retry_interval_seconds: Decimal = Decimal("1"),
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self.risk = risk
        self.optimizer = optimizer
        self.trading = trading
        self.expected_holding_seconds = expected_holding_seconds
        self.fill_interval_seconds = fill_interval_seconds
        self.retry_interval_seconds = retry_interval_seconds
        self.now = now
        self._task: Optional[asyncio.Task[None]] = None
        self._lock: Optional[asyncio.Lock] = None
        self._lock_loop: Optional[asyncio.AbstractEventLoop] = None
        self.reset()

    def reset(self) -> None:
        self._active: Optional[AutoHedgeIntervention] = None
        self._interventions: dict[str, AutoHedgeIntervention] = {}
        self._processed_trigger_event_ids: set[str] = set()
        self._sequence = 0
        self._fill_sequence = 0
        self._next_fill_at: Optional[datetime] = None
        self._next_retry_at: Optional[datetime] = None
        self._last_block_key: Optional[tuple[object, ...]] = None
        self._last_incomplete_market_version: Optional[int] = None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(
                self._run(),
                name="auto-risk-controller",
            )

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def view(self) -> Optional[AutoHedgeIntervention]:
        return self._active

    async def step(self) -> Optional[AutoHedgeIntervention]:
        """Process trigger/state changes once; exposed for deterministic tests."""

        async with self._active_lock():
            assessment = await self.risk.assess()
            await self._consume_trigger_events(assessment)
            if (
                self._active is not None
                and self._active.status not in TERMINAL_INTERVENTION_STATUSES
            ):
                assessment = await self.risk.assess()
                await self._drive(assessment)
            return self._active

    async def _consume_trigger_events(self, assessment: RiskAssessment) -> None:
        for event in tuple(self.trading.events):
            if (
                event.event_type is not EventType.AUTO_HEDGE_REQUIRED
                or event.event_id in self._processed_trigger_event_ids
            ):
                continue
            self._processed_trigger_event_ids.add(event.event_id)
            breach_id = str(event.payload.get("breach_id", ""))
            if not breach_id or breach_id in self._interventions:
                continue
            if (
                not assessment.auto_hedge_required
                or assessment.hard_breach_id != breach_id
            ):
                continue
            intervention = AutoHedgeIntervention(
                intervention_id=f"auto-intervention-{breach_id}",
                breach_id=breach_id,
                status=AutoHedgeInterventionStatus.STARTING,
                started_at=self.now(),
                target_notional_usd=assessment.auto_hedge_target_notional_usd,
                latest_risk_assessment_id=assessment.assessment_id,
                current_exposure_usd=assessment.absolute_delta_exposure_usd,
                latest_auto_remaining_hedge_btc=(
                    assessment.auto_remaining_hedge_requirement_btc
                ),
            )
            self._interventions[breach_id] = intervention
            self._active = intervention
            self._emit(
                EventType.AUTO_HEDGE_STARTED,
                {
                    "intervention_id": intervention.intervention_id,
                    "breach_id": breach_id,
                    "trigger_event_id": event.event_id,
                    "latest_risk_assessment_id": assessment.assessment_id,
                    "current_exposure_usd": assessment.absolute_delta_exposure_usd,
                    "target_notional_usd": assessment.auto_hedge_target_notional_usd,
                    "auto_remaining_hedge_requirement_btc": (
                        assessment.auto_remaining_hedge_requirement_btc
                    ),
                },
            )

    async def _drive(self, assessment: RiskAssessment) -> None:
        assert self._active is not None
        self._update_active_from_assessment(assessment)

        if self._target_reached(assessment):
            await self._complete(assessment)
            return
        if assessment.reference_price_usd is None:
            self._mark_blocked(
                assessment,
                tuple(assessment.auto_hedge_blocked_reasons)
                or ("RISK_REFERENCE_PRICE_UNAVAILABLE",),
            )
            return
        if not assessment.auto_hedge_required:
            await self._cancel_intervention(
                assessment,
                reason="UNDERLYING_BREACH_NO_LONGER_REQUIRES_AUTO_HEDGE",
            )
            return

        active_orders = self._working_auto_orders()
        non_auto_working_delta = self._non_auto_working_delta()
        position_changed = self._active_plan_position_changed(assessment)
        unsafe_overhedge = assessment.auto_working_order_overhedge and (
            not active_orders
            or non_auto_working_delta != 0
            or position_changed
        )
        if assessment.auto_working_order_conflict or unsafe_overhedge:
            cancelled = self.trading.cancel_auto_risk_orders(
                self._active.intervention_id,
                reason=(
                    "WORKING_ORDER_OVERHEDGE"
                    if assessment.auto_working_order_overhedge
                    else "WORKING_ORDER_CONFLICT"
                ),
            )
            if cancelled.cancelled_hedge_order_ids:
                self._emit(
                    EventType.AUTO_HEDGE_REOPTIMIZING,
                    {
                        "intervention_id": self._active.intervention_id,
                        "breach_id": self._active.breach_id,
                        "reason": "WORKING_ORDER_SET_CHANGED",
                        "cancelled_order_ids": cancelled.cancelled_hedge_order_ids,
                    },
                )
                assessment = await self.risk.assess()
                self._update_active_from_assessment(assessment)
                if self._target_reached(assessment):
                    await self._complete(assessment)
                    return
            if assessment.auto_working_order_conflict:
                self._mark_blocked(assessment, ("WORKING_ORDER_CONFLICT",))
                return

        snapshot = await self.optimizer.store.executable_snapshot("BTC")
        active_orders = self._working_auto_orders()
        if active_orders and not self._orders_remain_eligible(active_orders, snapshot):
            self.trading.cancel_auto_risk_orders(
                self._active.intervention_id,
                reason="REFERENCED_MARKET_NO_LONGER_ELIGIBLE",
            )
            self._emit(
                EventType.AUTO_HEDGE_REOPTIMIZING,
                {
                    "intervention_id": self._active.intervention_id,
                    "breach_id": self._active.breach_id,
                    "reason": "REFERENCED_MARKET_NO_LONGER_ELIGIBLE",
                },
            )
            assessment = await self.risk.assess()
            self._update_active_from_assessment(assessment)
            active_orders = self._working_auto_orders()

        remaining = assessment.auto_remaining_hedge_requirement_btc
        if active_orders:
            # Working orders already represent committed legitimate liquidity.
            # Let their fills/cancellations change DeskState before optimizing
            # any residual; otherwise fast live snapshots can repeatedly cover
            # the same requirement before the first simulated fill arrives.
            await self._fill_cycle(assessment)
            return
        if remaining is None:
            self._mark_blocked(assessment, ("AUTO_REQUIREMENT_UNAVAILABLE",))
            return
        if remaining == 0:
            self._set_status(
                AutoHedgeInterventionStatus.EXECUTING,
                reasons=("QUALIFYING_WORKING_ORDERS_COVER_AUTO_TARGET",),
            )
            return

        now = self.now()
        if self._next_retry_at is not None and now < self._next_retry_at:
            return
        if (
            self._last_incomplete_market_version is not None
            and snapshot.snapshot_version == self._last_incomplete_market_version
        ):
            return
        await self._optimize_and_submit(assessment)

    async def _optimize_and_submit(self, assessment: RiskAssessment) -> None:
        assert self._active is not None
        self._sequence += 1
        self._set_status(AutoHedgeInterventionStatus.REOPTIMIZING)
        if self._active.generated_plan_ids:
            self._emit(
                EventType.AUTO_HEDGE_REOPTIMIZING,
                {
                    "intervention_id": self._active.intervention_id,
                    "breach_id": self._active.breach_id,
                    "reason": "LATEST_AUTO_REQUIREMENT_CHANGED",
                    "latest_risk_assessment_id": assessment.assessment_id,
                },
            )
        request = HedgeOptimizationInput.from_risk_assessment(
            assessment,
            optimization_id=(
                f"auto-{self._active.breach_id}-{self._sequence:04}"
            ),
            expected_holding_seconds=self.expected_holding_seconds,
            mode=OptimizationMode.AUTO_RISK,
            requested_at=self.now(),
        )
        plan = await self.optimizer.optimize(request, base_asset="BTC")
        plan_is_executable = (
            plan.status
            in {
                HedgePlanStatus.FULLY_FEASIBLE,
                HedgePlanStatus.PARTIALLY_FEASIBLE,
            }
            and bool(plan.legs)
        )
        plan_update: dict[str, object] = {
            "generated_plan_ids": self._active.generated_plan_ids
            + (plan.plan_id,),
            "reason_codes": tuple(dict.fromkeys(plan.data_quality_flags)),
        }
        if plan_is_executable:
            plan_update.update(
                {"active_plan_id": plan.plan_id, "active_plan": plan}
            )
        self._active = self._active.model_copy(update=plan_update)
        self._interventions[self._active.breach_id] = self._active
        self._emit(
            EventType.AUTO_HEDGE_PLAN_CREATED,
            {
                "intervention_id": self._active.intervention_id,
                "breach_id": self._active.breach_id,
                "plan_id": plan.plan_id,
                "mode": plan.mode,
                "status": plan.status,
                "latest_risk_assessment_id": assessment.assessment_id,
                "requested_hedge_delta_btc": plan.requested_hedge_delta_btc,
                "allocated_hedge_delta_btc": plan.allocated_hedge_delta_btc,
                "residual_unallocated_delta_btc": (
                    plan.residual_unallocated_delta_btc
                ),
                "market_snapshot_version": plan.market_snapshot_version,
            },
        )
        if not plan_is_executable:
            self._next_retry_at = self.now() + timedelta(
                seconds=float(self.retry_interval_seconds)
            )
            self._mark_blocked(
                assessment,
                tuple(plan.data_quality_flags) or (plan.status.value,),
            )
            return

        try:
            batch = self.trading.create_auto_risk_hedge_orders(
                plan,
                intervention_id=self._active.intervention_id,
                breach_id=self._active.breach_id,
            )
        except (DemoStateError, HedgeAllocationError) as error:
            self._next_retry_at = self.now() + timedelta(
                seconds=float(self.retry_interval_seconds)
            )
            self._mark_blocked(
                assessment,
                ("DESK_STATE_CHANGED_DURING_AUTO_SUBMISSION", str(error)),
            )
            return

        new_order_ids = tuple(order.hedge_order_id for order in batch.orders)
        self._active = self._active.model_copy(
            update={
                "status": AutoHedgeInterventionStatus.EXECUTING,
                "auto_order_ids": tuple(
                    dict.fromkeys(self._active.auto_order_ids + new_order_ids)
                ),
                "planned_quantity_btc": (
                    self._active.planned_quantity_btc
                    + abs(batch.submitted_hedge_delta_btc)
                ),
            }
        )
        self._interventions[self._active.breach_id] = self._active
        self._next_fill_at = self.now() + timedelta(
            seconds=float(self.fill_interval_seconds)
        )
        self._next_retry_at = None
        self._last_block_key = None

    async def _fill_cycle(self, assessment: RiskAssessment) -> None:
        assert self._active is not None
        now = self.now()
        if self._next_fill_at is not None and now < self._next_fill_at:
            return
        for order in self._working_auto_orders():
            latest = await self.risk.assess()
            self._update_active_from_assessment(latest)
            if self._target_reached(latest):
                await self._complete(latest)
                return
            active_orders = self._working_auto_orders()
            unsafe_overhedge = latest.auto_working_order_overhedge and (
                not active_orders
                or self._non_auto_working_delta() != 0
                or self._active_plan_position_changed(latest)
            )
            if latest.auto_working_order_conflict or unsafe_overhedge:
                self.trading.cancel_auto_risk_orders(
                    self._active.intervention_id,
                    reason="AUTO_REQUIREMENT_CHANGED_BEFORE_FILL",
                )
                self._emit(
                    EventType.AUTO_HEDGE_REOPTIMIZING,
                    {
                        "intervention_id": self._active.intervention_id,
                        "breach_id": self._active.breach_id,
                        "reason": "AUTO_REQUIREMENT_CHANGED_BEFORE_FILL",
                    },
                )
                return

            proposed_fill_quantity = (
                order.remaining_quantity_btc / Decimal("2")
                if order.filled_quantity_btc == 0
                else order.remaining_quantity_btc
            )
            latest_gross = latest.auto_gross_required_hedge_delta_btc
            non_auto_working_delta = self._non_auto_working_delta()
            available_for_auto_fill = (
                None
                if latest_gross is None
                else latest_gross - non_auto_working_delta
            )
            order_direction = signed_hedge_delta(order.side, Decimal("1"))
            if (
                available_for_auto_fill is None
                or available_for_auto_fill == 0
                or (available_for_auto_fill > 0) != (order_direction > 0)
            ):
                self.trading.cancel_auto_risk_orders(
                    self._active.intervention_id,
                    reason="AUTO_REQUIREMENT_CHANGED_BEFORE_FILL",
                )
                self._emit(
                    EventType.AUTO_HEDGE_REOPTIMIZING,
                    {
                        "intervention_id": self._active.intervention_id,
                        "breach_id": self._active.breach_id,
                        "reason": "AUTO_REQUIREMENT_CHANGED_BEFORE_FILL",
                    },
                )
                return
            fill_quantity = min(
                proposed_fill_quantity,
                abs(available_for_auto_fill),
            )
            self._fill_sequence += 1
            try:
                result = self.trading.simulate_hedge_fill(
                    order.hedge_order_id,
                    fill_quantity,
                    (
                        f"auto-fill-{self._active.intervention_id}-"
                        f"{self._fill_sequence:06}"
                    ),
                )
            except (DemoStateError, HedgeFillError):
                self._emit(
                    EventType.AUTO_HEDGE_REOPTIMIZING,
                    {
                        "intervention_id": self._active.intervention_id,
                        "breach_id": self._active.breach_id,
                        "reason": "AUTO_ORDER_FILL_FAILED",
                        "hedge_order_id": order.hedge_order_id,
                    },
                )
                return
            self._active = self._active.model_copy(
                update={
                    "filled_quantity_btc": (
                        self._active.filled_quantity_btc
                        + result.fill.quantity_btc
                    )
                }
            )
            self._interventions[self._active.breach_id] = self._active

        latest = await self.risk.assess()
        self._update_active_from_assessment(latest)
        if self._target_reached(latest):
            await self._complete(latest)
            return
        self._next_fill_at = self.now() + timedelta(
            seconds=float(self.fill_interval_seconds)
        )
        if not self._working_auto_orders():
            plan = self._active.active_plan
            if (
                plan is not None
                and plan.status is HedgePlanStatus.PARTIALLY_FEASIBLE
                and latest.auto_remaining_hedge_requirement_btc not in {None, Decimal("0")}
            ):
                self._last_incomplete_market_version = plan.market_snapshot_version
                self._set_status(
                    AutoHedgeInterventionStatus.INCOMPLETE,
                    reasons=("RESIDUAL_HEDGE_REQUIREMENT_UNALLOCATED",),
                )
                self._emit(
                    EventType.AUTO_HEDGE_INCOMPLETE,
                    {
                        "intervention_id": self._active.intervention_id,
                        "breach_id": self._active.breach_id,
                        "remaining_hedge_requirement_btc": (
                            latest.auto_remaining_hedge_requirement_btc
                        ),
                        "wait_for_market_state_after_version": (
                            plan.market_snapshot_version
                        ),
                    },
                )

    async def _complete(self, assessment: RiskAssessment) -> None:
        assert self._active is not None
        if self._active.status is AutoHedgeInterventionStatus.COMPLETE:
            return
        self.trading.cancel_auto_risk_orders(
            self._active.intervention_id,
            reason="AUTO_TARGET_REACHED",
        )
        latest = await self.risk.assess()
        exposure = (
            latest.absolute_delta_exposure_usd
            if latest.absolute_delta_exposure_usd is not None
            else assessment.absolute_delta_exposure_usd
        )
        self._active = self._active.model_copy(
            update={
                "status": AutoHedgeInterventionStatus.COMPLETE,
                "completed_at": self.now(),
                "latest_risk_assessment_id": latest.assessment_id,
                "current_exposure_usd": exposure,
                "latest_auto_remaining_hedge_btc": Decimal("0"),
                "reason_codes": ("AUTO_TARGET_REACHED",),
            }
        )
        self._interventions[self._active.breach_id] = self._active
        self._emit(
            EventType.AUTO_HEDGE_COMPLETE,
            {
                "intervention_id": self._active.intervention_id,
                "breach_id": self._active.breach_id,
                "final_exposure_usd": exposure,
                "target_notional_usd": self._active.target_notional_usd,
                "filled_quantity_btc": self._active.filled_quantity_btc,
                "cancelled_remaining_auto_orders": True,
            },
        )

    async def _cancel_intervention(
        self,
        assessment: RiskAssessment,
        *,
        reason: str,
    ) -> None:
        assert self._active is not None
        self.trading.cancel_auto_risk_orders(
            self._active.intervention_id,
            reason=reason,
        )
        self._active = self._active.model_copy(
            update={
                "status": AutoHedgeInterventionStatus.CANCELLED,
                "completed_at": self.now(),
                "latest_risk_assessment_id": assessment.assessment_id,
                "current_exposure_usd": assessment.absolute_delta_exposure_usd,
                "latest_auto_remaining_hedge_btc": (
                    assessment.auto_remaining_hedge_requirement_btc
                ),
                "reason_codes": (reason,),
            }
        )
        self._interventions[self._active.breach_id] = self._active
        self._emit(
            EventType.AUTO_HEDGE_CANCELLED,
            {
                "intervention_id": self._active.intervention_id,
                "breach_id": self._active.breach_id,
                "reason": reason,
            },
        )

    def _mark_blocked(
        self,
        assessment: RiskAssessment,
        reasons: tuple[str, ...],
    ) -> None:
        assert self._active is not None
        normalized = tuple(dict.fromkeys(reasons or ("NO_VALID_CANDIDATE",)))
        block_key = (
            assessment.desk_state_version,
            assessment.market_snapshot_version,
            normalized,
        )
        self._set_status(AutoHedgeInterventionStatus.BLOCKED, reasons=normalized)
        if block_key == self._last_block_key:
            return
        self._last_block_key = block_key
        self._emit(
            EventType.AUTO_HEDGE_BLOCKED,
            {
                "intervention_id": self._active.intervention_id,
                "breach_id": self._active.breach_id,
                "reasons": normalized,
                "remaining_hedge_requirement_btc": (
                    assessment.auto_remaining_hedge_requirement_btc
                ),
            },
        )

    def _update_active_from_assessment(self, assessment: RiskAssessment) -> None:
        assert self._active is not None
        self._active = self._active.model_copy(
            update={
                "latest_risk_assessment_id": assessment.assessment_id,
                "current_exposure_usd": assessment.absolute_delta_exposure_usd,
                "latest_auto_remaining_hedge_btc": (
                    assessment.auto_remaining_hedge_requirement_btc
                ),
            }
        )
        self._interventions[self._active.breach_id] = self._active

    def _set_status(
        self,
        status: AutoHedgeInterventionStatus,
        *,
        reasons: tuple[str, ...] = (),
    ) -> None:
        assert self._active is not None
        self._active = self._active.model_copy(
            update={"status": status, "reason_codes": reasons}
        )
        self._interventions[self._active.breach_id] = self._active

    def _working_auto_orders(self) -> tuple[HedgeOrder, ...]:
        assert self._active is not None
        return tuple(
            order
            for order in self.trading.hedge_orders.values()
            if order.origin is HedgeOrderOrigin.AUTO_RISK
            and order.source_intervention_id == self._active.intervention_id
            and order.status in WORKING_HEDGE_ORDER_STATUSES
        )

    def _non_auto_working_delta(self) -> Decimal:
        return sum(
            (
                signed_hedge_delta(order.side, order.remaining_quantity_btc)
                for order in self.trading.hedge_orders.values()
                if order.origin is not HedgeOrderOrigin.AUTO_RISK
                and order.status in WORKING_HEDGE_ORDER_STATUSES
            ),
            Decimal("0"),
        )

    def _active_plan_position_changed(self, assessment: RiskAssessment) -> bool:
        """Separate external position changes from fills belonging to the plan."""

        assert self._active is not None
        plan = self._active.active_plan
        if plan is None:
            return True
        plan_filled_delta = sum(
            (
                signed_hedge_delta(order.side, order.filled_quantity_btc)
                for order in self.trading.hedge_orders.values()
                if order.source_plan_id == plan.plan_id
                and order.filled_quantity_btc > 0
            ),
            Decimal("0"),
        )
        expected_actual = plan.actual_delta_btc + plan_filled_delta
        return assessment.actual_delta_btc != expected_actual

    @staticmethod
    def _target_reached(assessment: RiskAssessment) -> bool:
        return (
            assessment.absolute_delta_exposure_usd is not None
            and assessment.absolute_delta_exposure_usd
            <= assessment.auto_hedge_target_notional_usd
        )

    @classmethod
    def _orders_remain_eligible(
        cls,
        orders: tuple[HedgeOrder, ...],
        snapshot: ExecutableMarketSnapshot,
    ) -> bool:
        return all(cls._order_market_eligible(order, snapshot) for order in orders)

    @staticmethod
    def _order_market_eligible(
        order: HedgeOrder,
        snapshot: ExecutableMarketSnapshot,
    ) -> bool:
        market = next(
            (
                item
                for item in snapshot.markets
                if item.venue.value == order.venue
                and item.instrument_type is order.instrument_type
                and AutoHedgeController._instrument_id(item) == order.instrument_id
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
        levels = (
            market.book.asks
            if order.side.value in {"BUY", "LONG"}
            else market.book.bids
        )
        return bool(levels)

    @staticmethod
    def _instrument_id(market: ExecutableBookView) -> str:
        if market.instrument is not None:
            return market.instrument.venue_symbol
        if market.book is not None:
            return market.book.venue_symbol
        return f"{market.symbol}:{market.instrument_type.value}"

    def _emit(self, event_type: EventType, payload: dict[str, object]) -> Event:
        assert self._active is not None
        return self.trading.record_system_event(
            event_type,
            aggregate_id=self._active.intervention_id,
            correlation_id=self._active.breach_id,
            payload=payload,
        )

    async def _run(self) -> None:
        while True:
            try:
                await self.step()
            except Exception:
                # A transient data or state race must not terminate the controller.
                pass
            await asyncio.sleep(0.1)

    def _active_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:
            self._lock = asyncio.Lock()
            self._lock_loop = loop
        return self._lock


auto_hedge_controller = AutoHedgeController(
    risk_service,
    hedge_optimizer_service,
    demo_service,
)
