from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from backend.config import RiskPolicyConfig
from backend.demo import DemoTradingService
from backend.domain.models import DeskState, EventType, InstrumentType
from backend.market.book import normalized_books_from_levels
from backend.market.models import (
    InstrumentRules,
    MarketConnectionState,
    MarketConnectionStatus,
    MarketStateView,
    MarketVenue,
    UnifiedMarketSnapshot,
)
from backend.risk.models import RiskAction, RiskBand, RiskReferencePrice
from backend.risk.policy import RiskPolicy, build_risk_reference_price
from backend.risk.service import RiskService


NOW = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)


def lifecycle_service(trading: DemoTradingService) -> RiskService:
    # These lifecycle tests call reconcile directly and intentionally need no store I/O.
    return RiskService(None, trading)  # type: ignore[arg-type]


def desk(actual: str, working: str = "0", *, version: int = 1) -> DeskState:
    return DeskState(
        version=version,
        as_of=NOW,
        spot_inventory_btc=Decimal(actual),
        derivative_delta_btc=Decimal("0"),
        total_delta_btc=Decimal(actual),
        working_order_delta_btc=Decimal(working),
    )


def reference(price: str = "100000", *, version: int = 1) -> RiskReferencePrice:
    return RiskReferencePrice(
        asset="BTC",
        price_usd=Decimal(price),
        captured_at=NOW,
        source="TEST_USD_SPOT",
        market_snapshot_version=version,
        eligible=True,
        degraded=False,
    )


def risk_market(venue: MarketVenue, mid: str) -> MarketStateView:
    midpoint = Decimal(mid)
    rules = InstrumentRules(
        venue=venue,
        symbol="BTC-USD",
        venue_symbol="BTC/USD" if venue is MarketVenue.KRAKEN else "BTC-USD",
        instrument_type=InstrumentType.SPOT,
        base_asset="BTC",
        quote_asset="USD",
        price_increment=Decimal("1"),
        quantity_increment=Decimal("0.01"),
        quantity_min=Decimal("0.01"),
        price_precision=0,
        quantity_precision=2,
        status="ONLINE",
        received_at=NOW,
    )
    book, executable = normalized_books_from_levels(
        rules=rules,
        bids=((midpoint - 1, Decimal("1")),),
        asks=((midpoint + 1, Decimal("1")),),
        exchange_timestamp=NOW,
        received_at=NOW,
    )
    return MarketStateView(
        venue=venue,
        symbol="BTC-USD",
        instrument_type=InstrumentType.SPOT,
        connection=MarketConnectionState(
            feed_id=f"{venue.value.lower()}-spot",
            venue=venue,
            status=MarketConnectionStatus.LIVE,
            endpoint="test",
        ),
        book=book,
        instrument=rules,
        executable_bid_levels=len(executable.bids),
        executable_ask_levels=len(executable.asks),
        book_data_age_ms=0,
        eligible=True,
        as_of=NOW,
    )


@pytest.mark.parametrize(
    ("actual", "expected_band"),
    [
        ("10", RiskBand.GREEN),
        ("10.0000001", RiskBand.YELLOW),
        ("30", RiskBand.YELLOW),
        ("30.0000001", RiskBand.RED),
    ],
)
def test_exact_demo_limit_boundaries(actual: str, expected_band: RiskBand) -> None:
    result = RiskPolicy().evaluate(desk(actual), reference(), assessed_at=NOW)
    assert result.risk_band is expected_band


@pytest.mark.parametrize("actual", ["12", "-12"])
def test_yellow_is_sign_symmetric_and_targets_soft_boundary(actual: str) -> None:
    result = RiskPolicy().evaluate(desk(actual), reference(), assessed_at=NOW)
    direction = Decimal("1") if Decimal(actual) > 0 else Decimal("-1")
    assert result.risk_band is RiskBand.YELLOW
    assert result.action is RiskAction.PARTIAL_HEDGE
    assert result.target_delta_btc == direction * Decimal("10")
    assert result.gross_required_hedge_delta_btc == (
        direction * Decimal("10") - Decimal(actual)
    )
    assert result.auto_hedge_target_delta_btc is None
    assert result.auto_remaining_hedge_requirement_btc is None


