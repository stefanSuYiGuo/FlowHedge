from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from backend.domain.models import InstrumentType
from backend.execution_cost.engine import estimate_execution_cost
from backend.execution_cost.models import (
    ExecutionCostComparisonRequest,
    ExecutionCostRequest,
    ExecutionCostStatus,
    ExecutionFeeConfig,
    ExecutionFeeEntry,
    ExecutionSide,
    FeeStatus,
)
from backend.execution_cost.service import ExecutionCostService
from backend.execution_cost.sweeper import sweep_executable_book
from backend.market.book import normalized_books_from_levels
from backend.market.models import (
    ContractStructure,
    ExecutableBookView,
    ExecutableMarketLevel,
    ExecutableMarketSnapshot,
    ExecutableOrderBook,
    InstrumentRules,
    MarketConnectionState,
    MarketConnectionStatus,
    MarketVenue,
)
from backend.market.store import InMemoryMarketStateStore


NOW = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)


def run(coroutine):
    return asyncio.run(coroutine)


def rules(
    *,
    venue: MarketVenue = MarketVenue.KRAKEN,
    instrument_type: InstrumentType = InstrumentType.SPOT,
    quote: str = "USD",
    venue_symbol: str = "BTC-USD",
    conversion_assumption: str | None = None,
    contract_structure: ContractStructure = ContractStructure.SPOT,
    contract_multiplier: Decimal = Decimal("1"),
) -> InstrumentRules:
    return InstrumentRules(
        venue=venue,
        symbol="BTC-USD",
        venue_symbol=venue_symbol,
        instrument_type=instrument_type,
        base_asset="BTC",
        quote_asset=quote,
        price_increment=Decimal("0.01"),
        quantity_increment=Decimal("0.0001"),
        quantity_min=Decimal("0.0001"),
        price_precision=2,
        quantity_precision=4,
        status="LIVE",
        contract_structure=contract_structure,
        contract_multiplier=contract_multiplier,
        contract_value_currency=(
            None if instrument_type is InstrumentType.SPOT else "BTC"
        ),
        native_quantity_unit=(
            "BTC" if instrument_type is InstrumentType.SPOT else "CONTRACTS"
        ),
        settlement_asset=quote,
        usd_conversion_rate=Decimal("1"),
        usd_conversion_assumption=conversion_assumption,
        received_at=NOW,
    )


def executable_book(
    *,
    instrument: InstrumentRules | None = None,
    bids: tuple[tuple[str, str], ...] = (("99", "1"), ("98", "2"), ("97", "3")),
    asks: tuple[tuple[str, str], ...] = (("101", "1"), ("102", "2"), ("103", "3")),
    received_at: datetime = NOW,
) -> ExecutableOrderBook:
    instrument = instrument or rules()

    def level(value: tuple[str, str]) -> ExecutableMarketLevel:
        price, quantity = value
        return ExecutableMarketLevel(
            price=Decimal(price),
            quantity_btc_equivalent=Decimal(quantity),
            source_quantity=Decimal(quantity),
            source_quantity_unit=instrument.native_quantity_unit,
        )

    return ExecutableOrderBook(
        venue=instrument.venue,
        symbol=instrument.symbol,
        venue_symbol=instrument.venue_symbol,
        instrument_type=instrument.instrument_type,
        max_levels=200,
        bids=tuple(level(item) for item in bids),
        asks=tuple(level(item) for item in asks),
        exchange_timestamp=received_at,
        received_at=received_at,
        source_sequence=7,
    )


def market_view(
    *,
    instrument: InstrumentRules | None = None,
    book: ExecutableOrderBook | None = None,
    status: MarketConnectionStatus = MarketConnectionStatus.LIVE,
    eligible: bool = True,
    exclusion_reason: str | None = None,
) -> ExecutableBookView:
    instrument = instrument or rules()
    if book is None and eligible:
        book = executable_book(instrument=instrument)
    return ExecutableBookView(
        venue=instrument.venue,
        symbol=instrument.symbol,
        instrument_type=instrument.instrument_type,
        connection=MarketConnectionState(
            feed_id=f"{instrument.venue.value}-{instrument.instrument_type.value}",
            venue=instrument.venue,
            status=status,
            endpoint="public",
            connected_at=NOW,
            last_message_at=NOW,
            last_book_update_at=NOW,
        ),
        book=book,
        instrument=instrument,
        book_data_age_ms=0 if book is not None else None,
        eligible=eligible,
        exclusion_reason=exclusion_reason,
        as_of=NOW,
    )


