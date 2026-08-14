"""Bounded in-memory cache for the latest valid state from each market venue."""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timezone
from decimal import Decimal

from ..domain.models import InstrumentType
from .models import (
    DerivativeMarketContext,
    ExecutableBookView,
    ExecutableMarketSnapshot,
    ExecutableOrderBook,
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
STALE_DERIVATIVE_CONTEXT_AFTER_MS = 30_000
DERIVATIVE_HISTORY_MAX_POINTS = 720
DERIVATIVE_HISTORY_MIN_INTERVAL_SECONDS = 5
MarketKey = tuple[MarketVenue, str, InstrumentType]


class InMemoryMarketStateStore:
    """Keep bounded current books plus bounded derivatives observations."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._books: dict[MarketKey, NormalizedOrderBook] = {}
        self._executable_books: dict[MarketKey, ExecutableOrderBook] = {}
        self._instruments: dict[MarketKey, InstrumentRules] = {}
        self._derivatives: dict[MarketKey, DerivativeMarketContext] = {}
        self._derivative_history: dict[
            MarketKey, deque[DerivativeMarketContext]
        ] = {}
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

    async def replace_books(
        self,
        display_book: NormalizedOrderBook,
        executable_book: ExecutableOrderBook,
    ) -> None:
        """Atomically publish compact display and deeper executable views."""

        key = (
            display_book.venue,
            display_book.symbol,
            display_book.instrument_type,
        )
        if key != (
            executable_book.venue,
            executable_book.symbol,
            executable_book.instrument_type,
        ):
            raise ValueError("display and executable book identities must match")
        async with self._lock:
            self._books[key] = display_book
            self._executable_books[key] = executable_book
            self._snapshot_version += 1

    async def replace_derivative_context(
        self, context: DerivativeMarketContext
    ) -> None:
        key = (context.venue, context.symbol, InstrumentType.PERPETUAL)
        async with self._lock:
            self._derivatives[key] = context
            history = self._derivative_history.setdefault(
                key, deque(maxlen=DERIVATIVE_HISTORY_MAX_POINTS)
            )
            if (
                not history
                or (context.received_at - history[-1].received_at).total_seconds()
                >= DERIVATIVE_HISTORY_MIN_INTERVAL_SECONDS
                or context.received_at < history[-1].received_at
            ):
                history.append(context)
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
            self._executable_books.pop((venue, symbol, instrument_type), None)
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
            derivatives = self._derivatives.get(key)
            executable = self._executable_books.get(key)
            feed_id = self._market_feeds[key]
            connection = self._connections[feed_id]
            references = tuple(
                (
                    reference_key,
                    self._connections[self._market_feeds[reference_key]],
                    reference_book,
                    self._instruments.get(reference_key),
                )
                for reference_key, reference_book in self._books.items()
            )
        market = self._build_view(
            key,
            connection,
            book,
            instrument,
            derivatives,
            executable,
            now=now,
        )
        return self._add_basis((market,), references, now=now)[0]

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
                    self._derivatives.get(key),
                    self._executable_books.get(key),
                )
                for key in keys
            )
        markets = tuple(
            self._build_view(
                key,
                connection,
                book,
                instrument,
                derivatives,
                executable,
                now=now,
            )
            for key, connection, book, instrument, derivatives, executable in state
        )
        markets = self._add_basis(markets, state, now=now)
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
        derivatives: DerivativeMarketContext | None,
        executable: ExecutableOrderBook | None,
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
        elif instrument is None:
            exclusion_reason = "INSTRUMENT_METADATA_UNAVAILABLE"
        elif executable is None:
            exclusion_reason = "EXECUTABLE_BOOK_UNAVAILABLE"
        else:
            exclusion_reason = None
        derivative_age_ms = (
            max(0, int((now - derivatives.received_at).total_seconds() * 1000))
            if derivatives is not None
            else None
        )
        return MarketStateView(
            venue=venue,
            symbol=symbol,
            instrument_type=instrument_type,
            connection=displayed_connection,
            book=book,
            instrument=instrument,
            derivatives=derivatives,
            executable_bid_levels=len(executable.bids) if executable else 0,
            executable_ask_levels=len(executable.asks) if executable else 0,
            book_data_age_ms=data_age_ms,
            derivative_data_age_ms=derivative_age_ms,
            derivative_data_stale=(
                derivative_age_ms > STALE_DERIVATIVE_CONTEXT_AFTER_MS
                if derivative_age_ms is not None
                else None
            ),
            eligible=exclusion_reason is None,
            exclusion_reason=exclusion_reason,
            as_of=now,
        )

    @staticmethod
    def _add_basis(
        markets: tuple[MarketStateView, ...],
        raw_state: tuple[tuple, ...],
        *,
        now: datetime,
    ) -> tuple[MarketStateView, ...]:
        """Attach basis versus fresh Kraken/Coinbase USD Spot mids, never stablecoin mids."""

        spot_prices: list[Decimal] = []
        for item in raw_state:
            key, connection, book, instrument = item[:4]
            venue, _, instrument_type = key
            if (
                venue in {MarketVenue.KRAKEN, MarketVenue.COINBASE}
                and instrument_type is InstrumentType.SPOT
                and connection.status is MarketConnectionStatus.LIVE
                and book is not None
                and instrument is not None
                and instrument.quote_asset == "USD"
                and (now - book.received_at).total_seconds() * 1000
                <= STALE_BOOK_AFTER_MS
            ):
                spot_prices.append(book.mid_price)
        if not spot_prices:
            return markets
        spot_prices.sort()
        midpoint = len(spot_prices) // 2
        spot_reference = (
            spot_prices[midpoint]
            if len(spot_prices) % 2
            else (spot_prices[midpoint - 1] + spot_prices[midpoint]) / Decimal("2")
        )
        updated: list[MarketStateView] = []
        for market in markets:
            context = market.derivatives
            if context is None or market.book is None:
                updated.append(market)
                continue
            derivative_reference = context.mark_price or market.book.mid_price
            basis = (
                (derivative_reference - spot_reference)
                / spot_reference
                * Decimal("10000")
            )
            updated.append(
                market.model_copy(
                    update={
                        "derivatives": context.model_copy(
                            update={
                                "basis_bps": basis,
                                "basis_reference_price_usd": spot_reference,
                                "basis_captured_at": now,
                            }
                        )
                    }
                )
            )
        return tuple(updated)

    async def executable_view(
        self,
        venue: MarketVenue,
        symbol: str,
        instrument_type: InstrumentType,
    ) -> ExecutableBookView:
        now = utc_now()
        key = (venue, symbol, instrument_type)
        async with self._lock:
            book = self._executable_books.get(key)
            instrument = self._instruments.get(key)
            derivatives = self._derivatives.get(key)
            connection = self._connections[self._market_feeds[key]]
        return self._build_executable_view(
            key,
            connection,
            book,
            instrument,
            derivatives,
            now=now,
        )

    async def executable_snapshot(
        self, base_asset: str | None = None
    ) -> ExecutableMarketSnapshot:
        """Atomically capture every normalized input required by the Cost Engine."""

        now = utc_now()
        normalized_base = base_asset.upper() if base_asset is not None else None
        async with self._lock:
            version = self._snapshot_version
            keys = tuple(
                key
                for key in self._market_feeds
                if normalized_base is None
                or key[1].split("-", 1)[0] == normalized_base
            )
            state = tuple(
                (
                    key,
                    self._connections[self._market_feeds[key]],
                    self._books.get(key),
                    self._instruments.get(key),
                    self._derivatives.get(key),
                    self._executable_books.get(key),
                )
                for key in keys
            )
        display_markets = tuple(
            self._build_view(
                key,
                connection,
                display_book,
                instrument,
                derivatives,
                executable_book,
                now=now,
            )
            for (
                key,
                connection,
                display_book,
                instrument,
                derivatives,
                executable_book,
            ) in state
        )
        display_markets = self._add_basis(display_markets, state, now=now)
        derivatives_by_key = {
            (market.venue, market.symbol, market.instrument_type): market.derivatives
            for market in display_markets
        }
        markets = tuple(
            self._build_executable_view(
                key,
                connection,
                executable_book,
                instrument,
                derivatives_by_key.get(key),
                now=now,
            )
            for (
                key,
                connection,
                _display_book,
                instrument,
                _derivatives,
                executable_book,
            ) in state
        )
        return ExecutableMarketSnapshot(
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
    def _build_executable_view(
        key: MarketKey,
        connection: MarketConnectionState,
        book: ExecutableOrderBook | None,
        instrument: InstrumentRules | None,
        derivatives: DerivativeMarketContext | None,
        *,
        now: datetime,
    ) -> ExecutableBookView:
        venue, symbol, instrument_type = key
        data_age_ms = (
            max(0, int((now - book.received_at).total_seconds() * 1000))
            if book is not None
            else None
        )
        status = connection.status
        if (
            status is MarketConnectionStatus.LIVE
            and data_age_ms is not None
            and data_age_ms > STALE_BOOK_AFTER_MS
        ):
            status = MarketConnectionStatus.STALE
            connection = connection.model_copy(update={"status": status})
        if status is not MarketConnectionStatus.LIVE:
            reason = f"FEED_{status.value}"
        elif book is None:
            reason = "EXECUTABLE_BOOK_UNAVAILABLE"
        elif instrument is None:
            reason = "INSTRUMENT_METADATA_UNAVAILABLE"
        else:
            reason = None
        derivative_age_ms = (
            max(0, int((now - derivatives.received_at).total_seconds() * 1000))
            if derivatives is not None
            else None
        )
        funding_age_ms = (
            max(
                0,
                int(
                    (now - derivatives.funding_captured_at).total_seconds()
                    * 1000
                ),
            )
            if derivatives is not None
            and derivatives.funding_captured_at is not None
            else None
        )
        return ExecutableBookView(
            venue=venue,
            symbol=symbol,
            instrument_type=instrument_type,
            connection=connection,
            book=book,
            instrument=instrument,
            derivatives=derivatives,
            book_data_age_ms=data_age_ms,
            derivative_data_age_ms=derivative_age_ms,
            derivative_data_stale=(
                derivative_age_ms > STALE_DERIVATIVE_CONTEXT_AFTER_MS
                if derivative_age_ms is not None
                else None
            ),
            funding_data_age_ms=funding_age_ms,
            funding_data_stale=(
                funding_age_ms > STALE_DERIVATIVE_CONTEXT_AFTER_MS
                if funding_age_ms is not None
                else None
            ),
            eligible=reason is None,
            exclusion_reason=reason,
            as_of=now,
        )

    async def derivative_history(
        self, venue: MarketVenue, symbol: str
    ) -> tuple[DerivativeMarketContext, ...]:
        async with self._lock:
            return tuple(
                self._derivative_history.get(
                    (venue, symbol, InstrumentType.PERPETUAL), ()
                )
            )

    async def connections(self) -> list[MarketConnectionState]:
        async with self._lock:
            return sorted(
                self._connections.values(), key=lambda connection: connection.feed_id
            )
