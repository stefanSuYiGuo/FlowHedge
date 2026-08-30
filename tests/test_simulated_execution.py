from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from backend.demo import DemoTradingService
from backend.domain.models import InstrumentType
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
from backend.simulated_execution.models import (
    ManualHedgeLegRequest,
    ManualHedgePreviewRequest,
)
from backend.simulated_execution.service import (
    ManualExecutionValidationError,
    SimulatedExecutionService,
)


NOW = datetime.now(timezone.utc)


def run(coroutine):
    return asyncio.run(coroutine)


def snapshot() -> ExecutableMarketSnapshot:
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
        contract_structure=ContractStructure.SPOT,
        native_quantity_unit="BTC",
        settlement_asset="USD",
        received_at=NOW,
    )
    _, book = normalized_books_from_levels(
        rules=rules,
        bids=((Decimal("99990"), Decimal("1")), (Decimal("99980"), Decimal("3"))),
        asks=((Decimal("100010"), Decimal("1")), (Decimal("100020"), Decimal("3"))),
        exchange_timestamp=NOW,
        received_at=NOW,
    )
    market = ExecutableBookView(
        venue=MarketVenue.COINBASE,
        symbol="BTC-USD",
        instrument_type=InstrumentType.SPOT,
        connection=MarketConnectionState(
            feed_id="coinbase-spot",
            venue=MarketVenue.COINBASE,
            status=MarketConnectionStatus.LIVE,
            endpoint="public",
            connected_at=NOW,
            last_message_at=NOW,
            last_book_update_at=NOW,
        ),
        book=book,
        instrument=rules,
        book_data_age_ms=0,
        eligible=True,
        as_of=NOW,
    )
    return ExecutableMarketSnapshot(
        snapshot_version=12,
        captured_at=NOW,
        base_asset="BTC",
        markets=(market,),
    )


class StaticStore:
    async def executable_snapshot(self, _base_asset):
        return snapshot()


def assessment():
    return SimpleNamespace(
        auto_hedge_active=False,
        auto_hedge_required=False,
        advisory_target_delta_btc=Decimal("-1"),
    )


def request(quantity: str) -> ManualHedgePreviewRequest:
    return ManualHedgePreviewRequest(
        request_id="manual-test",
        legs=(
            ManualHedgeLegRequest(
                venue=MarketVenue.COINBASE,
                instrument_type=InstrumentType.SPOT,
                quantity_btc=Decimal(quantity),
            ),
        ),
    )


def test_manual_preview_submit_and_execution_use_live_l2_metrics() -> None:
    trading = DemoTradingService()
    trading.run_fixed_client_trade()
    service = SimulatedExecutionService(StaticStore(), trading)  # type: ignore[arg-type]

    preview = run(service.preview(request("2.00"), assessment()))
    assert preview.can_submit is True
    assert preview.submitted_hedge_delta_btc == Decimal("2.00")
    assert preview.legs[0].expected_vwap_usd == Decimal("100015")

    submission = run(service.submit(preview.preview_id))
    assert submission.order_batch.orders[0].venue == "COINBASE"
    assert trading.desk_state.total_delta_btc == Decimal("-5")
    assert trading.desk_state.working_order_delta_btc == Decimal("2.00")

    metrics = run(
        service.execute_batch(
            submission.order_batch.batch_id,
            "manual-test-execution",
        )
    )
    assert metrics.status == "FILLED"
    assert metrics.filled_quantity_btc == Decimal("2.00")
    assert metrics.realized_vwap_usd == Decimal("100015")
    assert metrics.fee_usd > 0
    assert metrics.orders[0].execution_source == "COINBASE · SPOT · L2 SIMULATION"
    assert trading.desk_state.total_delta_btc == Decimal("-3.00")
    assert trading.desk_state.working_order_delta_btc == 0

    replay = run(
        service.execute_batch(
            submission.order_batch.batch_id,
            "manual-test-execution",
        )
    )
    assert replay == metrics
    assert len(trading.hedge_fills) == 1


@pytest.mark.parametrize("quantity", ["5.01", "0.001"])
def test_manual_allocation_rejects_overhedge_and_excess_precision(quantity: str) -> None:
    trading = DemoTradingService()
    trading.run_fixed_client_trade()
    service = SimulatedExecutionService(StaticStore(), trading)  # type: ignore[arg-type]

    with pytest.raises(ManualExecutionValidationError):
        run(service.preview(request(quantity), assessment()))
