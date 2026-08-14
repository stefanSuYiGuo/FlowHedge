"""Immutable contracts for Step 9.1 hedge-candidate preparation."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..domain.models import InstrumentType
from ..execution_cost.models import ExecutionSide, FeeStatus
from ..hedge_economics.models import MAX_HOLDING_SECONDS, OpenInterestContext
from ..market.models import MarketVenue
from ..risk.models import RiskAssessment


class OptimizationMode(str, Enum):
    ADVISORY = "ADVISORY"
    AUTO_RISK = "AUTO_RISK"


class CandidateBuilderStatus(str, Enum):
    READY = "READY"
    NO_HEDGE_REQUIRED = "NO_HEDGE_REQUIRED"
    OPTIMIZATION_BLOCKED_WORKING_ORDER_CONFLICT = (
        "OPTIMIZATION_BLOCKED_WORKING_ORDER_CONFLICT"
    )
    OPTIMIZATION_BLOCKED_WORKING_ORDER_OVERHEDGE = (
        "OPTIMIZATION_BLOCKED_WORKING_ORDER_OVERHEDGE"
    )
    OPTIMIZATION_DATA_UNAVAILABLE = "OPTIMIZATION_DATA_UNAVAILABLE"
    NO_ELIGIBLE_CANDIDATES = "NO_ELIGIBLE_CANDIDATES"


class CandidateExclusionReason(str, Enum):
    MARKET_STALE = "MARKET_STALE"
    MARKET_DISCONNECTED = "MARKET_DISCONNECTED"
    MARKET_UNAVAILABLE = "MARKET_UNAVAILABLE"
    NO_EXECUTABLE_DEPTH = "NO_EXECUTABLE_DEPTH"
    INVALID_BOOK = "INVALID_BOOK"
    FEE_UNCONFIGURED = "FEE_UNCONFIGURED"
    HOLDING_HORIZON_UNAVAILABLE = "HOLDING_HORIZON_UNAVAILABLE"
    FUNDING_DATA_UNAVAILABLE = "FUNDING_DATA_UNAVAILABLE"
    ECONOMICS_INCOMPLETE = "ECONOMICS_INCOMPLETE"
    INSTRUMENT_METADATA_UNAVAILABLE = "INSTRUMENT_METADATA_UNAVAILABLE"
    INSTRUMENT_NOT_EXECUTABLE = "INSTRUMENT_NOT_EXECUTABLE"
    QUANTITY_NORMALIZATION_UNAVAILABLE = "QUANTITY_NORMALIZATION_UNAVAILABLE"
    REQUIREMENT_BELOW_MINIMUM_ORDER_SIZE = (
        "REQUIREMENT_BELOW_MINIMUM_ORDER_SIZE"
    )


class HedgeOptimizationInput(BaseModel):
    """RiskPolicy-owned quantity plus immutable identifiers for one cycle."""

    model_config = ConfigDict(frozen=True)

    optimization_id: str = Field(min_length=1, max_length=160)
    mode: OptimizationMode = OptimizationMode.ADVISORY
    actual_delta_btc: Decimal
    target_delta_btc: Decimal
    remaining_hedge_requirement_btc: Decimal
    side: Optional[ExecutionSide] = None
    expected_holding_seconds: Optional[int] = Field(
        default=None, gt=0, le=MAX_HOLDING_SECONDS
    )
    desk_state_version: int = Field(ge=0)
    risk_assessment_id: str = Field(min_length=1, max_length=200)
    market_snapshot_version: int = Field(ge=0)
    requested_at: datetime
    working_order_conflict: bool = False
    working_order_overhedge: bool = False

    @model_validator(mode="after")
    def normalize_side_and_validate_time(self) -> "HedgeOptimizationInput":
        if self.requested_at.tzinfo is None:
            raise ValueError("requested_at must be timezone-aware")
        expected_side = (
            ExecutionSide.BUY
            if self.remaining_hedge_requirement_btc > 0
            else ExecutionSide.SELL
            if self.remaining_hedge_requirement_btc < 0
            else None
        )
        if self.side is not None and self.side is not expected_side:
            raise ValueError("side must agree with the signed hedge requirement")
        if self.side is None and expected_side is not None:
            object.__setattr__(self, "side", expected_side)
        return self

    @classmethod
    def from_risk_assessment(
        cls,
        assessment: RiskAssessment,
        *,
        optimization_id: str,
        expected_holding_seconds: Optional[int],
        mode: OptimizationMode = OptimizationMode.ADVISORY,
        requested_at: Optional[datetime] = None,
    ) -> "HedgeOptimizationInput":
        """Copy a RiskAssessment requirement without subtracting orders again."""

        if mode is OptimizationMode.AUTO_RISK:
            target = assessment.auto_hedge_target_delta_btc
            remaining = assessment.auto_remaining_hedge_requirement_btc
            conflict = assessment.auto_working_order_conflict
            overhedge = assessment.auto_working_order_overhedge
        else:
            target = assessment.advisory_target_delta_btc
            remaining = assessment.advisory_remaining_hedge_requirement_btc
            conflict = assessment.working_order_conflict
            overhedge = assessment.working_order_overhedge
        if target is None or remaining is None:
            raise ValueError("risk assessment has no hedge target for this mode")
        return cls(
            optimization_id=optimization_id,
            mode=mode,
            actual_delta_btc=assessment.actual_delta_btc,
            target_delta_btc=target,
            remaining_hedge_requirement_btc=remaining,
            expected_holding_seconds=expected_holding_seconds,
            desk_state_version=assessment.desk_state_version,
            risk_assessment_id=assessment.assessment_id,
            market_snapshot_version=assessment.market_snapshot_version,
            requested_at=requested_at or assessment.assessed_at,
            working_order_conflict=conflict,
            working_order_overhedge=overhedge,
        )


class NormalizedBookReference(BaseModel):
    """Small versioned pointer; the bounded L2 book remains in MarketState."""

    model_config = ConfigDict(frozen=True)

    market_snapshot_version: int = Field(ge=0)
    venue: MarketVenue
    instrument_id: str
    instrument_type: InstrumentType
    side: ExecutionSide
    source_sequence: Optional[int] = Field(default=None, ge=0)
    captured_at: datetime


class HedgeCandidate(BaseModel):
    """One market's eligibility and normalized inputs, never an allocation."""

    model_config = ConfigDict(frozen=True)

    candidate_id: str
    venue: MarketVenue
    instrument_id: str
    instrument_type: InstrumentType
    side: ExecutionSide
    requested_requirement_btc: Decimal = Field(gt=0)
    eligible: bool
    exclusion_reason: Optional[CandidateExclusionReason] = None
    market_snapshot_version: int = Field(ge=0)
    book_captured_at: Optional[datetime] = None
    max_executable_quantity_btc: Decimal = Field(default=Decimal("0"), ge=0)
    fully_executable_for_full_requirement: bool = False
    native_quantity_unit: Optional[str] = None
    contract_multiplier: Optional[Decimal] = Field(default=None, gt=0)
    native_quantity_step: Optional[Decimal] = Field(default=None, gt=0)
    btc_equivalent_quantity_step: Optional[Decimal] = Field(default=None, gt=0)
    minimum_order_quantity: Optional[Decimal] = Field(default=None, gt=0)
    minimum_order_quantity_btc_equivalent: Optional[Decimal] = Field(
        default=None, gt=0
    )
    immediate_economics_available: bool = False
    carry_economics_available: bool = False
    fee_status: FeeStatus
    immediate_cost_reference: Optional[str] = None
    hedge_economics_reference: Optional[str] = None
    expected_funding_cost_bps: Optional[Decimal] = None
    entry_basis_bps: Optional[Decimal] = None
    open_interest_context: Optional[OpenInterestContext] = None
    data_quality_flags: tuple[str, ...] = ()
    normalized_book_reference: Optional[NormalizedBookReference] = None

    @model_validator(mode="after")
    def eligibility_must_reconcile(self) -> "HedgeCandidate":
        if self.eligible and self.exclusion_reason is not None:
            raise ValueError("eligible candidate cannot have an exclusion reason")
        if not self.eligible and self.exclusion_reason is None:
            raise ValueError("excluded candidate requires an exclusion reason")
        if self.eligible and (
            self.max_executable_quantity_btc <= 0
            or not self.immediate_economics_available
            or not self.carry_economics_available
        ):
            raise ValueError("eligible candidate requires executable, comparable economics")
        return self


