import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from backend.domain.models import InstrumentType
from backend.market.book import normalized_books_from_levels
from backend.market.kraken_futures import (
    CANONICAL_SYMBOL as KRAKEN_CANONICAL_SYMBOL,
    KRAKEN_FUTURES_PRODUCT,
    derivative_context_from_kraken_ticker,
    instrument_rules_from_kraken_futures,
    parse_kraken_futures_timestamp,
)
from backend.market.models import (
    ContractStructure,
    DerivativeMarketContext,
    InstrumentRules,
    MarketConnectionStatus,
    MarketVenue,
)
from backend.market.okx import instrument_rules_from_okx
from backend.market.service import market_data_service
from backend.market.store import (
    DERIVATIVE_HISTORY_MAX_POINTS,
    InMemoryMarketStateStore,
)


NOW = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)


def run(coroutine):
    return asyncio.run(coroutine)


def test_initial_market_universe_has_six_spot_and_perpetual_candidates() -> None:
    expected = {
        (MarketVenue.KRAKEN, InstrumentType.SPOT),
        (MarketVenue.KRAKEN, InstrumentType.PERPETUAL),
        (MarketVenue.COINBASE, InstrumentType.SPOT),
        (MarketVenue.COINBASE, InstrumentType.PERPETUAL),
        (MarketVenue.OKX, InstrumentType.SPOT),
        (MarketVenue.OKX, InstrumentType.PERPETUAL),
    }
    assert {
        (venue, instrument_type)
        for venue, symbol, instrument_type in market_data_service._supported_markets
        if symbol == "BTC-USD"
    } == expected


def test_okx_contract_multiplier_is_dynamic_and_book_quantities_are_btc_equivalent() -> None:
    metadata = {
        "instId": "BTC-USDT-SWAP",
        "uly": "BTC-USDT",
        "ctType": "linear",
        "ctVal": "0.02",
        "ctMult": "3",
        "ctValCcy": "BTC",
        "settleCcy": "USDT",
        "tickSz": "0.1",
        "lotSz": "0.01",
        "minSz": "0.01",
        "state": "live",
    }
    rules = instrument_rules_from_okx(
        metadata, InstrumentType.PERPETUAL, received_at=NOW
    )
    display, executable = normalized_books_from_levels(
        rules=rules,
        bids=((Decimal("62999"), Decimal("10")),),
        asks=((Decimal("63001"), Decimal("12")),),
        exchange_timestamp=NOW,
        received_at=NOW,
    )

    assert rules.contract_multiplier == Decimal("0.06")
    assert rules.contract_value_currency == "BTC"
    assert rules.usd_conversion_assumption == "USDT_USD_1_TO_1_DEMO"
    assert executable.bids[0].source_quantity == Decimal("10")
    assert executable.bids[0].quantity_btc_equivalent == Decimal("0.60")
    assert display.bids[0].quantity == Decimal("0.60")


def test_kraken_inverse_contracts_normalize_at_each_level_price() -> None:
    metadata = {
        "symbol": KRAKEN_FUTURES_PRODUCT,
        "type": "futures_inverse",
        "tickSize": "0.5",
        "contractSize": "2",
        "tradeable": True,
        "contractValueTradePrecision": 0,
        "base": "BTC",
        "quote": "USD",
    }
    rules = instrument_rules_from_kraken_futures(metadata, received_at=NOW)
    _, executable = normalized_books_from_levels(
        rules=rules,
        bids=((Decimal("50000"), Decimal("25000")),),
        asks=((Decimal("100000"), Decimal("25000")),),
        exchange_timestamp=NOW,
        received_at=NOW,
    )

    assert rules.contract_structure is ContractStructure.INVERSE
    assert rules.contract_multiplier == Decimal("2")
    assert executable.bids[0].quantity_btc_equivalent == Decimal("1")
    assert executable.asks[0].quantity_btc_equivalent == Decimal("0.5")


def test_kraken_futures_timestamp_accepts_variable_fractional_precision() -> None:
    assert parse_kraken_futures_timestamp(
        "2026-08-14T19:26:12.5Z"
    ) == datetime(2026, 8, 14, 19, 26, 12, 500000, tzinfo=timezone.utc)


