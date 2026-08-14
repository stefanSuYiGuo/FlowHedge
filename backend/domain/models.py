"""Domain models for the first FlowHedge accounting slice."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class InstrumentType(str, Enum):
    SPOT = "SPOT"
    PERPETUAL = "PERPETUAL"


class ClientSide(str, Enum):
    """RFQ side from the client perspective, never the desk perspective."""

    BUY = "BUY"
    SELL = "SELL"


class RFQStatus(str, Enum):
    RECEIVED = "RECEIVED"
    PRICING = "PRICING"
    QUOTED = "QUOTED"
    FILLED = "FILLED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"


class QuoteStatus(str, Enum):
    ACTIVE = "ACTIVE"
    ACCEPTED = "ACCEPTED"
    EXPIRED = "EXPIRED"
    SUPERSEDED = "SUPERSEDED"
    REJECTED = "REJECTED"


class EventType(str, Enum):
    RFQ_RECEIVED = "RFQ_RECEIVED"
    RFQ_VALIDATED = "RFQ_VALIDATED"
    QUOTE_GENERATED = "QUOTE_GENERATED"
    QUOTE_ACCEPTED = "QUOTE_ACCEPTED"
    CLIENT_FILL = "CLIENT_FILL"
    POSITION_UPDATED = "POSITION_UPDATED"


class MarketObservation(BaseModel):
    venue: str
    instrument_id: str
    instrument_type: InstrumentType
    bid: Decimal = Field(gt=0)
    ask: Decimal = Field(gt=0)
    observed_at: datetime

    @model_validator(mode="after")
    def ask_must_not_be_below_bid(self) -> "MarketObservation":
        if self.ask < self.bid:
            raise ValueError("ask must be greater than or equal to bid")
        return self


class MarketSnapshot(BaseModel):
    market_snapshot_id: str
    version: int = Field(ge=1)
    captured_at: datetime
    base_asset: str
    quote_currency: str
    reference_price_usd: Decimal = Field(gt=0)
    observations: tuple[MarketObservation, ...]


class RFQ(BaseModel):
    rfq_id: str
    client_id: str
    instrument_id: str
    client_side: ClientSide
    quantity_btc: Decimal = Field(gt=0)
    received_at: datetime
    status: RFQStatus
    validation_market_snapshot_id: str
    validation_reference_price_usd: Decimal = Field(gt=0)
    validated_notional_usd: Decimal = Field(gt=0)


class Quote(BaseModel):
    quote_id: str
    rfq_id: str
    revision: int = Field(ge=1)
    quoted_price_usd: Decimal = Field(gt=0)
    quantity_btc: Decimal = Field(gt=0)
    created_at: datetime
    expires_at: datetime
    status: QuoteStatus
    market_snapshot_id: str
    desk_state_version: int = Field(ge=0)
    pricing_source: str


class ClientTrade(BaseModel):
    """An immutable economic client fill."""

    model_config = ConfigDict(frozen=True)

    client_trade_id: str
    rfq_id: str
    quote_id: str
    client_id: str
    instrument_id: str
    client_side: ClientSide
    quantity_btc: Decimal = Field(gt=0)
    trade_price_usd: Decimal = Field(gt=0)
    traded_at: datetime


class DeskState(BaseModel):
    version: int = Field(ge=0)
    as_of: datetime
    spot_inventory_btc: Decimal
    derivative_delta_btc: Decimal
    total_delta_btc: Decimal
    open_hedge_order_ids: tuple[str, ...] = ()
    working_order_delta_btc: Decimal = Decimal("0")

    @model_validator(mode="after")
    def total_delta_must_reconcile(self) -> "DeskState":
        expected = self.spot_inventory_btc + self.derivative_delta_btc
        if self.total_delta_btc != expected:
            raise ValueError(
                "total_delta_btc must equal spot_inventory_btc + derivative_delta_btc"
            )
        return self


class Event(BaseModel):
    model_config = ConfigDict(frozen=True)

    event_id: str
    event_type: EventType
    occurred_at: datetime
    aggregate_id: str
    correlation_id: str
    desk_state_version_before: int = Field(ge=0)
    desk_state_version_after: int = Field(ge=0)
    payload: dict[str, Any] = Field(default_factory=dict)


class DemoScenarioResult(BaseModel):
    replayed: bool
    market_snapshot: MarketSnapshot
    rfq: RFQ
    quote: Quote
    client_trade: ClientTrade
    desk_state_before: DeskState
    desk_state_after: DeskState
    events: tuple[Event, ...]
