"""Interfaces shared by current and future venue-specific market adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..domain.models import InstrumentType
from .models import MarketVenue


class MarketDataAdapter(ABC):
    feed_id: str
    venue: MarketVenue
    endpoint: str
    markets: tuple[tuple[str, InstrumentType], ...]

    @abstractmethod
    async def run(self) -> None:
        """Run the adapter until cancelled."""
