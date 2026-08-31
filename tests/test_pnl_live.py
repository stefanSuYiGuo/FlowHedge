from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal

from backend.demo import DemoTradingService
from backend.domain.models import InstrumentType
from backend.market.book import normalized_books_from_levels
from backend.market.models import (
    ContractStructure,
    DerivativeMarketContext,
    ExecutableBookView,
    ExecutableMarketSnapshot,
    InstrumentRules,
    MarketConnectionState,
    MarketConnectionStatus,
    MarketVenue,
)
from backend.pnl.live import (
    LivePnLService,
    consolidated_spot_mark,
    executable_perp_marks,
)


NOW = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)


def market(
    venue: MarketVenue,
    instrument_type: InstrumentType,
    *,
    bid: str,
    ask: str,
    venue_symbol: str,
    conversion: str = "1",
    mark: str | None = None,
    derivative_stale: bool | None = None,
    status: MarketConnectionStatus = MarketConnectionStatus.LIVE,
) -> ExecutableBookView:
    rules = InstrumentRules(
        venue=venue,
        symbol="BTC-USD",
        venue_symbol=venue_symbol,
        instrument_type=instrument_type,
        base_asset="BTC",
        quote_asset="USD" if conversion == "1" else "USDT",
        price_increment=Decimal("0.1"),
        quantity_increment=Decimal("0.01"),
        quantity_min=Decimal("0.01"),
        price_precision=1,
        quantity_precision=2,
        status="ONLINE",
        contract_structure=(
            ContractStructure.SPOT
            if instrument_type is InstrumentType.SPOT
            else ContractStructure.LINEAR
        ),
        usd_conversion_rate=Decimal(conversion),
        usd_conversion_assumption=(
            "USD_NATIVE" if conversion == "1" else "TEST_STABLECOIN_CONVERSION"
        ),
        received_at=NOW,
    )
    _, book = normalized_books_from_levels(
        rules=rules,
        bids=((Decimal(bid), Decimal("20")),),
        asks=((Decimal(ask), Decimal("20")),),
        exchange_timestamp=NOW,
        received_at=NOW,
    )
    derivatives = None
    if instrument_type is InstrumentType.PERPETUAL:
        derivatives = DerivativeMarketContext(
            venue=venue,
            symbol="BTC-USD",
            venue_symbol=venue_symbol,
            mark_price=Decimal(mark) if mark is not None else None,
            mark_price_captured_at=NOW if mark is not None else None,
            received_at=NOW,
            source="TEST",
        )
    connection = MarketConnectionState(
        feed_id=f"{venue.value}-{instrument_type.value}",
        venue=venue,
        status=status,
        endpoint="test",
        connected_at=NOW if status is MarketConnectionStatus.LIVE else None,
    )
    eligible = status is MarketConnectionStatus.LIVE
    return ExecutableBookView(
        venue=venue,
        symbol="BTC-USD",
        instrument_type=instrument_type,
        connection=connection,
        book=book,
        instrument=rules,
        derivatives=derivatives,
        derivative_data_stale=derivative_stale,
        eligible=eligible,
        exclusion_reason=None if eligible else f"FEED_{status.value}",
        as_of=NOW,
    )


def snapshot(*markets: ExecutableBookView) -> ExecutableMarketSnapshot:
    return ExecutableMarketSnapshot(
        snapshot_version=77,
        captured_at=NOW,
        base_asset="BTC",
        markets=markets,
    )


def test_spot_mark_is_median_of_live_usd_converted_books() -> None:
    result = consolidated_spot_mark(
        snapshot(
            market(
                MarketVenue.COINBASE,
                InstrumentType.SPOT,
                bid="99",
                ask="101",
                venue_symbol="BTC-USD",
            ),
            market(
                MarketVenue.KRAKEN,
                InstrumentType.SPOT,
                bid="109",
                ask="111",
                venue_symbol="XBT/USD",
            ),
            market(
                MarketVenue.OKX,
                InstrumentType.SPOT,
                bid="119",
                ask="121",
                venue_symbol="BTC-USDT",
                conversion="0.99",
            ),
            market(
                MarketVenue.OKX,
                InstrumentType.SPOT,
                bid="1",
                ask="3",
                venue_symbol="STALE-BTC-USDT",
                status=MarketConnectionStatus.DISCONNECTED,
            ),
        )
    )

    assert result == Decimal("110")


def test_perp_mark_prefers_fresh_derivative_then_falls_back_to_live_book() -> None:
    result = executable_perp_marks(
        snapshot(
            market(
                MarketVenue.OKX,
                InstrumentType.PERPETUAL,
                bid="99",
                ask="101",
                venue_symbol="BTC-USDT-SWAP",
                mark="102",
                derivative_stale=False,
            ),
            market(
                MarketVenue.KRAKEN,
                InstrumentType.PERPETUAL,
                bid="199",
                ask="201",
                venue_symbol="PF_XBTUSD",
                mark="210",
                derivative_stale=True,
            ),
        )
    )

    assert result == {
        ("OKX", "BTC-USDT-SWAP"): Decimal("102"),
        ("KRAKEN", "PF_XBTUSD"): Decimal("200"),
    }


def test_live_service_embeds_atomic_market_and_desk_versions() -> None:
    market_snapshot = snapshot(
        market(
            MarketVenue.COINBASE,
            InstrumentType.SPOT,
            bid="99",
            ask="101",
            venue_symbol="BTC-USD",
        )
    )

    class Store:
        async def executable_snapshot(self, _: str) -> ExecutableMarketSnapshot:
            return market_snapshot

    service = LivePnLService(Store(), DemoTradingService())  # type: ignore[arg-type]
    result = asyncio.run(service.snapshot())

    assert result.total_desk_pnl_usd == 0
    assert result.spot_mark_usd == Decimal("100")
    assert result.market_snapshot_version == 77
    assert result.desk_state_version == 0
