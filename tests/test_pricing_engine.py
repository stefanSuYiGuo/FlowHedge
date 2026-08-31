from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from backend.domain.models import ClientSide, InstrumentType, PricingStatus
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
from backend.pricing.engine import ECONOMICS_DISCLOSURE, price_client_rfq


NOW = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)


def market(
    venue: MarketVenue,
    *,
    bids: tuple[tuple[str, str], ...],
    asks: tuple[tuple[str, str], ...],
    instrument_type: InstrumentType = InstrumentType.SPOT,
    quote_asset: str = "USD",
    status: MarketConnectionStatus = MarketConnectionStatus.LIVE,
    eligible: bool = True,
    exclusion_reason: str | None = None,
) -> ExecutableBookView:
    symbol = "BTC-USD" if quote_asset == "USD" else f"BTC-{quote_asset}"
    rules = InstrumentRules(
        venue=venue,
        symbol=symbol,
        venue_symbol=symbol,
        instrument_type=instrument_type,
        base_asset="BTC",
        quote_asset=quote_asset,
        price_increment=Decimal("0.1"),
        quantity_increment=Decimal("0.01"),
        quantity_min=Decimal("0.01"),
        price_precision=1,
        quantity_precision=2,
        status="LIVE",
        eligible_for_execution=True,
        contract_structure=(
            ContractStructure.SPOT
            if instrument_type is InstrumentType.SPOT
            else ContractStructure.LINEAR
        ),
        native_quantity_unit="BTC",
        settlement_asset=quote_asset,
        usd_conversion_rate=Decimal("1"),
        usd_conversion_assumption=(
            None if quote_asset == "USD" else f"{quote_asset}_USD_1_TO_1_DEMO"
        ),
        received_at=NOW,
    )

    def levels(raw: tuple[tuple[str, str], ...]) -> tuple[ExecutableMarketLevel, ...]:
        return tuple(
            ExecutableMarketLevel(
                price=Decimal(price),
                quantity_btc_equivalent=Decimal(quantity),
                source_quantity=Decimal(quantity),
                source_quantity_unit="BTC",
            )
            for price, quantity in raw
        )

    book = ExecutableOrderBook(
        venue=venue,
        symbol=symbol,
        venue_symbol=symbol,
        instrument_type=instrument_type,
        max_levels=200,
        bids=levels(bids),
        asks=levels(asks),
        exchange_timestamp=NOW,
        received_at=NOW,
    )
    return ExecutableBookView(
        venue=venue,
        symbol=symbol,
        instrument_type=instrument_type,
        connection=MarketConnectionState(
            feed_id=f"{venue.value}-{instrument_type.value}",
            venue=venue,
            status=status,
            endpoint="public",
            connected_at=NOW,
            last_message_at=NOW,
            last_book_update_at=NOW,
        ),
        book=book,
        instrument=rules,
        book_data_age_ms=0,
        eligible=eligible,
        exclusion_reason=exclusion_reason,
        as_of=NOW,
    )


def markets_snapshot(*markets: ExecutableBookView) -> ExecutableMarketSnapshot:
    return ExecutableMarketSnapshot(
        snapshot_version=41,
        captured_at=NOW,
        base_asset="BTC",
        markets=markets,
    )


def standard_snapshot() -> ExecutableMarketSnapshot:
    return markets_snapshot(
        market(
            MarketVenue.COINBASE,
            bids=(("99.8", "5"),),
            asks=(("100.2", "1"), ("101", "5")),
        ),
        market(
            MarketVenue.KRAKEN,
            bids=(("99.5", "5"),),
            asks=(("100", "1"), ("102", "5")),
        ),
        market(
            MarketVenue.OKX,
            bids=(("99.7", "5"),),
            asks=(("99.9", "0.5"), ("103", "5")),
            quote_asset="USDT",
        ),
    )


def test_buy_sweeps_cheapest_all_in_spot_levels_across_venues_and_rounds_up() -> None:
    result = price_client_rfq(
        rfq_id="buy-001",
        client_side=ClientSide.BUY,
        quantity_btc=Decimal("2"),
        snapshot=standard_snapshot(),
    )

    assert result.status is PricingStatus.OK
    assert result.reference_mid_usd == Decimal("99.8")
    assert result.executable_replacement_vwap_usd == Decimal("100.025")
    assert result.final_quote_price_usd == Decimal("100.10")
    assert [(leg.venue, leg.quantity_btc) for leg in result.liquidity_legs] == [
        ("OKX", Decimal("0.5")),
        ("KRAKEN", Decimal("1")),
        ("COINBASE", Decimal("0.5")),
    ]
    assert result.expected_gross_edge_usd == (
        result.client_margin_usd + result.rounding_adjustment_usd
    )
    assert "NOT ACTUAL OSL OR VENUE INSTITUTIONAL FEES" in (
        result.adjustments[0].assumption_label
    )
    assert result.economics_disclosure == ECONOMICS_DISCLOSURE


