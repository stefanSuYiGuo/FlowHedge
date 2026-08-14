"""Immutable Step 8B request and result contracts."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..domain.models import InstrumentType
from ..execution_cost.models import (
    ExecutionCostStatus,
    ExecutionSide,
    FeeStatus,
)
from ..market.models import MarketVenue


MAX_HOLDING_SECONDS = 365 * 24 * 60 * 60


class CarryStatus(str, Enum):
    COMPLETE = "COMPLETE"
    UNAVAILABLE = "UNAVAILABLE"
    NOT_EVALUATED = "NOT_EVALUATED"


class HedgeEconomicsStatus(str, Enum):
    COMPLETE = "COMPLETE"
    PARTIAL_EXECUTION = "PARTIAL_EXECUTION"
    INCOMPLETE_IMMEDIATE_COST = "INCOMPLETE_IMMEDIATE_COST"
    CARRY_UNAVAILABLE = "CARRY_UNAVAILABLE"
    EXECUTION_UNAVAILABLE = "EXECUTION_UNAVAILABLE"
    INVALID_REQUEST = "INVALID_REQUEST"


class FundingRateSource(str, Enum):
    PREDICTED = "PREDICTED"
    CURRENT = "CURRENT"
    NOT_REQUIRED = "NOT_REQUIRED"
    UNAVAILABLE = "UNAVAILABLE"


class FundingProjectionMethod(str, Enum):
    NONE = "NONE"
    SINGLE_EVENT = "SINGLE_EVENT"
    FLAT_RATE_EXTRAPOLATION = "FLAT_RATE_EXTRAPOLATION"
    UNAVAILABLE = "UNAVAILABLE"


class BasisTreatment(str, Enum):
    CONTEXT_ONLY = "CONTEXT_ONLY"


class FundingBoundaryConvention(str, Enum):
    ENTRY_EXCLUSIVE_EXIT_INCLUSIVE = "ENTRY_EXCLUSIVE_EXIT_INCLUSIVE"


class HedgeEconomicsRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    request_id: str = Field(min_length=1, max_length=160)
    execution_cost_result_id: str = Field(min_length=1, max_length=300)
    expected_holding_seconds: int = Field(gt=0, le=MAX_HOLDING_SECONDS)
    market_snapshot_version: int = Field(ge=0)
    requested_at: datetime

    @model_validator(mode="after")
    def requested_at_must_be_timezone_aware(self) -> "HedgeEconomicsRequest":
        if self.requested_at.tzinfo is None:
            raise ValueError("requested_at must be timezone-aware")
        return self


class HedgeEconomicsCandidateRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    request_id: str = Field(min_length=1, max_length=120)
    venue: MarketVenue
    instrument_id: str = Field(min_length=1, max_length=80)
    instrument_type: InstrumentType
    side: ExecutionSide
    quantity_btc_equivalent: Decimal = Field(gt=0)
    expected_holding_seconds: int = Field(gt=0, le=MAX_HOLDING_SECONDS)
    market_snapshot_version: Optional[int] = Field(default=None, ge=0)
    requested_at: datetime

    @model_validator(mode="after")
    def requested_at_must_be_timezone_aware(
        self,
    ) -> "HedgeEconomicsCandidateRequest":
        if self.requested_at.tzinfo is None:
            raise ValueError("requested_at must be timezone-aware")
        return self


class HedgeEconomicsComparisonRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    request_id: str = Field(min_length=1, max_length=120)
    side: ExecutionSide
    quantity_btc_equivalent: Decimal = Field(gt=0)
    expected_holding_seconds: int = Field(gt=0, le=MAX_HOLDING_SECONDS)
    base_asset: str = Field(default="BTC", min_length=1, max_length=20)
    market_snapshot_version: Optional[int] = Field(default=None, ge=0)
    requested_at: datetime

    @model_validator(mode="after")
    def requested_at_must_be_timezone_aware(
        self,
    ) -> "HedgeEconomicsComparisonRequest":
        if self.requested_at.tzinfo is None:
            raise ValueError("requested_at must be timezone-aware")
        return self


class ProjectedFundingEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    event_time: datetime
    funding_rate: Decimal
    expected_cost_usd: Decimal


class OpenInterestContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    open_interest: Optional[Decimal] = Field(default=None, ge=0)
    open_interest_unit: Optional[str] = None
    open_interest_btc_equivalent: Optional[Decimal] = Field(default=None, ge=0)
    open_interest_usd: Optional[Decimal] = Field(default=None, ge=0)
    captured_at: Optional[datetime] = None


class HedgeEconomicsResult(BaseModel):
    """Comparable standalone economics; never an optimizer decision."""

    model_config = ConfigDict(frozen=True)

    result_id: str
    request_id: str
    execution_cost_result_id: str
    venue: MarketVenue
    instrument_id: str
    instrument_type: InstrumentType
    side: ExecutionSide
    requested_quantity_btc: Decimal = Field(gt=0)
    quantity_btc: Decimal = Field(ge=0)
    unfilled_quantity_btc: Decimal = Field(ge=0)
    fully_executable: bool
    execution_status: ExecutionCostStatus
    expected_holding_seconds: int = Field(gt=0)
    entry_time: datetime
    expected_exit_time: datetime
    immediate_price_cost_bps: Optional[Decimal] = None
    immediate_price_cost_usd: Optional[Decimal] = None
    immediate_execution_cost_bps: Optional[Decimal] = None
    immediate_execution_cost_usd: Optional[Decimal] = None
    immediate_fee_status: FeeStatus
    carry_status: CarryStatus
    economics_status: HedgeEconomicsStatus
    funding_rate_used: Optional[Decimal] = None
    funding_rate_source: FundingRateSource
    funding_projection_degraded: bool
    funding_projection_method: FundingProjectionMethod
    funding_boundary_convention: FundingBoundaryConvention = (
        FundingBoundaryConvention.ENTRY_EXCLUSIVE_EXIT_INCLUSIVE
    )
    modeled_funding_event_count: int = Field(ge=0)
    modeled_funding_events: tuple[ProjectedFundingEvent, ...]
    expected_funding_cost_bps: Optional[Decimal] = None
    expected_funding_cost_usd: Optional[Decimal] = None
    expected_carry_cost_bps: Optional[Decimal] = None
    expected_carry_cost_usd: Optional[Decimal] = None
    expected_total_hedge_cost_bps: Optional[Decimal] = None
    expected_total_hedge_cost_usd: Optional[Decimal] = None
    entry_basis_bps: Optional[Decimal] = None
    basis_reference: Optional[str] = None
    basis_reference_price_usd: Optional[Decimal] = Field(default=None, gt=0)
    basis_captured_at: Optional[datetime] = None
    basis_treatment: BasisTreatment = BasisTreatment.CONTEXT_ONLY
    open_interest_context: Optional[OpenInterestContext] = None
    market_snapshot_version: int = Field(ge=0)
    snapshot_captured_at: datetime
    book_captured_at: Optional[datetime] = None
    derivative_context_captured_at: Optional[datetime] = None
    funding_captured_at: Optional[datetime] = None
    data_quality_flags: tuple[str, ...]
    excluded_cost_components: tuple[str, ...]

    @model_validator(mode="after")
    def result_must_reconcile(self) -> "HedgeEconomicsResult":
        if self.quantity_btc + self.unfilled_quantity_btc != self.requested_quantity_btc:
            raise ValueError("economics quantities must reconcile")
        if self.modeled_funding_event_count != len(self.modeled_funding_events):
            raise ValueError("modeled funding event count must reconcile")
        if self.carry_status is CarryStatus.UNAVAILABLE and any(
            value is not None
            for value in (
                self.expected_funding_cost_bps,
                self.expected_funding_cost_usd,
                self.expected_carry_cost_bps,
                self.expected_carry_cost_usd,
                self.expected_total_hedge_cost_bps,
                self.expected_total_hedge_cost_usd,
            )
        ):
            raise ValueError("unavailable carry cannot produce expected totals")
        return self


class HedgeEconomicsComparisonResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    comparison_id: str
    request_id: str
    side: ExecutionSide
    requested_quantity_btc: Decimal = Field(gt=0)
    expected_holding_seconds: int = Field(gt=0)
    base_asset: str
    market_snapshot_version: int = Field(ge=0)
    snapshot_captured_at: datetime
    results: tuple[HedgeEconomicsResult, ...]
