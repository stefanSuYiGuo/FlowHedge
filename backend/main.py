"""FastAPI entry point for the FlowHedge simulator."""

from __future__ import annotations

from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .client_flow import client_flow_service
from .demo import (
    DemoStateError,
    HedgeAllocationError,
    HedgeFillError,
    demo_service,
)
from .domain.models import (
    ClientFlowState,
    DemoScenarioResult,
    DemoWorkspaceState,
    DeskState,
    Event,
    HedgeCancellationResult,
    HedgeFill,
    HedgeFillResult,
    HedgeOrder,
    HedgeOrderBatchResult,
)
from .domain.validation import (
    RFQBelowMinimumNotional,
    calculate_notional_usd,
    validate_client_rfq_notional,
)
from .market import market_data_service, market_state_store
from .market.models import MarketConnectionState, MarketStateView, MarketVenue


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Run public market data and slow client flow with the API process."""

    await market_data_service.start()
    await client_flow_service.start()
    try:
        yield
    finally:
        await client_flow_service.stop()
        await market_data_service.stop()

app = FastAPI(
    title="FlowHedge API",
    description="Backend API for the institutional crypto sales-trading simulator.",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health", tags=["system"])
async def health() -> dict[str, str]:
    """Return a lightweight readiness response for local development."""

    return {"status": "ok", "service": "flowhedge-api"}


@app.get(
    "/market/books/{venue}/{symbol}",
    response_model=MarketStateView,
    tags=["market-data"],
)
async def get_market_book(venue: str, symbol: str) -> MarketStateView:
    """Return the latest normalized book, metadata, and connection state."""

    try:
        market_venue = MarketVenue(venue.upper())
    except ValueError as error:
        raise HTTPException(status_code=404, detail="unsupported market venue") from error

    canonical_symbol = symbol.upper()
    if not market_data_service.supports(market_venue, canonical_symbol):
        raise HTTPException(
            status_code=404,
            detail=f"unsupported market: {market_venue.value}/{canonical_symbol}",
        )
    return await market_state_store.view(market_venue, canonical_symbol)


@app.get(
    "/market/connections",
    response_model=list[MarketConnectionState],
    tags=["market-data"],
)
async def get_market_connections() -> list[MarketConnectionState]:
    """Expose adapter connectivity without leaking venue implementation details."""

    return await market_state_store.connections()


class RFQValidationRequest(BaseModel):
    quantity_btc: Decimal = Field(gt=0)
    reference_price_usd: Decimal = Field(gt=0)


class RFQValidationResponse(BaseModel):
    valid: bool
    notional_usd: Decimal
    rule: str = "notional_usd > 500000"


class ManualHedgeOrderRequest(BaseModel):
    batch_id: str = Field(min_length=1, max_length=100)
    spot_quantity_btc: Decimal = Field(ge=0)


class SimulatedHedgeFillRequest(BaseModel):
    hedge_fill_id: str = Field(min_length=1, max_length=100)
    quantity_btc: Decimal = Field(gt=0)


@app.post(
    "/rfqs/validate",
    response_model=RFQValidationResponse,
    tags=["rfqs"],
)
async def validate_rfq(request: RFQValidationRequest) -> RFQValidationResponse:
    """Validate the strict institutional minimum without changing desk state."""

    notional_usd = calculate_notional_usd(
        request.quantity_btc, request.reference_price_usd
    )
    try:
        validate_client_rfq_notional(
            request.quantity_btc, request.reference_price_usd
        )
    except RFQBelowMinimumNotional as error:
        raise HTTPException(
            status_code=422,
            detail={
                "message": str(error),
                "notional_usd": str(notional_usd),
                "rule": "notional_usd > 500000",
            },
        ) from error
    return RFQValidationResponse(valid=True, notional_usd=notional_usd)


@app.post(
    "/demo/run-client-trade",
    response_model=DemoScenarioResult,
    tags=["demo"],
)
async def run_fixed_client_trade() -> DemoScenarioResult:
    """Run the deterministic RFQ → Quote → ClientTrade → DeskState chain."""

    return demo_service.run_fixed_client_trade()


@app.get(
    "/demo/workspace",
    response_model=DemoWorkspaceState,
    tags=["demo", "client-flow"],
)
async def get_demo_workspace() -> DemoWorkspaceState:
    """Return one coherent view for the continuously updating trading screen."""

    return DemoWorkspaceState(
        client_flow=client_flow_service.state(),
        desk_state=demo_service.desk_state,
        hedge_orders=tuple(demo_service.archived_hedge_orders)
        + tuple(demo_service.hedge_orders.values()),
        hedge_fills=tuple(demo_service.hedge_fills),
        events=tuple(demo_service.events[-100:]),
    )


@app.post(
    "/demo/client-flow/pause",
    response_model=ClientFlowState,
    tags=["demo", "client-flow"],
)
async def pause_client_flow() -> ClientFlowState:
    return client_flow_service.pause()


@app.post(
    "/demo/client-flow/resume",
    response_model=ClientFlowState,
    tags=["demo", "client-flow"],
)
async def resume_client_flow() -> ClientFlowState:
    return client_flow_service.resume()


@app.post(
    "/demo/client-flow/generate",
    response_model=DemoScenarioResult,
    tags=["test-support"],
)
async def generate_client_flow_now() -> DemoScenarioResult:
    """Test seam for generating one live-sized RFQ; the normal UI never calls it."""

    result = await client_flow_service.generate_once()
    if result is None:
        raise HTTPException(status_code=409, detail="live Kraken book is unavailable")
    return result


@app.post("/demo/reset", response_model=DeskState, tags=["demo"])
async def reset_demo() -> DeskState:
    """Clear all client/hedge state and restart the slow arrival schedule."""

    client_flow_service.reset()
    return demo_service.desk_state


@app.get(
    "/demo/scenario",
    response_model=Optional[DemoScenarioResult],
    tags=["demo"],
)
async def get_demo_scenario() -> Optional[DemoScenarioResult]:
    """Return the booked demo scenario, or null after a reset."""

    return demo_service.saved_result


@app.post(
    "/demo/hedge-orders",
    response_model=HedgeOrderBatchResult,
    tags=["demo", "hedging"],
)
async def create_manual_hedge_orders(
    request: ManualHedgeOrderRequest,
) -> HedgeOrderBatchResult:
    """Create a manual Spot/Perp split for the explicit Step 4 demo target."""

    try:
        return demo_service.create_manual_hedge_orders(
            request.spot_quantity_btc,
            request.batch_id,
        )
    except HedgeAllocationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except DemoStateError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post(
    "/demo/hedge-orders/cancel",
    response_model=HedgeCancellationResult,
    tags=["demo", "hedging"],
)
async def cancel_unfilled_hedge_orders() -> HedgeCancellationResult:
    """Cancel untouched hedge orders and return the allocation to draft state."""

    try:
        return demo_service.cancel_unfilled_hedge_orders()
    except DemoStateError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post(
    "/demo/hedge-orders/{hedge_order_id}/fills",
    response_model=HedgeFillResult,
    tags=["demo", "hedging"],
)
async def simulate_hedge_fill(
    hedge_order_id: str,
    request: SimulatedHedgeFillRequest,
) -> HedgeFillResult:
    """Simulate one idempotent fill; only this endpoint changes positions."""

    try:
        return demo_service.simulate_hedge_fill(
            hedge_order_id,
            request.quantity_btc,
            request.hedge_fill_id,
        )
    except HedgeFillError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except DemoStateError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.get("/demo/hedge-orders", response_model=list[HedgeOrder], tags=["hedging"])
async def get_hedge_orders() -> list[HedgeOrder]:
    return list(demo_service.archived_hedge_orders) + list(
        demo_service.hedge_orders.values()
    )


@app.get("/demo/hedge-fills", response_model=list[HedgeFill], tags=["hedging"])
async def get_hedge_fills() -> list[HedgeFill]:
    return demo_service.hedge_fills


@app.get("/desk/state", response_model=DeskState, tags=["desk"])
async def get_desk_state() -> DeskState:
    return demo_service.desk_state


@app.get("/events", response_model=list[Event], tags=["events"])
async def get_events() -> list[Event]:
    return demo_service.events