def test_executable_book_keeps_only_real_depth_up_to_200_and_display_is_25() -> None:
    rules = InstrumentRules(
        venue=MarketVenue.COINBASE,
        symbol="BTC-USD",
        venue_symbol="BTC-USD",
        instrument_type=InstrumentType.SPOT,
        base_asset="BTC",
        quote_asset="USD",
        price_increment=Decimal("1"),
        quantity_increment=Decimal("0.01"),
        quantity_min=Decimal("0.01"),
        price_precision=0,
        quantity_precision=2,
        status="ONLINE",
        received_at=NOW,
    )
    display, executable = normalized_books_from_levels(
        rules=rules,
        bids=(
            (Decimal("50000") - Decimal(index), Decimal("1"))
            for index in range(250)
        ),
        asks=(
            (Decimal("50001") + Decimal(index), Decimal("1"))
            for index in range(137)
        ),
        exchange_timestamp=NOW,
        received_at=NOW,
    )
    assert len(display.bids) == 25
    assert len(display.asks) == 25
    assert len(executable.bids) == 200
    assert len(executable.asks) == 137
    assert executable.asks[-1].price == Decimal("50137")


def test_kraken_context_preserves_public_derivatives_fields_and_inverse_oi() -> None:
    rules = instrument_rules_from_kraken_futures(
        {
            "symbol": KRAKEN_FUTURES_PRODUCT,
            "type": "futures_inverse",
            "tickSize": "0.5",
            "contractSize": "1",
            "tradeable": True,
            "contractValueTradePrecision": 0,
            "base": "BTC",
            "quote": "USD",
        },
        received_at=NOW,
    )
    context = derivative_context_from_kraken_ticker(
        {
            "symbol": KRAKEN_FUTURES_PRODUCT,
            "markPrice": "62500",
            "indexPrice": "62490",
            "fundingRate": "-0.000001",
            "fundingRatePrediction": "0.000002",
            "openInterest": "3125000",
        },
        rules,
        captured_at=NOW,
    )
    assert context.mark_price == Decimal("62500")
    assert context.index_price == Decimal("62490")
    assert context.open_interest_unit == "CONTRACTS"
    assert context.open_interest_btc_equivalent == Decimal("50")
    assert context.open_interest_usd == Decimal("3125000")
    assert context.next_funding_time is None


def test_derivatives_history_is_bounded_for_future_delta_oi_features() -> None:
    async def exercise():
        store = InMemoryMarketStateStore()
        for index in range(DERIVATIVE_HISTORY_MAX_POINTS + 5):
            captured_at = NOW + timedelta(seconds=index * 5)
            await store.replace_derivative_context(
                DerivativeMarketContext(
                    venue=MarketVenue.OKX,
                    symbol="BTC-USD",
                    venue_symbol="BTC-USDT-SWAP",
                    open_interest=Decimal(index),
                    open_interest_unit="CONTRACTS",
                    received_at=captured_at,
                    open_interest_captured_at=captured_at,
                    source="TEST",
                )
            )
        return await store.derivative_history(MarketVenue.OKX, "BTC-USD")

    history = run(exercise())
    assert len(history) == DERIVATIVE_HISTORY_MAX_POINTS
    assert history[0].open_interest == Decimal("5")
    assert history[-1].open_interest == Decimal(DERIVATIVE_HISTORY_MAX_POINTS + 4)


def test_stale_market_is_ineligible_without_affecting_other_registered_market() -> None:
    async def exercise():
        store = InMemoryMarketStateStore()
        markets = (
            ("BTC-USD", InstrumentType.SPOT),
            ("BTC-USD", InstrumentType.PERPETUAL),
        )
        await store.register_feed("okx", MarketVenue.OKX, "public", markets)
        await store.update_connection("okx", status=MarketConnectionStatus.LIVE)
        spot_rules = InstrumentRules(
            venue=MarketVenue.OKX,
            symbol="BTC-USD",
            venue_symbol="BTC-USDT",
            instrument_type=InstrumentType.SPOT,
            base_asset="BTC",
            quote_asset="USDT",
            price_increment=Decimal("0.1"),
            quantity_increment=Decimal("0.0001"),
            quantity_min=Decimal("0.0001"),
            price_precision=1,
            quantity_precision=4,
            status="LIVE",
            received_at=NOW,
        )
        stale_display, stale_executable = normalized_books_from_levels(
            rules=spot_rules,
            bids=((Decimal("62999"), Decimal("1")),),
            asks=((Decimal("63001"), Decimal("1")),),
            exchange_timestamp=NOW,
            received_at=datetime.now(timezone.utc) - timedelta(seconds=10),
        )
        await store.replace_books(stale_display, stale_executable)
        return await store.snapshot("BTC")

    snapshot = run(exercise())
    spot = next(m for m in snapshot.markets if m.instrument_type is InstrumentType.SPOT)
    perp = next(m for m in snapshot.markets if m.instrument_type is InstrumentType.PERPETUAL)
    assert spot.eligible is False and spot.exclusion_reason == "FEED_STALE"
    assert perp.eligible is False and perp.exclusion_reason == "BOOK_UNAVAILABLE"
