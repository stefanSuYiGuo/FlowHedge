"""Market data service registry and lifecycle orchestration."""

from __future__ import annotations

import asyncio

from .base import MarketDataAdapter
from .kraken import KrakenSpotMarketDataAdapter
from .models import MarketVenue
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
            (adapter.venue, symbol)
            for adapter in adapters
            for symbol in adapter.symbols
        }

    def supports(self, venue: MarketVenue, symbol: str) -> bool:
        return (venue, symbol) in self._supported_markets

    async def start(self) -> None:
        if self._tasks:
            return
        for adapter in self.adapters:
            await self.store.register_venue(adapter.venue, adapter.endpoint)
            self._tasks.append(
                asyncio.create_task(
                    adapter.run(), name=f"market-data-{adapter.venue.value.lower()}"
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
    adapters=(KrakenSpotMarketDataAdapter(market_state_store),),
)
