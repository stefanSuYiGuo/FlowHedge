"""Minimal FastAPI shell for the FlowHedge simulator.

Business endpoints, market-data adapters, pricing, hedging, and book state are
intentionally deferred until the terminal layout is approved.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(
    title="FlowHedge API",
    description="Backend API for the institutional crypto sales-trading simulator.",
    version="0.1.0",
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