def snapshot(
    market: ExecutableBookView | None = None,
    *,
    version: int = 17,
) -> ExecutableMarketSnapshot:
    return ExecutableMarketSnapshot(
        snapshot_version=version,
        captured_at=NOW,
        base_asset="BTC",
        markets=(market or market_view(),),
    )


def request(
    side: ExecutionSide,
    quantity: str,
    *,
    instrument: InstrumentRules | None = None,
    snapshot_version: int | None = None,
) -> ExecutionCostRequest:
    instrument = instrument or rules()
    return ExecutionCostRequest(
        request_id="acceptance",
        venue=instrument.venue,
        instrument_id=instrument.venue_symbol,
        instrument_type=instrument.instrument_type,
        side=side,
        quantity_btc_equivalent=Decimal(quantity),
        market_snapshot_version=snapshot_version,
        requested_at=NOW,
    )


@pytest.mark.parametrize(
    ("side", "expected_price"),
    [
        (ExecutionSide.BUY, Decimal("101")),
        (ExecutionSide.SELL, Decimal("99")),
    ],
)
def test_single_level_buy_and_sell(side: ExecutionSide, expected_price: Decimal) -> None:
    result = sweep_executable_book(executable_book(), side, Decimal("0.5"))

    assert result.fully_executable is True
    assert len(result.fills) == 1
    assert result.fills[0].price == expected_price
    assert result.fills[0].quantity_btc == Decimal("0.5")
    assert result.execution_vwap == expected_price


def test_multi_level_buy_partially_consumes_final_level_and_calculates_vwap() -> None:
    result = sweep_executable_book(
        executable_book(), ExecutionSide.BUY, Decimal("2.5")
    )

    assert [(fill.price, fill.quantity_btc) for fill in result.fills] == [
        (Decimal("101"), Decimal("1")),
        (Decimal("102"), Decimal("1.5")),
    ]
    assert result.execution_vwap == Decimal("101.6")
    assert result.executed_notional_quote == Decimal("254.0")


def test_multi_level_sell_sweeps_bids_high_to_low() -> None:
    result = sweep_executable_book(
        executable_book(), ExecutionSide.SELL, Decimal("2.5")
    )

    assert [(fill.price, fill.quantity_btc) for fill in result.fills] == [
        (Decimal("99"), Decimal("1")),
        (Decimal("98"), Decimal("1.5")),
    ]
    assert result.execution_vwap == Decimal("98.4")


def test_exact_depth_is_full_but_excess_request_is_explicitly_partial() -> None:
    book = executable_book()
    exact = sweep_executable_book(book, ExecutionSide.BUY, Decimal("6"))
    partial = estimate_execution_cost(
        request(ExecutionSide.BUY, "7"), snapshot(market_view(book=book))
    )

    assert exact.fully_executable is True
    assert exact.filled_quantity_btc == Decimal("6")
    assert exact.unfilled_quantity_btc == 0
    assert partial.status is ExecutionCostStatus.INSUFFICIENT_LIQUIDITY
    assert partial.filled_quantity_btc == Decimal("6")
    assert partial.unfilled_quantity_btc == Decimal("1")
    assert partial.fully_executable is False
    assert partial.execution_vwap == Decimal("614") / Decimal("6")
    assert len(partial.fills) == 3


def test_high_precision_inverse_levels_preserve_exact_quantity_reconciliation() -> None:
    inverse_rules = rules(
        instrument_type=InstrumentType.PERPETUAL,
        venue_symbol="PI_XBTUSD",
        contract_structure=ContractStructure.INVERSE,
    )
    quantities = (
        "0.0007001909611712285168682367919",
        "0.2130946698488464598249801114",
        "0.0007000739850916062720264755253",
        "0.8752237096427804424081895626",
    )
    book = executable_book(
        instrument=inverse_rules,
        bids=tuple((str(99999 - index), quantity) for index, quantity in enumerate(quantities)),
        asks=tuple((str(100001 + index), quantity) for index, quantity in enumerate(quantities)),
    )

    result = sweep_executable_book(book, ExecutionSide.BUY, Decimal("8"))

    assert result.filled_quantity_btc + result.unfilled_quantity_btc == Decimal("8")
    assert result.filled_quantity_btc == Decimal("8") - result.unfilled_quantity_btc
    assert result.fully_executable is False


