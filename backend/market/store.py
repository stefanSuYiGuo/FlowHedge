"""Bounded in-memory cache for the latest valid state from each market venue."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from ..domain.models import InstrumentType
from .models import (
    InstrumentRules,
    MarketConnectionState,
    MarketConnectionStatus,
    MarketStateView,
    MarketVenue,
    NormalizedOrderBook,
    UnifiedMarketSnapshot,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


STALE_BOOK_AFTER_MS = 5_000
MarketKey = tuple[MarketVenue, str, InstrumentType]


class InMemoryMarketStateStore:
    """Keep one current book per venue/instrument; never retain tick history."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._books: dict[MarketKey, NormalizedOrderBook] = {}
        self._instruments: dict[MarketKey, InstrumentRules] = {}
        self._connections: dict[str, MarketConnectionState] = {}
        self._market_feeds: dict[MarketKey, str] = {}
        self._snapshot_version = 0

    async def register_feed(
        self,
        feed_id: str,
        venue: MarketVenue,
        endpoint: str,
        markets: tuple[tuple[str, InstrumentType], ...],
    ) -> None:
        async with self._lock:
            self._connections[feed_id] = MarketConnectionState(
                feed_id=feed_id,
                venue=venue,
                status=MarketConnectionStatus.DISCONNECTED,
                endpoint=endpoint,
            )
            for symbol, instrument_type in markets:
                key = (venue, symbol, instrument_type)
                existing_feed = self._market_feeds.get(key)
                if existing_feed is not None and existing_feed != feed_id:
                    raise ValueError(f"market already registered by {existing_feed}: {key}")
                self._market_feeds[key] = feed_id
            self._snapshot_version += 1

    async def update_connection(
        self,
        feed_id: str,
        *,
        status: MarketConnectionStatus | None = None,
        connected_at: datetime | None = None,
        last_message_at: datetime | None = None,
        last_book_update_at: datetime | None = None,
        last_error: str | None = None,
        reconnect_attempt: int | None = None,
        clear_error: bool = False,
    ) -> MarketConnectionState:
        async with self._lock:
            current = self._connections[feed_id]
            updates: dict[str, object] = {}
            if status is not None:
                updates["status"] = status
            if connected_at is not None:
                updates["connected_at"] = connected_at
            if last_message_at is not None:
                updates["last_message_at"] = last_message_at
            if last_book_update_at is not None:
                updates["last_book_update_at"] = last_book_update_at
            if last_error is not None or clear_error:
                updates["last_error"] = last_error
            if reconnect_attempt is not None:
                updates["reconnect_attempt"] = reconnect_attempt
            updated = current.model_copy(update=updates)
            self._connections[feed_id] = updated
            if (
                updated.status is not current.status
                or updated.last_error != current.last_error
            ):
                self._snapshot_version += 1
            return updated

    async def replace_book(self, book: NormalizedOrderBook) -> None:
        async with self._lock:
            self._books[(book.venue, book.symbol, book.instrument_type)] = book
            self._snapshot_version += 1

    async def replace_instrument(self, instrument: InstrumentRules) -> None:
        async with self._lock:
            self._instruments[
                (instrument.venue, instrument.symbol, instrument.instrument_type)
            ] = instrument
            self._snapshot_version += 1

    async def clear_book(
        self,
        venue: MarketVenue,
        symbol: str,
        instrument_type: InstrumentType = InstrumentType.SPOT,
    ) -> None:
        async with self._lock:
            self._books.pop((venue, symbol, instrument_type), None)
            self._snapshot_version += 1

    async def view(
        self,
        venue: MarketVenue,
        symbol: str,
        instrument_type: InstrumentType = InstrumentType.SPOT,
    ) -> MarketStateView:
        now = utc_now()
        key = (venue, symbol, instrument_type)
        async with self._lock:
            book = self._books.get(key)
            instrument = self._instruments.get(key)
            feed_id = self._market_feeds[key]
            connection = self._connections[feed_id]
        return self._build_view(key, connection, book, instrument, now=now)

    async def snapshot(self, base_asset: str) -> UnifiedMarketSnapshot:
        now = utc_now()
        normalized_base = base_asset.upper()
        async with self._lock:
            version = self._snapshot_version
            keys = tuple(
                key
                for key in self._market_feeds
                if key[1].split("-", 1)[0] == normalized_base
            )
            state = tuple(
                (
                    key,
                    self._connections[self._market_feeds[key]],
                    self._books.get(key),
                    self._instruments.get(key),
                )
                for key in keys
            )
        markets = tuple(
            self._build_view(key, connection, book, instrument, now=now)
            for key, connection, book, instrument in state
        )
        return UnifiedMarketSnapshot(
            snapshot_version=version,
            captured_at=now,
            base_asset=normalized_base,
            markets=tuple(
                sorted(
                    markets,
                    key=lambda market: (
                        market.venue.value,
                        market.instrument_type.value,
                        market.symbol,
                    ),
                )
            ),
        )

    @staticmethod
    def _build_view(
        key: MarketKey,
        connection: MarketConnectionState,
        book: NormalizedOrderBook | None,
        instrument: InstrumentRules | None,
        *,
        now: datetime,
    ) -> MarketStateView:
        venue, symbol, instrument_type = key
        data_age_ms = (
            max(0, int((now - book.received_at).total_seconds() * 1000))
            if book is not None
            else None
        )
        displayed_connection = connection
        if (
            connection.status is MarketConnectionStatus.LIVE
            and data_age_ms is not None
            and data_age_ms > STALE_BOOK_AFTER_MS
        ):
            displayed_connection = connection.model_copy(
                update={"status": MarketConnectionStatus.STALE}
            )
        if displayed_connection.status is not MarketConnectionStatus.LIVE:
            exclusion_reason = f"FEED_{displayed_connection.status.value}"
        elif book is None:
            exclusion_reason = "BOOK_UNAVAILABLE"
        else:
            exclusion_reason = None
        return MarketStateView(
            venue=venue,
            symbol=symbol,
            instrument_type=instrument_type,
            connection=displayed_connection,
            book=book,
            instrument=instrument,
            book_data_age_ms=data_age_ms,
            eligible=exclusion_reason is None,
            exclusion_reason=exclusion_reason,
            as_of=now,
        )

    async def connections(self) -> list[MarketConnectionState]:
        async with self._lock:
            return sorted(
                self._connections.values(), key=lambda connection: connection.feed_id
            )
