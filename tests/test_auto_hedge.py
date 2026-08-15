from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from backend.auto_hedge.models import AutoHedgeInterventionStatus
from backend.auto_hedge.service import AutoHedgeController
from backend.config import risk_policy_config
from backend.demo import DemoTradingService
from backend.domain.models import (
    DeskState,
    EventType,
    HedgeOrderOrigin,
    HedgeOrderStatus,
    InstrumentType,
)
from backend.execution_cost.models import ExecutionFeeConfig, ExecutionFeeEntry
from backend.hedge_optimizer.service import HedgeOptimizerService
from backend.market.book import normalized_books_from_levels
from backend.market.models import (
    ContractStructure,
    ExecutableBookView,
    ExecutableMarketSnapshot,
    InstrumentRules,
    MarketConnectionState,
    MarketConnectionStatus,
    MarketVenue,
)
from backend.risk.models import RiskReferencePrice
from backend.risk.policy import RiskPolicy


NOW = datetime(2026, 8, 14, 21, 0, tzinfo=timezone.utc)
BREACH_ID = "breach-auto-test"


def run(coroutine):
    return asyncio.run(coroutine)


def desk(actual: str) -> DeskState:
    value = Decimal(actual)
    return DeskState(
        version=1,
        as_of=NOW,
        spot_inventory_btc=value,
        derivative_delta_btc=Decimal("0"),
        total_delta_btc=value,
    )


def spot_market(
    *,
    depth: str = "100",
    status: MarketConnectionStatus = MarketConnectionStatus.LIVE,
) -> ExecutableBookView:
    rules = InstrumentRules(
        venue=MarketVenue.COINBASE,
        symbol="BTC-USD",
        venue_symbol="BTC-USD",
        instrument_type=InstrumentType.SPOT,
        base_asset="BTC",
        quote_asset="USD",
        price_increment=Decimal("0.01"),
        quantity_increment=Decimal("0.01"),
        quantity_min=Decimal("0.01"),
        price_precision=2,
        quantity_precision=2,
        status="LIVE",
        eligible_for_execution=status is MarketConnectionStatus.LIVE,
        contract_structure=ContractStructure.SPOT,
        native_quantity_unit="BTC",
        settlement_asset="USD",
        received_at=NOW,
    )
    _, book = normalized_books_from_levels(
        rules=rules,
        bids=((Decimal("99990"), Decimal(depth)),),
        asks=((Decimal("100010"), Decimal(depth)),),
        exchange_timestamp=NOW,
        received_at=NOW,
    )
    live = status is MarketConnectionStatus.LIVE
    return ExecutableBookView(
        venue=MarketVenue.COINBASE,
        symbol="BTC-USD",
        instrument_type=InstrumentType.SPOT,
        connection=MarketConnectionState(
            feed_id="coinbase-spot",
            venue=MarketVenue.COINBASE,
            status=status,
            endpoint="public",
            connected_at=NOW,
            last_message_at=NOW,
            last_book_update_at=NOW,
        ),
        book=book,
        instrument=rules,
        book_data_age_ms=0,
        eligible=live,
        exclusion_reason=None if live else f"FEED_{status.value}",
        as_of=NOW,
    )


def snapshot(
    market: ExecutableBookView,
    *,
    version: int = 501,
) -> ExecutableMarketSnapshot:
    return ExecutableMarketSnapshot(
        snapshot_version=version,
        captured_at=NOW,
        base_asset="BTC",
        markets=(market,),
    )


class MutableStore:
    def __init__(self, value: ExecutableMarketSnapshot) -> None:
        self.value = value

    async def executable_snapshot(self, _: str) -> ExecutableMarketSnapshot:
        return self.value


class ActiveAutoRisk:
    """Evaluate current DeskState while preserving an already-triggered breach."""

    def __init__(self, trading: DemoTradingService, store: MutableStore) -> None:
        self.trading = trading
        self.store = store
        self.policy = RiskPolicy(risk_policy_config)
        self.price = Decimal("100000")

    async def assess(self):
        reference = RiskReferencePrice(
            asset="BTC",
            price_usd=self.price,
            captured_at=NOW,
            source="TEST_SPOT_REFERENCE",
            market_snapshot_version=self.store.value.snapshot_version,
            eligible=True,
            degraded=False,
        )
        core = self.policy.evaluate(
            self.trading.desk_state,
            reference,
            assessed_at=NOW,
        )
        auto = self.policy.apply_auto_target(core)
        complete = (
            auto.absolute_delta_exposure_usd is not None
            and auto.absolute_delta_exposure_usd
            <= auto.auto_hedge_target_notional_usd
        )
        return auto.model_copy(
            update={
                "hard_breach_id": BREACH_ID,
                "hard_breach_started_at": NOW,
                "hard_breach_seconds_remaining": Decimal("0"),
                "auto_hedge_required": not complete,
                "auto_hedge_active": not complete,
                "auto_hedge_complete": complete,
                "auto_remaining_hedge_requirement_btc": (
                    Decimal("0")
                    if complete
                    else auto.auto_remaining_hedge_requirement_btc
                ),
            }
        )


