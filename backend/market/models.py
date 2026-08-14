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
    COINBASE = "COINBASE"
    OKX = "OKX"


class ContractStructure(str, Enum):
    SPOT = "SPOT"
    LINEAR = "LINEAR"
    INVERSE = "INVERSE"


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
    contract_structure: ContractStructure = ContractStructure.SPOT
    contract_multiplier: Decimal = Field(default=Decimal("1"), gt=0)
    contract_value_currency: Optional[str] = None
    native_quantity_unit: str = "BASE_ASSET"
    settlement_asset: str = "USD"
    usd_conversion_rate: Decimal = Field(default=Decimal("1"), gt=0)
    usd_conversion_assumption: Optional[str] = None
    received_at: datetime

    def quantity_to_btc_equivalent(
        self, native_quantity: Decimal, *, price: Decimal
    ) -> Decimal:
        """Normalize venue-native quantities using dynamically retrieved metadata."""

        if native_quantity < 0:
            raise ValueError("native quantity cannot be negative")
        if price <= 0:
            raise ValueError("price must be positive")
        if self.instrument_type is InstrumentType.SPOT:
            return native_quantity
        if self.contract_structure is ContractStructure.INVERSE:
            return native_quantity * self.contract_multiplier / price
        return native_quantity * self.contract_multiplier


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
    checksum: Optional[int] = Field(default=None, ge=0)
    source_sequence: Optional[int] = Field(default=None, ge=0)

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


class ExecutableMarketLevel(BaseModel):
    model_config = ConfigDict(frozen=True)

    price: Decimal = Field(gt=0)
    quantity_btc_equivalent: Decimal = Field(gt=0)
    source_quantity: Decimal = Field(gt=0)
    source_quantity_unit: str


class ExecutableOrderBook(BaseModel):
    """Bounded legitimate L2 depth prepared for the future Cost Engine."""

    model_config = ConfigDict(frozen=True)

    venue: MarketVenue
    symbol: str
    venue_symbol: str
    instrument_type: InstrumentType
    max_levels: int = Field(gt=0, le=200)
    bids: tuple[ExecutableMarketLevel, ...]
    asks: tuple[ExecutableMarketLevel, ...]
    exchange_timestamp: datetime
    received_at: datetime
    source_sequence: Optional[int] = Field(default=None, ge=0)


class DerivativeMarketContext(BaseModel):
    """Timestamped public derivatives context; missing venue fields remain null."""

    model_config = ConfigDict(frozen=True)

    venue: MarketVenue
    symbol: str
    venue_symbol: str
    mark_price: Optional[Decimal] = Field(default=None, gt=0)
    index_price: Optional[Decimal] = Field(default=None, gt=0)
    current_funding_rate: Optional[Decimal] = None
    predicted_funding_rate: Optional[Decimal] = None
    next_funding_time: Optional[datetime] = None
    funding_interval_seconds: Optional[int] = Field(default=None, gt=0)
    open_interest: Optional[Decimal] = Field(default=None, ge=0)
    open_interest_unit: Optional[str] = None
    open_interest_btc_equivalent: Optional[Decimal] = Field(default=None, ge=0)
    open_interest_usd: Optional[Decimal] = Field(default=None, ge=0)
    mark_price_captured_at: Optional[datetime] = None
    index_price_captured_at: Optional[datetime] = None
    funding_captured_at: Optional[datetime] = None
    open_interest_captured_at: Optional[datetime] = None
    received_at: datetime
    source: str
    basis_bps: Optional[Decimal] = None
    basis_reference_price_usd: Optional[Decimal] = Field(default=None, gt=0)
    basis_captured_at: Optional[datetime] = None


class MarketConnectionState(BaseModel):
    model_config = ConfigDict(frozen=True)

    feed_id: str
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
    instrument_type: InstrumentType
    connection: MarketConnectionState
    book: Optional[NormalizedOrderBook]
    instrument: Optional[InstrumentRules]
    derivatives: Optional[DerivativeMarketContext] = None
    executable_bid_levels: int = Field(default=0, ge=0)
    executable_ask_levels: int = Field(default=0, ge=0)
    book_data_age_ms: Optional[int] = Field(default=None, ge=0)
    derivative_data_age_ms: Optional[int] = Field(default=None, ge=0)
    derivative_data_stale: Optional[bool] = None
    eligible: bool
    exclusion_reason: Optional[str] = None
    as_of: datetime


class UnifiedMarketSnapshot(BaseModel):
    """One atomic, versioned read of all normalized markets for an asset."""

    model_config = ConfigDict(frozen=True)

    snapshot_version: int = Field(ge=0)
    captured_at: datetime
    base_asset: str
    markets: tuple[MarketStateView, ...]


class ExecutableBookView(BaseModel):
    model_config = ConfigDict(frozen=True)

    venue: MarketVenue
    symbol: str
    instrument_type: InstrumentType
    connection: MarketConnectionState
    book: Optional[ExecutableOrderBook]
    book_data_age_ms: Optional[int] = Field(default=None, ge=0)
    eligible: bool
    exclusion_reason: Optional[str] = None
    as_of: datetime
