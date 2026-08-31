import asyncio
import json
import random
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import backend.client_flow.service as client_flow_module
from backend.client_flow.service import ClientFlowSimulator, generate_rfq_quantity
from backend.demo import DemoTradingService
from backend.domain.models import (
    ClientSide,
    InstrumentType,
    MarketObservation,
    MarketSnapshot,
)
from backend.market.book import KrakenOrderBookBuilder, normalized_books_from_levels
from backend.market.kraken import BOOK_DEPTH, CANONICAL_SYMBOL, KRAKEN_SPOT_WS_ENDPOINT
from backend.market.models import InstrumentRules, MarketConnectionStatus, MarketVenue
from backend.market.store import InMemoryMarketStateStore
from backend.pricing.engine import price_client_rfq


FIXTURES = Path(__file__).parent / "fixtures"


def run(coroutine):
    return asyncio.run(coroutine)


def test_generated_quantities_are_varied_and_strictly_above_500k() -> None:
    price = Decimal("63168.05")
    rng = random.Random(17)
    quantities = [generate_rfq_quantity(price, rng) for _ in range(200)]

    assert all(quantity * price > Decimal("500000") for quantity in quantities)
    assert all(quantity == quantity.quantize(Decimal("0.01")) for quantity in quantities)
    assert any(quantity == quantity.to_integral_value() for quantity in quantities)
    assert any(quantity != quantity.to_integral_value() for quantity in quantities)
    assert len(set(quantities)) > 10


async def live_fixture_store() -> InMemoryMarketStateStore:
    store = InMemoryMarketStateStore()
    await store.register_feed(
        "kraken-public-spot",
        MarketVenue.KRAKEN,
        KRAKEN_SPOT_WS_ENDPOINT,
        ((CANONICAL_SYMBOL, InstrumentType.SPOT),),
    )
    fixture = json.loads(
        (FIXTURES / "kraken_book_snapshot.json").read_text()
    )["data"][0]
    source_book = KrakenOrderBookBuilder(depth=BOOK_DEPTH).apply_snapshot(
        fixture, received_at=datetime.now(timezone.utc)
    )
    rules = InstrumentRules(
        venue=MarketVenue.KRAKEN,
        symbol=CANONICAL_SYMBOL,
        venue_symbol="BTC/USD",
        instrument_type=InstrumentType.SPOT,
        base_asset="BTC",
        quote_asset="USD",
        price_increment=Decimal("0.1"),
        quantity_increment=Decimal("0.00000001"),
        quantity_min=Decimal("0.0001"),
        price_precision=1,
        quantity_precision=8,
        status="ONLINE",
        received_at=datetime.now(timezone.utc),
    )
    book, executable_book = normalized_books_from_levels(
        rules=rules,
        bids=((level.price, level.quantity * 20) for level in source_book.bids),
        asks=((level.price, level.quantity * 20) for level in source_book.asks),
        exchange_timestamp=source_book.exchange_timestamp,
        received_at=datetime.now(timezone.utc),
    )
    await store.replace_instrument(rules)
    await store.replace_books(book, executable_book)
    await store.update_connection(
        "kraken-public-spot",
        status=MarketConnectionStatus.LIVE,
        last_book_update_at=datetime.now(timezone.utc),
    )
    return store


def test_flow_exposes_pricing_then_auto_accepts_and_books_trade() -> None:
    async def exercise():
        store = await live_fixture_store()
        trading = DemoTradingService()
        pricing_started = asyncio.Event()
        release_pricing = asyncio.Event()

        async def controlled_sleep(_: float) -> None:
            pricing_started.set()
            await release_pricing.wait()

        flow = ClientFlowSimulator(
            store,
            trading,
            rng=random.Random(7),
            sleep=controlled_sleep,
        )
        generation = asyncio.create_task(flow.generate_once())
        await pricing_started.wait()
        pricing_state = flow.state()
        desk_before = trading.desk_state
        release_pricing.set()
        result = await generation
        return flow.state(), pricing_state, desk_before, result, trading

    completed_state, pricing_state, desk_before, result, trading = run(exercise())

    assert len(pricing_state.pending_rfqs) == 1
    assert pricing_state.pending_rfqs[0].rfq.status == "PRICING"
    assert desk_before.total_delta_btc == 0
    assert result is not None
    assert result.rfq.status == "FILLED"
    assert result.quote.status == "ACCEPTED"
    assert result.quote.pricing_source == "EXECUTABLE_MULTI_VENUE_L2_V1_AUTO_ACCEPT"
    assert result.pricing_result is not None
    assert result.quote.pricing_result_id == result.pricing_result.pricing_result_id
    assert result.rfq.validated_notional_usd > Decimal("500000")
    assert result.quote.quoted_price_usd == result.pricing_result.final_quote_price_usd
    expected_delta = (
        -result.rfq.quantity_btc
        if result.rfq.client_side is ClientSide.BUY
        else result.rfq.quantity_btc
    )
    assert trading.desk_state.total_delta_btc == expected_delta
    assert completed_state.pending_rfqs == ()
    assert completed_state.completed_count == 1
    assert len(result.events) == 6
    assert "next" not in " ".join(completed_state.model_dump().keys()).lower()


