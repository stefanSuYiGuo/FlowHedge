"""Deterministic Step 2 scenario for verifying the accounting chain."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from .domain.accounting import apply_client_trade
from .domain.models import (
    ClientSide,
    ClientTrade,
    DemoScenarioResult,
    DeskState,
    Event,
    EventType,
    InstrumentType,
    MarketObservation,
    MarketSnapshot,
    Quote,
    QuoteStatus,
    RFQ,
    RFQStatus,
)
from .domain.validation import validate_client_rfq_notional


FIXED_REFERENCE_PRICE_USD = Decimal("118000")
FIXED_CLIENT_QUOTE_PRICE_USD = Decimal("118087")
FIXED_RFQ_QUANTITY_BTC = Decimal("5")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class DemoTradingService:
    """Small in-memory service for one repeatable RFQ-to-desk-state scenario."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> DeskState:
        now = utc_now()
        self.desk_state = DeskState(
            version=0,
            as_of=now,
            spot_inventory_btc=Decimal("0"),
            derivative_delta_btc=Decimal("0"),
            total_delta_btc=Decimal("0"),
        )
        self.events: list[Event] = []
        self.processed_trade_ids: set[str] = set()
        self.saved_result: DemoScenarioResult | None = None
        return self.desk_state

    def _event(
        self,
        event_type: EventType,
        aggregate_id: str,
        correlation_id: str,
        before_version: int,
        after_version: int,
        payload: dict[str, object] | None = None,
    ) -> Event:
        event = Event(
            event_id=f"evt-{uuid4().hex[:12]}",
            event_type=event_type,
            occurred_at=utc_now(),
            aggregate_id=aggregate_id,
            correlation_id=correlation_id,
            desk_state_version_before=before_version,
            desk_state_version_after=after_version,
            payload=payload or {},
        )
        self.events.append(event)
        return event

    def run_fixed_client_trade(self) -> DemoScenarioResult:
        """Run the fixed valid RFQ once; subsequent calls are idempotent replays."""

        if self.saved_result is not None:
            return self.saved_result.model_copy(update={"replayed": True})

        started_at = utc_now()
        correlation_id = f"flow-{uuid4().hex[:10]}"
        snapshot = MarketSnapshot(
            market_snapshot_id="market-step2-v1",
            version=1,
            captured_at=started_at,
            base_asset="BTC",
            quote_currency="USD",
            reference_price_usd=FIXED_REFERENCE_PRICE_USD,
            observations=(
                MarketObservation(
                    venue="DEMO",
                    instrument_id="BTC-USD",
                    instrument_type=InstrumentType.SPOT,
                    bid=Decimal("117995"),
                    ask=Decimal("118005"),
                    observed_at=started_at,
                ),
            ),
        )

        notional_usd = validate_client_rfq_notional(
            FIXED_RFQ_QUANTITY_BTC, snapshot.reference_price_usd
        )
        rfq = RFQ(
            rfq_id="rfq-step2-001",
            client_id="INST-042",
            instrument_id="BTC-USD",
            client_side=ClientSide.BUY,
            quantity_btc=FIXED_RFQ_QUANTITY_BTC,
            received_at=started_at,
            status=RFQStatus.RECEIVED,
            validation_market_snapshot_id=snapshot.market_snapshot_id,
            validation_reference_price_usd=snapshot.reference_price_usd,
            validated_notional_usd=notional_usd,
        )
        scenario_events: list[Event] = []
        scenario_events.append(
            self._event(
                EventType.RFQ_RECEIVED,
                rfq.rfq_id,
                correlation_id,
                self.desk_state.version,
                self.desk_state.version,
                {"client_side": rfq.client_side, "quantity_btc": rfq.quantity_btc},
            )
        )
        scenario_events.append(
            self._event(
                EventType.RFQ_VALIDATED,
                rfq.rfq_id,
                correlation_id,
                self.desk_state.version,
                self.desk_state.version,
                {"notional_usd": notional_usd, "rule": "notional_usd > 500000"},
            )
        )

        rfq = rfq.model_copy(update={"status": RFQStatus.PRICING})
        quote = Quote(
            quote_id="quote-step2-001-r1",
            rfq_id=rfq.rfq_id,
            revision=1,
            quoted_price_usd=FIXED_CLIENT_QUOTE_PRICE_USD,
            quantity_btc=rfq.quantity_btc,
            created_at=utc_now(),
            expires_at=utc_now() + timedelta(seconds=5),
            status=QuoteStatus.ACTIVE,
            market_snapshot_id=snapshot.market_snapshot_id,
            desk_state_version=self.desk_state.version,
            pricing_source="FIXED_STEP_2_FIXTURE",
        )
        rfq = rfq.model_copy(update={"status": RFQStatus.QUOTED})
        scenario_events.append(
            self._event(
                EventType.QUOTE_GENERATED,
                quote.quote_id,
                correlation_id,
                self.desk_state.version,
                self.desk_state.version,
                {"quoted_price_usd": quote.quoted_price_usd},
            )
        )

        quote = quote.model_copy(update={"status": QuoteStatus.ACCEPTED})
        scenario_events.append(
            self._event(
                EventType.QUOTE_ACCEPTED,
                quote.quote_id,
                correlation_id,
                self.desk_state.version,
                self.desk_state.version,
                {"acceptance_model": "AUTO_ACCEPT"},
            )
        )

        trade = ClientTrade(
            client_trade_id="client-trade-step2-001",
            rfq_id=rfq.rfq_id,
            quote_id=quote.quote_id,
            client_id=rfq.client_id,
            instrument_id=rfq.instrument_id,
            client_side=rfq.client_side,
            quantity_btc=rfq.quantity_btc,
            trade_price_usd=quote.quoted_price_usd,
            traded_at=utc_now(),
        )
        before = self.desk_state.model_copy(deep=True)
        if trade.client_trade_id not in self.processed_trade_ids:
            self.desk_state = apply_client_trade(self.desk_state, trade)
            self.processed_trade_ids.add(trade.client_trade_id)
        rfq = rfq.model_copy(update={"status": RFQStatus.FILLED})
        scenario_events.append(
            self._event(
                EventType.CLIENT_FILL,
                trade.client_trade_id,
                correlation_id,
                before.version,
                self.desk_state.version,
                {
                    "client_side": trade.client_side,
                    "quantity_btc": trade.quantity_btc,
                    "desk_spot_change_btc": -trade.quantity_btc,
                },
            )
        )
        scenario_events.append(
            self._event(
                EventType.POSITION_UPDATED,
                "desk-btc",
                correlation_id,
                before.version,
                self.desk_state.version,
                {
                    "spot_inventory_btc": self.desk_state.spot_inventory_btc,
                    "derivative_delta_btc": self.desk_state.derivative_delta_btc,
                    "total_delta_btc": self.desk_state.total_delta_btc,
                },
            )
        )

        self.saved_result = DemoScenarioResult(
            replayed=False,
            market_snapshot=snapshot,
            rfq=rfq,
            quote=quote,
            client_trade=trade,
            desk_state_before=before,
            desk_state_after=self.desk_state,
            events=tuple(scenario_events),
        )
        return self.saved_result


demo_service = DemoTradingService()
