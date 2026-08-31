"""Immutable contracts for reconciled desk PnL.

The models in this module deliberately describe accounting results only.  Market
data acquisition and workflow orchestration live outside the PnL package so the
engine can be replayed deterministically in tests and future reconciliation jobs.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..domain.models import InstrumentType


class PnLStatus(str, Enum):
    """Whether an exact, reconciled total can be published."""

    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    UNRECONCILED = "UNRECONCILED"


class AttributionStatus(str, Enum):
    """Completeness of the explanatory PnL bridge, independent of accounting."""

    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"


class PositionValuationStatus(str, Enum):
    FLAT = "FLAT"
    VALUED = "VALUED"
    MARK_UNAVAILABLE = "MARK_UNAVAILABLE"


class PnLPosition(BaseModel):
    """Average-cost state and current valuation for one accounting bucket."""

    model_config = ConfigDict(frozen=True)

    bucket_id: str
    instrument_type: InstrumentType
    venue: Optional[str] = None
    instrument_id: str
    signed_quantity_btc: Decimal
    average_entry_price_usd: Optional[Decimal] = Field(default=None, gt=0)
    mark_price_usd: Optional[Decimal] = Field(default=None, gt=0)
    gross_realized_pnl_usd: Decimal
    unrealized_mtm_usd: Optional[Decimal] = None
    valuation_status: PositionValuationStatus
    valuation_method: str
    data_quality_flags: tuple[str, ...] = ()

    @model_validator(mode="after")
    def valuation_fields_must_match_status(self) -> "PnLPosition":
        if self.valuation_status is PositionValuationStatus.FLAT:
            if self.signed_quantity_btc != 0 or self.average_entry_price_usd is not None:
                raise ValueError("flat position must have zero quantity and no entry price")
            if self.unrealized_mtm_usd != 0:
                raise ValueError("flat position must have zero unrealized PnL")
        elif self.signed_quantity_btc == 0 or self.average_entry_price_usd is None:
            raise ValueError("non-flat position requires quantity and average entry price")
        if self.valuation_status is PositionValuationStatus.VALUED:
            if self.mark_price_usd is None or self.unrealized_mtm_usd is None:
                raise ValueError("valued position requires a mark and unrealized PnL")
        if self.valuation_status is PositionValuationStatus.MARK_UNAVAILABLE:
            if self.mark_price_usd is not None or self.unrealized_mtm_usd is not None:
                raise ValueError("unvalued position cannot publish mark-based PnL")
        return self


class PnLSnapshot(BaseModel):
    """One immutable, fail-closed view of session-to-date desk economics."""

    model_config = ConfigDict(frozen=True)

    status: PnLStatus
    as_of: datetime
    desk_state_version: int = Field(ge=0)
    market_snapshot_version: Optional[int] = Field(default=None, ge=0)
    currency: str = "USD"
    valuation_method: str
    spot_mark_usd: Optional[Decimal] = None

    gross_realized_pnl_usd: Decimal
    trading_fees_usd: Optional[Decimal] = Field(default=None, ge=0)
    net_realized_pnl_usd: Optional[Decimal] = None
    spot_unrealized_mtm_usd: Optional[Decimal] = None
    perp_unrealized_mtm_usd: Optional[Decimal] = None
    total_desk_pnl_usd: Optional[Decimal] = None

    client_spread_capture_usd: Decimal
    hedge_slippage_vs_expected_usd: Optional[Decimal] = None
    hedge_implementation_shortfall_usd: Optional[Decimal] = None
    inventory_market_movement_usd: Optional[Decimal] = None
    attribution_status: AttributionStatus

    reconciliation_difference_usd: Optional[Decimal] = None
    reconciled: bool
    positions: tuple[PnLPosition, ...] = ()
    data_quality_flags: tuple[str, ...] = ()

    @model_validator(mode="after")
    def publication_state_must_be_consistent(self) -> "PnLSnapshot":
        if self.status is PnLStatus.COMPLETE:
            if not self.reconciled or self.total_desk_pnl_usd is None:
                raise ValueError("complete PnL must publish a reconciled total")
            if self.attribution_status is not AttributionStatus.COMPLETE:
                raise ValueError("complete PnL requires complete attribution")
        if self.status is PnLStatus.UNRECONCILED:
            if self.reconciled or self.reconciliation_difference_usd is None:
                raise ValueError("unreconciled PnL requires a reconciliation difference")
            if self.total_desk_pnl_usd is not None:
                raise ValueError("unreconciled PnL cannot publish a desk total")
        if self.reconciled and self.reconciliation_difference_usd is None:
            raise ValueError("reconciled PnL requires a reconciliation difference")
        return self
