"""Bounded in-memory cache for the latest valid state from each market venue."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from .models import (
    InstrumentRules,
    MarketConnectionState,
    MarketConnectionStatus,
    MarketStateView,
    MarketVenue,
    NormalizedOrderBook,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class InMemoryMarketStateStore:
    """Keep one current book per venue/symbol; never retain an unbounded history."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._books: dict[tuple[MarketVenue, str], NormalizedOrderBook] = {}
        self._instruments: dict[tuple[MarketVenue, str], InstrumentRules] = {}
        self._connections: dict[MarketVenue, MarketConnectionState] = {}

    async def register_venue(self, venue: MarketVenue, endpoint: str) -> None:
        async with self._lock:
            self._connections[venue] = MarketConnectionState(
                venue=venue,
                status=MarketConnectionStatus.DISCONNECTED,
                endpoint=endpoint,
            )

    async def update_connection(
        self,
        venue: MarketVenue,
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
            current = self._connections[venue]
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
            self._connections[venue] = updated
            return updated

    async def replace_book(self, book: NormalizedOrderBook) -> None:
        async with self._lock:
            self._books[(book.venue, book.symbol)] = book

    async def replace_instrument(self, instrument: InstrumentRules) -> None:
        async with self._lock:
            self._instruments[(instrument.venue, instrument.symbol)] = instrument

    async def clear_book(self, venue: MarketVenue, symbol: str) -> None:
        async with self._lock:
            self._books.pop((venue, symbol), None)

    async def view(self, venue: MarketVenue, symbol: str) -> MarketStateView:
        now = utc_now()
        async with self._lock:
            book = self._books.get((venue, symbol))
            instrument = self._instruments.get((venue, symbol))
            connection = self._connections[venue]
        data_age_ms = (
            max(0, int((now - book.received_at).total_seconds() * 1000))
            if book is not None
            else None
        )
        return MarketStateView(
            venue=venue,
            symbol=symbol,
            connection=connection,
            book=book,
            instrument=instrument,
            book_data_age_ms=data_age_ms,
            as_of=now,
        )

    async def connections(self) -> list[MarketConnectionState]:
        async with self._lock:
            return list(self._connections.values())