@pytest.mark.parametrize("actual", ["35", "-35"])
def test_red_is_sign_symmetric_with_distinct_advisory_and_auto_targets(
    actual: str,
) -> None:
    result = RiskPolicy().evaluate(desk(actual), reference(), assessed_at=NOW)
    direction = Decimal("1") if Decimal(actual) > 0 else Decimal("-1")
    assert result.risk_band is RiskBand.RED
    assert result.action is RiskAction.IMMEDIATE_HEDGE
    assert result.policy_version == "RISK_POLICY_V1_1"
    assert result.advisory_target_delta_btc == direction * Decimal("10")
    assert result.advisory_gross_required_hedge_delta_btc == (
        direction * Decimal("10") - Decimal(actual)
    )
    assert result.auto_hedge_target_notional_usd == Decimal("900000.00")
    assert result.auto_hedge_target_delta_btc == direction * Decimal("9.00")
    assert result.auto_gross_required_hedge_delta_btc == (
        direction * Decimal("9.00") - Decimal(actual)
    )
    assert result.target_delta_btc == result.advisory_target_delta_btc
    assert result.target_delta_btc != 0


def test_auto_target_ratio_is_configurable_not_hard_coded() -> None:
    config = RiskPolicyConfig(auto_hedge_target_ratio_of_soft=Decimal("0.80"))
    result = RiskPolicy(config).evaluate(desk("35"), reference(), assessed_at=NOW)

    assert result.auto_hedge_target_ratio_of_soft == Decimal("0.80")
    assert result.auto_hedge_target_notional_usd == Decimal("800000.00")
    assert result.auto_hedge_target_delta_btc == Decimal("8.00")


def test_working_orders_reduce_advisory_and_auto_requirements_separately() -> None:
    result = RiskPolicy().evaluate(
        desk("-35", "5"), reference(), assessed_at=NOW
    )

    assert result.advisory_gross_required_hedge_delta_btc == Decimal("25")
    assert result.advisory_remaining_hedge_requirement_btc == Decimal("20")
    assert result.auto_gross_required_hedge_delta_btc == Decimal("26.00")
    assert result.auto_qualifying_working_order_delta_btc == Decimal("5")
    assert result.auto_remaining_hedge_requirement_btc == Decimal("21.00")


def test_same_direction_working_orders_reduce_remaining_requirement() -> None:
    result = RiskPolicy().evaluate(
        desk("-12", "1.25"), reference(), assessed_at=NOW
    )
    assert result.gross_required_hedge_delta_btc == Decimal("2")
    assert result.remaining_hedge_requirement_btc == Decimal("0.75")
    assert result.projected_delta_btc == Decimal("-10.75")
    assert result.working_order_conflict is False
    assert result.working_order_overhedge is False


def test_conflicting_and_overhedging_working_orders_are_blocked() -> None:
    policy = RiskPolicy()
    conflict = policy.evaluate(desk("-12", "-1"), reference(), assessed_at=NOW)
    overhedge = policy.evaluate(desk("-12", "3"), reference(), assessed_at=NOW)

    assert conflict.working_order_conflict is True
    assert conflict.remaining_hedge_requirement_btc == Decimal("2")
    assert "WORKING_ORDER_CONFLICT" in conflict.auto_hedge_blocked_reasons
    assert overhedge.working_order_overhedge is True
    assert overhedge.remaining_hedge_requirement_btc == 0
    assert "WORKING_ORDER_OVERHEDGE" in overhedge.auto_hedge_blocked_reasons


def test_offsetting_spot_and_perp_delta_is_green_while_inventory_is_not_evaluated() -> None:
    state = DeskState(
        version=2,
        as_of=NOW,
        spot_inventory_btc=Decimal("-10"),
        derivative_delta_btc=Decimal("10"),
        total_delta_btc=Decimal("0"),
    )
    result = RiskPolicy().evaluate(state, reference(), assessed_at=NOW)
    assert result.risk_band is RiskBand.GREEN
    assert result.actual_delta_btc == 0
    assert result.auto_hedge_target_delta_btc is None
    assert result.inventory_or_settlement_state == "NOT_EVALUATED"


def test_missing_all_reference_sources_is_unavailable_not_green() -> None:
    snapshot = UnifiedMarketSnapshot(
        snapshot_version=4,
        captured_at=NOW,
        base_asset="BTC",
        markets=(),
    )
    unavailable = build_risk_reference_price(snapshot)
    result = RiskPolicy().evaluate(desk("0"), unavailable, assessed_at=NOW)
    assert unavailable.eligible is False
    assert result.risk_band is RiskBand.UNAVAILABLE
    assert result.action is RiskAction.HOLD
    assert result.remaining_hedge_requirement_btc is None