def controller(
    actual: str,
    *,
    depth: str = "100",
    status: MarketConnectionStatus = MarketConnectionStatus.LIVE,
):
    trading = DemoTradingService()
    trading.desk_state = desk(actual)
    store = MutableStore(snapshot(spot_market(depth=depth, status=status)))
    fees = ExecutionFeeConfig(
        entries=(
            ExecutionFeeEntry(
                venue=MarketVenue.COINBASE,
                instrument_type=InstrumentType.SPOT,
                fee_bps=Decimal("2"),
                assumption_label="TEST_DEMO_FEE",
            ),
        )
    )
    optimizer = HedgeOptimizerService(store, fees)
    risk = ActiveAutoRisk(trading, store)
    service = AutoHedgeController(
        risk,
        optimizer,
        trading,
        fill_interval_seconds=Decimal("0"),
        retry_interval_seconds=Decimal("0"),
        now=lambda: NOW,
    )
    trigger(trading)
    return service, risk, store, trading


def trigger(trading: DemoTradingService, *, suffix: str = "") -> None:
    trading.record_system_event(
        EventType.AUTO_HEDGE_REQUIRED,
        aggregate_id="desk-btc-risk",
        correlation_id=BREACH_ID,
        payload={
            "breach_id": BREACH_ID,
            "auto_hedge_target_notional_usd": Decimal("900000"),
            "auto_remaining_hedge_requirement_btc": Decimal("26"),
            "test_suffix": suffix,
        },
    )


def move_actual(trading: DemoTradingService, actual: str) -> None:
    value = Decimal(actual)
    before = trading.desk_state
    trading.desk_state = before.model_copy(
        update={
            "version": before.version + 1,
            "spot_inventory_btc": value,
            "total_delta_btc": value + before.derivative_delta_btc,
        }
    )


def event_count(trading: DemoTradingService, event_type: EventType) -> int:
    return sum(event.event_type is event_type for event in trading.events)


def test_grace_period_without_required_event_remains_trader_controlled() -> None:
    service, _, _, trading = controller("-35")
    trading.events = []

    assert run(service.step()) is None
    assert trading.hedge_orders == {}


def test_stale_required_event_is_ignored_when_trader_already_reached_target() -> None:
    service, _, _, trading = controller("-35")
    move_actual(trading, "-9")

    assert run(service.step()) is None
    assert trading.hedge_orders == {}


@pytest.mark.parametrize("actual,side", [("-35", "BUY"), ("35", "SELL")])
def test_auto_risk_uses_900k_target_and_completes_symmetrically(
    actual: str,
    side: str,
) -> None:
    service, _, _, trading = controller(actual)

    started = run(service.step())
    assert started is not None
    assert started.status is AutoHedgeInterventionStatus.EXECUTING
    assert started.target_notional_usd == Decimal("900000.00")
    assert started.active_plan is not None
    assert started.active_plan.mode.value == "AUTO_RISK"
    assert abs(started.active_plan.target_delta_btc) == Decimal("9.00")
    assert started.active_plan.target_delta_btc != 0
    assert started.active_plan.legs[0].side.value == side
    assert len(trading.hedge_orders) == 1
    assert trading.hedge_fills == []

    partial = run(service.step())
    assert partial is not None
    assert partial.status is AutoHedgeInterventionStatus.EXECUTING
    assert len(trading.hedge_fills) == 1
    assert abs(trading.desk_state.total_delta_btc) > Decimal("9")

    completed = run(service.step())
    assert completed is not None
    assert completed.status is AutoHedgeInterventionStatus.COMPLETE
    assert abs(trading.desk_state.total_delta_btc) == Decimal("9.00")
    assert completed.current_exposure_usd == Decimal("900000.00")
    assert event_count(trading, EventType.AUTO_HEDGE_COMPLETE) == 1


def test_950k_does_not_complete_but_900k_does() -> None:
    service, _, _, trading = controller("9.5")
    at_950 = run(service.step())
    assert at_950 is not None
    assert at_950.status is AutoHedgeInterventionStatus.EXECUTING
    assert len(trading.hedge_orders) == 1

    move_actual(trading, "9")
    at_900 = run(service.step())
    assert at_900 is not None
    assert at_900.status is AutoHedgeInterventionStatus.COMPLETE
    assert all(
        order.status is HedgeOrderStatus.CANCELLED
        for order in trading.hedge_orders.values()
    )
    assert trading.desk_state.working_order_delta_btc == 0


def test_repeated_required_event_is_one_intervention_and_no_duplicate_orders() -> None:
    service, _, _, trading = controller("-35")
    trigger(trading, suffix="duplicate")
    first = run(service.step())
    second = run(service.step())

    assert first is not None and second is not None
    assert first.intervention_id == second.intervention_id
    assert event_count(trading, EventType.AUTO_HEDGE_STARTED) == 1
    assert event_count(trading, EventType.AUTO_HEDGE_PLAN_CREATED) == 1
    assert len(trading.hedge_orders) == 1


