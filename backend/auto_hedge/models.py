"""Observable contracts for the simulated Step 9.4 Auto Risk Controller."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from ..hedge_optimizer.models import HedgePlan


class AutoHedgeInterventionStatus(str, Enum):
    STARTING = "STARTING"
    EXECUTING = "EXECUTING"
    REOPTIMIZING = "REOPTIMIZING"
    INCOMPLETE = "INCOMPLETE"
    BLOCKED = "BLOCKED"
    COMPLETE = "COMPLETE"
    CANCELLED = "CANCELLED"


class AutoHedgeIntervention(BaseModel):
    """Audit-friendly state for one persistent hard-limit breach."""

    model_config = ConfigDict(frozen=True)

    intervention_id: str
    breach_id: str
    status: AutoHedgeInterventionStatus
    started_at: datetime
    completed_at: Optional[datetime] = None
    target_notional_usd: Decimal = Field(gt=0)
    latest_risk_assessment_id: str
    current_exposure_usd: Optional[Decimal] = Field(default=None, ge=0)
    latest_auto_remaining_hedge_btc: Optional[Decimal] = None
    active_plan_id: Optional[str] = None
    active_plan: Optional[HedgePlan] = None
    generated_plan_ids: tuple[str, ...] = ()
    auto_order_ids: tuple[str, ...] = ()
    planned_quantity_btc: Decimal = Field(default=Decimal("0"), ge=0)
    filled_quantity_btc: Decimal = Field(default=Decimal("0"), ge=0)
    reason_codes: tuple[str, ...] = ()