class CandidateBuilderResult(BaseModel):
    """Deterministic Step 9.1 output consumed later by the allocator."""

    model_config = ConfigDict(frozen=True)

    optimization_id: str
    status: CandidateBuilderStatus
    required_hedge_delta_btc: Decimal
    side: Optional[ExecutionSide]
    expected_holding_seconds: Optional[int] = Field(default=None, gt=0)
    desk_state_version: int = Field(ge=0)
    risk_assessment_id: str
    market_snapshot_version: int = Field(ge=0)
    market_snapshot_captured_at: datetime
    eligible_candidates: tuple[HedgeCandidate, ...]
    excluded_candidates: tuple[HedgeCandidate, ...]
    total_eligible_depth_btc: Decimal = Field(ge=0)
    full_requirement_possible: bool
    data_quality_flags: tuple[str, ...] = ()

    @model_validator(mode="after")
    def candidates_and_depth_must_reconcile(self) -> "CandidateBuilderResult":
        if any(not candidate.eligible for candidate in self.eligible_candidates):
            raise ValueError("eligible_candidates contains an excluded candidate")
        if any(candidate.eligible for candidate in self.excluded_candidates):
            raise ValueError("excluded_candidates contains an eligible candidate")
        candidates = self.eligible_candidates + self.excluded_candidates
        ids = [candidate.candidate_id for candidate in candidates]
        if len(ids) != len(set(ids)):
            raise ValueError("candidate ids must be unique")
        expected_depth = sum(
            (candidate.max_executable_quantity_btc for candidate in self.eligible_candidates),
            Decimal("0"),
        )
        if self.total_eligible_depth_btc != expected_depth:
            raise ValueError("total eligible depth must reconcile with candidates")
        return self
