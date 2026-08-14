"""Public Kraken inverse BTC perpetual market-data adapter."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from decimal import Decimal
from typing import Any, Awaitable, Callable
from urllib.request import Request, urlopen

from ..domain.models import InstrumentType
from .base import MarketDataAdapter
from .book import decimal_value, normalized_books_from_levels
from .coinbase import decimal_precision
from .models import (
    ContractStructure,
    DerivativeMarketContext,
    InstrumentRules,
    MarketConnectionStatus,
    MarketVenue,
)
from .store import InMemoryMarketStateStore, utc_now


logger = logging.getLogger(__name__)

KRAKEN_FUTURES_REST_ENDPOINT = "https://futures.kraken.com/derivatives/api/v3"
KRAKEN_FUTURES_INSTRUMENTS_ENDPOINT = f"{KRAKEN_FUTURES_REST_ENDPOINT}/instruments"
KRAKEN_FUTURES_BOOK_ENDPOINT = (
    f"{KRAKEN_FUTURES_REST_ENDPOINT}/orderbook?symbol=PI_XBTUSD"
)
KRAKEN_FUTURES_TICKER_ENDPOINT = (
    f"{KRAKEN_FUTURES_REST_ENDPOINT}/tickers?symbol=PI_XBTUSD"
)
KRAKEN_FUTURES_PRODUCT = "PI_XBTUSD"
CANONICAL_SYMBOL = "BTC-USD"
POLL_INTERVAL_SECONDS = 1
RECONNECT_BACKOFF_SECONDS = (1, 2, 5, 10)


def parse_kraken_futures_timestamp(value: str) -> datetime:
    if value.endswith("Z"):
        body = value[:-1]
        if "." in body:
            whole_seconds, fractional_seconds = body.split(".", 1)
            microseconds = fractional_seconds[:6].ljust(6, "0")
            body = f"{whole_seconds}.{microseconds}"
        return datetime.fromisoformat(f"{body}+00:00")
    return datetime.fromisoformat(value)


async def fetch_public_json(url: str) -> dict[str, Any]:
    def fetch() -> dict[str, Any]:
        request = Request(
            url,
            headers={
                "Accept": "application/json",
                "Cache-Control": "no-cache",
                "User-Agent": "FlowHedge/0.1 public-market-data",
            },
        )
        with urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"), parse_float=Decimal)

    return await asyncio.to_thread(fetch)


def instrument_rules_from_kraken_futures(
    instrument: dict[str, Any], *, received_at: datetime
) -> InstrumentRules:
    contract_type = str(instrument["type"])
    inverse = "inverse" in contract_type
    contract_size = decimal_value(instrument["contractSize"])
    quantity_precision = int(instrument.get("contractValueTradePrecision", 0))
    quantity_increment = Decimal("1").scaleb(-quantity_precision)
    tick_size = decimal_value(instrument["tickSize"])
    base_asset = "BTC" if str(instrument.get("base")) == "XBT" else str(
        instrument.get("base", "BTC")
    )
    quote_asset = str(instrument.get("quote", "USD"))
    return InstrumentRules(
        venue=MarketVenue.KRAKEN,
        symbol=CANONICAL_SYMBOL,
        venue_symbol=KRAKEN_FUTURES_PRODUCT,
        instrument_type=InstrumentType.PERPETUAL,
        base_asset=base_asset,
        quote_asset=quote_asset,
        price_increment=tick_size,
        quantity_increment=quantity_increment,
        quantity_min=quantity_increment,
        price_precision=decimal_precision(tick_size),
        quantity_precision=quantity_precision,
        status="ONLINE" if instrument.get("tradeable") else "OFFLINE",
        contract_structure=(
            ContractStructure.INVERSE if inverse else ContractStructure.LINEAR
        ),
        contract_multiplier=contract_size,
        contract_value_currency=quote_asset if inverse else base_asset,
        native_quantity_unit="CONTRACTS",
        settlement_asset=base_asset if inverse else quote_asset,
        received_at=received_at,
    )


def derivative_context_from_kraken_ticker(
    ticker: dict[str, Any],
    rules: InstrumentRules,
    *,
    captured_at: datetime,
) -> DerivativeMarketContext:
    mark = decimal_value(ticker["markPrice"]) if ticker.get("markPrice") else None
    index = decimal_value(ticker["indexPrice"]) if ticker.get("indexPrice") else None
    open_interest = (
        decimal_value(ticker["openInterest"]) if ticker.get("openInterest") else None
    )
    open_interest_btc = None
    open_interest_usd = None
    if open_interest is not None and mark is not None:
        open_interest_btc = rules.quantity_to_btc_equivalent(
            open_interest, price=mark
        )
        open_interest_usd = open_interest_btc * mark
    return DerivativeMarketContext(
        venue=MarketVenue.KRAKEN,
        symbol=CANONICAL_SYMBOL,
        venue_symbol=KRAKEN_FUTURES_PRODUCT,
        mark_price=mark,
        index_price=index,
        current_funding_rate=(
            decimal_value(ticker["fundingRate"]) if ticker.get("fundingRate") else None
        ),
        predicted_funding_rate=(
            decimal_value(ticker["fundingRatePrediction"])
            if ticker.get("fundingRatePrediction")
            else None
        ),
        next_funding_time=None,
        funding_interval_seconds=None,
        open_interest=open_interest,
        open_interest_unit="CONTRACTS" if open_interest is not None else None,
        open_interest_btc_equivalent=open_interest_btc,
        open_interest_usd=open_interest_usd,
        mark_price_captured_at=captured_at if mark is not None else None,
        index_price_captured_at=captured_at if index is not None else None,
        funding_captured_at=captured_at if ticker.get("fundingRate") else None,
        open_interest_captured_at=captured_at if open_interest is not None else None,
        received_at=captured_at,
        source="KRAKEN_FUTURES_PUBLIC_TICKER",
    )


JsonFetcher = Callable[[str], Awaitable[dict[str, Any]]]


class KrakenFuturesMarketDataAdapter(MarketDataAdapter):
    feed_id = "kraken-public-perpetual"
    venue = MarketVenue.KRAKEN
    endpoint = KRAKEN_FUTURES_REST_ENDPOINT
    markets = ((CANONICAL_SYMBOL, InstrumentType.PERPETUAL),)

    def __init__(
        self,
        store: InMemoryMarketStateStore,
        *,
        fetcher: JsonFetcher = fetch_public_json,
    ) -> None:
        self.store = store
        self.fetcher = fetcher
        self._rules: InstrumentRules | None = None

    async def run(self) -> None:
        reconnect_attempt = 0
        try:
            while True:
                status = (
                    MarketConnectionStatus.CONNECTING
                    if reconnect_attempt == 0
                    else MarketConnectionStatus.RECONNECTING
                )
                await self.store.update_connection(
                    self.feed_id,
                    status=status,
                    reconnect_attempt=reconnect_attempt,
                    clear_error=reconnect_attempt == 0,
                )
                try:
                    await self._refresh_instrument()
                    await self.store.update_connection(
                        self.feed_id,
                        status=status,
                        connected_at=utc_now(),
                        clear_error=True,
                    )
                    reconnect_attempt = 0
                    while True:
                        await self.poll_once()
                        await asyncio.sleep(POLL_INTERVAL_SECONDS)
                except (OSError, ValueError, KeyError, TypeError) as error:
                    reconnect_attempt += 1
                    await self.store.update_connection(
                        self.feed_id,
                        status=MarketConnectionStatus.DISCONNECTED,
                        last_error=str(error),
                        reconnect_attempt=reconnect_attempt,
                    )
                    logger.warning("Kraken perpetual feed disconnected: %s", error)
                    delay = RECONNECT_BACKOFF_SECONDS[
                        min(reconnect_attempt - 1, len(RECONNECT_BACKOFF_SECONDS) - 1)
                    ]
                    await asyncio.sleep(delay)
        finally:
            await self.store.update_connection(
                self.feed_id,
                status=MarketConnectionStatus.DISCONNECTED,
                last_error="market data service stopped",
            )

    async def _refresh_instrument(self) -> None:
        response = await self.fetcher(KRAKEN_FUTURES_INSTRUMENTS_ENDPOINT)
        instrument = next(
            item
            for item in response.get("instruments", ())
            if item.get("symbol") == KRAKEN_FUTURES_PRODUCT
        )
        self._rules = instrument_rules_from_kraken_futures(
            instrument, received_at=utc_now()
        )
        await self.store.replace_instrument(self._rules)

    async def poll_once(self) -> None:
        if self._rules is None:
            await self._refresh_instrument()
        book_result, ticker_result = await asyncio.gather(
            self.fetcher(KRAKEN_FUTURES_BOOK_ENDPOINT),
            self.fetcher(KRAKEN_FUTURES_TICKER_ENDPOINT),
            return_exceptions=True,
        )
        received_at = utc_now()
        if isinstance(book_result, Exception):
            raise book_result
        order_book = book_result["orderBook"]
        exchange_timestamp = parse_kraken_futures_timestamp(book_result["serverTime"])
        display, executable = normalized_books_from_levels(
            rules=self._rules,
            bids=((level[0], level[1]) for level in order_book["bids"]),
            asks=((level[0], level[1]) for level in order_book["asks"]),
            exchange_timestamp=exchange_timestamp,
            received_at=received_at,
        )
        await self.store.replace_books(display, executable)
        if not isinstance(ticker_result, Exception):
            ticker = next(
                item
                for item in ticker_result.get("tickers", ())
                if item.get("symbol") == KRAKEN_FUTURES_PRODUCT
            )
            await self.store.replace_derivative_context(
                derivative_context_from_kraken_ticker(
                    ticker, self._rules, captured_at=received_at
                )
            )
        await self.store.update_connection(
            self.feed_id,
            status=MarketConnectionStatus.LIVE,
            last_message_at=received_at,
            last_book_update_at=received_at,
            reconnect_attempt=0,
            clear_error=True,
        )
