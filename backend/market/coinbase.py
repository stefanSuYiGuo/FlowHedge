"""Coinbase Advanced Trade public Spot and Perpetual L2 market data."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Awaitable, Callable
from urllib.parse import quote
from urllib.request import Request, urlopen

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from ..domain.models import InstrumentType
from .base import MarketDataAdapter
from .book import decimal_value
from .models import (
    ContractStructure,
    InstrumentRules,
    MarketConnectionStatus,
    MarketLevel,
    MarketVenue,
    NormalizedOrderBook,
)
from .store import InMemoryMarketStateStore, utc_now


logger = logging.getLogger(__name__)

COINBASE_MARKET_WS_ENDPOINT = "wss://advanced-trade-ws.coinbase.com"
COINBASE_PUBLIC_PRODUCT_ENDPOINT = (
    "https://api.coinbase.com/api/v3/brokerage/market/products"
)
COINBASE_FEED_ID = "coinbase-public-spot-perp"
CANONICAL_SYMBOL = "BTC-USD"
COINBASE_SPOT_PRODUCT = "BTC-USD"
COINBASE_PERP_PRODUCT = "BTC-PERP-INTX"
BOOK_DEPTH = 25
RETAINED_LEVELS_PER_SIDE = 2_000
MESSAGE_TIMEOUT_SECONDS = 5
RECONNECT_BACKOFF_SECONDS = (1, 2, 5, 10)
PUBLIC_MESSAGE_MAX_BYTES = 8 * 1024 * 1024
STABLECOIN_USD_ASSUMPTION = "USDC_USD_1_TO_1_DEMO"


@dataclass(frozen=True)
class CoinbaseProductSpec:
    venue_symbol: str
    canonical_symbol: str
    instrument_type: InstrumentType


PRODUCT_SPECS = {
    COINBASE_SPOT_PRODUCT: CoinbaseProductSpec(
        venue_symbol=COINBASE_SPOT_PRODUCT,
        canonical_symbol=CANONICAL_SYMBOL,
        instrument_type=InstrumentType.SPOT,
    ),
    COINBASE_PERP_PRODUCT: CoinbaseProductSpec(
        venue_symbol=COINBASE_PERP_PRODUCT,
        canonical_symbol=CANONICAL_SYMBOL,
        instrument_type=InstrumentType.PERPETUAL,
    ),
}


class CoinbaseMessageTimeout(TimeoutError):
    """Raised when the public feed stops delivering L2 data and heartbeats."""


def parse_coinbase_timestamp(value: str) -> datetime:
    if value.endswith("Z"):
        body = value[:-1]
        if "." in body:
            whole_seconds, fractional_seconds = body.split(".", 1)
            microseconds = fractional_seconds[:6].ljust(6, "0")
            body = f"{whole_seconds}.{microseconds}"
        return datetime.fromisoformat(f"{body}+00:00")
    return datetime.fromisoformat(value)


def decimal_precision(value: Decimal) -> int:
    return max(0, -value.normalize().as_tuple().exponent)


async def fetch_public_product(product_id: str) -> dict[str, Any]:
    """Load public instrument metadata once without account credentials."""

    def fetch() -> dict[str, Any]:
        url = f"{COINBASE_PUBLIC_PRODUCT_ENDPOINT}/{quote(product_id, safe='')}"
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


def instrument_rules_from_product(
    product: dict[str, Any],
    spec: CoinbaseProductSpec,
    *,
    received_at: datetime,
) -> InstrumentRules:
    price_increment = decimal_value(
        product.get("price_increment") or product["quote_increment"]
    )
    quantity_increment = decimal_value(product["base_increment"])
    future_details = product.get("future_product_details") or {}
    quote_asset = str(product["quote_currency_id"])
    base_asset = str(
        product.get("base_currency_id")
        or future_details.get("contract_code")
        or CANONICAL_SYMBOL.split("-", 1)[0]
    )
    is_perpetual = spec.instrument_type is InstrumentType.PERPETUAL
    contract_multiplier = decimal_value(
        future_details.get("contract_size") or "1"
    )
    return InstrumentRules(
        venue=MarketVenue.COINBASE,
        symbol=spec.canonical_symbol,
        venue_symbol=spec.venue_symbol,
        instrument_type=spec.instrument_type,
        base_asset=base_asset,
        quote_asset=quote_asset,
        price_increment=price_increment,
        quantity_increment=quantity_increment,
        quantity_min=decimal_value(product["base_min_size"]),
        price_precision=decimal_precision(price_increment),
        quantity_precision=decimal_precision(quantity_increment),
        status=str(product["status"]),
        contract_structure=(
            ContractStructure.LINEAR if is_perpetual else ContractStructure.SPOT
        ),
        contract_multiplier=contract_multiplier,
        settlement_asset=quote_asset,
        usd_conversion_rate=Decimal("1"),
        usd_conversion_assumption=(
            STABLECOIN_USD_ASSUMPTION if quote_asset == "USDC" else None
        ),
        received_at=received_at,
    )


class CoinbaseOrderBookBuilder:
    """Maintain a bounded local buffer and publish a normalized depth-25 book."""

    def __init__(
        self,
        spec: CoinbaseProductSpec,
        *,
        depth: int = BOOK_DEPTH,
        retained_levels: int = RETAINED_LEVELS_PER_SIDE,
    ) -> None:
        self.spec = spec
        self.depth = depth
        self.retained_levels = retained_levels
        self._bids: dict[Decimal, Decimal] = {}
        self._asks: dict[Decimal, Decimal] = {}
        self._exchange_timestamp: datetime | None = None
        self._received_at: datetime | None = None
        self._sequence: int | None = None

    def apply_event(
        self,
        event: dict[str, Any],
        *,
        sequence: int,
        received_at: datetime,
    ) -> NormalizedOrderBook:
        event_type = event.get("type")
        if event_type == "snapshot":
            bids: dict[Decimal, Decimal] = {}
            asks: dict[Decimal, Decimal] = {}
        elif event_type == "update":
            if self._sequence is None:
                raise ValueError("cannot apply a Coinbase update before a snapshot")
            bids = dict(self._bids)
            asks = dict(self._asks)
        else:
            raise ValueError(f"unsupported Coinbase L2 event type: {event_type}")

        latest_event_time: datetime | None = None
        for update in event.get("updates", ()):
            side = str(update["side"]).lower()
            target = bids if side in {"bid", "buy"} else asks
            if side not in {"bid", "buy", "ask", "offer", "sell"}:
                raise ValueError(f"unsupported Coinbase book side: {side}")
            price = decimal_value(update["price_level"])
            quantity = decimal_value(update["new_quantity"])
            target.pop(price, None)
            if quantity > 0:
                target[price] = quantity
            latest_event_time = parse_coinbase_timestamp(update["event_time"])

        self._bids = self._truncate(bids, reverse=True)
        self._asks = self._truncate(asks, reverse=False)
        if not self._bids or not self._asks:
            raise ValueError("Coinbase order book requires at least one bid and ask")
        self._exchange_timestamp = latest_event_time or received_at
        self._received_at = received_at
        self._sequence = sequence
        return self.current_book()

    def current_book(self) -> NormalizedOrderBook:
        if (
            self._exchange_timestamp is None
            or self._received_at is None
            or self._sequence is None
        ):
            raise ValueError("Coinbase order book has not received a valid snapshot")
        bids = self._sorted(self._bids, reverse=True)[: self.depth]
        asks = self._sorted(self._asks, reverse=False)[: self.depth]
        best_bid = bids[0][0]
        best_ask = asks[0][0]
        mid_price = (best_bid + best_ask) / Decimal("2")
        spread = best_ask - best_bid
        return NormalizedOrderBook(
            venue=MarketVenue.COINBASE,
            symbol=self.spec.canonical_symbol,
            venue_symbol=self.spec.venue_symbol,
            instrument_type=self.spec.instrument_type,
            depth=self.depth,
            bids=tuple(MarketLevel(price=price, quantity=quantity) for price, quantity in bids),
            asks=tuple(MarketLevel(price=price, quantity=quantity) for price, quantity in asks),
            best_bid=best_bid,
            best_ask=best_ask,
            mid_price=mid_price,
            spread=spread,
            spread_bps=(spread / mid_price) * Decimal("10000"),
            exchange_timestamp=self._exchange_timestamp,
            received_at=self._received_at,
            checksum=None,
            source_sequence=self._sequence,
        )

    def _truncate(
        self, side: dict[Decimal, Decimal], *, reverse: bool
    ) -> dict[Decimal, Decimal]:
        return dict(self._sorted(side, reverse=reverse)[: self.retained_levels])

    @staticmethod
    def _sorted(
        side: dict[Decimal, Decimal], *, reverse: bool
    ) -> list[tuple[Decimal, Decimal]]:
        return sorted(side.items(), key=lambda level: level[0], reverse=reverse)


ProductFetcher = Callable[[str], Awaitable[dict[str, Any]]]


class CoinbaseMarketDataAdapter(MarketDataAdapter):
    feed_id = COINBASE_FEED_ID
    venue = MarketVenue.COINBASE
    endpoint = COINBASE_MARKET_WS_ENDPOINT
    markets = (
        (CANONICAL_SYMBOL, InstrumentType.SPOT),
        (CANONICAL_SYMBOL, InstrumentType.PERPETUAL),
    )

    def __init__(
        self,
        store: InMemoryMarketStateStore,
        *,
        product_fetcher: ProductFetcher = fetch_public_product,
    ) -> None:
        self.store = store
        self.product_fetcher = product_fetcher
        self._builders = self._new_builders()

    def _new_builders(self) -> dict[str, CoinbaseOrderBookBuilder]:
        return {
            product_id: CoinbaseOrderBookBuilder(spec)
            for product_id, spec in PRODUCT_SPECS.items()
        }

    async def run(self) -> None:
        try:
            await self._run_forever()
        finally:
            await self.store.update_connection(
                self.feed_id,
                status=MarketConnectionStatus.DISCONNECTED,
                last_error="market data service stopped",
            )

    async def _run_forever(self) -> None:
        reconnect_attempt = 0
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
                async with connect(
                    self.endpoint,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                    max_queue=128,
                    max_size=PUBLIC_MESSAGE_MAX_BYTES,
                ) as websocket:
                    connected_at = utc_now()
                    self._builders = self._new_builders()
                    for _, spec in PRODUCT_SPECS.items():
                        await self.store.clear_book(
                            self.venue,
                            spec.canonical_symbol,
                            spec.instrument_type,
                        )
                    await self.store.update_connection(
                        self.feed_id,
                        status=status,
                        connected_at=connected_at,
                        reconnect_attempt=reconnect_attempt,
                        clear_error=True,
                    )
                    await self._refresh_product_metadata()
                    await self._subscribe(websocket)
                    reconnect_attempt = 0
                    logger.info("Coinbase public Spot/Perp market data connected")
                    await self._consume(websocket)
            except CoinbaseMessageTimeout as error:
                reconnect_attempt += 1
                await self.store.update_connection(
                    self.feed_id,
                    status=MarketConnectionStatus.STALE,
                    last_error=str(error),
                    reconnect_attempt=reconnect_attempt,
                )
            except (ConnectionClosed, OSError, ValueError, KeyError, TypeError) as error:
                reconnect_attempt += 1
                await self.store.update_connection(
                    self.feed_id,
                    status=MarketConnectionStatus.DISCONNECTED,
                    last_error=str(error),
                    reconnect_attempt=reconnect_attempt,
                )
                logger.warning("Coinbase market data disconnected: %s", error)

            delay_index = min(
                max(reconnect_attempt - 1, 0), len(RECONNECT_BACKOFF_SECONDS) - 1
            )
            await asyncio.sleep(RECONNECT_BACKOFF_SECONDS[delay_index])

    async def _refresh_product_metadata(self) -> None:
        for product_id, spec in PRODUCT_SPECS.items():
            try:
                product = await self.product_fetcher(product_id)
                rules = instrument_rules_from_product(
                    product, spec, received_at=utc_now()
                )
                await self.store.replace_instrument(rules)
            except Exception as error:
                logger.warning(
                    "Coinbase public metadata unavailable for %s: %s",
                    product_id,
                    error,
                )

    async def _subscribe(self, websocket: ClientConnection) -> None:
        product_ids = list(PRODUCT_SPECS)
        await websocket.send(
            json.dumps(
                {
                    "type": "subscribe",
                    "product_ids": product_ids,
                    "channel": "level2",
                }
            )
        )
        await websocket.send(
            json.dumps({"type": "subscribe", "channel": "heartbeats"})
        )

    async def _consume(self, websocket: ClientConnection) -> None:
        while True:
            try:
                raw_message = await asyncio.wait_for(
                    websocket.recv(), timeout=MESSAGE_TIMEOUT_SECONDS
                )
            except asyncio.TimeoutError as error:
                raise CoinbaseMessageTimeout(
                    "no Coinbase L2 update or heartbeat received for 5 seconds"
                ) from error
            received_at = utc_now()
            await self.store.update_connection(
                self.feed_id, last_message_at=received_at
            )
            message = json.loads(raw_message, parse_float=Decimal)
            await self.handle_message(message, received_at=received_at)

    async def handle_message(
        self, message: dict[str, Any], *, received_at: datetime
    ) -> None:
        """Handle one decoded public message; exposed for deterministic tests."""

        channel = message.get("channel")
        if channel in {"heartbeats", "subscriptions"}:
            return
        if channel == "l2_data":
            sequence = int(message.get("sequence_num", 0))
            published = False
            for event in message.get("events", ()):
                product_id = event.get("product_id")
                builder = self._builders.get(product_id)
                if builder is None:
                    continue
                book = builder.apply_event(
                    event, sequence=sequence, received_at=received_at
                )
                await self.store.replace_book(book)
                published = True
            if published:
                await self.store.update_connection(
                    self.feed_id,
                    status=MarketConnectionStatus.LIVE,
                    last_book_update_at=received_at,
                    reconnect_attempt=0,
                    clear_error=True,
                )
            return
        if channel == "error" or message.get("type") == "error":
            raise ValueError(message.get("message", "Coinbase subscription failed"))
