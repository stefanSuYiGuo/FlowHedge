"""Interfaces shared by current and future venue-specific market adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod

from .models import MarketVenue


class MarketDataAdapter(ABC):
    venue: MarketVenue
    endpoint: str
    symbols: tuple[str, ...]

    @abstractmethod
    async def run(self) -> None:
        """Run the adapter until cancelled."""
