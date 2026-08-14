import asyncio
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from backend.market.book import KrakenChecksumMismatch, KrakenOrderBookBuilder
from backend.market.coinbase import (
    BOOK_DEPTH as COINBASE_BOOK_DEPTH,
    CANONICAL_SYMBOL as COINBASE_CANONICAL_SYMBOL,
    COINBASE_FEED_ID,
    COINBASE_MARKET_WS_ENDPOINT,
    COINBASE_PERP_PRODUCT,
    CoinbaseMarketDataAdapter,
    PRODUCT_SPECS,
    STABLECOIN_USD_ASSUMPTION,
    instrument_rules_from_product,
    parse_coinbase_timestamp,
)
from backend.market.kraken import (
    BOOK_DEPTH,
    CANONICAL_SYMBOL,
    KrakenSpotMarketDataAdapter,
    KRAKEN_SPOT_WS_ENDPOINT,
)
from backend.domain.models import InstrumentType
from backend.market.models import (
    ContractStructure,
    MarketConnectionStatus,
    MarketVenue,
)
from backend.market.store import InMemoryMarketStateStore


FIXTURES = Path(__file__).parent / "fixtures"


def run(coroutine):
    return asyncio.run(coroutine)


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


async def registered_store() -> InMemoryMarketStateStore:
    store = InMemoryMarketStateStore()
    await store.register_feed(
        KrakenSpotMarketDataAdapter.feed_id,
        MarketVenue.KRAKEN,
        KRAKEN_SPOT_WS_ENDPOINT,
        ((CANONICAL_SYMBOL, InstrumentType.SPOT),),
    )
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


def test_coinbase_public_l2_normalizes_spot_and_perp_without_identity_collision() -> None:
    async def exercise():
        store = InMemoryMarketStateStore()
        await store.register_feed(
            COINBASE_FEED_ID,
            MarketVenue.COINBASE,
            COINBASE_MARKET_WS_ENDPOINT,
            CoinbaseMarketDataAdapter.markets,
        )
        adapter = CoinbaseMarketDataAdapter(store)
        now = datetime.now(timezone.utc)
        await adapter.handle_message(
            load_fixture("coinbase_l2_snapshot.json"), received_at=now
        )
        spot = await store.view(
            MarketVenue.COINBASE,
            COINBASE_CANONICAL_SYMBOL,
            InstrumentType.SPOT,
        )
        perp = await store.view(
            MarketVenue.COINBASE,
            COINBASE_CANONICAL_SYMBOL,
            InstrumentType.PERPETUAL,
        )
        return store, spot, perp

    store, spot, perp = run(exercise())

    assert len(store._books) == 2
    assert spot.eligible is True
    assert perp.eligible is True
    assert spot.book is not None and perp.book is not None
    assert spot.book.venue_symbol == "BTC-USD"
    assert spot.book.best_bid == Decimal("62920.84")
    assert spot.book.best_ask == Decimal("62920.85")
    assert perp.book.venue_symbol == "BTC-PERP-INTX"
    assert perp.book.instrument_type is InstrumentType.PERPETUAL
    assert perp.book.best_bid == Decimal("62935.4")
    assert perp.book.best_ask == Decimal("62935.5")
    assert perp.book.depth == COINBASE_BOOK_DEPTH
    assert perp.book.checksum is None
    assert perp.book.source_sequence == 17


def test_coinbase_perp_metadata_preserves_contract_and_usdc_assumption() -> None:
    products = load_fixture("coinbase_products.json")
    rules = instrument_rules_from_product(
        products[COINBASE_PERP_PRODUCT],
        PRODUCT_SPECS[COINBASE_PERP_PRODUCT],
        received_at=datetime.now(timezone.utc),
    )

    assert rules.instrument_type is InstrumentType.PERPETUAL
    assert rules.base_asset == "BTC"
    assert rules.quote_asset == "USDC"
    assert rules.settlement_asset == "USDC"
    assert rules.contract_structure is ContractStructure.LINEAR
    assert rules.contract_multiplier == Decimal("1")
    assert rules.price_increment == Decimal("0.1")
    assert rules.quantity_increment == Decimal("0.0001")
    assert rules.usd_conversion_rate == Decimal("1")
    assert rules.usd_conversion_assumption == STABLECOIN_USD_ASSUMPTION
    assert not hasattr(rules, "funding_rate")


def test_coinbase_timestamp_accepts_variable_subsecond_precision() -> None:
    assert parse_coinbase_timestamp(
        "2026-08-14T08:14:43.1269Z"
    ) == datetime(2026, 8, 14, 8, 14, 43, 126900, tzinfo=timezone.utc)


def test_unified_snapshot_is_atomic_and_marks_stale_books_ineligible() -> None:
    async def exercise():
        store = InMemoryMarketStateStore()
        await store.register_feed(
            COINBASE_FEED_ID,
            MarketVenue.COINBASE,
            COINBASE_MARKET_WS_ENDPOINT,
            CoinbaseMarketDataAdapter.markets,
        )
        adapter = CoinbaseMarketDataAdapter(store)
        stale_received_at = datetime.now(timezone.utc) - timedelta(seconds=10)
        await adapter.handle_message(
            load_fixture("coinbase_l2_snapshot.json"),
            received_at=stale_received_at,
        )
        return await store.snapshot("BTC")

    snapshot = run(exercise())

    assert snapshot.snapshot_version > 0
    assert snapshot.base_asset == "BTC"
    assert len(snapshot.markets) == 2
    assert {
        (market.venue, market.instrument_type) for market in snapshot.markets
    } == {
        (MarketVenue.COINBASE, InstrumentType.SPOT),
        (MarketVenue.COINBASE, InstrumentType.PERPETUAL),
    }
    assert all(market.eligible is False for market in snapshot.markets)
    assert all(
        market.connection.status is MarketConnectionStatus.STALE
        for market in snapshot.markets
    )
    assert all(market.exclusion_reason == "FEED_STALE" for market in snapshot.markets)


def test_coinbase_channel_sequence_is_retained_without_assuming_contiguity() -> None:
    async def exercise():
        store = InMemoryMarketStateStore()
        await store.register_feed(
            COINBASE_FEED_ID,
            MarketVenue.COINBASE,
            COINBASE_MARKET_WS_ENDPOINT,
            CoinbaseMarketDataAdapter.markets,
        )
        adapter = CoinbaseMarketDataAdapter(store)
        now = datetime.now(timezone.utc)
        snapshot_message = load_fixture("coinbase_l2_snapshot.json")
        await adapter.handle_message(snapshot_message, received_at=now)
        gap_message = {
            "channel": "l2_data",
            "sequence_num": snapshot_message["sequence_num"] + 2,
            "events": [
                {
                    "type": "update",
                    "product_id": COINBASE_PERP_PRODUCT,
                    "updates": [
                        {
                            "side": "bid",
                            "event_time": "2026-08-14T07:55:16.1000Z",
                            "price_level": "62935.4",
                            "new_quantity": "9.0000",
                        }
                    ],
                }
            ],
        }
        await adapter.handle_message(gap_message, received_at=now)
        after = await store.view(
            MarketVenue.COINBASE,
            COINBASE_CANONICAL_SYMBOL,
            InstrumentType.PERPETUAL,
        )
        return after.book

    after = run(exercise())
    assert after is not None
    assert after.source_sequence == 19
    assert after.bids[0].quantity == Decimal("9.0000")
