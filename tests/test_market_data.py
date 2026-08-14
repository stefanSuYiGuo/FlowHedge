import asyncio
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from backend.market.book import KrakenChecksumMismatch, KrakenOrderBookBuilder
from backend.market.kraken import (
    BOOK_DEPTH,
    CANONICAL_SYMBOL,
    KRAKEN_SPOT_WS_ENDPOINT,
    KrakenSpotMarketDataAdapter,
)
from backend.market.models import MarketConnectionStatus, MarketVenue
from backend.market.store import InMemoryMarketStateStore


FIXTURES = Path(__file__).parent / "fixtures"


def run(coroutine):
    return asyncio.run(coroutine)


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


async def registered_store() -> InMemoryMarketStateStore:
    store = InMemoryMarketStateStore()
    await store.register_venue(MarketVenue.KRAKEN, KRAKEN_SPOT_WS_ENDPOINT)
    return store


def test_official_kraken_checksum_snapshot_builds_normalized_book() -> None:
    message = load_fixture("kraken_book_snapshot.json")
    received_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    book = KrakenOrderBookBuilder(depth=BOOK_DEPTH).apply_snapshot(
        message["data"][0], received_at=received_at
    )

    assert book.venue is MarketVenue.KRAKEN
    assert book.symbol == CANONICAL_SYMBOL
    assert book.checksum == 3310070434
    assert book.best_bid == Decimal("45283.5")
    assert book.best_ask == Decimal("45285.2")
    assert book.mid_price == Decimal("45284.35")
    assert book.spread == Decimal("1.7")
    assert len(book.bids) == 10
    assert len(book.asks) == 10


def test_checksum_failure_does_not_replace_last_valid_book() -> None:
    payload = load_fixture("kraken_book_snapshot.json")["data"][0]
    builder = KrakenOrderBookBuilder(depth=BOOK_DEPTH)
    valid = builder.apply_snapshot(payload, received_at=datetime.now(timezone.utc))
    bad_update = {
        "symbol": "BTC/USD",
        "bids": [{"price": "45283.5", "qty": "0.20000000"}],
        "asks": [],
        "checksum": 1,
        "timestamp": "2023-10-04T16:25:13.000000Z",
    }

    with pytest.raises(KrakenChecksumMismatch):
        builder.apply_update(bad_update, received_at=datetime.now(timezone.utc))

    assert builder.current_book() == valid


def test_adapter_publishes_book_metadata_and_live_state() -> None:
    async def exercise():
        store = await registered_store()
        adapter = KrakenSpotMarketDataAdapter(store)
        now = datetime.now(timezone.utc)

        await adapter.handle_message(
            load_fixture("kraken_instrument_snapshot.json"), received_at=now
        )
        await adapter.handle_message(
            load_fixture("kraken_book_snapshot.json"), received_at=now
        )
        return await store.view(MarketVenue.KRAKEN, CANONICAL_SYMBOL)

    state = run(exercise())

    assert state.connection.status is MarketConnectionStatus.LIVE
    assert state.book is not None
    assert state.book.depth == BOOK_DEPTH
    assert state.instrument is not None
    assert state.instrument.price_increment == Decimal("0.1")
    assert state.instrument.quantity_min == Decimal("0.0001")


def test_in_memory_state_replaces_current_book_without_accumulating_history() -> None:
    async def exercise():
        store = await registered_store()
        builder = KrakenOrderBookBuilder(depth=BOOK_DEPTH)
        payload = load_fixture("kraken_book_snapshot.json")["data"][0]
        first = builder.apply_snapshot(
            payload, received_at=datetime.now(timezone.utc) - timedelta(seconds=1)
        )
        await store.replace_book(first)
        await store.replace_book(first.model_copy(update={"received_at": datetime.now(timezone.utc)}))
        return store, await store.view(MarketVenue.KRAKEN, CANONICAL_SYMBOL)

    store, state = run(exercise())

    assert len(store._books) == 1
    assert state.book_data_age_ms is not None
    assert state.book_data_age_ms < 1000
