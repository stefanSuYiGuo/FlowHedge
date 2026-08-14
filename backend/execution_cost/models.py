"""Immutable request/result contracts for Step 8A."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..domain.models import InstrumentType
from ..market.models import MarketVenue


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ExecutionSide(str, Enum):
    """Desk execution perspective for both Spot and Perpetual markets."""

    BUY = "BUY"
    SELL = "SELL"


class ExecutionCostStatus(str, Enum):
    OK = "OK"
    INSUFFICIENT_LIQUIDITY = "INSUFFICIENT_LIQUIDITY"
    MARKET_STALE = "MARKET_STALE"
    MARKET_UNAVAILABLE = "MARKET_UNAVAILABLE"
    INVALID_REQUEST = "INVALID_REQUEST"


class LiquidityType(str, Enum):
    TAKER = "TAKER"


class FeeStatus(str, Enum):
    CONFIGURED = "CONFIGURED"
    UNCONFIGURED = "UNCONFIGURED"


class ExecutionCostRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    request_id: str = Field(min_length=1, max_length=160)
    venue: MarketVenue
    instrument_id: str = Field(min_length=1, max_length=80)
    instrument_type: InstrumentType
    side: ExecutionSide
    quantity_btc_equivalent: Decimal = Field(gt=0)
    market_snapshot_version: Optional[int] = Field(default=None, ge=0)
    requested_at: datetime = Field(default_factory=utc_now)


class ExecutionCostComparisonRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    request_id: str = Field(min_length=1, max_length=120)
    side: ExecutionSide
    quantity_btc_equivalent: Decimal = Field(gt=0)
    base_asset: str = Field(default="BTC", min_length=1, max_length=20)
    market_snapshot_version: Optional[int] = Field(default=None, ge=0)
    requested_at: datetime = Field(default_factory=utc_now)


class SimulatedExecutionFill(BaseModel):
    model_config = ConfigDict(frozen=True)

    price: Decimal = Field(gt=0)
    quantity_btc: Decimal = Field(gt=0)


class BookSweepResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    requested_quantity_btc: Decimal = Field(gt=0)
    filled_quantity_btc: Decimal = Field(ge=0)
    unfilled_quantity_btc: Decimal = Field(ge=0)
    execution_vwap: Optional[Decimal] = Field(default=None, gt=0)
    executed_notional_quote: Decimal = Field(ge=0)
    fully_executable: bool
    fills: tuple[SimulatedExecutionFill, ...]

    @model_validator(mode="after")
    def quantities_and_vwap_must_reconcile(self) -> "BookSweepResult":
        if self.filled_quantity_btc + self.unfilled_quantity_btc != self.requested_quantity_btc:
            raise ValueError("filled and unfilled quantities must equal requested quantity")
        if self.fully_executable != (self.unfilled_quantity_btc == 0):
            raise ValueError("fully_executable must agree with unfilled quantity")
        if (self.filled_quantity_btc == 0) != (self.execution_vwap is None):
            raise ValueError("VWAP must exist exactly when quantity was filled")
        return self


class ExecutionFeeEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    venue: MarketVenue
    instrument_type: InstrumentType
    liquidity_type: LiquidityType = LiquidityType.TAKER
    fee_bps: Decimal = Field(ge=0)
    assumption_label: str = Field(min_length=1, max_length=120)


class ExecutionFeeConfig(BaseModel):
    """Central fee schedule; empty by default until desk assumptions are supplied."""

    model_config = ConfigDict(frozen=True)

    entries: tuple[ExecutionFeeEntry, ...] = ()

    @model_validator(mode="after")
    def entries_must_be_unique(self) -> "ExecutionFeeConfig":
        identities = [
            (entry.venue, entry.instrument_type, entry.liquidity_type)
            for entry in self.entries
        ]
        if len(identities) != len(set(identities)):
            raise ValueError("fee entries must be unique by venue/instrument/liquidity")
        return self

    def taker_fee_for(
        self, venue: MarketVenue, instrument_type: InstrumentType
    ) -> Optional[ExecutionFeeEntry]:
        return next(
            (
                entry
                for entry in self.entries
                if entry.venue is venue
                and entry.instrument_type is instrument_type
                and entry.liquidity_type is LiquidityType.TAKER
            ),
            None,
        )


class ExecutionCostResult(BaseModel):
    """Analytical output only; it never creates orders, fills, or desk mutations."""

    model_config = ConfigDict(frozen=True)

    result_id: str
    request_id: str
    venue: MarketVenue
    instrument_id: str
    instrument_type: InstrumentType
    side: ExecutionSide
    market_snapshot_version: int = Field(ge=0)
    snapshot_captured_at: datetime
    book_captured_at: Optional[datetime] = None
    requested_quantity_btc: Decimal = Field(gt=0)
    filled_quantity_btc: Decimal = Field(ge=0)
    unfilled_quantity_btc: Decimal = Field(ge=0)
    fully_executable: bool
    status: ExecutionCostStatus
    status_reason: Optional[str] = None
    best_bid: Optional[Decimal] = Field(default=None, gt=0)
    best_ask: Optional[Decimal] = Field(default=None, gt=0)
    arrival_mid: Optional[Decimal] = Field(default=None, gt=0)
    execution_vwap: Optional[Decimal] = Field(default=None, gt=0)
    quote_currency: Optional[str] = None
    usd_conversion_rate: Optional[Decimal] = Field(default=None, gt=0)
    usd_conversion_assumption: Optional[str] = None
    executed_notional_quote: Optional[Decimal] = Field(default=None, ge=0)
    executed_notional_usd: Optional[Decimal] = Field(default=None, ge=0)
    spread_cost_bps: Optional[Decimal] = Field(default=None, ge=0)
    depth_impact_bps: Optional[Decimal] = Field(default=None, ge=0)
    total_price_cost_bps: Optional[Decimal] = Field(default=None, ge=0)
    price_cost_usd: Optional[Decimal] = Field(default=None, ge=0)
    taker_fee_bps: Optional[Decimal] = Field(default=None, ge=0)
    fee_usd: Optional[Decimal] = Field(default=None, ge=0)
    fee_status: FeeStatus
    fee_assumption_label: Optional[str] = None
    all_in_immediate_cost_bps: Optional[Decimal] = Field(default=None, ge=0)
    all_in_immediate_cost_usd: Optional[Decimal] = Field(default=None, ge=0)
    fills: tuple[SimulatedExecutionFill, ...]

    @model_validator(mode="after")
    def result_must_reconcile(self) -> "ExecutionCostResult":
        if self.filled_quantity_btc + self.unfilled_quantity_btc != self.requested_quantity_btc:
            raise ValueError("execution result quantities must reconcile")
        if self.status is ExecutionCostStatus.OK and not self.fully_executable:
            raise ValueError("OK execution result must be fully executable")
        if self.status is ExecutionCostStatus.INSUFFICIENT_LIQUIDITY and self.fully_executable:
            raise ValueError("insufficient-liquidity result cannot be fully executable")
        if self.fee_status is FeeStatus.UNCONFIGURED and any(
            value is not None
            for value in (
                self.taker_fee_bps,
                self.fee_usd,
                self.all_in_immediate_cost_bps,
                self.all_in_immediate_cost_usd,
            )
        ):
            raise ValueError("unconfigured fees cannot produce all-in economics")
        return self


class ExecutionCostComparisonResult(BaseModel):
    """Standalone candidate results with no allocation or optimizer ranking."""

    model_config = ConfigDict(frozen=True)

    comparison_id: str
    request_id: str
    side: ExecutionSide
    requested_quantity_btc: Decimal = Field(gt=0)
    base_asset: str
    market_snapshot_version: int = Field(ge=0)
    snapshot_captured_at: datetime
    results: tuple[ExecutionCostResult, ...]
