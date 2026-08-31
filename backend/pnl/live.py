"""Live orchestration for the pure, replayable PnL engine."""

from __future__ import annotations

from decimal import Decimal

from ..demo import DemoTradingService, demo_service
from ..domain.models import InstrumentType
from ..market.models import (
    ExecutableBookView,
    ExecutableMarketSnapshot,
    MarketConnectionStatus,
)
from ..market.service import market_state_store
from ..market.store import InMemoryMarketStateStore
from .engine import calculate_pnl
from .models import PnLSnapshot


class LivePnLService:
    """Value immutable session fills against one atomic live market snapshot."""

    def __init__(
        self,
        market_store: InMemoryMarketStateStore,
        trading_service: DemoTradingService,
    ) -> None:
        self.market_store = market_store
        self.trading_service = trading_service

    async def snapshot(self) -> PnLSnapshot:
        market_snapshot = await self.market_store.executable_snapshot("BTC")
        spot_mark = consolidated_spot_mark(market_snapshot)
        perp_marks = executable_perp_marks(market_snapshot)

        # No await after this point: capture the mutable demo ledger and its
        # DeskState version together before replaying immutable copies.
        scenarios = tuple(self.trading_service.completed_scenarios)
        fills = tuple(self.trading_service.hedge_fills)
        desk_state_version = self.trading_service.desk_state.version
        return calculate_pnl(
            completed_scenarios=scenarios,
            hedge_fills=fills,
            spot_mark_usd=spot_mark,
            perp_marks=perp_marks,
            as_of=market_snapshot.captured_at,
            desk_state_version=desk_state_version,
            market_snapshot_version=market_snapshot.snapshot_version,
        )


def consolidated_spot_mark(
    snapshot: ExecutableMarketSnapshot,
) -> Decimal | None:
    """Return the median USD midpoint across current executable Spot books."""

    marks = sorted(
        mark
        for market in snapshot.markets
        if market.instrument_type is InstrumentType.SPOT
        for mark in (_book_mid_usd(market),)
        if mark is not None
    )
    if not marks:
        return None
    midpoint = len(marks) // 2
    if len(marks) % 2:
        return marks[midpoint]
    return (marks[midpoint - 1] + marks[midpoint]) / Decimal("2")


def executable_perp_marks(
    snapshot: ExecutableMarketSnapshot,
) -> dict[tuple[str, str], Decimal]:
    """Build USD marks keyed exactly like Perpetual HedgeFill buckets.

    A fresh venue mark is preferred.  When it is missing or stale, the same
    current executable book used by the hedge engine supplies the midpoint.
    """

    marks: dict[tuple[str, str], Decimal] = {}
    for market in snapshot.markets:
        if market.instrument_type is not InstrumentType.PERPETUAL:
            continue
        instrument = market.instrument
        if (
            not _market_is_current(market)
            or instrument is None
            or not instrument.eligible_for_execution
        ):
            continue
        mark = None
        if (
            market.derivatives is not None
            and market.derivatives.mark_price is not None
            and market.derivative_data_stale is False
        ):
            mark = market.derivatives.mark_price * instrument.usd_conversion_rate
        else:
            mark = _book_mid_usd(market)
        if mark is not None:
            marks[(market.venue.value, instrument.venue_symbol)] = mark
    return marks


def _book_mid_usd(market: ExecutableBookView) -> Decimal | None:
    if not _market_is_current(market):
        return None
    instrument = market.instrument
    book = market.book
    if instrument is None or book is None or not instrument.eligible_for_execution:
        return None
    midpoint = (book.bids[0].price + book.asks[0].price) / Decimal("2")
    return midpoint * instrument.usd_conversion_rate


def _market_is_current(market: ExecutableBookView) -> bool:
    return (
        market.eligible
        and market.connection.status is MarketConnectionStatus.LIVE
        and market.book is not None
        and bool(market.book.bids)
        and bool(market.book.asks)
    )


pnl_service = LivePnLService(market_state_store, demo_service)
