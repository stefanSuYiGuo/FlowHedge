"""Contracts for trader-directed routing, preview, and simulated execution."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from ..domain.models import HedgeOrderBatchResult, InstrumentType
from ..execution_cost.models import ExecutionCostStatus, ExecutionSide
from ..market.models import MarketVenue


class ExecutionBatchStatus(str, Enum):
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    UNFILLED = "UNFILLED"


class ManualHedgeLegRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    venue: MarketVenue
    instrument_type: InstrumentType
    quantity_btc: Decimal = Field(gt=0)


class ManualHedgePreviewRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    request_id: str = Field(min_length=1, max_length=120)
    legs: tuple[ManualHedgeLegRequest, ...] = Field(min_length=1, max_length=4)


class ManualExecutionLegPreview(BaseModel):
    model_config = ConfigDict(frozen=True)

    venue: MarketVenue
    instrument_id: str
    instrument_type: InstrumentType
    side: ExecutionSide
    requested_quantity_btc: Decimal
    executable_quantity_btc: Decimal
    unfilled_quantity_btc: Decimal
    status: ExecutionCostStatus
    status_reason: Optional[str] = None
    market_snapshot_version: int
    arrival_mid_usd: Optional[Decimal] = None
    expected_vwap_usd: Optional[Decimal] = None
    spread_cost_bps: Optional[Decimal] = None
    depth_impact_bps: Optional[Decimal] = None
    taker_fee_bps: Optional[Decimal] = None
    expected_fee_usd: Optional[Decimal] = None
    expected_price_cost_usd: Optional[Decimal] = None
    expected_all_in_cost_usd: Optional[Decimal] = None


class ManualHedgePreview(BaseModel):
    model_config = ConfigDict(frozen=True)

    preview_id: str
    request_id: str
    created_at: datetime
    expires_at: datetime
    desk_state_version: int
    market_snapshot_version: int
    actual_delta_btc: Decimal
    advisory_target_delta_btc: Optional[Decimal] = None
    maximum_hedge_quantity_btc: Decimal
    submitted_hedge_delta_btc: Decimal
    projected_delta_btc: Decimal
    can_submit: bool
    reason_codes: tuple[str, ...] = ()
    legs: tuple[ManualExecutionLegPreview, ...]
    total_expected_fee_usd: Optional[Decimal] = None
    total_expected_all_in_cost_usd: Optional[Decimal] = None


class ManualHedgeSubmitRequest(BaseModel):
    preview_id: str = Field(min_length=1, max_length=160)


class ExecutionBatchRequest(BaseModel):
    execution_id: str = Field(min_length=1, max_length=160)


class ExecutionOrderMetrics(BaseModel):
    model_config = ConfigDict(frozen=True)

    hedge_order_id: str
    venue: str
    instrument_id: str
    instrument_type: InstrumentType
    side: str
    execution_source: str
    status: str
    market_snapshot_version: Optional[int] = None
    ordered_quantity_btc: Decimal
    filled_quantity_btc: Decimal
    remaining_quantity_btc: Decimal
    expected_vwap_usd: Optional[Decimal] = None
    realized_vwap_usd: Optional[Decimal] = None
    arrival_mid_usd: Optional[Decimal] = None
    slippage_vs_expected_usd: Decimal = Decimal("0")
    implementation_shortfall_usd: Decimal = Decimal("0")
    taker_fee_bps: Optional[Decimal] = None
    fee_usd: Decimal = Decimal("0")
    filled_notional_usd: Decimal = Decimal("0")
    all_in_cost_usd: Decimal = Decimal("0")


class ExecutionBatchMetrics(BaseModel):
    model_config = ConfigDict(frozen=True)

    execution_id: str
    batch_id: str
    origin: str
    executed_at: datetime
    status: ExecutionBatchStatus
    market_snapshot_version: int
    requested_quantity_btc: Decimal
    filled_quantity_btc: Decimal
    remaining_quantity_btc: Decimal
    expected_vwap_usd: Optional[Decimal] = None
    realized_vwap_usd: Optional[Decimal] = None
    filled_notional_usd: Decimal
    implementation_shortfall_usd: Decimal
    slippage_vs_expected_usd: Decimal
    fee_usd: Decimal
    all_in_cost_usd: Decimal
    orders: tuple[ExecutionOrderMetrics, ...]


class ManualHedgeSubmission(BaseModel):
    model_config = ConfigDict(frozen=True)

    order_batch: HedgeOrderBatchResult
    preview: ManualHedgePreview
