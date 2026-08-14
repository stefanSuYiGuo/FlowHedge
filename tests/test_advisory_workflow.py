from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal

from backend.advisory.models import AdvisoryLifecycleStatus
from backend.advisory.service import AdvisoryHedgeService
from backend.config import risk_policy_config
from backend.demo import DemoTradingService
from backend.domain.models import DeskState, EventType, InstrumentType
from backend.execution_cost.models import ExecutionFeeConfig, ExecutionFeeEntry
from backend.hedge_optimizer.models import HedgePlanStatus
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


NOW = datetime(2026, 8, 14, 18, 0, tzinfo=timezone.utc)
SNAPSHOT_VERSION = 91


def run(coroutine):
    return asyncio.run(coroutine)


def desk(
    actual: str,
    *,
    working: str = "0",
    version: int = 4,
) -> DeskState:
    actual_delta = Decimal(actual)
    return DeskState(
        version=version,
        as_of=NOW,
        spot_inventory_btc=actual_delta,
        derivative_delta_btc=Decimal("0"),
        total_delta_btc=actual_delta,
        working_order_delta_btc=Decimal(working),
    )


def assessment(state: DeskState):
    reference = RiskReferencePrice(
        asset="BTC",
        price_usd=Decimal("100000"),
        captured_at=NOW,
        source="TEST_SPOT_REFERENCE",
        market_snapshot_version=SNAPSHOT_VERSION,
        eligible=True,
        degraded=False,
    )
    return RiskPolicy(risk_policy_config).evaluate(
        state,
        reference,
        assessed_at=NOW,
    )


