"""Normalized risk-policy inputs and outputs."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..domain.models import ClientFlowState, DeskState, Event, HedgeFill, HedgeOrder


class RiskBand(str, Enum):
    GREEN = "GREEN"
    YELLOW = "YELLOW"
    RED = "RED"
    UNAVAILABLE = "UNAVAILABLE"


class RiskAction(str, Enum):
    WAREHOUSE = "WAREHOUSE"
    PARTIAL_HEDGE = "PARTIAL_HEDGE"
    IMMEDIATE_HEDGE = "IMMEDIATE_HEDGE"
    HOLD = "HOLD"


class InventoryOrSettlementState(str, Enum):
    NOT_EVALUATED = "NOT_EVALUATED"


class RiskReferencePrice(BaseModel):
    model_config = ConfigDict(frozen=True)

    asset: str
    price_usd: Optional[Decimal] = Field(default=None, gt=0)
    captured_at: datetime
    source: str
    market_snapshot_version: int = Field(ge=0)
    eligible: bool
    degraded: bool


class RiskAssessment(BaseModel):
    """Pure risk-policy result plus lifecycle state; never an execution instruction."""

    model_config = ConfigDict(frozen=True)

    assessment_id: str
    assessed_at: datetime
    policy_version: str
    assumption_label: str
    desk_state_version: int = Field(ge=0)
    market_snapshot_version: int = Field(ge=0)
    reference_price_usd: Optional[Decimal] = Field(default=None, gt=0)
    reference_price_degraded: bool
    reference_price_source: str
    actual_delta_btc: Decimal
    signed_delta_notional_usd: Optional[Decimal]
    absolute_delta_exposure_usd: Optional[Decimal] = Field(default=None, ge=0)
    risk_band: RiskBand
    action: RiskAction
    advisory_target_delta_btc: Optional[Decimal]
    advisory_gross_required_hedge_delta_btc: Optional[Decimal]
    advisory_remaining_hedge_requirement_btc: Optional[Decimal]
    # Compatibility aliases consumed by the Step 7 UI. They always map to the
    # advisory fields above and never to the automatic intervention target.
    target_delta_btc: Optional[Decimal]
    gross_required_hedge_delta_btc: Optional[Decimal]
    remaining_hedge_requirement_btc: Optional[Decimal]
    auto_hedge_target_ratio_of_soft: Decimal = Field(gt=0, lt=1)
    auto_hedge_target_notional_usd: Decimal = Field(gt=0)
    auto_hedge_target_delta_btc: Optional[Decimal]
    auto_gross_required_hedge_delta_btc: Optional[Decimal]
    auto_qualifying_working_order_delta_btc: Optional[Decimal]
    auto_remaining_hedge_requirement_btc: Optional[Decimal]
    auto_working_order_conflict: bool
    auto_working_order_overhedge: bool
    working_order_delta_btc: Decimal
    projected_delta_btc: Decimal
    working_order_conflict: bool
    working_order_overhedge: bool
    hard_breach_id: Optional[str] = None
    hard_breach_started_at: Optional[datetime] = None
    hard_breach_seconds_remaining: Optional[Decimal] = Field(default=None, ge=0)
    auto_hedge_required: bool = False
    auto_hedge_active: bool = False
    auto_hedge_complete: bool = False
    auto_hedge_blocked: bool = False
    auto_hedge_blocked_reasons: tuple[str, ...] = ()
    inventory_or_settlement_state: InventoryOrSettlementState = (
        InventoryOrSettlementState.NOT_EVALUATED
    )

    @model_validator(mode="after")
    def compatibility_aliases_and_lifecycle_must_reconcile(
        self,
    ) -> "RiskAssessment":
        if (
            self.target_delta_btc != self.advisory_target_delta_btc
            or self.gross_required_hedge_delta_btc
            != self.advisory_gross_required_hedge_delta_btc
            or self.remaining_hedge_requirement_btc
            != self.advisory_remaining_hedge_requirement_btc
        ):
            raise ValueError("legacy target fields must map to advisory fields")
        if self.auto_hedge_complete and (
            self.auto_hedge_active or self.auto_hedge_required
        ):
            raise ValueError("completed auto intervention cannot remain active")
        return self


class RiskAwareDemoWorkspaceState(BaseModel):
    client_flow: ClientFlowState
    desk_state: DeskState
    risk_assessment: RiskAssessment
    hedge_orders: tuple[HedgeOrder, ...]
    hedge_fills: tuple[HedgeFill, ...]
    events: tuple[Event, ...]
