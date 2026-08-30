"""Step 11 trader routing and common L2-backed simulated execution service."""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from ..demo import DemoStateError, DemoTradingService, demo_service, utc_now
from ..domain.models import HedgeOrderStatus, HedgeSide, InstrumentType
from ..execution_cost.config import execution_fee_config
from ..execution_cost.engine import estimate_execution_cost
from ..execution_cost.models import ExecutionCostRequest, ExecutionCostStatus, ExecutionSide
from ..market.models import ExecutableBookView, ExecutableMarketSnapshot, MarketVenue
from ..market.service import market_state_store
from ..market.store import InMemoryMarketStateStore
from ..risk.models import RiskAssessment
from .models import (
    ExecutionBatchMetrics,
    ExecutionBatchStatus,
    ExecutionOrderMetrics,
    ManualExecutionLegPreview,
    ManualHedgePreview,
    ManualHedgePreviewRequest,
    ManualHedgeSubmission,
)


PREVIEW_TTL_SECONDS = 30
QUANTITY_INCREMENT_BTC = Decimal("0.01")
SUPPORTED_MANUAL_MARKETS = {
    (MarketVenue.COINBASE, InstrumentType.SPOT),
    (MarketVenue.KRAKEN, InstrumentType.SPOT),
    (MarketVenue.OKX, InstrumentType.SPOT),
    (MarketVenue.OKX, InstrumentType.PERPETUAL),
}
WORKING_STATUSES = {HedgeOrderStatus.OPEN, HedgeOrderStatus.PARTIALLY_FILLED}


class ManualExecutionValidationError(ValueError):
    """Raised for an invalid trader allocation."""


class ManualExecutionStateError(ValueError):
    """Raised when a preview or batch is stale/incompatible with desk state."""


