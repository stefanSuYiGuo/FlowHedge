"""Normalized market data models shared by every future venue adapter."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..domain.models import InstrumentType


class MarketVenue(str, Enum):
    KRAKEN = "KRAKEN"


class MarketConnectionStatus(str, Enum):
    CONNECTING = "CONNECTING"
    LIVE = "LIVE"
    STALE = "STALE"
    DISCONNECTED = "DISCONNECTED"
    RECONNECTING = "RECONNECTING"


class MarketLevel(BaseModel):
    model_config = ConfigDict(frozen=True)

    price: Decimal = Field(gt=0)
    quantity: Decimal = Field(gt=0)


class InstrumentRules(BaseModel):
    model_config = ConfigDict(frozen=True)

    venue: MarketVenue
    symbol: str
    venue_symbol: str
    instrument_type: InstrumentType
    base_asset: str
    quote_asset: str
    price_increment: Decimal = Field(gt=0)
    quantity_increment: Decimal = Field(gt=0)
    quantity_min: Decimal = Field(gt=0)
    price_precision: int = Field(ge=0)
    quantity_precision: int = Field(ge=0)
    status: str
    received_at: datetime


class NormalizedOrderBook(BaseModel):
    """A bounded, current L2 book that contains no historical tick stream."""

    model_config = ConfigDict(frozen=True)

    venue: MarketVenue
    symbol: str
    venue_symbol: str
    instrument_type: InstrumentType
    depth: int = Field(gt=0)
    bids: tuple[MarketLevel, ...]
    asks: tuple[MarketLevel, ...]
    best_bid: Decimal = Field(gt=0)
    best_ask: Decimal = Field(gt=0)
    mid_price: Decimal = Field(gt=0)
    spread: Decimal = Field(ge=0)
    spread_bps: Decimal = Field(ge=0)
    exchange_timestamp: datetime
    received_at: datetime
    checksum: int = Field(ge=0)

    @model_validator(mode="after")
    def levels_and_derived_values_must_reconcile(self) -> "NormalizedOrderBook":
        if not self.bids or not self.asks:
            raise ValueError("a normalized order book requires bids and asks")
        if len(self.bids) > self.depth or len(self.asks) > self.depth:
            raise ValueError("book levels cannot exceed subscribed depth")
        if any(
            self.bids[index].price < self.bids[index + 1].price
            for index in range(len(self.bids) - 1)
        ):
            raise ValueError("bids must be sorted from high to low")
        if any(
            self.asks[index].price > self.asks[index + 1].price
            for index in range(len(self.asks) - 1)
        ):
            raise ValueError("asks must be sorted from low to high")
        if self.best_bid != self.bids[0].price or self.best_ask != self.asks[0].price:
            raise ValueError("best bid and ask must match the first book levels")
        if self.best_bid > self.best_ask:
            raise ValueError("best bid cannot exceed best ask")
        expected_spread = self.best_ask - self.best_bid
        expected_mid = (self.best_bid + self.best_ask) / Decimal("2")
        if self.spread != expected_spread or self.mid_price != expected_mid:
            raise ValueError("mid price and spread must reconcile with top of book")
        return self


class MarketConnectionState(BaseModel):
    model_config = ConfigDict(frozen=True)

    venue: MarketVenue
    status: MarketConnectionStatus
    endpoint: str
    connected_at: Optional[datetime] = None
    last_message_at: Optional[datetime] = None
    last_book_update_at: Optional[datetime] = None
    last_error: Optional[str] = None
    reconnect_attempt: int = Field(default=0, ge=0)


class MarketStateView(BaseModel):
    model_config = ConfigDict(frozen=True)

    venue: MarketVenue
    symbol: str
    connection: MarketConnectionState
    book: Optional[NormalizedOrderBook]
    instrument: Optional[InstrumentRules]
    book_data_age_ms: Optional[int] = Field(default=None, ge=0)
    as_of: datetime
