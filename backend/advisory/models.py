"""API contracts for the Step 9.3 trader-controlled advisory workflow."""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict

from ..domain.models import ClientFlowState, DeskState, Event, HedgeFill, HedgeOrder
from ..hedge_optimizer.models import HedgePlan
from ..risk.models import RiskAssessment


class AdvisoryLifecycleStatus(str, Enum):
    NOT_REQUIRED = "NOT_REQUIRED"
    AVAILABLE = "AVAILABLE"
    PARTIALLY_FEASIBLE = "PARTIALLY_FEASIBLE"
    NO_FEASIBLE_HEDGE = "NO_FEASIBLE_HEDGE"
    BLOCKED = "BLOCKED"
    REJECTED = "REJECTED"
    AUTO_HANDOFF_PENDING = "AUTO_HANDOFF_PENDING"


class AdvisoryHedgeRecommendation(BaseModel):
    """Current advisory view; only an explicit accept can create orders."""

    model_config = ConfigDict(frozen=True)

    lifecycle_status: AdvisoryLifecycleStatus
    plan: Optional[HedgePlan] = None
    can_use_system_plan: bool = False
    reason_codes: tuple[str, ...] = ()
    expected_holding_seconds: Optional[int] = None
    holding_horizon_status: str = "UNAVAILABLE_SPOT_ONLY"


class AdvisoryWorkspaceState(BaseModel):
    client_flow: ClientFlowState
    desk_state: DeskState
    risk_assessment: RiskAssessment
    advisory_recommendation: AdvisoryHedgeRecommendation
    hedge_orders: tuple[HedgeOrder, ...]
    hedge_fills: tuple[HedgeFill, ...]
    events: tuple[Event, ...]
