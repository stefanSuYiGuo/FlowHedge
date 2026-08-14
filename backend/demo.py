"""Deterministic Step 2 scenario for verifying the accounting chain."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from .domain.accounting import (
    apply_client_trade,
    apply_hedge_fill,
    signed_hedge_delta,
)
from .domain.models import (
    ClientSide,
    ClientTrade,
    DemoScenarioResult,
    DeskState,
    Event,
    EventType,
    HedgeFill,
    HedgeFillResult,
    HedgeCancellationResult,
    HedgeOrder,
    HedgeOrderBatchResult,
    HedgeOrderOrigin,
    HedgeOrderStatus,
    HedgeSide,
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
FIXED_SPOT_HEDGE_BUY_PRICE_USD = Decimal("118005")
FIXED_SPOT_HEDGE_SELL_PRICE_USD = Decimal("117995")
FIXED_PERP_HEDGE_LONG_PRICE_USD = Decimal("118010")
FIXED_PERP_HEDGE_SHORT_PRICE_USD = Decimal("117990")
STEP_4_DEMO_TARGET_DELTA_BTC = Decimal("0")
STEP_4_QUANTITY_INCREMENT_BTC = Decimal("0.01")


class DemoStateError(ValueError):
    """Raised when a demo action is incompatible with the current state."""


class HedgeAllocationError(ValueError):
    """Raised when the manual Spot/Perp split cannot reach the demo target."""


class HedgeFillError(ValueError):
    """Raised when a simulated fill is invalid for its hedge order."""


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
        self.hedge_orders: dict[str, HedgeOrder] = {}
        self.hedge_fills: list[HedgeFill] = []
        self.processed_fill_results: dict[str, HedgeFillResult] = {}
        self.saved_result: DemoScenarioResult | None = None
        self.saved_order_batch: HedgeOrderBatchResult | None = None
        self.hedge_order_revision = 0
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

    def create_manual_hedge_orders(
        self,
        spot_quantity_btc: Decimal,
        batch_id: str,
    ) -> HedgeOrderBatchResult:
        """Create a manual Spot hedge and calculate the exact Perp remainder."""

        if spot_quantity_btc < 0:
            raise HedgeAllocationError("spot hedge quantity cannot be negative")
        if spot_quantity_btc != spot_quantity_btc.quantize(
            STEP_4_QUANTITY_INCREMENT_BTC
        ):
            raise HedgeAllocationError(
                "spot hedge quantity supports at most two decimal places"
            )

        if self.saved_order_batch is not None:
            original_spot = sum(
                order.quantity_btc
                for order in self.saved_order_batch.orders
                if order.instrument_type is InstrumentType.SPOT
            )
            if (
                self.saved_order_batch.batch_id == batch_id
                and original_spot == spot_quantity_btc
            ):
                return self.saved_order_batch.model_copy(update={"replayed": True})
            raise DemoStateError(
                "hedge orders already exist; cancel untouched orders or reset the demo"
            )

        if self.saved_result is None:
            raise DemoStateError("book the fixed client trade before creating hedge orders")

        before = self.desk_state.model_copy(deep=True)
        required_hedge_delta = STEP_4_DEMO_TARGET_DELTA_BTC - before.total_delta_btc

        if required_hedge_delta == 0:
            raise DemoStateError("the desk is already at the Step 4 demo target")
        if spot_quantity_btc > abs(required_hedge_delta):
            raise HedgeAllocationError(
                "spot hedge quantity cannot exceed the absolute required hedge delta "
                f"({abs(required_hedge_delta)} BTC)"
            )
        perp_quantity_btc = abs(required_hedge_delta) - spot_quantity_btc

        created_at = utc_now()
        increasing_delta = required_hedge_delta > 0
        orders: list[HedgeOrder] = []
        self.hedge_order_revision += 1
        revision = self.hedge_order_revision

        if spot_quantity_btc > 0:
            orders.append(
                HedgeOrder(
                    hedge_order_id=f"hedge-order-step4-spot-{revision:03}",
                    batch_id=batch_id,
                    origin=HedgeOrderOrigin.MANUAL,
                    venue="DEMO-SPOT",
                    instrument_id="BTC-USD",
                    instrument_type=InstrumentType.SPOT,
                    side=HedgeSide.BUY if increasing_delta else HedgeSide.SELL,
                    quantity_btc=spot_quantity_btc,
                    filled_quantity_btc=Decimal("0"),
                    remaining_quantity_btc=spot_quantity_btc,
                    status=HedgeOrderStatus.OPEN,
                    created_at=created_at,
                    created_desk_state_version=before.version,
                )
            )
        if perp_quantity_btc > 0:
            orders.append(
                HedgeOrder(
                    hedge_order_id=f"hedge-order-step4-perp-{revision:03}",
                    batch_id=batch_id,
                    origin=HedgeOrderOrigin.MANUAL,
                    venue="DEMO-PERP",
                    instrument_id="BTC-USD-PERP",
                    instrument_type=InstrumentType.PERPETUAL,
                    side=HedgeSide.LONG if increasing_delta else HedgeSide.SHORT,
                    quantity_btc=perp_quantity_btc,
                    filled_quantity_btc=Decimal("0"),
                    remaining_quantity_btc=perp_quantity_btc,
                    status=HedgeOrderStatus.OPEN,
                    created_at=created_at,
                    created_desk_state_version=before.version,
                )
            )

        self.hedge_orders = {order.hedge_order_id: order for order in orders}
        self.desk_state = DeskState(
            version=before.version + 1,
            as_of=created_at,
            spot_inventory_btc=before.spot_inventory_btc,
            derivative_delta_btc=before.derivative_delta_btc,
            total_delta_btc=before.total_delta_btc,
            open_hedge_order_ids=tuple(order.hedge_order_id for order in orders),
            working_order_delta_btc=required_hedge_delta,
        )

        batch_events: list[Event] = []
        for order in orders:
            batch_events.append(
                self._event(
                    EventType.HEDGE_ORDER_CREATED,
                    order.hedge_order_id,
                    batch_id,
                    before.version,
                    self.desk_state.version,
                    {
                        "instrument_type": order.instrument_type,
                        "side": order.side,
                        "quantity_btc": order.quantity_btc,
                        "origin": order.origin,
                    },
                )
            )
        batch_events.append(
            self._position_event(
                batch_id,
                before.version,
                self.desk_state.version,
                reason="HEDGE_ORDERS_REGISTERED",
            )
        )

        self.saved_order_batch = HedgeOrderBatchResult(
            replayed=False,
            batch_id=batch_id,
            demo_target_total_delta_btc=STEP_4_DEMO_TARGET_DELTA_BTC,
            required_hedge_delta_btc=required_hedge_delta,
            orders=tuple(orders),
            desk_state_before=before,
            desk_state_after=self.desk_state,
            events=tuple(batch_events),
        )
        return self.saved_order_batch

    def cancel_unfilled_hedge_orders(self) -> HedgeCancellationResult:
        """Cancel an untouched demo hedge batch so the allocation can be revised."""

        if not self.hedge_orders:
            raise DemoStateError("there are no hedge orders to cancel")
        if self.hedge_fills or any(
            order.filled_quantity_btc > 0 for order in self.hedge_orders.values()
        ):
            raise DemoStateError(
                "hedge orders with fills cannot be revised; reset the full demo instead"
            )

        before = self.desk_state.model_copy(deep=True)
        cancelled_order_ids = tuple(self.hedge_orders)
        correlation_id = self.saved_order_batch.batch_id if self.saved_order_batch else ""
        self.hedge_orders = {}
        self.saved_order_batch = None
        cancelled_at = utc_now()
        self.desk_state = DeskState(
            version=before.version + 1,
            as_of=cancelled_at,
            spot_inventory_btc=before.spot_inventory_btc,
            derivative_delta_btc=before.derivative_delta_btc,
            total_delta_btc=before.total_delta_btc,
            open_hedge_order_ids=(),
            working_order_delta_btc=Decimal("0"),
        )
        cancellation_events = (
            self._event(
                EventType.HEDGE_ORDERS_CANCELLED,
                "desk-btc-hedges",
                correlation_id,
                before.version,
                self.desk_state.version,
                {"cancelled_hedge_order_ids": cancelled_order_ids},
            ),
            self._position_event(
                correlation_id,
                before.version,
                self.desk_state.version,
                reason="UNFILLED_HEDGE_ORDERS_CANCELLED",
            ),
        )
        return HedgeCancellationResult(
            cancelled_hedge_order_ids=cancelled_order_ids,
            desk_state_before=before,
            desk_state_after=self.desk_state,
            events=cancellation_events,
        )

    def simulate_hedge_fill(
        self,
        hedge_order_id: str,
        quantity_btc: Decimal,
        hedge_fill_id: str,
    ) -> HedgeFillResult:
        """Apply one idempotent simulated fill to an existing hedge order."""

        if hedge_fill_id in self.processed_fill_results:
            previous = self.processed_fill_results[hedge_fill_id]
            if previous.fill.hedge_order_id != hedge_order_id:
                raise HedgeFillError("hedge_fill_id is already used for another order")
            return previous.model_copy(update={"replayed": True})
        if quantity_btc <= 0:
            raise HedgeFillError("fill quantity must be positive")
        if quantity_btc != quantity_btc.quantize(STEP_4_QUANTITY_INCREMENT_BTC):
            raise HedgeFillError("fill quantity supports at most two decimal places")

        order = self.hedge_orders.get(hedge_order_id)
        if order is None:
            raise DemoStateError(f"unknown hedge order: {hedge_order_id}")
        if quantity_btc > order.remaining_quantity_btc:
            raise HedgeFillError(
                f"fill quantity exceeds remaining quantity ({order.remaining_quantity_btc} BTC)"
            )

        filled_at = utc_now()
        fill = HedgeFill(
            hedge_fill_id=hedge_fill_id,
            hedge_order_id=order.hedge_order_id,
            instrument_id=order.instrument_id,
            instrument_type=order.instrument_type,
            side=order.side,
            quantity_btc=quantity_btc,
            fill_price_usd=self._fixed_hedge_fill_price(order),
            filled_at=filled_at,
            execution_source="FIXED_STEP_4_SIMULATION",
        )

        new_filled_quantity = order.filled_quantity_btc + quantity_btc
        new_remaining_quantity = order.quantity_btc - new_filled_quantity
        new_status = (
            HedgeOrderStatus.FILLED
            if new_remaining_quantity == 0
            else HedgeOrderStatus.PARTIALLY_FILLED
        )
        updated_order = order.model_copy(
            update={
                "filled_quantity_btc": new_filled_quantity,
                "remaining_quantity_btc": new_remaining_quantity,
                "status": new_status,
            }
        )
        self.hedge_orders[hedge_order_id] = updated_order

        open_orders = tuple(
            candidate
            for candidate in self.hedge_orders.values()
            if candidate.status is not HedgeOrderStatus.FILLED
        )
        working_order_delta = sum(
            (
                signed_hedge_delta(candidate.side, candidate.remaining_quantity_btc)
                for candidate in open_orders
            ),
            Decimal("0"),
        )
        before = self.desk_state.model_copy(deep=True)
        self.desk_state = apply_hedge_fill(
            before,
            fill,
            open_hedge_order_ids=tuple(
                candidate.hedge_order_id for candidate in open_orders
            ),
            working_order_delta_btc=working_order_delta,
        )
        self.hedge_fills.append(fill)

        fill_events = (
            self._event(
                EventType.HEDGE_FILL,
                fill.hedge_fill_id,
                order.batch_id,
                before.version,
                self.desk_state.version,
                {
                    "hedge_order_id": order.hedge_order_id,
                    "instrument_type": fill.instrument_type,
                    "side": fill.side,
                    "quantity_btc": fill.quantity_btc,
                    "fill_price_usd": fill.fill_price_usd,
                },
            ),
            self._event(
                EventType.HEDGE_ORDER_UPDATED,
                order.hedge_order_id,
                order.batch_id,
                before.version,
                self.desk_state.version,
                {
                    "status": updated_order.status,
                    "filled_quantity_btc": updated_order.filled_quantity_btc,
                    "remaining_quantity_btc": updated_order.remaining_quantity_btc,
                },
            ),
            self._position_event(
                order.batch_id,
                before.version,
                self.desk_state.version,
                reason="HEDGE_FILL_APPLIED",
            ),
        )
        result = HedgeFillResult(
            replayed=False,
            fill=fill,
            order=updated_order,
            desk_state_before=before,
            desk_state_after=self.desk_state,
            events=fill_events,
        )
        self.processed_fill_results[hedge_fill_id] = result
        return result

    def _position_event(
        self,
        correlation_id: str,
        before_version: int,
        after_version: int,
        *,
        reason: str,
    ) -> Event:
        return self._event(
            EventType.POSITION_UPDATED,
            "desk-btc",
            correlation_id,
            before_version,
            after_version,
            {
                "reason": reason,
                "spot_inventory_btc": self.desk_state.spot_inventory_btc,
                "derivative_delta_btc": self.desk_state.derivative_delta_btc,
                "total_delta_btc": self.desk_state.total_delta_btc,
                "working_order_delta_btc": self.desk_state.working_order_delta_btc,
                "projected_total_delta_btc": (
                    self.desk_state.total_delta_btc
                    + self.desk_state.working_order_delta_btc
                ),
            },
        )

    @staticmethod
    def _fixed_hedge_fill_price(order: HedgeOrder) -> Decimal:
        if order.instrument_type is InstrumentType.SPOT:
            return (
                FIXED_SPOT_HEDGE_BUY_PRICE_USD
                if order.side is HedgeSide.BUY
                else FIXED_SPOT_HEDGE_SELL_PRICE_USD
            )
        return (
            FIXED_PERP_HEDGE_LONG_PRICE_USD
            if order.side is HedgeSide.LONG
            else FIXED_PERP_HEDGE_SHORT_PRICE_USD
        )


demo_service = DemoTradingService()
