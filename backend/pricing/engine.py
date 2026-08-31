"""Pure multi-venue Spot L2 pricing for client BTC RFQs."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from statistics import median

from ..config import PricingConfig, pricing_config
from ..domain.models import (
    ClientSide,
    InstrumentType,
    PricingAdjustment,
    PricingAdjustmentType,
    PricingLiquidityLeg,
    PricingResult,
    PricingStatus,
)
from ..execution_cost.config import execution_fee_config
from ..execution_cost.models import ExecutionFeeConfig
from ..market.models import (
    ExecutableBookView,
    ExecutableMarketSnapshot,
    MarketConnectionStatus,
)


BASIS_POINTS = Decimal("10000")
ECONOMICS_DISCLOSURE = (
    "EXPECTED PRICING ECONOMICS · NOT ACTUAL HEDGE EXECUTION OR REALIZED PNL"
)
USD_IDENTITY_ASSUMPTION = "USD_IDENTITY"


@dataclass(frozen=True)
class _AvailableLevel:
    market: ExecutableBookView
    price_usd: Decimal
    quantity_btc: Decimal
    fee_bps: Decimal
    fee_assumption_label: str

    def ranking_price(self, client_side: ClientSide) -> Decimal:
        fee_rate = self.fee_bps / BASIS_POINTS
        return (
            self.price_usd * (Decimal("1") + fee_rate)
            if client_side is ClientSide.BUY
            else self.price_usd * (Decimal("1") - fee_rate)
        )


@dataclass
class _LegAccumulator:
    market: ExecutableBookView
    quantity_btc: Decimal = Decimal("0")
    notional_usd: Decimal = Decimal("0")
    expected_fee_usd: Decimal = Decimal("0")
    fee_bps: Decimal = Decimal("0")
    fee_assumption_label: str = ""


def price_client_rfq(
    *,
    rfq_id: str,
    client_side: ClientSide,
    quantity_btc: Decimal,
    snapshot: ExecutableMarketSnapshot,
    config: PricingConfig = pricing_config,
    fee_config: ExecutionFeeConfig = execution_fee_config,
) -> PricingResult:
    """Return a fail-closed quote from the cheapest known Spot L2 liquidity."""

    result_id = f"pricing-{rfq_id}-m{snapshot.snapshot_version}"
    request_id = f"pricing-request-{rfq_id}"
    excluded_markets: list[str] = []
    eligible_markets: list[tuple[ExecutableBookView, Decimal, str]] = []

    if quantity_btc <= 0 or snapshot.base_asset not in {None, "BTC"}:
        return _empty_result(
            result_id=result_id,
            request_id=request_id,
            rfq_id=rfq_id,
            client_side=client_side,
            quantity_btc=quantity_btc,
            snapshot=snapshot,
            config=config,
            status=PricingStatus.INVALID_REQUEST,
            reason="INVALID_QUANTITY_OR_BASE_ASSET",
            excluded_markets=(),
        )

    for market in snapshot.markets:
        identity = f"{market.venue.value}:{market.symbol}:{market.instrument_type.value}"
        if market.instrument_type is not InstrumentType.SPOT:
            excluded_markets.append(f"{identity}:NON_SPOT")
            continue
        if (
            market.connection.status is not MarketConnectionStatus.LIVE
            or not market.eligible
            or market.book is None
            or market.instrument is None
            or not market.instrument.eligible_for_execution
        ):
            excluded_markets.append(
                f"{identity}:{market.exclusion_reason or market.connection.status.value}"
            )
            continue
        if market.instrument.base_asset.upper() != "BTC":
            excluded_markets.append(f"{identity}:BASE_ASSET_MISMATCH")
            continue
        fee_entry = fee_config.taker_fee_for(market.venue, InstrumentType.SPOT)
        if fee_entry is None:
            excluded_markets.append(f"{identity}:TAKER_FEE_UNCONFIGURED")
            continue
        eligible_markets.append(
            (market, fee_entry.fee_bps, fee_entry.assumption_label)
        )

    if not eligible_markets:
        return _empty_result(
            result_id=result_id,
            request_id=request_id,
            rfq_id=rfq_id,
            client_side=client_side,
            quantity_btc=quantity_btc,
            snapshot=snapshot,
            config=config,
            status=PricingStatus.NO_ELIGIBLE_SPOT_MARKETS,
            reason="NO_CURRENT_EXECUTABLE_SPOT_BOOKS",
            excluded_markets=tuple(excluded_markets),
        )

    reference_mid = Decimal(
        median(
            [
                (market.book.bids[0].price + market.book.asks[0].price)
                / Decimal("2")
                * market.instrument.usd_conversion_rate
                for market, _, _ in eligible_markets
                if market.book is not None and market.instrument is not None
            ]
        )
    )
    levels: list[_AvailableLevel] = []
    for market, fee_bps, fee_assumption_label in eligible_markets:
        assert market.book is not None and market.instrument is not None
        book_levels = (
            market.book.asks
            if client_side is ClientSide.BUY
            else market.book.bids
        )
        for level in book_levels:
            levels.append(
                _AvailableLevel(
                    market=market,
                    price_usd=level.price * market.instrument.usd_conversion_rate,
                    quantity_btc=level.quantity_btc_equivalent,
                    fee_bps=fee_bps,
                    fee_assumption_label=fee_assumption_label,
                )
            )
    levels.sort(
        key=lambda level: (
            level.ranking_price(client_side)
            if client_side is ClientSide.BUY
            else -level.ranking_price(client_side),
            level.market.venue.value,
            level.price_usd,
        )
    )

    remaining = quantity_btc
    accumulators: dict[tuple[str, str], _LegAccumulator] = {}
    for level in levels:
        if remaining <= 0:
            break
        consumed = min(remaining, level.quantity_btc)
        if consumed <= 0:
            continue
        market = level.market
        key = (market.venue.value, market.symbol)
        accumulator = accumulators.setdefault(key, _LegAccumulator(market=market))
        level_notional = level.price_usd * consumed
        accumulator.quantity_btc += consumed
        accumulator.notional_usd += level_notional
        accumulator.expected_fee_usd += (
            level_notional * level.fee_bps / BASIS_POINTS
        )
        accumulator.fee_bps = level.fee_bps
        accumulator.fee_assumption_label = level.fee_assumption_label
        remaining -= consumed

    priced_quantity = quantity_btc - remaining
    legs = tuple(
        PricingLiquidityLeg(
            venue=accumulator.market.venue.value,
            instrument_id=accumulator.market.symbol,
            instrument_type=InstrumentType.SPOT,
            quantity_btc=accumulator.quantity_btc,
            execution_vwap_usd=(
                accumulator.notional_usd / accumulator.quantity_btc
            ),
            executed_notional_usd=accumulator.notional_usd,
            expected_taker_fee_bps=accumulator.fee_bps,
            expected_fee_usd=accumulator.expected_fee_usd,
            usd_conversion_rate=accumulator.market.instrument.usd_conversion_rate,
            usd_conversion_assumption=(
                accumulator.market.instrument.usd_conversion_assumption
                or (
                    USD_IDENTITY_ASSUMPTION
                    if accumulator.market.instrument.quote_asset == "USD"
                    else "CONFIGURED_QUOTE_TO_USD_RATE"
                )
            ),
        )
        for accumulator in accumulators.values()
        if accumulator.market.instrument is not None
    )
    executed_notional = sum(
        (leg.executed_notional_usd for leg in legs), Decimal("0")
    )
    expected_fee_usd = sum((leg.expected_fee_usd for leg in legs), Decimal("0"))

    if remaining > 0:
        return _empty_result(
            result_id=result_id,
            request_id=request_id,
            rfq_id=rfq_id,
            client_side=client_side,
            quantity_btc=quantity_btc,
            snapshot=snapshot,
            config=config,
            status=PricingStatus.INSUFFICIENT_LIQUIDITY,
            reason="KNOWN_ELIGIBLE_SPOT_DEPTH_EXHAUSTED",
            excluded_markets=tuple(excluded_markets),
            reference_mid=reference_mid,
            priced_quantity=priced_quantity,
            executed_notional=executed_notional,
            expected_fee_usd=expected_fee_usd,
            legs=legs,
        )

    replacement_vwap = executed_notional / quantity_btc
    impact_per_btc = (
        replacement_vwap - reference_mid
        if client_side is ClientSide.BUY
        else reference_mid - replacement_vwap
    )
    impact_usd = impact_per_btc * quantity_btc
    impact_bps = impact_per_btc / reference_mid * BASIS_POINTS
    expected_fee_bps = expected_fee_usd / executed_notional * BASIS_POINTS
    client_margin_usd = (
        reference_mid * quantity_btc * config.base_client_margin_bps / BASIS_POINTS
    )
    raw_quote = (
        (executed_notional + expected_fee_usd + client_margin_usd) / quantity_btc
        if client_side is ClientSide.BUY
        else (executed_notional - expected_fee_usd - client_margin_usd) / quantity_btc
    )
    rounding = ROUND_CEILING if client_side is ClientSide.BUY else ROUND_FLOOR
    final_quote = (
        (raw_quote / config.client_price_increment_usd).to_integral_value(
            rounding=rounding
        )
        * config.client_price_increment_usd
    )
    raw_client_notional = (
        executed_notional + expected_fee_usd + client_margin_usd
        if client_side is ClientSide.BUY
        else executed_notional - expected_fee_usd - client_margin_usd
    )
    final_client_notional = final_quote * quantity_btc
    rounding_adjustment = (
        final_client_notional - raw_client_notional
        if client_side is ClientSide.BUY
        else raw_client_notional - final_client_notional
    )
    expected_gross_edge = client_margin_usd + rounding_adjustment
    fee_assumptions = sorted(
        {accumulator.fee_assumption_label for accumulator in accumulators.values()}
    )
    adjustments = (
        PricingAdjustment(
            adjustment_type=PricingAdjustmentType.EXPECTED_TAKER_FEE,
            amount_bps=expected_fee_bps,
            amount_usd=expected_fee_usd,
            assumption_label=" · ".join(fee_assumptions),
        ),
        PricingAdjustment(
            adjustment_type=PricingAdjustmentType.BASE_CLIENT_MARGIN,
            amount_bps=config.base_client_margin_bps,
            amount_usd=client_margin_usd,
            assumption_label=config.assumption_label,
        ),
        PricingAdjustment(
            adjustment_type=PricingAdjustmentType.CLIENT_PRICE_ROUNDING,
            amount_bps=None,
            amount_usd=rounding_adjustment,
            assumption_label=f"CLIENT_TICK_{config.client_price_increment_usd}_USD",
        ),
    )
    return PricingResult(
        pricing_result_id=result_id,
        request_id=request_id,
        rfq_id=rfq_id,
        model_version=config.model_version,
        status=PricingStatus.OK,
        client_side=client_side,
        requested_quantity_btc=quantity_btc,
        priced_quantity_btc=quantity_btc,
        unpriced_quantity_btc=Decimal("0"),
        market_snapshot_version=snapshot.snapshot_version,
        snapshot_captured_at=snapshot.captured_at,
        reference_mid_usd=reference_mid,
        reference_source=config.reference_source,
        executable_replacement_vwap_usd=replacement_vwap,
        executed_notional_usd=executed_notional,
        expected_market_impact_bps=impact_bps,
        expected_market_impact_usd=impact_usd,
        expected_fee_bps=expected_fee_bps,
        expected_fee_usd=expected_fee_usd,
        client_margin_bps=config.base_client_margin_bps,
        client_margin_usd=client_margin_usd,
        rounding_adjustment_usd=rounding_adjustment,
        expected_gross_edge_usd=expected_gross_edge,
        final_quote_price_usd=final_quote,
        client_price_increment_usd=config.client_price_increment_usd,
        quote_validity_seconds=config.quote_validity_seconds,
        assumption_label=config.assumption_label,
        economics_disclosure=ECONOMICS_DISCLOSURE,
        liquidity_legs=legs,
        adjustments=adjustments,
        excluded_markets=tuple(excluded_markets),
    )


def _empty_result(
    *,
    result_id: str,
    request_id: str,
    rfq_id: str,
    client_side: ClientSide,
    quantity_btc: Decimal,
    snapshot: ExecutableMarketSnapshot,
    config: PricingConfig,
    status: PricingStatus,
    reason: str,
    excluded_markets: tuple[str, ...],
    reference_mid: Decimal | None = None,
    priced_quantity: Decimal = Decimal("0"),
    executed_notional: Decimal = Decimal("0"),
    expected_fee_usd: Decimal = Decimal("0"),
    legs: tuple[PricingLiquidityLeg, ...] = (),
) -> PricingResult:
    return PricingResult(
        pricing_result_id=result_id,
        request_id=request_id,
        rfq_id=rfq_id,
        model_version=config.model_version,
        status=status,
        status_reason=reason,
        client_side=client_side,
        requested_quantity_btc=max(quantity_btc, Decimal("0")),
        priced_quantity_btc=priced_quantity,
        unpriced_quantity_btc=max(quantity_btc, Decimal("0")) - priced_quantity,
        market_snapshot_version=snapshot.snapshot_version,
        snapshot_captured_at=snapshot.captured_at,
        reference_mid_usd=reference_mid,
        reference_source=config.reference_source,
        executed_notional_usd=executed_notional,
        expected_fee_usd=expected_fee_usd,
        client_margin_bps=config.base_client_margin_bps,
        client_margin_usd=Decimal("0"),
        rounding_adjustment_usd=Decimal("0"),
        client_price_increment_usd=config.client_price_increment_usd,
        quote_validity_seconds=config.quote_validity_seconds,
        assumption_label=config.assumption_label,
        economics_disclosure=ECONOMICS_DISCLOSURE,
        liquidity_legs=legs,
        excluded_markets=excluded_markets,
    )