def test_new_client_trade_can_arrive_before_prior_exposure_is_hedged() -> None:
    async def exercise():
        store = await live_fixture_store()
        trading = DemoTradingService()

        async def no_delay(_: float) -> None:
            return None

        flow = ClientFlowSimulator(
            store,
            trading,
            rng=random.Random(11),
            sleep=no_delay,
        )
        first = await flow.generate_once()
        first_delta = trading.desk_state.total_delta_btc
        second = await flow.generate_once()
        return flow, trading, first, first_delta, second

    flow, trading, first, first_delta, second = run(exercise())

    assert first is not None and second is not None
    assert first.rfq.rfq_id != second.rfq.rfq_id
    assert first_delta != 0
    expected_delta = sum(
        (
            -scenario.rfq.quantity_btc
            if scenario.rfq.client_side is ClientSide.BUY
            else scenario.rfq.quantity_btc
            for scenario in (first, second)
        ),
        Decimal("0"),
    )
    assert trading.desk_state.total_delta_btc == expected_delta
    assert flow.state().completed_count == 2
    assert trading.hedge_orders == {}


def test_client_flow_state_exposes_the_full_session_rfq_history() -> None:
    async def exercise():
        trading = DemoTradingService()
        result = trading.run_fixed_client_trade()
        trading.completed_scenarios = [result] * 25
        flow = ClientFlowSimulator(InMemoryMarketStateStore(), trading)
        return flow.state()

    state = run(exercise())

    assert state.completed_count == 25
    assert len(state.completed_scenarios) == 25


def test_reset_during_pricing_discards_the_pending_trade() -> None:
    async def exercise():
        store = await live_fixture_store()
        trading = DemoTradingService()
        pricing_started = asyncio.Event()
        release_pricing = asyncio.Event()

        async def controlled_sleep(_: float) -> None:
            pricing_started.set()
            await release_pricing.wait()

        flow = ClientFlowSimulator(store, trading, sleep=controlled_sleep)
        generation = asyncio.create_task(flow.generate_once())
        await pricing_started.wait()
        flow.reset()
        release_pricing.set()
        result = await generation
        return result, flow.state(), trading.desk_state

    result, state, desk = run(exercise())
    assert result is None
    assert state.pending_rfqs == ()
    assert state.completed_count == 0
    assert desk.version == 0
    assert desk.total_delta_btc == 0


def test_background_schedule_generates_automatically_and_pause_stops_arrivals(
    monkeypatch,
) -> None:
    monkeypatch.setattr(client_flow_module, "SLOW_FLOW_MIN_SECONDS", 0.01)
    monkeypatch.setattr(client_flow_module, "SLOW_FLOW_MAX_SECONDS", 0.01)

    async def exercise():
        store = await live_fixture_store()
        trading = DemoTradingService()

        async def no_delay(_: float) -> None:
            return None

        flow = ClientFlowSimulator(
            store,
            trading,
            rng=random.Random(29),
            sleep=no_delay,
        )
        await flow.start()
        await asyncio.sleep(0.045)
        flow.pause()
        await asyncio.sleep(0.02)
        count_after_pause = flow.state().completed_count
        await asyncio.sleep(0.03)
        count_still_paused = flow.state().completed_count
        await flow.stop()
        return count_after_pause, count_still_paused

    count_after_pause, count_still_paused = run(exercise())
    assert count_after_pause >= 1
    assert count_still_paused == count_after_pause


def test_completed_hedge_batch_does_not_block_the_next_manual_batch() -> None:
    service = DemoTradingService()
    service.run_fixed_client_trade()
    first_batch = service.create_manual_hedge_orders(
        Decimal("5"), Decimal("0"), "batch-one"
    )
    first_order = first_batch.orders[0]
    service.simulate_hedge_fill(
        first_order.hedge_order_id,
        first_order.quantity_btc,
        "fill-batch-one",
    )

    now = datetime.now(timezone.utc)
    snapshot = MarketSnapshot(
        market_snapshot_id="market-next-flow",
        version=2,
        captured_at=now,
        base_asset="BTC",
        quote_currency="USD",
        reference_price_usd=Decimal("100000"),
        observations=(
            MarketObservation(
                venue="KRAKEN",
                instrument_id=CANONICAL_SYMBOL,
                instrument_type="SPOT",
                bid=Decimal("99999"),
                ask=Decimal("100001"),
                observed_at=now,
            ),
        ),
    )
    pending = service.begin_generated_client_rfq(
        snapshot=snapshot,
        client_side=ClientSide.BUY,
        quantity_btc=Decimal("6"),
        client_id="INST-NEXT",
    )
    pricing_store = run(live_fixture_store())
    pricing_snapshot = run(pricing_store.executable_snapshot("BTC"))
    pricing_result = price_client_rfq(
        rfq_id=pending.rfq.rfq_id,
        client_side=pending.rfq.client_side,
        quantity_btc=pending.rfq.quantity_btc,
        snapshot=pricing_snapshot,
    )
    service.complete_generated_client_rfq(pending, pricing_result)

    second_batch = service.create_manual_hedge_orders(
        Decimal("2.50"), Decimal("3.50"), "batch-two"
    )
    assert second_batch.required_hedge_delta_btc == Decimal("6")
    assert sum((order.quantity_btc for order in second_batch.orders), Decimal("0")) == Decimal("6")
    assert [order.hedge_order_id for order in service.archived_hedge_orders] == [
        first_order.hedge_order_id
    ]
    cancelled = service.cancel_unfilled_hedge_orders()
    assert len(cancelled.cancelled_hedge_order_ids) == 2