class SimulatedExecutionService:
    def __init__(self, store: InMemoryMarketStateStore, trading: DemoTradingService) -> None:
        self._store = store
        self._trading = trading
        self.reset()

    def reset(self) -> None:
        self._previews: dict[str, ManualHedgePreview] = {}
        self._executions: dict[str, ExecutionBatchMetrics] = {}
        self._batch_metrics: dict[str, ExecutionBatchMetrics] = {}

    @property
    def batch_metrics(self) -> tuple[ExecutionBatchMetrics, ...]:
        return tuple(self._batch_metrics.values())

    async def preview(
        self,
        request: ManualHedgePreviewRequest,
        assessment: RiskAssessment,
    ) -> ManualHedgePreview:
        desk = self._trading.desk_state
        if self._trading.saved_result is None:
            raise ManualExecutionStateError("book a client trade before previewing a hedge")
        if desk.total_delta_btc == 0:
            raise ManualExecutionStateError("the desk has no exposure to hedge")
        if desk.open_hedge_order_ids or desk.working_order_delta_btc != 0:
            raise ManualExecutionStateError("working hedge orders already exist")
        if assessment.auto_hedge_active or assessment.auto_hedge_required:
            raise ManualExecutionStateError("Auto Risk Control owns the current exposure")

        identities = [(leg.venue, leg.instrument_type) for leg in request.legs]
        if len(identities) != len(set(identities)):
            raise ManualExecutionValidationError("each venue/instrument may appear only once")
        for leg in request.legs:
            if (leg.venue, leg.instrument_type) not in SUPPORTED_MANUAL_MARKETS:
                raise ManualExecutionValidationError(
                    f"unsupported manual market: {leg.venue.value}/{leg.instrument_type.value}"
                )
            if leg.quantity_btc != leg.quantity_btc.quantize(QUANTITY_INCREMENT_BTC):
                raise ManualExecutionValidationError(
                    "manual hedge quantities support at most two decimal places"
                )

        total_quantity = sum((leg.quantity_btc for leg in request.legs), Decimal("0"))
        maximum = abs(desk.total_delta_btc)
        if total_quantity > maximum:
            raise ManualExecutionValidationError(
                f"manual allocation cannot cross flat ({maximum} BTC maximum)"
            )
        direction = Decimal("1") if desk.total_delta_btc < 0 else Decimal("-1")
        side = ExecutionSide.BUY if direction > 0 else ExecutionSide.SELL
        snapshot = await self._store.executable_snapshot("BTC")
        previews: list[ManualExecutionLegPreview] = []
        reasons: list[str] = []
        for index, leg in enumerate(request.legs, start=1):
            market = self._find_market(snapshot, leg.venue, leg.instrument_type)
            instrument_id = self._instrument_id(market, leg.venue, leg.instrument_type)
            result = estimate_execution_cost(
                ExecutionCostRequest(
                    request_id=f"{request.request_id}-leg-{index}",
                    venue=leg.venue,
                    instrument_id=instrument_id,
                    instrument_type=leg.instrument_type,
                    side=side,
                    quantity_btc_equivalent=leg.quantity_btc,
                    market_snapshot_version=snapshot.snapshot_version,
                ),
                snapshot,
                execution_fee_config,
            )
            if result.filled_quantity_btc == 0:
                reasons.append(
                    f"{leg.venue.value}_{leg.instrument_type.value}_{result.status.value}"
                )
            elif result.status is ExecutionCostStatus.INSUFFICIENT_LIQUIDITY:
                reasons.append(
                    f"{leg.venue.value}_{leg.instrument_type.value}_PARTIAL_DEPTH"
                )
            previews.append(
                ManualExecutionLegPreview(
                    venue=leg.venue,
                    instrument_id=instrument_id,
                    instrument_type=leg.instrument_type,
                    side=side,
                    requested_quantity_btc=leg.quantity_btc,
                    executable_quantity_btc=result.filled_quantity_btc,
                    unfilled_quantity_btc=result.unfilled_quantity_btc,
                    status=result.status,
                    status_reason=result.status_reason,
                    market_snapshot_version=snapshot.snapshot_version,
                    arrival_mid_usd=result.arrival_mid,
                    expected_vwap_usd=result.execution_vwap,
                    spread_cost_bps=result.spread_cost_bps,
                    depth_impact_bps=result.depth_impact_bps,
                    taker_fee_bps=result.taker_fee_bps,
                    expected_fee_usd=result.fee_usd,
                    expected_price_cost_usd=result.price_cost_usd,
                    expected_all_in_cost_usd=result.all_in_immediate_cost_usd,
                )
            )

        submitted_delta = direction * total_quantity
        now = utc_now()
        preview = ManualHedgePreview(
            preview_id=f"manual-preview-{uuid4().hex[:12]}",
            request_id=request.request_id,
            created_at=now,
            expires_at=now + timedelta(seconds=PREVIEW_TTL_SECONDS),
            desk_state_version=desk.version,
            market_snapshot_version=snapshot.snapshot_version,
            actual_delta_btc=desk.total_delta_btc,
            advisory_target_delta_btc=assessment.advisory_target_delta_btc,
            maximum_hedge_quantity_btc=maximum,
            submitted_hedge_delta_btc=submitted_delta,
            projected_delta_btc=desk.total_delta_btc + submitted_delta,
            can_submit=all(leg.executable_quantity_btc > 0 for leg in previews),
            reason_codes=tuple(reasons),
            legs=tuple(previews),
            total_expected_fee_usd=self._sum_optional(
                leg.expected_fee_usd for leg in previews
            ),
            total_expected_all_in_cost_usd=self._sum_optional(
                leg.expected_all_in_cost_usd for leg in previews
            ),
        )
        self._previews[preview.preview_id] = preview
        return preview

    async def submit(self, preview_id: str) -> ManualHedgeSubmission:
        preview = self._previews.get(preview_id)
        if preview is None:
            raise ManualExecutionStateError("unknown manual execution preview")
        if utc_now() > preview.expires_at:
            raise ManualExecutionStateError("manual execution preview expired; preview again")
        if self._trading.desk_state.version != preview.desk_state_version:
            raise ManualExecutionStateError("DeskState changed; preview the allocation again")
        snapshot = await self._store.executable_snapshot("BTC")
        for leg in preview.legs:
            market = self._find_market(snapshot, leg.venue, leg.instrument_type)
            if market is None or not market.eligible or market.book is None:
                raise ManualExecutionStateError(
                    f"{leg.venue.value} {leg.instrument_type.value} is no longer executable"
                )
        try:
            batch = self._trading.create_manual_market_hedge_orders(preview)
        except DemoStateError as error:
            raise ManualExecutionStateError(str(error)) from error
        return ManualHedgeSubmission(order_batch=batch, preview=preview)

    async def execute_batch(
        self, batch_id: str, execution_id: str
    ) -> ExecutionBatchMetrics:
        replay = self._executions.get(execution_id)
        if replay is not None:
            if replay.batch_id != batch_id:
                raise ManualExecutionValidationError(
                    "execution_id is already used for a different batch"
                )
            return replay
        orders = tuple(
            order for order in self._trading.hedge_orders.values() if order.batch_id == batch_id
        )
        if not orders:
            raise ManualExecutionStateError("unknown hedge execution batch")
        if any(order.origin.value == "AUTO_RISK" for order in orders):
            raise ManualExecutionStateError("AUTO_RISK batches are controller-owned")

        snapshot = await self._store.executable_snapshot("BTC")
        for order in orders:
            current = self._trading.hedge_orders[order.hedge_order_id]
            if current.status not in WORKING_STATUSES:
                continue
            try:
                venue = MarketVenue(current.venue)
            except ValueError as error:
                raise ManualExecutionStateError(
                    f"{current.hedge_order_id} has no executable venue"
                ) from error
            result = estimate_execution_cost(
                ExecutionCostRequest(
                    request_id=f"{execution_id}-{current.hedge_order_id}",
                    venue=venue,
                    instrument_id=current.instrument_id,
                    instrument_type=current.instrument_type,
                    side=self._execution_side(current.side),
                    quantity_btc_equivalent=current.remaining_quantity_btc,
                    market_snapshot_version=snapshot.snapshot_version,
                ),
                snapshot,
                execution_fee_config,
            )
            if result.filled_quantity_btc == 0 or result.execution_vwap is None:
                continue
            self._trading.simulate_hedge_fill(
                current.hedge_order_id,
                result.filled_quantity_btc,
                f"{execution_id}-{current.hedge_order_id}-fill",
                fill_price_usd=result.execution_vwap,
                execution_source=(
                    f"{venue.value} · "
                    f"{'PERP' if current.instrument_type is InstrumentType.PERPETUAL else 'SPOT'}"
                    " · L2 SIMULATION"
                ),
                market_snapshot_version=snapshot.snapshot_version,
                arrival_mid_usd=result.arrival_mid,
                expected_vwap_usd=current.expected_vwap_usd,
                taker_fee_bps=result.taker_fee_bps,
                fee_usd=result.fee_usd,
            )

        metrics = self._build_metrics(execution_id, batch_id, snapshot.snapshot_version)
        self._executions[execution_id] = metrics
        self._batch_metrics[batch_id] = metrics
        return metrics

    def _build_metrics(
        self, execution_id: str, batch_id: str, snapshot_version: int
    ) -> ExecutionBatchMetrics:
        orders = tuple(
            order for order in self._trading.hedge_orders.values() if order.batch_id == batch_id
        )
        fills_by_order: dict[str, list] = defaultdict(list)
        for fill in self._trading.hedge_fills:
            fills_by_order[fill.hedge_order_id].append(fill)
        order_metrics: list[ExecutionOrderMetrics] = []
        for order in orders:
            fills = fills_by_order[order.hedge_order_id]
            filled = sum((fill.quantity_btc for fill in fills), Decimal("0"))
            notional = sum(
                (fill.fill_price_usd * fill.quantity_btc for fill in fills), Decimal("0")
            )
            realized_vwap = notional / filled if filled else None
            order_metrics.append(
                ExecutionOrderMetrics(
                    hedge_order_id=order.hedge_order_id,
                    venue=order.venue,
                    instrument_id=order.instrument_id,
                    instrument_type=order.instrument_type,
                    side=order.side.value,
                    execution_source=(
                        fills[-1].execution_source
                        if fills
                        else f"{order.venue} · L2 SIMULATION"
                    ),
                    status=order.status.value,
                    market_snapshot_version=(
                        fills[-1].market_snapshot_version if fills else snapshot_version
                    ),
                    ordered_quantity_btc=order.quantity_btc,
                    filled_quantity_btc=filled,
                    remaining_quantity_btc=order.remaining_quantity_btc,
                    expected_vwap_usd=order.expected_vwap_usd,
                    realized_vwap_usd=realized_vwap,
                    arrival_mid_usd=(
                        fills[-1].arrival_mid_usd if fills else order.arrival_mid_usd
                    ),
                    slippage_vs_expected_usd=sum(
                        (fill.slippage_vs_expected_usd or Decimal("0") for fill in fills),
                        Decimal("0"),
                    ),
                    implementation_shortfall_usd=sum(
                        (fill.implementation_shortfall_usd or Decimal("0") for fill in fills),
                        Decimal("0"),
                    ),
                    taker_fee_bps=(fills[-1].taker_fee_bps if fills else order.expected_taker_fee_bps),
                    fee_usd=sum((fill.fee_usd or Decimal("0") for fill in fills), Decimal("0")),
                    filled_notional_usd=notional,
                    all_in_cost_usd=sum(
                        (fill.all_in_cost_usd or Decimal("0") for fill in fills), Decimal("0")
                    ),
                )
            )
        requested = sum((item.ordered_quantity_btc for item in order_metrics), Decimal("0"))
        filled = sum((item.filled_quantity_btc for item in order_metrics), Decimal("0"))
        remaining = sum((item.remaining_quantity_btc for item in order_metrics), Decimal("0"))
        realized_notional = sum((item.filled_notional_usd for item in order_metrics), Decimal("0"))
        expected_notional = sum(
            (
                (item.expected_vwap_usd or Decimal("0")) * item.filled_quantity_btc
                for item in order_metrics
            ),
            Decimal("0"),
        )
        status = (
            ExecutionBatchStatus.FILLED
            if remaining == 0
            else ExecutionBatchStatus.PARTIALLY_FILLED
            if filled > 0
            else ExecutionBatchStatus.UNFILLED
        )
        return ExecutionBatchMetrics(
            execution_id=execution_id,
            batch_id=batch_id,
            origin=orders[0].origin.value,
            executed_at=utc_now(),
            status=status,
            market_snapshot_version=snapshot_version,
            requested_quantity_btc=requested,
            filled_quantity_btc=filled,
            remaining_quantity_btc=remaining,
            expected_vwap_usd=expected_notional / filled if filled else None,
            realized_vwap_usd=realized_notional / filled if filled else None,
            filled_notional_usd=realized_notional,
            implementation_shortfall_usd=sum(
                (item.implementation_shortfall_usd for item in order_metrics), Decimal("0")
            ),
            slippage_vs_expected_usd=sum(
                (item.slippage_vs_expected_usd for item in order_metrics), Decimal("0")
            ),
            fee_usd=sum((item.fee_usd for item in order_metrics), Decimal("0")),
            all_in_cost_usd=sum((item.all_in_cost_usd for item in order_metrics), Decimal("0")),
            orders=tuple(order_metrics),
        )

    @staticmethod
    def _execution_side(side: HedgeSide) -> ExecutionSide:
        return (
            ExecutionSide.BUY
            if side in {HedgeSide.BUY, HedgeSide.LONG}
            else ExecutionSide.SELL
        )

    @staticmethod
    def _find_market(
        snapshot: ExecutableMarketSnapshot,
        venue: MarketVenue,
        instrument_type: InstrumentType,
    ) -> ExecutableBookView | None:
        return next(
            (
                market
                for market in snapshot.markets
                if market.venue is venue and market.instrument_type is instrument_type
            ),
            None,
        )

    @staticmethod
    def _instrument_id(
        market: ExecutableBookView | None,
        venue: MarketVenue,
        instrument_type: InstrumentType,
    ) -> str:
        if market and market.instrument:
            return market.instrument.venue_symbol
        if market and market.book:
            return market.book.venue_symbol
        return f"{venue.value}:BTC:{instrument_type.value}"

    @staticmethod
    def _sum_optional(values) -> Decimal | None:
        materialized = tuple(values)
        return (
            sum((value for value in materialized if value is not None), Decimal("0"))
            if materialized and all(value is not None for value in materialized)
            else None
        )


simulated_execution_service = SimulatedExecutionService(
    market_state_store,
    demo_service,
)
