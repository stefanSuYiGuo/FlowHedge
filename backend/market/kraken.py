"""Kraken Spot WebSocket v2 adapter for public BTC/USD market data."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from decimal import Decimal
from typing import Any

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from ..domain.models import InstrumentType
from .base import MarketDataAdapter
from .book import KrakenOrderBookBuilder, decimal_value
from .models import (
    InstrumentRules,
    MarketConnectionStatus,
    MarketVenue,
)
from .store import InMemoryMarketStateStore, utc_now


logger = logging.getLogger(__name__)

KRAKEN_SPOT_WS_ENDPOINT = "wss://ws.kraken.com/v2"
KRAKEN_VENUE_SYMBOL = "BTC/USD"
CANONICAL_SYMBOL = "BTC-USD"
BOOK_DEPTH = 25
MESSAGE_TIMEOUT_SECONDS = 3
RECONNECT_BACKOFF_SECONDS = (1, 2, 5, 10)


class KrakenMessageTimeout(TimeoutError):
    """Raised after approximately three missed Kraken heartbeat intervals."""


class KrakenSpotMarketDataAdapter(MarketDataAdapter):
    venue = MarketVenue.KRAKEN
    endpoint = KRAKEN_SPOT_WS_ENDPOINT
    symbols = (CANONICAL_SYMBOL,)

    def __init__(self, store: InMemoryMarketStateStore) -> None:
        self.store = store
        self._book_builder = KrakenOrderBookBuilder(depth=BOOK_DEPTH)

    async def run(self) -> None:
        try:
            await self._run_forever()
        finally:
            await self.store.update_connection(
                self.venue,
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
                self.venue,
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
                ) as websocket:
                    connected_at = utc_now()
                    self._book_builder = KrakenOrderBookBuilder(depth=BOOK_DEPTH)
                    await self.store.clear_book(self.venue, CANONICAL_SYMBOL)
                    await self.store.update_connection(
                        self.venue,
                        status=status,
                        connected_at=connected_at,
                        reconnect_attempt=reconnect_attempt,
                        clear_error=True,
                    )
                    await self._subscribe(websocket)
                    reconnect_attempt = 0
                    logger.info("Kraken public market data connected")
                    await self._consume(websocket)
            except KrakenMessageTimeout as error:
                reconnect_attempt += 1
                await self.store.update_connection(
                    self.venue,
                    status=MarketConnectionStatus.STALE,
                    last_error=str(error),
                    reconnect_attempt=reconnect_attempt,
                )
            except (ConnectionClosed, OSError, ValueError, KeyError, TypeError) as error:
                reconnect_attempt += 1
                await self.store.update_connection(
                    self.venue,
                    status=MarketConnectionStatus.DISCONNECTED,
                    last_error=str(error),
                    reconnect_attempt=reconnect_attempt,
                )
                logger.warning("Kraken market data disconnected: %s", error)

            delay_index = min(
                max(reconnect_attempt - 1, 0), len(RECONNECT_BACKOFF_SECONDS) - 1
            )
            await asyncio.sleep(RECONNECT_BACKOFF_SECONDS[delay_index])

    async def _subscribe(self, websocket: ClientConnection) -> None:
        await websocket.send(
            json.dumps(
                {
                    "method": "subscribe",
                    "params": {
                        "channel": "book",
                        "symbol": [KRAKEN_VENUE_SYMBOL],
                        "depth": BOOK_DEPTH,
                        "snapshot": True,
                    },
                    "req_id": 1,
                }
            )
        )
        await websocket.send(
            json.dumps(
                {
                    "method": "subscribe",
                    "params": {"channel": "instrument", "snapshot": True},
                    "req_id": 2,
                }
            )
        )

    async def _consume(self, websocket: ClientConnection) -> None:
        while True:
            try:
                raw_message = await asyncio.wait_for(
                    websocket.recv(), timeout=MESSAGE_TIMEOUT_SECONDS
                )
            except asyncio.TimeoutError as error:
                raise KrakenMessageTimeout(
                    "no Kraken update or heartbeat received for 3 seconds"
                ) from error

            received_at = utc_now()
            await self.store.update_connection(
                self.venue, last_message_at=received_at
            )
            message = json.loads(raw_message, parse_float=Decimal)
            await self.handle_message(message, received_at=received_at)

    async def handle_message(
        self, message: dict[str, Any], *, received_at: datetime
    ) -> None:
        """Handle one decoded Kraken message; exposed for deterministic tests."""

        channel = message.get("channel")
        if channel == "heartbeat":
            return
        if channel == "book":
            await self._handle_book_message(message, received_at=received_at)
            return
        if channel == "instrument":
            await self._handle_instrument_message(message, received_at=received_at)
            return
        if message.get("success") is False:
            raise ValueError(message.get("error", "Kraken subscription failed"))

    async def _handle_book_message(
        self, message: dict[str, Any], *, received_at: datetime
    ) -> None:
        data_items = message.get("data", ())
        if not data_items:
            return
        payload = data_items[0]
        if payload.get("symbol") != KRAKEN_VENUE_SYMBOL:
            return
        if message.get("type") == "snapshot":
            book = self._book_builder.apply_snapshot(payload, received_at=received_at)
        elif message.get("type") == "update":
            book = self._book_builder.apply_update(payload, received_at=received_at)
        else:
            return
        await self.store.replace_book(book)
        await self.store.update_connection(
            self.venue,
            status=MarketConnectionStatus.LIVE,
            last_book_update_at=received_at,
            reconnect_attempt=0,
            clear_error=True,
        )

    async def _handle_instrument_message(
        self, message: dict[str, Any], *, received_at: datetime
    ) -> None:
        data = message.get("data", {})
        for pair in data.get("pairs", ()):
            if pair.get("symbol") != KRAKEN_VENUE_SYMBOL:
                continue
            rules = InstrumentRules(
                venue=self.venue,
                symbol=CANONICAL_SYMBOL,
                venue_symbol=KRAKEN_VENUE_SYMBOL,
                instrument_type=InstrumentType.SPOT,
                base_asset=pair["base"],
                quote_asset=pair["quote"],
                price_increment=decimal_value(pair["price_increment"]),
                quantity_increment=decimal_value(pair["qty_increment"]),
                quantity_min=decimal_value(pair["qty_min"]),
                price_precision=int(pair["price_precision"]),
                quantity_precision=int(pair["qty_precision"]),
                status=str(pair["status"]),
                received_at=received_at,
            )
            await self.store.replace_instrument(rules)
            return
