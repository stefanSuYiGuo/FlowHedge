"""Backend-driven RFQ arrivals for the manual trader demonstration."""

from __future__ import annotations

import asyncio
import random
from decimal import Decimal, ROUND_CEILING
from typing import Awaitable, Callable, Optional

from ..demo import DemoTradingService, demo_service, utc_now
from ..domain.models import (
    ClientFlowState,
    ClientSide,
    DemoScenarioResult,
    MarketObservation,
    MarketSnapshot,
    PendingClientFlow,
)
from ..domain.validation import validate_client_rfq_notional
from ..market.models import MarketConnectionStatus, MarketVenue
from ..market.service import market_state_store
from ..market.store import InMemoryMarketStateStore


SLOW_FLOW_MIN_SECONDS = 75
SLOW_FLOW_MAX_SECONDS = 105
PRICING_ACCEPTANCE_DELAY_SECONDS = 1.5
MARKET_RETRY_SECONDS = 5
QUANTITY_INCREMENT_BTC = Decimal("0.01")
QUANTITY_WHOLE_OFFSETS = (0, 1, 2, 4, 7, 12, 20)
QUANTITY_FRACTIONS = (
    Decimal("0.00"),
    Decimal("0.10"),
    Decimal("0.25"),
    Decimal("0.40"),
    Decimal("0.50"),
    Decimal("0.75"),
    Decimal("0.90"),
)
CLIENT_IDS = ("INST-017", "INST-028", "INST-042", "INST-063", "INST-091")


def generate_rfq_quantity(
    reference_price_usd: Decimal, rng: random.Random
) -> Decimal:
    """Generate varied two-decimal BTC size that is always strictly above $500K."""

    minimum_whole_btc = (
        Decimal("500000") / reference_price_usd
    ).to_integral_value(rounding=ROUND_CEILING)
    quantity = (
        minimum_whole_btc
        + Decimal(rng.choice(QUANTITY_WHOLE_OFFSETS))
        + rng.choice(QUANTITY_FRACTIONS)
    ).quantize(QUANTITY_INCREMENT_BTC)
    if quantity * reference_price_usd <= Decimal("500000"):
        quantity += QUANTITY_INCREMENT_BTC
    validate_client_rfq_notional(quantity, reference_price_usd)
    return quantity


class ClientFlowSimulator:
    """Generate independent RFQs without waiting for prior hedge completion."""

    def __init__(
        self,
        market_store: InMemoryMarketStateStore,
        trading_service: DemoTradingService,
        *,
        rng: Optional[random.Random] = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.market_store = market_store
        self.trading_service = trading_service
        self.rng = rng or random.Random()
        self.sleep = sleep
        self.active = True
        self.pending_rfqs: list[PendingClientFlow] = []
        self._task: Optional[asyncio.Task[None]] = None
        self._schedule_changed = asyncio.Event()
        self._generation_lock = asyncio.Lock()
        self._generation_epoch = 0

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(
            self._run(), name="slow-institutional-client-flow"
        )

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    def pause(self) -> ClientFlowState:
        self.active = False
        self._schedule_changed.set()
        return self.state()

    def resume(self) -> ClientFlowState:
        self.active = True
        self._schedule_changed.set()
        return self.state()

    def reset(self) -> None:
        self._generation_epoch += 1
        self.pending_rfqs.clear()
        self.trading_service.reset()
        self._schedule_changed.set()

    def state(self) -> ClientFlowState:
        return ClientFlowState(
            active=self.active,
            pending_rfqs=tuple(self.pending_rfqs),
            completed_scenarios=tuple(self.trading_service.completed_scenarios[-20:]),
            completed_count=len(self.trading_service.completed_scenarios),
        )

    async def generate_once(
        self, *, pricing_delay_seconds: float = PRICING_ACCEPTANCE_DELAY_SECONDS
    ) -> Optional[DemoScenarioResult]:
        """Generate one RFQ from the latest valid Kraken book, then auto-accept it."""

        async with self._generation_lock:
            market = await self.market_store.view(MarketVenue.KRAKEN, "BTC-USD")
            if (
                market.connection.status is not MarketConnectionStatus.LIVE
                or market.book is None
            ):
                return None

            epoch = self._generation_epoch
            book = market.book
            quantity = generate_rfq_quantity(book.mid_price, self.rng)
            side = self.rng.choice((ClientSide.BUY, ClientSide.SELL))
            next_sequence = self.trading_service.client_flow_sequence + 1
            captured_at = utc_now()
            snapshot = MarketSnapshot(
                market_snapshot_id=f"market-flow-{next_sequence:04}",
                version=next_sequence,
                captured_at=captured_at,
                base_asset="BTC",
                quote_currency="USD",
                reference_price_usd=book.mid_price,
                observations=(
                    MarketObservation(
                        venue=book.venue.value,
                        instrument_id=book.symbol,
                        instrument_type=book.instrument_type,
                        bid=book.best_bid,
                        ask=book.best_ask,
                        observed_at=book.exchange_timestamp,
                    ),
                ),
            )
            pending = self.trading_service.begin_generated_client_rfq(
                snapshot=snapshot,
                client_side=side,
                quantity_btc=quantity,
                client_id=self.rng.choice(CLIENT_IDS),
            )
            self.pending_rfqs.append(pending)
            try:
                await self.sleep(pricing_delay_seconds)
                if epoch != self._generation_epoch:
                    return None
                return self.trading_service.complete_generated_client_rfq(pending)
            finally:
                self.pending_rfqs = [
                    candidate
                    for candidate in self.pending_rfqs
                    if candidate.rfq.rfq_id != pending.rfq.rfq_id
                ]

    async def _run(self) -> None:
        retry_after_market_failure = False
        while True:
            if not self.active:
                self._schedule_changed.clear()
                await self._schedule_changed.wait()
                continue

            delay = (
                MARKET_RETRY_SECONDS
                if retry_after_market_failure
                else self.rng.uniform(SLOW_FLOW_MIN_SECONDS, SLOW_FLOW_MAX_SECONDS)
            )
            self._schedule_changed.clear()
            try:
                await asyncio.wait_for(self._schedule_changed.wait(), timeout=delay)
                continue
            except asyncio.TimeoutError:
                pass

            if not self.active:
                continue
            result = await self.generate_once()
            if result is not None:
                retry_after_market_failure = False
                continue
            market = await self.market_store.view(MarketVenue.KRAKEN, "BTC-USD")
            retry_after_market_failure = (
                market.connection.status is not MarketConnectionStatus.LIVE
                or market.book is None
            )


client_flow_service = ClientFlowSimulator(market_state_store, demo_service)
