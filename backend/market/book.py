"""Venue adapter helpers for maintaining a bounded normalized L2 order book."""

from __future__ import annotations

import zlib
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable

from ..domain.models import InstrumentType
from .models import (
    ExecutableMarketLevel,
    ExecutableOrderBook,
    InstrumentRules,
    MarketLevel,
    MarketVenue,
    NormalizedOrderBook,
)


DISPLAY_BOOK_LEVELS = 25
EXECUTABLE_BOOK_MAX_LEVELS = 200


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


def normalized_books_from_levels(
    *,
    rules: InstrumentRules,
    bids: Iterable[tuple[Decimal, Decimal]],
    asks: Iterable[tuple[Decimal, Decimal]],
    exchange_timestamp: datetime,
    received_at: datetime,
    checksum: int | None = None,
    source_sequence: int | None = None,
    display_levels: int = DISPLAY_BOOK_LEVELS,
    executable_levels: int = EXECUTABLE_BOOK_MAX_LEVELS,
) -> tuple[NormalizedOrderBook, ExecutableOrderBook]:
    """Create compact and executable books from real venue levels only."""

    sorted_bids = sorted(
        ((decimal_value(price), decimal_value(quantity)) for price, quantity in bids),
        key=lambda level: level[0],
        reverse=True,
    )[:executable_levels]
    sorted_asks = sorted(
        ((decimal_value(price), decimal_value(quantity)) for price, quantity in asks),
        key=lambda level: level[0],
    )[:executable_levels]
    sorted_bids = [level for level in sorted_bids if level[1] > 0]
    sorted_asks = [level for level in sorted_asks if level[1] > 0]
    if not sorted_bids or not sorted_asks:
        raise ValueError("normalized books require at least one real bid and ask")

    def executable_level(level: tuple[Decimal, Decimal]) -> ExecutableMarketLevel:
        price, source_quantity = level
        return ExecutableMarketLevel(
            price=price,
            quantity_btc_equivalent=rules.quantity_to_btc_equivalent(
                source_quantity, price=price
            ),
            source_quantity=source_quantity,
            source_quantity_unit=rules.native_quantity_unit,
        )

    executable_bids = tuple(executable_level(level) for level in sorted_bids)
    executable_asks = tuple(executable_level(level) for level in sorted_asks)
    display_bids = executable_bids[:display_levels]
    display_asks = executable_asks[:display_levels]
    best_bid = display_bids[0].price
    best_ask = display_asks[0].price
    mid_price = (best_bid + best_ask) / Decimal("2")
    spread = best_ask - best_bid

    display = NormalizedOrderBook(
        venue=rules.venue,
        symbol=rules.symbol,
        venue_symbol=rules.venue_symbol,
        instrument_type=rules.instrument_type,
        depth=display_levels,
        bids=tuple(
            MarketLevel(
                price=level.price,
                quantity=level.quantity_btc_equivalent,
            )
            for level in display_bids
        ),
        asks=tuple(
            MarketLevel(
                price=level.price,
                quantity=level.quantity_btc_equivalent,
            )
            for level in display_asks
        ),
        best_bid=best_bid,
        best_ask=best_ask,
        mid_price=mid_price,
        spread=spread,
        spread_bps=(spread / mid_price) * Decimal("10000"),
        exchange_timestamp=exchange_timestamp,
        received_at=received_at,
        checksum=checksum,
        source_sequence=source_sequence,
    )
    executable = ExecutableOrderBook(
        venue=rules.venue,
        symbol=rules.symbol,
        venue_symbol=rules.venue_symbol,
        instrument_type=rules.instrument_type,
        max_levels=executable_levels,
        bids=executable_bids,
        asks=executable_asks,
        exchange_timestamp=exchange_timestamp,
        received_at=received_at,
        source_sequence=source_sequence,
    )
    return display, executable


def normalized_books_from_book(
    book: NormalizedOrderBook,
    rules: InstrumentRules,
) -> tuple[NormalizedOrderBook, ExecutableOrderBook]:
    """Normalize a venue builder's bounded native-quantity book."""

    return normalized_books_from_levels(
        rules=rules,
        bids=((level.price, level.quantity) for level in book.bids),
        asks=((level.price, level.quantity) for level in book.asks),
        exchange_timestamp=book.exchange_timestamp,
        received_at=book.received_at,
        checksum=book.checksum,
        source_sequence=book.source_sequence,
    )