def test_high_precision_partial_sweep_clamps_negative_depth_rounding_residue() -> None:
    inverse_rules = rules(
        instrument_type=InstrumentType.PERPETUAL,
        venue_symbol="PI_XBTUSD",
        contract_structure=ContractStructure.INVERSE,
    )
    quantities = (
        "0.0007001909611712285168682367919",
        "0.2130946698488464598249801114",
        "0.0007000739850916062720264755253",
        "0.8752237096427804424081895626",
    )
    book = executable_book(
        instrument=inverse_rules,
        bids=tuple(("99999", quantity) for quantity in quantities),
        asks=tuple(("100001", quantity) for quantity in quantities),
    )

    result = estimate_execution_cost(
        request(ExecutionSide.BUY, "8", instrument=inverse_rules),
        snapshot(market_view(instrument=inverse_rules, book=book)),
    )

    assert result.status is ExecutionCostStatus.INSUFFICIENT_LIQUIDITY
    assert result.depth_impact_bps == Decimal("0")
    assert result.spread_cost_bps == result.total_price_cost_bps


@pytest.mark.parametrize("side", [ExecutionSide.BUY, ExecutionSide.SELL])
def test_cost_sign_and_spread_depth_decomposition(side: ExecutionSide) -> None:
    result = estimate_execution_cost(request(side, "2.5"), snapshot())

    assert result.status is ExecutionCostStatus.OK
    assert result.spread_cost_bps is not None and result.spread_cost_bps > 0
    assert result.depth_impact_bps is not None and result.depth_impact_bps > 0
    assert result.total_price_cost_bps is not None and result.total_price_cost_bps > 0
    assert (
        result.spread_cost_bps + result.depth_impact_bps
        == result.total_price_cost_bps
    )
    assert result.price_cost_usd is not None and result.price_cost_usd > 0


def test_spot_and_perpetual_books_use_the_same_btc_equivalent_sweeper() -> None:
    spot_rules = rules()
    inverse_rules = rules(
        instrument_type=InstrumentType.PERPETUAL,
        venue_symbol="PI_XBTUSD",
        contract_structure=ContractStructure.INVERSE,
        contract_multiplier=Decimal("1"),
    )
    _, spot_book = normalized_books_from_levels(
        rules=spot_rules,
        bids=((Decimal("99900"), Decimal("2")),),
        asks=((Decimal("100000"), Decimal("2")),),
        exchange_timestamp=NOW,
        received_at=NOW,
    )
    _, perp_book = normalized_books_from_levels(
        rules=inverse_rules,
        bids=((Decimal("99900"), Decimal("199800")),),
        asks=((Decimal("100000"), Decimal("200000")),),
        exchange_timestamp=NOW,
        received_at=NOW,
    )
    spot = estimate_execution_cost(
        request(ExecutionSide.BUY, "2", instrument=spot_rules),
        snapshot(market_view(instrument=spot_rules, book=spot_book)),
    )
    perp = estimate_execution_cost(
        request(ExecutionSide.BUY, "2", instrument=inverse_rules),
        snapshot(market_view(instrument=inverse_rules, book=perp_book)),
    )

    assert spot.filled_quantity_btc == Decimal("2")
    assert perp.filled_quantity_btc == Decimal("2")
    assert spot.execution_vwap == perp.execution_vwap == Decimal("100000")


@pytest.mark.parametrize(
    ("connection_status", "reason", "expected_status"),
    [
        (
            MarketConnectionStatus.STALE,
            "FEED_STALE",
            ExecutionCostStatus.MARKET_STALE,
        ),
        (
            MarketConnectionStatus.DISCONNECTED,
            "FEED_DISCONNECTED",
            ExecutionCostStatus.MARKET_UNAVAILABLE,
        ),
    ],
)
def test_stale_and_disconnected_markets_do_not_produce_normal_estimates(
    connection_status: MarketConnectionStatus,
    reason: str,
    expected_status: ExecutionCostStatus,
) -> None:
    market = market_view(
        status=connection_status,
        eligible=False,
        exclusion_reason=reason,
    )
    result = estimate_execution_cost(
        request(ExecutionSide.BUY, "1"), snapshot(market)
    )

    assert result.status is expected_status
    assert result.filled_quantity_btc == 0
    assert result.unfilled_quantity_btc == 1
    assert result.execution_vwap is None
    assert result.total_price_cost_bps is None


def test_requested_snapshot_version_is_enforced_and_recorded() -> None:
    matching = estimate_execution_cost(
        request(ExecutionSide.BUY, "1", snapshot_version=17), snapshot(version=17)
    )
    mismatch = estimate_execution_cost(
        request(ExecutionSide.BUY, "1", snapshot_version=16), snapshot(version=17)
    )

    assert matching.market_snapshot_version == 17
    assert matching.snapshot_captured_at == NOW
    assert matching.book_captured_at == NOW
    assert mismatch.status is ExecutionCostStatus.INVALID_REQUEST
    assert mismatch.status_reason == "SNAPSHOT_VERSION_MISMATCH"
    assert mismatch.market_snapshot_version == 17