def test_risk_reference_uses_median_of_two_usd_spots_and_degrades_to_one() -> None:
    kraken = risk_market(MarketVenue.KRAKEN, "100000")
    coinbase = risk_market(MarketVenue.COINBASE, "100100")
    healthy = build_risk_reference_price(
        UnifiedMarketSnapshot(
            snapshot_version=8,
            captured_at=NOW,
            base_asset="BTC",
            markets=(kraken, coinbase),
        )
    )
    degraded = build_risk_reference_price(
        UnifiedMarketSnapshot(
            snapshot_version=9,
            captured_at=NOW,
            base_asset="BTC",
            markets=(coinbase,),
        )
    )
    assert healthy.price_usd == Decimal("100050")
    assert healthy.degraded is False
    assert degraded.price_usd == Decimal("100100")
    assert degraded.degraded is True


def test_red_breach_timer_is_stable_and_required_event_is_exactly_once() -> None:
    trading = DemoTradingService()
    service = lifecycle_service(trading)
    policy = RiskPolicy()

    first = service.reconcile(
        policy.evaluate(desk("35"), reference(version=1), assessed_at=NOW),
        now=NOW,
    )
    second = service.reconcile(
        policy.evaluate(
            desk("34", version=2),
            reference(version=99),
            assessed_at=NOW + timedelta(seconds=2),
        ),
        now=NOW + timedelta(seconds=2),
    )
    expired = service.reconcile(
        policy.evaluate(
            desk("18", "-1", version=3),
            reference("200000", version=100),
            assessed_at=NOW + timedelta(seconds=5),
        ),
        now=NOW + timedelta(seconds=5),
    )
    repeated = service.reconcile(
        policy.evaluate(
            desk("18", "-1", version=3),
            reference("200000", version=101),
            assessed_at=NOW + timedelta(seconds=8),
        ),
        now=NOW + timedelta(seconds=8),
    )

    assert first.hard_breach_id == second.hard_breach_id == expired.hard_breach_id
    assert first.hard_breach_started_at == second.hard_breach_started_at == NOW
    assert second.hard_breach_seconds_remaining == Decimal("3.0")
    assert expired.auto_hedge_required is True
    assert expired.auto_hedge_active is True
    assert expired.auto_hedge_target_delta_btc == Decimal("4.50")
    assert expired.auto_remaining_hedge_requirement_btc == Decimal("-12.50")
    assert repeated.auto_hedge_required is True
    assert [event.event_type for event in trading.events].count(
        EventType.AUTO_HEDGE_REQUIRED
    ) == 1
    event = next(
        event
        for event in trading.events
        if event.event_type is EventType.AUTO_HEDGE_REQUIRED
    )
    assert event.payload == {
        "breach_id": expired.hard_breach_id,
        "desk_state_version": 3,
        "market_snapshot_version": 100,
        "actual_delta_btc": Decimal("18"),
        "actual_delta_notional_usd": Decimal("3600000"),
        "soft_delta_limit_usd": Decimal("1000000"),
        "hard_delta_limit_usd": Decimal("3000000"),
        "auto_hedge_target_ratio_of_soft": Decimal("0.90"),
        "auto_hedge_target_notional_usd": Decimal("900000.00"),
        "auto_hedge_target_delta_btc": Decimal("4.50"),
        "auto_gross_required_hedge_delta_btc": Decimal("-13.50"),
        "qualifying_working_order_delta_btc": Decimal("-1"),
        "auto_remaining_hedge_requirement_btc": Decimal("-12.50"),
    }


def test_fill_like_actual_reduction_exits_red_and_cancels_countdown() -> None:
    trading = DemoTradingService()
    service = lifecycle_service(trading)
    policy = RiskPolicy()
    entered = service.reconcile(
        policy.evaluate(desk("35"), reference(), assessed_at=NOW), now=NOW
    )
    exited = service.reconcile(
        policy.evaluate(
            desk("25", version=2),
            reference(version=2),
            assessed_at=NOW + timedelta(seconds=2),
        ),
        now=NOW + timedelta(seconds=2),
    )

    assert entered.risk_band is RiskBand.RED
    assert exited.risk_band is RiskBand.YELLOW
    assert exited.hard_breach_id is None
    assert exited.auto_hedge_required is False
    assert exited.auto_hedge_active is False
    assert [event.event_type for event in trading.events].count(
        EventType.AUTO_HEDGE_CANCELLED
    ) == 1