def test_reference_price_drift_does_not_churn_valid_working_auto_orders() -> None:
    service, risk, _, trading = controller("-35")
    initial = run(service.step())
    assert initial is not None
    assert len(trading.hedge_orders) == 1

    # A lower reference price moves the BTC target outward and makes the old
    # working quantity microscopically larger than the refreshed requirement.
    # It is still correctly directed and should fill, not be cancelled/rebuilt.
    risk.price = Decimal("99900")
    partial = run(service.step())
    completed = run(service.step())

    assert partial is not None and completed is not None
    assert completed.status is AutoHedgeInterventionStatus.COMPLETE
    assert len(completed.generated_plan_ids) == 1
    assert len(trading.hedge_orders) == 1
    order = next(iter(trading.hedge_orders.values()))
    assert order.status is HedgeOrderStatus.CANCELLED
    assert order.filled_quantity_btc > 0
    assert event_count(trading, EventType.AUTO_HEDGE_COMPLETE) == 1


def test_auto_orders_have_auditable_origin_and_identifiers() -> None:
    service, _, _, trading = controller("-35")
    intervention = run(service.step())
    assert intervention is not None
    order = next(iter(trading.hedge_orders.values()))

    assert order.origin is HedgeOrderOrigin.AUTO_RISK
    assert order.source_intervention_id == intervention.intervention_id
    assert order.source_breach_id == BREACH_ID
    assert order.source_plan_id == intervention.active_plan_id
    assert event_count(trading, EventType.AUTO_HEDGE_ORDER_CREATED) == 1


def test_helpful_client_flow_cancels_excess_orders_and_reoptimizes_latest_state() -> None:
    service, _, _, trading = controller("-35")
    initial = run(service.step())
    assert initial is not None and initial.active_plan is not None
    assert initial.active_plan.requested_hedge_delta_btc == Decimal("26.00")

    move_actual(trading, "-25")
    refreshed = run(service.step())
    assert refreshed is not None and refreshed.active_plan is not None
    assert refreshed.active_plan.requested_hedge_delta_btc == Decimal("16.00")
    assert len(refreshed.generated_plan_ids) == 2
    assert any(
        order.status is HedgeOrderStatus.CANCELLED
        for order in trading.hedge_orders.values()
    )


def test_worsening_client_flow_adds_only_the_latest_remaining_requirement() -> None:
    service, _, _, trading = controller("-35")
    initial = run(service.step())
    assert initial is not None and initial.active_plan is not None

    move_actual(trading, "-40")
    run(service.step())
    run(service.step())
    refreshed = run(service.step())
    assert refreshed is not None and refreshed.active_plan is not None
    assert refreshed.active_plan.requested_hedge_delta_btc == Decimal("5.00")
    assert len(refreshed.generated_plan_ids) == 2
    assert trading.desk_state.total_delta_btc == Decimal("-14.00")
    assert trading.desk_state.working_order_delta_btc == Decimal("5.00")


def test_disconnect_cancels_working_auto_orders_and_blocks_without_fabrication() -> None:
    service, _, store, trading = controller("-35")
    run(service.step())
    store.value = snapshot(
        spot_market(status=MarketConnectionStatus.DISCONNECTED),
        version=502,
    )

    blocked = run(service.step())
    assert blocked is not None
    assert blocked.status is AutoHedgeInterventionStatus.BLOCKED
    assert trading.desk_state.working_order_delta_btc == 0
    assert any(
        order.status is HedgeOrderStatus.CANCELLED
        for order in trading.hedge_orders.values()
    )
    assert event_count(trading, EventType.AUTO_HEDGE_BLOCKED) == 1


def test_partial_liquidity_reduces_risk_then_waits_incomplete_for_new_market_state() -> None:
    service, _, _, trading = controller("-35", depth="10")
    initial = run(service.step())
    assert initial is not None and initial.active_plan is not None
    assert initial.active_plan.status.value == "PARTIALLY_FEASIBLE"
    assert initial.active_plan.allocated_hedge_delta_btc == Decimal("10")

    run(service.step())
    incomplete = run(service.step())
    repeated = run(service.step())

    assert incomplete is not None and repeated is not None
    assert incomplete.status is AutoHedgeInterventionStatus.INCOMPLETE
    assert repeated.status is AutoHedgeInterventionStatus.INCOMPLETE
    assert trading.desk_state.total_delta_btc == Decimal("-25")
    assert event_count(trading, EventType.AUTO_HEDGE_INCOMPLETE) == 1
    assert event_count(trading, EventType.AUTO_HEDGE_PLAN_CREATED) == 1


def test_no_legitimate_liquidity_is_blocked_and_does_not_create_orders() -> None:
    service, _, _, trading = controller(
        "-35",
        status=MarketConnectionStatus.DISCONNECTED,
    )
    blocked = run(service.step())

    assert blocked is not None
    assert blocked.status is AutoHedgeInterventionStatus.BLOCKED
    assert trading.hedge_orders == {}
    assert trading.hedge_fills == []
    assert event_count(trading, EventType.AUTO_HEDGE_BLOCKED) == 1
