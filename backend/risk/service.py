"""Persistent hard-breach lifecycle around the pure RiskPolicy calculation."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Callable, Optional
from uuid import uuid4

from ..config import RiskPolicyConfig, risk_policy_config
from ..demo import DemoTradingService, demo_service, utc_now
from ..domain.models import DeskState, EventType
from ..market.service import market_state_store
from ..market.store import InMemoryMarketStateStore
from .models import RiskAssessment, RiskBand
from .policy import RiskPolicy, build_risk_reference_price


@dataclass
class ActiveHardBreach:
    breach_id: str
    started_at: datetime
    auto_hedge_event_emitted: bool = False


class RiskService:
    """Own stable RED timers and idempotent escalation events; never place orders."""

    def __init__(
        self,
        market_store: InMemoryMarketStateStore,
        trading_service: DemoTradingService,
        *,
        config: RiskPolicyConfig = risk_policy_config,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self.market_store = market_store
        self.trading_service = trading_service
        self.config = config
        self.policy = RiskPolicy(config)
        self.now = now
        self._active_breach: Optional[ActiveHardBreach] = None
        self._task: Optional[asyncio.Task[None]] = None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="risk-policy-lifecycle")

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def reset(self) -> None:
        self._active_breach = None

    async def assess(
        self,
        desk_state: Optional[DeskState] = None,
        *,
        assessed_at: Optional[datetime] = None,
    ) -> RiskAssessment:
        snapshot = await self.market_store.snapshot("BTC")
        reference = build_risk_reference_price(snapshot)
        now = assessed_at or self.now()
        core = self.policy.evaluate(
            desk_state or self.trading_service.desk_state,
            reference,
            assessed_at=now,
        )
        return self.reconcile(core, now=now)

    def reconcile(self, assessment: RiskAssessment, *, now: datetime) -> RiskAssessment:
        """Attach persistent lifecycle state and emit transition events exactly once."""

        if assessment.risk_band is RiskBand.RED:
            if self._active_breach is None:
                self._active_breach = ActiveHardBreach(
                    breach_id=f"breach-{uuid4().hex[:12]}",
                    started_at=now,
                )
                self._emit(
                    EventType.RISK_RED,
                    assessment,
                    {
                        "breach_id": self._active_breach.breach_id,
                        "absolute_delta_exposure_usd": assessment.absolute_delta_exposure_usd,
                        "hard_delta_limit_usd": self.config.hard_delta_limit_usd,
                    },
                )
                self._emit(
                    EventType.AUTO_HEDGE_ARMED,
                    assessment,
                    {
                        "breach_id": self._active_breach.breach_id,
                        "grace_seconds": self.config.hard_breach_grace_seconds,
                    },
                )

            active = self._active_breach
            deadline = active.started_at + timedelta(
                seconds=float(self.config.hard_breach_grace_seconds)
            )
            remaining = Decimal(str(max(0.0, (deadline - now).total_seconds())))
            required = active.auto_hedge_event_emitted or now >= deadline
            updated = assessment.model_copy(
                update={
                    "hard_breach_id": active.breach_id,
                    "hard_breach_started_at": active.started_at,
                    "hard_breach_seconds_remaining": remaining,
                    "auto_hedge_required": required,
                    "auto_hedge_active": required,
                    "auto_hedge_complete": False,
                }
            )
            if required and not active.auto_hedge_event_emitted:
                active.auto_hedge_event_emitted = True
                self._emit(
                    EventType.AUTO_HEDGE_REQUIRED,
                    updated,
                    {
                        "breach_id": active.breach_id,
                        "desk_state_version": updated.desk_state_version,
                        "market_snapshot_version": updated.market_snapshot_version,
                        "actual_delta_btc": updated.actual_delta_btc,
                        "actual_delta_notional_usd": (
                            updated.signed_delta_notional_usd
                        ),
                        "soft_delta_limit_usd": self.config.soft_delta_limit_usd,
                        "hard_delta_limit_usd": self.config.hard_delta_limit_usd,
                        "auto_hedge_target_ratio_of_soft": (
                            updated.auto_hedge_target_ratio_of_soft
                        ),
                        "auto_hedge_target_notional_usd": (
                            updated.auto_hedge_target_notional_usd
                        ),
                        "auto_hedge_target_delta_btc": (
                            updated.auto_hedge_target_delta_btc
                        ),
                        "auto_gross_required_hedge_delta_btc": (
                            updated.auto_gross_required_hedge_delta_btc
                        ),
                        "qualifying_working_order_delta_btc": (
                            updated.auto_qualifying_working_order_delta_btc
                        ),
                        "auto_remaining_hedge_requirement_btc": (
                            updated.auto_remaining_hedge_requirement_btc
                        ),
                    },
                )
            return updated

        if (
            self._active_breach is not None
            and assessment.risk_band is RiskBand.UNAVAILABLE
        ):
            # Missing reference data cannot prove actual exposure exited RED.
            active = self._active_breach
            deadline = active.started_at + timedelta(
                seconds=float(self.config.hard_breach_grace_seconds)
            )
            remaining = Decimal(str(max(0.0, (deadline - now).total_seconds())))
            reasons = tuple(
                dict.fromkeys(
                    assessment.auto_hedge_blocked_reasons
                    + ("ACTIVE_RED_BREACH_UNVERIFIED",)
                )
            )
            return assessment.model_copy(
                update={
                    "hard_breach_id": active.breach_id,
                    "hard_breach_started_at": active.started_at,
                    "hard_breach_seconds_remaining": remaining,
                    "auto_hedge_required": active.auto_hedge_event_emitted,
                    "auto_hedge_active": active.auto_hedge_event_emitted,
                    "auto_hedge_complete": False,
                    "auto_hedge_blocked": True,
                    "auto_hedge_blocked_reasons": reasons,
                }
            )

        if self._active_breach is not None:
            active = self._active_breach
            if active.auto_hedge_event_emitted:
                auto_assessment = self.policy.apply_auto_target(assessment)
                target_reached = (
                    auto_assessment.absolute_delta_exposure_usd is not None
                    and auto_assessment.absolute_delta_exposure_usd
                    <= auto_assessment.auto_hedge_target_notional_usd
                )
                if not target_reached:
                    return auto_assessment.model_copy(
                        update={
                            "hard_breach_id": active.breach_id,
                            "hard_breach_started_at": active.started_at,
                            "hard_breach_seconds_remaining": Decimal("0"),
                            "auto_hedge_required": True,
                            "auto_hedge_active": True,
                            "auto_hedge_complete": False,
                        }
                    )

                self._active_breach = None
                return auto_assessment.model_copy(
                    update={
                        "hard_breach_id": active.breach_id,
                        "hard_breach_started_at": active.started_at,
                        "hard_breach_seconds_remaining": Decimal("0"),
                        "auto_remaining_hedge_requirement_btc": Decimal("0"),
                        "auto_hedge_required": False,
                        "auto_hedge_active": False,
                        "auto_hedge_complete": True,
                    }
                )

            self._active_breach = None
            self._emit(
                EventType.AUTO_HEDGE_CANCELLED,
                assessment,
                {
                    "breach_id": active.breach_id,
                    "exit_risk_band": assessment.risk_band,
                },
            )
        return assessment

    def _emit(
        self,
        event_type: EventType,
        assessment: RiskAssessment,
        payload: dict[str, object],
    ) -> None:
        self.trading_service.record_system_event(
            event_type,
            aggregate_id="desk-btc-risk",
            correlation_id=assessment.hard_breach_id or payload.get("breach_id", "risk"),
            payload=payload,
        )

    async def _run(self) -> None:
        while True:
            try:
                await self.assess()
            except Exception:
                # A transient failure must never terminate market/client-flow services.
                pass
            await asyncio.sleep(0.1)


risk_service = RiskService(market_state_store, demo_service)