def test_sell_sweeps_highest_net_proceeds_and_rounds_down() -> None:
    snapshot = markets_snapshot(
        market(
            MarketVenue.COINBASE,
            bids=(("100", "1"), ("99", "5")),
            asks=(("100.2", "5"),),
        ),
        market(
            MarketVenue.KRAKEN,
            bids=(("100.1", "0.5"), ("98", "5")),
            asks=(("100.3", "5"),),
        ),
        market(
            MarketVenue.OKX,
            bids=(("99.9", "5"),),
            asks=(("100.1", "5"),),
            quote_asset="USDT",
        ),
    )

    result = price_client_rfq(
        rfq_id="sell-001",
        client_side=ClientSide.SELL,
        quantity_btc=Decimal("2"),
        snapshot=snapshot,
    )

    assert result.status is PricingStatus.OK
    assert result.executable_replacement_vwap_usd == Decimal("100")
    assert result.final_quote_price_usd == Decimal("99.90")
    assert [(leg.venue, leg.quantity_btc) for leg in result.liquidity_legs] == [
        ("KRAKEN", Decimal("0.5")),
        ("COINBASE", Decimal("1")),
        ("OKX", Decimal("0.5")),
    ]


def test_perpetual_and_stale_markets_are_never_used_for_client_spot_pricing() -> None:
    snapshot = markets_snapshot(
        market(
            MarketVenue.OKX,
            bids=(("110", "10"),),
            asks=(("111", "10"),),
            instrument_type=InstrumentType.PERPETUAL,
        ),
        market(
            MarketVenue.KRAKEN,
            bids=(("105", "10"),),
            asks=(("106", "10"),),
            status=MarketConnectionStatus.STALE,
            eligible=False,
            exclusion_reason="FEED_STALE",
        ),
        market(
            MarketVenue.COINBASE,
            bids=(("99", "5"),),
            asks=(("101", "5"),),
        ),
    )

    result = price_client_rfq(
        rfq_id="eligible-only",
        client_side=ClientSide.BUY,
        quantity_btc=Decimal("2"),
        snapshot=snapshot,
    )

    assert result.status is PricingStatus.OK
    assert [leg.venue for leg in result.liquidity_legs] == ["COINBASE"]
    assert any("NON_SPOT" in reason for reason in result.excluded_markets)
    assert any("FEED_STALE" in reason for reason in result.excluded_markets)


def test_insufficient_known_depth_fails_closed_without_publishing_quote() -> None:
    result = price_client_rfq(
        rfq_id="too-large",
        client_side=ClientSide.BUY,
        quantity_btc=Decimal("20"),
        snapshot=standard_snapshot(),
    )

    assert result.status is PricingStatus.INSUFFICIENT_LIQUIDITY
    assert result.final_quote_price_usd is None
    assert result.priced_quantity_btc == Decimal("17.5")
    assert result.unpriced_quantity_btc == Decimal("2.5")


def test_larger_quantity_cannot_improve_the_buy_quote_on_same_snapshot() -> None:
    small = price_client_rfq(
        rfq_id="small",
        client_side=ClientSide.BUY,
        quantity_btc=Decimal("1"),
        snapshot=standard_snapshot(),
    )
    large = price_client_rfq(
        rfq_id="large",
        client_side=ClientSide.BUY,
        quantity_btc=Decimal("5"),
        snapshot=standard_snapshot(),
    )

    assert small.status is PricingStatus.OK
    assert large.status is PricingStatus.OK
    assert large.final_quote_price_usd >= small.final_quote_price_usd


def test_no_current_spot_market_returns_explicit_failure() -> None:
    result = price_client_rfq(
        rfq_id="none",
        client_side=ClientSide.SELL,
        quantity_btc=Decimal("8"),
        snapshot=markets_snapshot(
            market(
                MarketVenue.OKX,
                bids=(("99", "10"),),
                asks=(("101", "10"),),
                instrument_type=InstrumentType.PERPETUAL,
            )
        ),
    )

    assert result.status is PricingStatus.NO_ELIGIBLE_SPOT_MARKETS
    assert result.final_quote_price_usd is None
    assert result.priced_quantity_btc == 0