@pytest.mark.parametrize(
    ("quote", "assumption"),
    [
        ("USD", "USD_IDENTITY"),
        ("USDT", "USDT_USD_1_TO_1_DEMO"),
        ("USDC", "USDC_USD_1_TO_1_DEMO"),
    ],
)
def test_quote_notional_normalization_is_explicit(
    quote: str, assumption: str
) -> None:
    instrument = rules(
        quote=quote,
        venue_symbol=f"BTC-{quote}",
        conversion_assumption=None if quote == "USD" else assumption,
    )
    result = estimate_execution_cost(
        request(ExecutionSide.BUY, "1", instrument=instrument),
        snapshot(market_view(instrument=instrument)),
    )

    assert result.executed_notional_quote == Decimal("101")
    assert result.executed_notional_usd == Decimal("101")
    assert result.usd_conversion_rate == Decimal("1")
    assert result.usd_conversion_assumption == assumption


def test_unconfigured_fee_keeps_prefee_economics_and_all_in_unknown() -> None:
    result = estimate_execution_cost(
        request(ExecutionSide.BUY, "1"),
        snapshot(),
        ExecutionFeeConfig(),
    )

    assert result.fee_status is FeeStatus.UNCONFIGURED
    assert result.total_price_cost_bps is not None
    assert result.price_cost_usd is not None
    assert result.taker_fee_bps is None
    assert result.fee_usd is None
    assert result.all_in_immediate_cost_bps is None
    assert result.all_in_immediate_cost_usd is None


def test_configured_taker_fee_is_added_exactly_once() -> None:
    fee_bps = Decimal("7.5")
    fee_config = ExecutionFeeConfig(
        entries=(
            ExecutionFeeEntry(
                venue=MarketVenue.KRAKEN,
                instrument_type=InstrumentType.SPOT,
                fee_bps=fee_bps,
                assumption_label="TEST_TAKER_FEE",
            ),
        )
    )
    result = estimate_execution_cost(
        request(ExecutionSide.BUY, "2.5"), snapshot(), fee_config
    )

    expected_fee = result.executed_notional_usd * fee_bps / Decimal("10000")
    assert result.fee_status is FeeStatus.CONFIGURED
    assert result.fee_usd == expected_fee
    assert result.all_in_immediate_cost_bps == result.total_price_cost_bps + fee_bps
    assert result.all_in_immediate_cost_usd == result.price_cost_usd + expected_fee


def test_batch_evaluates_all_six_candidates_on_one_atomic_snapshot() -> None:
    async def exercise():
        store = InMemoryMarketStateStore()
        captured_at = datetime.now(timezone.utc)
        for venue in MarketVenue:
            for instrument_type in InstrumentType:
                venue_symbol = (
                    f"{venue.value}-BTC-SPOT"
                    if instrument_type is InstrumentType.SPOT
                    else f"{venue.value}-BTC-PERP"
                )
                instrument = rules(
                    venue=venue,
                    instrument_type=instrument_type,
                    venue_symbol=venue_symbol,
                    contract_structure=(
                        ContractStructure.SPOT
                        if instrument_type is InstrumentType.SPOT
                        else ContractStructure.LINEAR
                    ),
                ).model_copy(update={"received_at": captured_at})
                display, book = normalized_books_from_levels(
                    rules=instrument,
                    bids=((Decimal("99"), Decimal("10")),),
                    asks=((Decimal("101"), Decimal("10")),),
                    exchange_timestamp=captured_at,
                    received_at=captured_at,
                )
                feed_id = f"{venue.value}-{instrument_type.value}"
                await store.register_feed(
                    feed_id,
                    venue,
                    "public",
                    (("BTC-USD", instrument_type),),
                )
                await store.replace_instrument(instrument)
                await store.replace_books(display, book)
                await store.update_connection(
                    feed_id,
                    status=MarketConnectionStatus.LIVE,
                    connected_at=captured_at,
                    last_message_at=captured_at,
                    last_book_update_at=captured_at,
                )
        service = ExecutionCostService(store)
        return await service.compare(
            ExecutionCostComparisonRequest(
                request_id="six-candidates",
                side=ExecutionSide.BUY,
                quantity_btc_equivalent=Decimal("8"),
                requested_at=captured_at,
            )
        )

    comparison = run(exercise())

    assert len(comparison.results) == 6
    assert all(
        result.market_snapshot_version == comparison.market_snapshot_version
        for result in comparison.results
    )
    assert all(
        result.snapshot_captured_at == comparison.snapshot_captured_at
        for result in comparison.results
    )
    assert all(result.status is ExecutionCostStatus.OK for result in comparison.results)
    assert not hasattr(comparison, "best_hedge")
    assert not hasattr(comparison, "optimal_allocation")