def test_missing_reference_does_not_cancel_or_reset_active_red_breach() -> None:
    trading = DemoTradingService()
    service = lifecycle_service(trading)
    policy = RiskPolicy()
    entered = service.reconcile(
        policy.evaluate(desk("31"), reference(), assessed_at=NOW), now=NOW
    )
    missing_reference = RiskReferencePrice(
        asset="BTC",
        captured_at=NOW + timedelta(seconds=2),
        source="UNAVAILABLE",
        market_snapshot_version=2,
        eligible=False,
        degraded=True,
    )
    unavailable = service.reconcile(
        policy.evaluate(
            desk("31"), missing_reference, assessed_at=NOW + timedelta(seconds=2)
        ),
        now=NOW + timedelta(seconds=2),
    )
    assert unavailable.hard_breach_id == entered.hard_breach_id
    assert unavailable.hard_breach_started_at == NOW
    assert unavailable.auto_hedge_blocked is True
    assert not any(
        event.event_type is EventType.AUTO_HEDGE_CANCELLED for event in trading.events
    )


def test_missing_reference_blocks_but_does_not_cancel_triggered_intervention() -> None:
    trading = DemoTradingService()
    service = lifecycle_service(trading)
    policy = RiskPolicy()
    service.reconcile(
        policy.evaluate(desk("35"), reference(), assessed_at=NOW), now=NOW
    )
    triggered = service.reconcile(
        policy.evaluate(
            desk("35", version=2),
            reference(version=2),
            assessed_at=NOW + timedelta(seconds=5),
        ),
        now=NOW + timedelta(seconds=5),
    )
    unavailable_reference = RiskReferencePrice(
        asset="BTC",
        captured_at=NOW + timedelta(seconds=6),
        source="UNAVAILABLE",
        market_snapshot_version=3,
        eligible=False,
        degraded=True,
    )
    unavailable = service.reconcile(
        policy.evaluate(
            desk("20", version=3),
            unavailable_reference,
            assessed_at=NOW + timedelta(seconds=6),
        ),
        now=NOW + timedelta(seconds=6),
    )

    assert triggered.auto_hedge_required is True
    assert unavailable.hard_breach_id == triggered.hard_breach_id
    assert unavailable.auto_hedge_required is True
    assert unavailable.auto_hedge_active is True
    assert unavailable.auto_hedge_blocked is True
    assert "ACTIVE_RED_BREACH_UNVERIFIED" in unavailable.auto_hedge_blocked_reasons


def test_active_auto_intervention_continues_below_soft_limit_until_900k() -> None:
    trading = DemoTradingService()
    service = lifecycle_service(trading)
    policy = RiskPolicy()
    service.reconcile(
        policy.evaluate(desk("35"), reference(), assessed_at=NOW), now=NOW
    )
    service.reconcile(
        policy.evaluate(
            desk("35", version=2),
            reference(version=2),
            assessed_at=NOW + timedelta(seconds=5),
        ),
        now=NOW + timedelta(seconds=5),
    )

    below_soft = service.reconcile(
        policy.evaluate(
            desk("9.5", version=3),
            reference(version=3),
            assessed_at=NOW + timedelta(seconds=6),
        ),
        now=NOW + timedelta(seconds=6),
    )

    assert below_soft.risk_band is RiskBand.GREEN
    assert below_soft.absolute_delta_exposure_usd == Decimal("950000.0")
    assert below_soft.auto_hedge_required is True
    assert below_soft.auto_hedge_active is True
    assert below_soft.auto_hedge_complete is False
    assert below_soft.auto_hedge_target_delta_btc == Decimal("9.00")
    assert below_soft.auto_remaining_hedge_requirement_btc == Decimal("-0.50")


@pytest.mark.parametrize("actual", ["9", "8.99999", "-9", "-8.99999"])
def test_active_auto_intervention_completes_at_or_inside_900k(actual: str) -> None:
    trading = DemoTradingService()
    service = lifecycle_service(trading)
    policy = RiskPolicy()
    service.reconcile(
        policy.evaluate(desk("35"), reference(), assessed_at=NOW), now=NOW
    )
    service.reconcile(
        policy.evaluate(
            desk("35", version=2),
            reference(version=2),
            assessed_at=NOW + timedelta(seconds=5),
        ),
        now=NOW + timedelta(seconds=5),
    )

    completed = service.reconcile(
        policy.evaluate(
            desk(actual, version=3),
            reference(version=3),
            assessed_at=NOW + timedelta(seconds=6),
        ),
        now=NOW + timedelta(seconds=6),
    )

    assert completed.risk_band is RiskBand.GREEN
    assert completed.absolute_delta_exposure_usd <= Decimal("900000")
    assert completed.auto_hedge_target_notional_usd == Decimal("900000.00")
    assert completed.auto_hedge_required is False
    assert completed.auto_hedge_active is False
    assert completed.auto_hedge_complete is True
    assert completed.auto_remaining_hedge_requirement_btc == 0
    assert completed.auto_hedge_target_delta_btc != 0
