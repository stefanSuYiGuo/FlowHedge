"""Market data service registry and lifecycle orchestration."""

from __future__ import annotations

import asyncio

from ..domain.models import InstrumentType
from .base import MarketDataAdapter
from .coinbase import CoinbaseMarketDataAdapter
from .kraken import KrakenSpotMarketDataAdapter
from .kraken_futures import KrakenFuturesMarketDataAdapter
from .models import MarketVenue
from .okx import OKXMarketDataAdapter
from .store import InMemoryMarketStateStore


class MarketDataService:
    """Run independently replaceable venue adapters against one normalized store."""

    def __init__(
        self,
        store: InMemoryMarketStateStore,
        adapters: tuple[MarketDataAdapter, ...],
    ) -> None:
        self.store = store
        self.adapters = adapters
        self._tasks: list[asyncio.Task[None]] = []
        self._supported_markets = {
            (adapter.venue, symbol, instrument_type)
            for adapter in adapters
            for symbol, instrument_type in adapter.markets
        }

    def supports(
        self,
        venue: MarketVenue,
        symbol: str,
        instrument_type: InstrumentType,
    ) -> bool:
        return (venue, symbol, instrument_type) in self._supported_markets

    async def start(self) -> None:
        if self._tasks:
            return
        for adapter in self.adapters:
            await self.store.register_feed(
                adapter.feed_id,
                adapter.venue,
                adapter.endpoint,
                adapter.markets,
            )
            self._tasks.append(
                asyncio.create_task(
                    adapter.run(), name=f"market-data-{adapter.feed_id}"
                )
            )

    async def stop(self) -> None:
        tasks = tuple(self._tasks)
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


market_state_store = InMemoryMarketStateStore()
market_data_service = MarketDataService(
    market_state_store,
    adapters=(
        KrakenSpotMarketDataAdapter(market_state_store),
        KrakenFuturesMarketDataAdapter(market_state_store),
        CoinbaseMarketDataAdapter(market_state_store),
        OKXMarketDataAdapter(market_state_store),
    ),
)
