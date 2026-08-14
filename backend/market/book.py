"""Venue adapter helpers for maintaining a bounded normalized L2 order book."""

from __future__ import annotations

import zlib
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable

from ..domain.models import InstrumentType
from .models import MarketLevel, MarketVenue, NormalizedOrderBook


class KrakenChecksumMismatch(ValueError):
    """Raised when the local Kraken book no longer matches the exchange checksum."""


def parse_kraken_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def decimal_value(value: object) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _checksum_component(value: Decimal) -> str:
    compact = format(value, "f").replace(".", "").lstrip("0")
    return compact or "0"


def calculate_kraken_checksum(
    asks: Iterable[tuple[Decimal, Decimal]],
    bids: Iterable[tuple[Decimal, Decimal]],
) -> int:
    """Calculate Kraken's unsigned CRC32 over asks then bids, top ten each."""

    payload = "".join(
        _checksum_component(price) + _checksum_component(quantity)
        for price, quantity in list(asks)[:10]
    )
    payload += "".join(
        _checksum_component(price) + _checksum_component(quantity)
        for price, quantity in list(bids)[:10]
    )
    return zlib.crc32(payload.encode("utf-8")) & 0xFFFFFFFF


class KrakenOrderBookBuilder:
    """Translate Kraken snapshots and deltas into the venue-neutral book model."""

    def __init__(self, *, depth: int = 25) -> None:
        self.depth = depth
        self._bids: dict[Decimal, Decimal] = {}
        self._asks: dict[Decimal, Decimal] = {}
        self._exchange_timestamp: datetime | None = None
        self._received_at: datetime | None = None
        self._checksum: int | None = None

    def apply_snapshot(
        self, data: dict[str, Any], *, received_at: datetime
    ) -> NormalizedOrderBook:
        bids: dict[Decimal, Decimal] = {}
        asks: dict[Decimal, Decimal] = {}
        self._apply_levels(bids, data.get("bids", ()))
        self._apply_levels(asks, data.get("asks", ()))
        bids, asks = self._truncate(bids, asks)
        checksum = int(data["checksum"])
        self._validate_checksum(asks, bids, checksum)
        self._bids = bids
        self._asks = asks
        self._exchange_timestamp = parse_kraken_timestamp(data["timestamp"])
        self._received_at = received_at
        self._checksum = checksum
        return self.current_book()

    def apply_update(
        self, data: dict[str, Any], *, received_at: datetime
    ) -> NormalizedOrderBook:
        if self._exchange_timestamp is None:
            raise ValueError("cannot apply a Kraken update before a snapshot")
        bids = dict(self._bids)
        asks = dict(self._asks)
        self._apply_levels(bids, data.get("bids", ()))
        self._apply_levels(asks, data.get("asks", ()))
        bids, asks = self._truncate(bids, asks)
        checksum = int(data["checksum"])
        self._validate_checksum(asks, bids, checksum)
        self._bids = bids
        self._asks = asks
        self._exchange_timestamp = parse_kraken_timestamp(data["timestamp"])
        self._received_at = received_at
        self._checksum = checksum
        return self.current_book()

    def current_book(self) -> NormalizedOrderBook:
        if (
            self._exchange_timestamp is None
            or self._received_at is None
            or self._checksum is None
        ):
            raise ValueError("Kraken order book has not received a valid snapshot")
        sorted_bids = self._sorted_levels(self._bids, reverse=True)
        sorted_asks = self._sorted_levels(self._asks, reverse=False)
        if not sorted_bids or not sorted_asks:
            raise ValueError("Kraken order book requires at least one bid and ask")
        best_bid = sorted_bids[0][0]
        best_ask = sorted_asks[0][0]
        mid_price = (best_bid + best_ask) / Decimal("2")
        spread = best_ask - best_bid
        return NormalizedOrderBook(
            venue=MarketVenue.KRAKEN,
            symbol="BTC-USD",
            venue_symbol="BTC/USD",
            instrument_type=InstrumentType.SPOT,
            depth=self.depth,
            bids=tuple(
                MarketLevel(price=price, quantity=quantity)
                for price, quantity in sorted_bids
            ),
            asks=tuple(
                MarketLevel(price=price, quantity=quantity)
                for price, quantity in sorted_asks
            ),
            best_bid=best_bid,
            best_ask=best_ask,
            mid_price=mid_price,
            spread=spread,
            spread_bps=(spread / mid_price) * Decimal("10000"),
            exchange_timestamp=self._exchange_timestamp,
            received_at=self._received_at,
            checksum=self._checksum,
            source_sequence=None,
        )

    @staticmethod
    def _apply_levels(
        side: dict[Decimal, Decimal], levels: Iterable[dict[str, Any]]
    ) -> None:
        for level in levels:
            price = decimal_value(level["price"])
            quantity = decimal_value(level["qty"])
            side.pop(price, None)
            if quantity > 0:
                side[price] = quantity

    def _truncate(
        self,
        bids: dict[Decimal, Decimal],
        asks: dict[Decimal, Decimal],
    ) -> tuple[dict[Decimal, Decimal], dict[Decimal, Decimal]]:
        return (
            dict(self._sorted_levels(bids, reverse=True)[: self.depth]),
            dict(self._sorted_levels(asks, reverse=False)[: self.depth]),
        )

    @staticmethod
    def _sorted_levels(
        side: dict[Decimal, Decimal], *, reverse: bool
    ) -> list[tuple[Decimal, Decimal]]:
        return sorted(side.items(), key=lambda level: level[0], reverse=reverse)

    @staticmethod
    def _validate_checksum(
        asks: dict[Decimal, Decimal],
        bids: dict[Decimal, Decimal],
        expected: int,
    ) -> None:
        actual = calculate_kraken_checksum(
            sorted(asks.items(), key=lambda level: level[0]),
            sorted(bids.items(), key=lambda level: level[0], reverse=True),
        )
        if actual != expected:
            raise KrakenChecksumMismatch(
                f"Kraken checksum mismatch: expected {expected}, calculated {actual}"
            )