def spot_market(
    *,
    ask_depth: str = "50",
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
        bids=((Decimal("99990"), Decimal("50")),),
        asks=((Decimal("100010"), Decimal(ask_depth)),),
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


def snapshot(market: ExecutableBookView) -> ExecutableMarketSnapshot:
    return ExecutableMarketSnapshot(
        snapshot_version=SNAPSHOT_VERSION,
        captured_at=NOW,
        base_asset="BTC",
        markets=(market,),
    )


class MutableStore:
    def __init__(self, value: ExecutableMarketSnapshot) -> None:
        self.value = value

    async def executable_snapshot(self, _: str) -> ExecutableMarketSnapshot:
        return self.value


class MutableRisk:
    def __init__(self, value) -> None:
        self.value = value

    async def assess(self):
        return self.value


def workflow(
    state: DeskState,
    *,
    market: ExecutableBookView | None = None,
    configured_fees: bool = True,
):
    trading = DemoTradingService()
    trading.desk_state = state
    risk = MutableRisk(assessment(state))
    store = MutableStore(snapshot(market or spot_market()))
    fees = (
        ExecutionFeeConfig(
            entries=(
                ExecutionFeeEntry(
                    venue=MarketVenue.COINBASE,
                    instrument_type=InstrumentType.SPOT,
                    fee_bps=Decimal("0"),
                    assumption_label="TEST_ZERO_FEE",
                ),
            )
        )
        if configured_fees
        else ExecutionFeeConfig()
    )
    optimizer = HedgeOptimizerService(store, fees)
    service = AdvisoryHedgeService(
        risk,
        optimizer,
        trading,
        expected_holding_seconds=None,
    )
    return service, risk, store, trading


def event_types(trading: DemoTradingService) -> list[EventType]:
    return [event.event_type for event in trading.events]


def test_green_has_no_recommendation_and_yellow_targets_soft_boundary() -> None:
    green_service, green_risk, _, _ = workflow(desk("-5"))
    green = run(green_service.recommendation(green_risk.value))
    assert green.lifecycle_status is AdvisoryLifecycleStatus.NOT_REQUIRED
    assert green.plan is None

    yellow_service, yellow_risk, _, trading = workflow(desk("-20"))
    yellow = run(yellow_service.recommendation(yellow_risk.value))
    assert yellow.lifecycle_status is AdvisoryLifecycleStatus.AVAILABLE
    assert yellow.plan is not None
    assert yellow.plan.mode.value == "ADVISORY"
    assert yellow.plan.target_delta_btc == Decimal("-10")
    assert yellow.plan.requested_hedge_delta_btc == Decimal("10")
    assert yellow.plan.status is HedgePlanStatus.FULLY_FEASIBLE
    assert EventType.HEDGE_PLAN_GENERATED in event_types(trading)


def test_red_grace_plan_uses_soft_target_not_auto_target() -> None:
    service, risk, _, _ = workflow(desk("-35"))
    risk.value = risk.value.model_copy(
        update={
            "hard_breach_id": "breach-test",
            "hard_breach_started_at": NOW,
            "hard_breach_seconds_remaining": Decimal("5"),
            "auto_hedge_required": False,
        }
    )
    recommendation = run(service.recommendation(risk.value))
    assert recommendation.plan is not None
    assert recommendation.plan.target_delta_btc == Decimal("-10")
    assert risk.value.auto_hedge_target_delta_btc == Decimal("-9")
    assert recommendation.plan.target_delta_btc != risk.value.auto_hedge_target_delta_btc


def test_use_system_plan_creates_orders_not_fills_and_is_idempotent() -> None:
    service, risk, _, trading = workflow(desk("-20"))
    recommendation = run(service.recommendation(risk.value))
    assert recommendation.plan is not None

    first = run(service.accept(recommendation.plan.plan_id))
    second = run(service.accept(recommendation.plan.plan_id))

    assert first.replayed is False
    assert second.replayed is True
    assert len(first.orders) == 1
    assert len(trading.hedge_orders) == 1
    assert trading.hedge_fills == []
    assert first.orders[0].origin.value == "SYSTEM_ADVISORY"
    assert first.orders[0].venue == "COINBASE"
    assert first.orders[0].source_plan_id == recommendation.plan.plan_id
    assert event_types(trading).count(EventType.HEDGE_PLAN_ACCEPTED) == 1
    assert event_types(trading).count(EventType.HEDGE_PLAN_EXECUTION_STARTED) == 1


def test_fill_or_new_client_state_invalidates_old_plan() -> None:
    service, risk, _, trading = workflow(desk("-20"))
    recommendation = run(service.recommendation(risk.value))
    assert recommendation.plan is not None
    batch = run(service.accept(recommendation.plan.plan_id))

    trading.simulate_hedge_fill(
        batch.orders[0].hedge_order_id,
        Decimal("2"),
        "advisory-partial-fill",
    )
    risk.value = assessment(trading.desk_state)
    after_fill = run(service.recommendation(risk.value))
    assert after_fill.lifecycle_status is AdvisoryLifecycleStatus.NOT_REQUIRED
    assert EventType.HEDGE_PLAN_STALE in event_types(trading)

    second_service, second_risk, _, second_trading = workflow(desk("-20"))
    original = run(second_service.recommendation(second_risk.value))
    second_trading.desk_state = desk("-22", version=5)
    second_risk.value = assessment(second_trading.desk_state)
    regenerated = run(second_service.recommendation(second_risk.value))
    assert original.plan is not None and regenerated.plan is not None
    assert regenerated.plan.plan_id != original.plan.plan_id
    assert EventType.HEDGE_PLAN_STALE in event_types(second_trading)


def test_conflict_partial_and_no_feasible_states_are_honest() -> None:
    blocked_service, blocked_risk, _, _ = workflow(desk("-20", working="-1"))
    blocked = run(blocked_service.recommendation(blocked_risk.value))
    assert blocked.lifecycle_status is AdvisoryLifecycleStatus.BLOCKED
    assert blocked.plan is not None
    assert blocked.can_use_system_plan is False

    partial_service, partial_risk, _, _ = workflow(
        desk("-20"), market=spot_market(ask_depth="4")
    )
    partial = run(partial_service.recommendation(partial_risk.value))
    assert partial.lifecycle_status is AdvisoryLifecycleStatus.PARTIALLY_FEASIBLE
    assert partial.plan is not None
    assert partial.plan.allocated_hedge_delta_btc == Decimal("4")
    assert partial.plan.residual_unallocated_delta_btc == Decimal("6")

    unavailable_service, unavailable_risk, _, _ = workflow(
        desk("-20"), configured_fees=False
    )
    unavailable = run(unavailable_service.recommendation(unavailable_risk.value))
    assert unavailable.lifecycle_status is AdvisoryLifecycleStatus.NO_FEASIBLE_HEDGE
    assert unavailable.plan is not None
    assert unavailable.plan.legs == ()


def test_manual_override_is_sticky_until_material_state_changes() -> None:
    service, risk, _, trading = workflow(desk("-20"))
    recommendation = run(service.recommendation(risk.value))
    assert recommendation.plan is not None

    rejected = run(service.reject(recommendation.plan.plan_id))
    repeated = run(service.recommendation(risk.value))
    assert rejected.lifecycle_status is AdvisoryLifecycleStatus.REJECTED
    assert repeated.lifecycle_status is AdvisoryLifecycleStatus.REJECTED
    assert trading.hedge_orders == {}
    assert EventType.HEDGE_PLAN_REJECTED in event_types(trading)

    trading.desk_state = desk("-21", version=5)
    risk.value = assessment(trading.desk_state)
    refreshed = run(service.recommendation(risk.value))
    assert refreshed.lifecycle_status is AdvisoryLifecycleStatus.AVAILABLE
    assert refreshed.plan is not None
    assert refreshed.plan.plan_id != recommendation.plan.plan_id


def test_market_eligibility_change_invalidates_and_recomputes_plan() -> None:
    service, risk, store, trading = workflow(desk("-20"))
    first = run(service.recommendation(risk.value))
    assert first.lifecycle_status is AdvisoryLifecycleStatus.AVAILABLE

    store.value = snapshot(
        spot_market(status=MarketConnectionStatus.DISCONNECTED)
    )
    second = run(service.recommendation(risk.value))
    assert second.lifecycle_status is AdvisoryLifecycleStatus.NO_FEASIBLE_HEDGE
    assert second.plan is not None and second.plan.legs == ()
    assert EventType.HEDGE_PLAN_STALE in event_types(trading)


def test_auto_required_ends_advisory_without_executing() -> None:
    service, risk, _, trading = workflow(desk("-35"))
    initial = run(service.recommendation(risk.value))
    assert initial.plan is not None
    risk.value = risk.value.model_copy(update={"auto_hedge_required": True})

    handoff = run(service.recommendation(risk.value))
    assert handoff.lifecycle_status is AdvisoryLifecycleStatus.AUTO_HANDOFF_PENDING
    assert handoff.plan is None
    assert trading.hedge_orders == {}
    assert EventType.HEDGE_PLAN_STALE in event_types(trading)
