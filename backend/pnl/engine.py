"""Pure, replayable average-cost PnL and reconciliation engine."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Mapping, Sequence

from ..domain.accounting import signed_client_spot_change, signed_hedge_delta
from ..domain.models import (
    ClientSide,
    DemoScenarioResult,
    HedgeFill,
    InstrumentType,
)
from .models import (
    AttributionStatus,
    PnLPosition,
    PnLSnapshot,
    PnLStatus,
    PositionValuationStatus,
)


VALUATION_METHOD = "AVERAGE_COST_BTC_EQUIVALENT_LINEARIZED_USD"
SPOT_BUCKET_ID = "SPOT:BTC:CONSOLIDATED"
SPOT_INSTRUMENT_ID = "BTC-SPOT-CONSOLIDATED"
PERP_VALUATION_FLAG = "PERPETUAL_BTC_EQUIVALENT_LINEARIZED_USD"
RECONCILIATION_TOLERANCE_USD = Decimal("0.01")


class PnLInputError(ValueError):
    """Raised when immutable event identifiers contain conflicting economics."""


@dataclass
class _AverageCostLedger:
    signed_quantity_btc: Decimal = Decimal("0")
    average_entry_price_usd: Decimal | None = None
    gross_realized_pnl_usd: Decimal = Decimal("0")
    transaction_cash_usd: Decimal = Decimal("0")

    def apply(self, signed_quantity_btc: Decimal, price_usd: Decimal) -> None:
        if signed_quantity_btc == 0:
            raise PnLInputError("accounting event quantity cannot be zero")
        if price_usd <= 0:
            raise PnLInputError("accounting event price must be positive")

        self.transaction_cash_usd -= signed_quantity_btc * price_usd
        current = self.signed_quantity_btc
        average = self.average_entry_price_usd

        if current == 0:
            self.signed_quantity_btc = signed_quantity_btc
            self.average_entry_price_usd = price_usd
            return

        same_direction = (current > 0) == (signed_quantity_btc > 0)
        if same_direction:
            current_abs = abs(current)
            incoming_abs = abs(signed_quantity_btc)
            if average is None:  # Defensive invariant; valid ledgers never reach this.
                raise PnLInputError("non-flat position is missing average entry price")
            self.signed_quantity_btc = current + signed_quantity_btc
            self.average_entry_price_usd = (
                current_abs * average + incoming_abs * price_usd
            ) / (current_abs + incoming_abs)
            return

        if average is None:
            raise PnLInputError("non-flat position is missing average entry price")

        closed_quantity = min(abs(current), abs(signed_quantity_btc))
        if current > 0:
            self.gross_realized_pnl_usd += (
                price_usd - average
            ) * closed_quantity
        else:
            self.gross_realized_pnl_usd += (
                average - price_usd
            ) * closed_quantity

        new_quantity = current + signed_quantity_btc
        self.signed_quantity_btc = new_quantity
        if new_quantity == 0:
            self.average_entry_price_usd = None
        elif (new_quantity > 0) == (current > 0):
            # Partial close leaves the remaining entry basis unchanged.
            self.average_entry_price_usd = average
        else:
            # The event closed the old position and opened the opposite remainder.
            self.average_entry_price_usd = price_usd

    def unrealized(self, mark_price_usd: Decimal) -> Decimal:
        if self.signed_quantity_btc == 0:
            return Decimal("0")
        if self.average_entry_price_usd is None:
            raise PnLInputError("non-flat position is missing average entry price")
        return self.signed_quantity_btc * (
            mark_price_usd - self.average_entry_price_usd
        )

    def cash_reconciled_value(self, mark_price_usd: Decimal) -> Decimal:
        return self.transaction_cash_usd + self.signed_quantity_btc * mark_price_usd


@dataclass(frozen=True)
class _AccountingEvent:
    occurred_at: datetime
    stable_id: str
    instrument_type: InstrumentType
    venue: str | None
    instrument_id: str
    signed_quantity_btc: Decimal
    price_usd: Decimal


def calculate_pnl(
    *,
    completed_scenarios: Sequence[DemoScenarioResult],
    hedge_fills: Sequence[HedgeFill],
    spot_mark_usd: Decimal | None,
    perp_marks: Mapping[tuple[str, str], Decimal] | None,
    as_of: datetime,
    desk_state_version: int = 0,
    market_snapshot_version: int | None = None,
    reconciliation_tolerance_usd: Decimal = RECONCILIATION_TOLERANCE_USD,
) -> PnLSnapshot:
    """Replay immutable fills and value the resulting books at explicit marks.

    ``perp_marks`` is keyed by ``(venue, instrument_id)``.  The function never
    reads a market store, considers working orders, or estimates missing fees.
    """

    if spot_mark_usd is not None and spot_mark_usd <= 0:
        raise PnLInputError("spot_mark_usd must be positive when supplied")
    if reconciliation_tolerance_usd < 0:
        raise PnLInputError("reconciliation_tolerance_usd cannot be negative")

    normalized_perp_marks = _normalize_perp_marks(perp_marks or {})
    scenarios = _deduplicate_scenarios(completed_scenarios)
    fills = _deduplicate_fills(hedge_fills)
    quality_flags: list[str] = []

    spot_ledger = _AverageCostLedger()
    perp_ledgers: dict[tuple[str, str], _AverageCostLedger] = {}
    events = _accounting_events(scenarios, fills, quality_flags)
    for event in events:
        if event.instrument_type is InstrumentType.SPOT:
            spot_ledger.apply(event.signed_quantity_btc, event.price_usd)
            continue
        key = (_perp_venue(event.venue), event.instrument_id)
        ledger = perp_ledgers.setdefault(key, _AverageCostLedger())
        ledger.apply(event.signed_quantity_btc, event.price_usd)

    client_spread_capture = _client_spread_capture(scenarios, quality_flags)
    fee_total = _actual_fees(fills, quality_flags)
    hedge_slippage = _hedge_benchmark_total(
        fills,
        field="expected_vwap",
        missing_flag="HEDGE_EXPECTED_VWAP_UNAVAILABLE",
        quality_flags=quality_flags,
    )
    hedge_shortfall = _hedge_benchmark_total(
        fills,
        field="arrival_mid",
        missing_flag="HEDGE_ARRIVAL_MID_UNAVAILABLE",
        quality_flags=quality_flags,
    )

    positions: list[PnLPosition] = []
    spot_unrealized: Decimal | None
    spot_cash_value: Decimal | None
    if spot_ledger.signed_quantity_btc == 0:
        spot_unrealized = Decimal("0")
        spot_cash_value = spot_ledger.transaction_cash_usd
        spot_status = PositionValuationStatus.FLAT
        spot_position_flags: tuple[str, ...] = ()
    elif spot_mark_usd is None:
        spot_unrealized = None
        spot_cash_value = None
        spot_status = PositionValuationStatus.MARK_UNAVAILABLE
        spot_position_flags = ("OPEN_SPOT_MARK_UNAVAILABLE",)
        quality_flags.append("OPEN_SPOT_MARK_UNAVAILABLE")
    else:
        spot_unrealized = spot_ledger.unrealized(spot_mark_usd)
        spot_cash_value = spot_ledger.cash_reconciled_value(spot_mark_usd)
        spot_status = PositionValuationStatus.VALUED
        spot_position_flags = ()

    positions.append(
        PnLPosition(
            bucket_id=SPOT_BUCKET_ID,
            instrument_type=InstrumentType.SPOT,
            instrument_id=SPOT_INSTRUMENT_ID,
            signed_quantity_btc=spot_ledger.signed_quantity_btc,
            average_entry_price_usd=spot_ledger.average_entry_price_usd,
            mark_price_usd=spot_mark_usd,
            gross_realized_pnl_usd=spot_ledger.gross_realized_pnl_usd,
            unrealized_mtm_usd=spot_unrealized,
            valuation_status=spot_status,
            valuation_method=VALUATION_METHOD,
            data_quality_flags=spot_position_flags,
        )
    )

    perp_unrealized_known = Decimal("0")
    perp_cash_value_known = Decimal("0")
    all_perp_marks_available = True
    for (venue, instrument_id), ledger in sorted(perp_ledgers.items()):
        mark = normalized_perp_marks.get((venue, instrument_id))
        position_flags = [PERP_VALUATION_FLAG]
        if ledger.signed_quantity_btc == 0:
            unrealized = Decimal("0")
            perp_cash_value_known += ledger.transaction_cash_usd
            valuation_status = PositionValuationStatus.FLAT
        elif mark is None:
            unrealized = None
            valuation_status = PositionValuationStatus.MARK_UNAVAILABLE
            all_perp_marks_available = False
            missing_flag = f"OPEN_PERP_MARK_UNAVAILABLE:{venue}:{instrument_id}"
            position_flags.append(missing_flag)
            quality_flags.append(missing_flag)
        else:
            unrealized = ledger.unrealized(mark)
            perp_unrealized_known += unrealized
            perp_cash_value_known += ledger.cash_reconciled_value(mark)
            valuation_status = PositionValuationStatus.VALUED
        positions.append(
            PnLPosition(
                bucket_id=f"PERPETUAL:{venue}:{instrument_id}",
                instrument_type=InstrumentType.PERPETUAL,
                venue=venue,
                instrument_id=instrument_id,
                signed_quantity_btc=ledger.signed_quantity_btc,
                average_entry_price_usd=ledger.average_entry_price_usd,
                mark_price_usd=mark,
                gross_realized_pnl_usd=ledger.gross_realized_pnl_usd,
                unrealized_mtm_usd=unrealized,
                valuation_status=valuation_status,
                valuation_method=VALUATION_METHOD,
                data_quality_flags=tuple(position_flags),
            )
        )
    if perp_ledgers:
        quality_flags.append(PERP_VALUATION_FLAG)

    perp_unrealized = (
        perp_unrealized_known if all_perp_marks_available else None
    )
    gross_realized = spot_ledger.gross_realized_pnl_usd + sum(
        (ledger.gross_realized_pnl_usd for ledger in perp_ledgers.values()),
        Decimal("0"),
    )
    net_realized = gross_realized - fee_total if fee_total is not None else None

    accounting_total: Decimal | None = None
    cash_total: Decimal | None = None
    reconciliation_difference: Decimal | None = None
    reconciled = False
    total_desk_pnl: Decimal | None = None
    if (
        net_realized is not None
        and spot_unrealized is not None
        and perp_unrealized is not None
        and spot_cash_value is not None
    ):
        accounting_total = net_realized + spot_unrealized + perp_unrealized
        cash_total = spot_cash_value + perp_cash_value_known - fee_total
        reconciliation_difference = accounting_total - cash_total
        reconciled = abs(reconciliation_difference) <= reconciliation_tolerance_usd
        if reconciled:
            total_desk_pnl = accounting_total
        else:
            quality_flags.append("PNL_RECONCILIATION_OUT_OF_TOLERANCE")

    attribution_complete = (
        total_desk_pnl is not None
        and hedge_slippage is not None
        and hedge_shortfall is not None
        and fee_total is not None
    )
    inventory_market_movement = (
        total_desk_pnl
        - client_spread_capture
        + hedge_shortfall
        + fee_total
        if attribution_complete
        else None
    )

    if accounting_total is not None and not reconciled:
        status = PnLStatus.UNRECONCILED
    elif total_desk_pnl is None or not attribution_complete:
        status = PnLStatus.PARTIAL
    else:
        status = PnLStatus.COMPLETE

    return PnLSnapshot(
        status=status,
        as_of=as_of,
        desk_state_version=desk_state_version,
        market_snapshot_version=market_snapshot_version,
        valuation_method=VALUATION_METHOD,
        spot_mark_usd=spot_mark_usd,
        gross_realized_pnl_usd=gross_realized,
        trading_fees_usd=fee_total,
        net_realized_pnl_usd=net_realized,
        spot_unrealized_mtm_usd=spot_unrealized,
        perp_unrealized_mtm_usd=perp_unrealized,
        total_desk_pnl_usd=total_desk_pnl,
        client_spread_capture_usd=client_spread_capture,
        hedge_slippage_vs_expected_usd=hedge_slippage,
        hedge_implementation_shortfall_usd=hedge_shortfall,
        inventory_market_movement_usd=inventory_market_movement,
        attribution_status=(
            AttributionStatus.COMPLETE
            if attribution_complete
            else AttributionStatus.PARTIAL
        ),
        reconciliation_difference_usd=reconciliation_difference,
        reconciled=reconciled,
        positions=tuple(positions),
        data_quality_flags=tuple(dict.fromkeys(quality_flags)),
    )


def _deduplicate_scenarios(
    scenarios: Sequence[DemoScenarioResult],
) -> tuple[DemoScenarioResult, ...]:
    unique: dict[str, DemoScenarioResult] = {}
    for scenario in scenarios:
        trade_id = scenario.client_trade.client_trade_id
        previous = unique.get(trade_id)
        if previous is not None:
            same_trade = previous.client_trade == scenario.client_trade
            same_reference = (
                previous.market_snapshot.reference_price_usd
                == scenario.market_snapshot.reference_price_usd
                and previous.pricing_result == scenario.pricing_result
            )
            if not same_trade or not same_reference:
                raise PnLInputError(f"conflicting client trade id: {trade_id}")
        unique[trade_id] = scenario
    return tuple(unique.values())


def _deduplicate_fills(fills: Sequence[HedgeFill]) -> tuple[HedgeFill, ...]:
    unique: dict[str, HedgeFill] = {}
    for fill in fills:
        previous = unique.get(fill.hedge_fill_id)
        if previous is not None and previous != fill:
            raise PnLInputError(f"conflicting hedge fill id: {fill.hedge_fill_id}")
        unique[fill.hedge_fill_id] = fill
    return tuple(unique.values())


def _accounting_events(
    scenarios: Sequence[DemoScenarioResult],
    fills: Sequence[HedgeFill],
    quality_flags: list[str],
) -> tuple[_AccountingEvent, ...]:
    events: list[_AccountingEvent] = []
    for scenario in scenarios:
        trade = scenario.client_trade
        events.append(
            _AccountingEvent(
                occurred_at=trade.traded_at,
                stable_id=f"CLIENT:{trade.client_trade_id}",
                instrument_type=InstrumentType.SPOT,
                venue=None,
                instrument_id=trade.instrument_id,
                signed_quantity_btc=signed_client_spot_change(
                    trade.client_side, trade.quantity_btc
                ),
                price_usd=trade.trade_price_usd,
            )
        )
    for fill in fills:
        venue = fill.venue
        if fill.instrument_type is InstrumentType.PERPETUAL and not venue:
            venue = "UNSPECIFIED"
            quality_flags.append(
                f"PERPETUAL_VENUE_UNAVAILABLE:{fill.hedge_fill_id}"
            )
        events.append(
            _AccountingEvent(
                occurred_at=fill.filled_at,
                stable_id=f"HEDGE:{fill.hedge_fill_id}",
                instrument_type=fill.instrument_type,
                venue=venue,
                instrument_id=fill.instrument_id,
                signed_quantity_btc=signed_hedge_delta(
                    fill.side, fill.quantity_btc
                ),
                price_usd=fill.fill_price_usd,
            )
        )
    return tuple(sorted(events, key=lambda item: (item.occurred_at, item.stable_id)))


def _client_spread_capture(
    scenarios: Sequence[DemoScenarioResult], quality_flags: list[str]
) -> Decimal:
    total = Decimal("0")
    for scenario in scenarios:
        trade = scenario.client_trade
        pricing_result = scenario.pricing_result
        reference_mid = (
            pricing_result.reference_mid_usd
            if pricing_result is not None
            else None
        )
        if reference_mid is None:
            reference_mid = scenario.market_snapshot.reference_price_usd
            quality_flags.append(
                f"LEGACY_CLIENT_REFERENCE_FALLBACK:{trade.client_trade_id}"
            )
        if trade.client_side is ClientSide.BUY:
            total += (
                trade.trade_price_usd - reference_mid
            ) * trade.quantity_btc
        else:
            total += (
                reference_mid - trade.trade_price_usd
            ) * trade.quantity_btc
    return total


def _actual_fees(
    fills: Sequence[HedgeFill], quality_flags: list[str]
) -> Decimal | None:
    if any(fill.fee_usd is None for fill in fills):
        quality_flags.append("ACTUAL_HEDGE_FEE_UNAVAILABLE")
        return None
    return sum((fill.fee_usd or Decimal("0") for fill in fills), Decimal("0"))


def _hedge_benchmark_total(
    fills: Sequence[HedgeFill],
    *,
    field: str,
    missing_flag: str,
    quality_flags: list[str],
) -> Decimal | None:
    total = Decimal("0")
    for fill in fills:
        increasing_delta = fill.side.value in {"BUY", "LONG"}
        if field == "expected_vwap":
            benchmark = fill.expected_vwap_usd
            recorded = fill.slippage_vs_expected_usd
        elif field == "arrival_mid":
            benchmark = fill.arrival_mid_usd
            recorded = fill.implementation_shortfall_usd
        else:  # pragma: no cover - private caller contract
            raise AssertionError(f"unknown benchmark field: {field}")

        if recorded is not None:
            total += recorded
            continue
        if benchmark is None:
            quality_flags.append(f"{missing_flag}:{fill.hedge_fill_id}")
            return None
        price_difference = (
            fill.fill_price_usd - benchmark
            if increasing_delta
            else benchmark - fill.fill_price_usd
        )
        total += price_difference * fill.quantity_btc
    return total


def _normalize_perp_marks(
    marks: Mapping[tuple[str, str], Decimal],
) -> dict[tuple[str, str], Decimal]:
    normalized: dict[tuple[str, str], Decimal] = {}
    for (venue, instrument_id), mark in marks.items():
        if mark <= 0:
            raise PnLInputError(
                f"perpetual mark must be positive: {venue}:{instrument_id}"
            )
        normalized_venue = getattr(venue, "value", venue)
        normalized[(str(normalized_venue), instrument_id)] = mark
    return normalized


def _perp_venue(venue: str | None) -> str:
    return venue or "UNSPECIFIED"
