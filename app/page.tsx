"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import {
  cancelUnfilledHedgeOrders,
  createManualHedgeOrders,
  getDemoWorkspace,
  getUnifiedMarketSnapshot,
  pauseClientFlow,
  resetDemo,
  resumeClientFlow,
  simulateHedgeFill,
} from "./lib/api";
import type {
  DemoScenarioResult,
  DemoWorkspaceState,
  DeskState,
  FlowEvent,
  HedgeFill,
  HedgeOrder,
  MarketStateView,
  PendingClientFlow,
  UnifiedMarketSnapshot,
} from "./lib/types";

type TradingMode = "manual" | "auto";

const flatDeskState: DeskState = {
  version: 0,
  as_of: "",
  spot_inventory_btc: "0",
  derivative_delta_btc: "0",
  total_delta_btc: "0",
  open_hedge_order_ids: [],
  working_order_delta_btc: "0",
};

export default function Home() {
  const [mode, setMode] = useState<TradingMode>("manual");
  const [flowActive, setFlowActive] = useState(true);
  const [completedScenarios, setCompletedScenarios] = useState<DemoScenarioResult[]>([]);
  const [pendingRfqs, setPendingRfqs] = useState<PendingClientFlow[]>([]);
  const [completedFlowCount, setCompletedFlowCount] = useState(0);
  const [deskState, setDeskState] = useState<DeskState>(flatDeskState);
  const [events, setEvents] = useState<FlowEvent[]>([]);
  const [hedgeOrders, setHedgeOrders] = useState<HedgeOrder[]>([]);
  const [hedgeFills, setHedgeFills] = useState<HedgeFill[]>([]);
  const [spotAllocation, setSpotAllocation] = useState("");
  const [perpAllocation, setPerpAllocation] = useState("");
  const [busy, setBusy] = useState(false);
  const [apiState, setApiState] = useState<"connecting" | "online" | "offline">(
    "connecting",
  );
  const [error, setError] = useState<string | null>(null);
  const [backendError, setBackendError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [marketSnapshot, setMarketSnapshot] = useState<UnifiedMarketSnapshot | null>(null);
  const [selectedMarketId, setSelectedMarketId] = useState(
    "KRAKEN:SPOT:BTC-USD",
  );
  const [marketPollFailed, setMarketPollFailed] = useState(false);

  const applyWorkspace = useCallback((workspace: DemoWorkspaceState) => {
    setDeskState(workspace.desk_state);
    setEvents(workspace.events);
    setHedgeOrders(workspace.hedge_orders);
    setHedgeFills(workspace.hedge_fills);
    setCompletedScenarios(workspace.client_flow.completed_scenarios);
    setPendingRfqs(workspace.client_flow.pending_rfqs);
    setCompletedFlowCount(workspace.client_flow.completed_count);
    setFlowActive(workspace.client_flow.active);
  }, []);

  useEffect(() => {
    let active = true;
    let pollTimer: number | undefined;

    async function pollWorkspace() {
      try {
        const workspace = await getDemoWorkspace();
        if (!active) return;
        applyWorkspace(workspace);
        setApiState("online");
        setBackendError(null);
      } catch {
        if (!active) return;
        setApiState("offline");
        setBackendError(
          "Backend unavailable. Start the FastAPI service on port 8000, then refresh.",
        );
      } finally {
        if (active) pollTimer = window.setTimeout(pollWorkspace, 500);
      }
    }

    void pollWorkspace();
    return () => {
      active = false;
      if (pollTimer !== undefined) window.clearTimeout(pollTimer);
    };
  }, [applyWorkspace]);

  useEffect(() => {
    let active = true;
    let pollTimer: number | undefined;

    async function pollUnifiedMarket() {
      try {
        const latestMarketSnapshot = await getUnifiedMarketSnapshot("BTC");
        if (!active) return;
        setMarketSnapshot(latestMarketSnapshot);
        setMarketPollFailed(false);
      } catch {
        if (!active) return;
        setMarketPollFailed(true);
      } finally {
        if (active) {
          pollTimer = window.setTimeout(pollUnifiedMarket, 250);
        }
      }
    }

    void pollUnifiedMarket();
    return () => {
      active = false;
      if (pollTimer !== undefined) window.clearTimeout(pollTimer);
    };
  }, []);

  const scenario = completedScenarios.at(-1) ?? null;
  const pendingRfq = pendingRfqs.at(-1) ?? null;

  const marketStates = marketSnapshot?.markets ?? [];
  const krakenSpotState = marketStates.find(
    (market) => market.venue === "KRAKEN" && market.instrument_type === "SPOT",
  ) ?? null;
  const selectedMarketState = marketStates.find(
    (market) => marketIdentity(market) === selectedMarketId,
  ) ?? krakenSpotState ?? marketStates[0] ?? null;
  const selectedBook = selectedMarketState?.book ?? null;
  const selectedMarketStatus = marketPollFailed
    ? "DISCONNECTED"
    : (selectedMarketState?.connection.status ?? "CONNECTING");
  const liveBook = krakenSpotState?.book ?? null;
  const liveMidPrice = liveBook ? Number(liveBook.mid_price) : null;
  const deltaMarkPrice = liveMidPrice ?? (
    scenario ? Number(scenario.market_snapshot.reference_price_usd) : null
  );
  const marketStatus = marketPollFailed
    ? "DISCONNECTED"
    : (krakenSpotState?.connection.status ?? "CONNECTING");
  const totalDelta = Number(deskState.total_delta_btc);
  const workingDelta = Number(deskState.working_order_delta_btc);
  const projectedDelta = totalDelta + workingDelta;
  const deltaNotional =
    deltaMarkPrice === null ? null : totalDelta * deltaMarkPrice;
  const activeHedgeOrders = hedgeOrders.filter((order) => order.status !== "FILLED");
  const hedgeOrdersCreated = activeHedgeOrders.length > 0;
  const activeBatchId = activeHedgeOrders[0]?.batch_id ?? null;
  const activeBatchOrders = activeBatchId === null
    ? []
    : hedgeOrders.filter((order) => order.batch_id === activeBatchId);
  const demoHedgeQuantity = hedgeOrdersCreated
    ? activeBatchOrders.reduce((sum, order) => sum + Number(order.quantity_btc), 0)
    : Math.abs(totalDelta);
  const hasValidSpotPrecision = /^(?:\d+(?:\.\d{0,2})?)?$/.test(spotAllocation);
  const hasValidPerpPrecision = /^(?:\d+(?:\.\d{0,2})?)?$/.test(perpAllocation);
  const spotAllocationNumber = hasValidSpotPrecision
    ? Number(spotAllocation || "0")
    : Number.NaN;
  const perpAllocationNumber = hasValidPerpPrecision
    ? Number(perpAllocation || "0")
    : Number.NaN;
  const spotQuantityIsValid =
    Number.isFinite(spotAllocationNumber) && spotAllocationNumber >= 0;
  const perpQuantityIsValid =
    Number.isFinite(perpAllocationNumber) && perpAllocationNumber >= 0;
  const submittedHedgeQuantity = spotQuantityIsValid && perpQuantityIsValid
    ? roundBtc(spotAllocationNumber + perpAllocationNumber)
    : Number.NaN;
  const overHedgeQuantity = Number.isFinite(submittedHedgeQuantity)
    ? roundBtc(Math.max(0, submittedHedgeQuantity - demoHedgeQuantity))
    : 0;
  const validAllocation =
    spotQuantityIsValid &&
    perpQuantityIsValid &&
    submittedHedgeQuantity > 0 &&
    submittedHedgeQuantity <= demoHedgeQuantity;
  const maximumSpotQuantity = roundBtc(Math.max(
    0,
    demoHedgeQuantity - (perpQuantityIsValid ? perpAllocationNumber : 0),
  ));
  const maximumPerpQuantity = roundBtc(Math.max(
    0,
    demoHedgeQuantity - (spotQuantityIsValid ? spotAllocationNumber : 0),
  ));
  const requiredHedgeDelta = hedgeOrdersCreated
    ? activeHedgeOrders.reduce(
        (sum, order) => sum + signedHedgeOrderQuantity(order),
        0,
      )
    : -totalDelta;
  const draftHedgeDirection = requiredHedgeDelta >= 0 ? 1 : -1;
  const submittedHedgeDelta = Number.isFinite(submittedHedgeQuantity)
    ? draftHedgeDirection * submittedHedgeQuantity
    : 0;
  const hedgeOutcomeDelta = totalDelta + (
    hedgeOrdersCreated ? requiredHedgeDelta : submittedHedgeDelta
  );
  const spotHedgeSide = requiredHedgeDelta >= 0 ? "BUY" : "SELL";
  const spotReferencePrice = liveBook
    ? Number(spotHedgeSide === "BUY" ? liveBook.best_ask : liveBook.best_bid)
    : null;
  const canReviseAllocation =
    hedgeOrdersCreated && activeHedgeOrders.every((order) => Number(order.filled_quantity_btc) === 0);
  const allHedgeOrdersFilled =
    hedgeOrders.length > 0 && hedgeOrders.every((order) => order.status === "FILLED");
  const canSimulateHalfFill = hedgeOrders.some(
    (order) =>
      Number(order.filled_quantity_btc) === 0 &&
      Math.floor((Number(order.quantity_btc) * 100) / 2) > 0,
  );
  const canFillRemainder = hedgeOrders.some(
    (order) => Number(order.remaining_quantity_btc) > 0,
  );
  const visibleEvents = useMemo(() => events.slice(-30), [events]);

  async function refreshHedgeState() {
    applyWorkspace(await getDemoWorkspace());
  }

  async function handleFlowToggle() {
    if (busy) return;
    setBusy(true);
    setError(null);
    setNotice(null);

    try {
      const nextFlowState = flowActive
        ? await pauseClientFlow()
        : await resumeClientFlow();
      setFlowActive(nextFlowState.active);
      setApiState("online");
      setNotice(nextFlowState.active ? "Slow client flow resumed." : "New client RFQ arrivals paused.");
    } catch {
      setApiState("offline");
      setError("Client flow could not be updated because the backend is unavailable.");
    } finally {
      setBusy(false);
    }
  }

  async function handleReset() {
    if (busy) return;
    setBusy(true);
    setError(null);
    setNotice(null);

    try {
      const resetState = await resetDemo();
      setDeskState(resetState);
      setCompletedScenarios([]);
      setPendingRfqs([]);
      setCompletedFlowCount(0);
      setEvents([]);
      setHedgeOrders([]);
      setHedgeFills([]);
      setSpotAllocation("");
      setPerpAllocation("");
      setApiState("online");
    } catch {
      setApiState("offline");
      setError("The demo could not be reset because the backend is unavailable.");
    } finally {
      setBusy(false);
    }
  }

  async function handleCreateHedgeOrders() {
    if (
      busy ||
      !scenario ||
      !validAllocation ||
      hedgeOrdersCreated ||
      demoHedgeQuantity === 0
    ) return;
    setBusy(true);
    setError(null);
    setNotice(null);

    try {
      await createManualHedgeOrders(
        spotAllocation || "0",
        perpAllocation || "0",
        `manual-hedge-v${deskState.version}-${Date.now()}`,
      );
      await refreshHedgeState();
      setSpotAllocation("");
      setPerpAllocation("");
      setApiState("online");
      setNotice(
        "Hedge orders created — actual delta is unchanged until simulated fills arrive.",
      );
    } catch (caught) {
      setError(apiErrorMessage(caught, "The manual hedge allocation could not be created."));
    } finally {
      setBusy(false);
    }
  }

  async function handleCancelAndRevise() {
    if (busy || !canReviseAllocation) return;
    setBusy(true);
    setError(null);
    setNotice(null);

    try {
      await cancelUnfilledHedgeOrders();
      await refreshHedgeState();
      setApiState("online");
      setNotice("Unfilled hedge orders cancelled — the allocation is editable again.");
    } catch (caught) {
      setError(apiErrorMessage(caught, "The hedge allocation could not be revised."));
    } finally {
      setBusy(false);
    }
  }

  async function handleHalfFills() {
    if (busy || !canSimulateHalfFill) return;
    setBusy(true);
    setError(null);
    setNotice(null);

    try {
      for (const order of hedgeOrders) {
        if (Number(order.filled_quantity_btc) !== 0) continue;
        const partialQuantity =
          Math.floor((Number(order.quantity_btc) * 100) / 2) / 100;
        if (partialQuantity <= 0) continue;
        await simulateHedgeFill(
          order.hedge_order_id,
          partialQuantity,
          `${order.hedge_order_id}-half`,
        );
      }
      await refreshHedgeState();
      setApiState("online");
      setNotice("Partial demo fills applied — actual exposure moved and working delta was reduced.");
    } catch (caught) {
      await refreshHedgeState().catch(() => undefined);
      setError(apiErrorMessage(caught, "The partial fills could not be completed."));
    } finally {
      setBusy(false);
    }
  }

  async function handleFillRemainder() {
    if (busy || !canFillRemainder) return;
    setBusy(true);
    setError(null);
    setNotice(null);

    try {
      for (const order of hedgeOrders) {
        const remainingQuantity = Number(order.remaining_quantity_btc);
        if (remainingQuantity <= 0) continue;
        await simulateHedgeFill(
          order.hedge_order_id,
          remainingQuantity,
          `${order.hedge_order_id}-remainder`,
        );
      }
      await refreshHedgeState();
      setApiState("online");
      setNotice(
        "All current hedge orders filled — working delta cleared; actual delta includes any later client flow.",
      );
    } catch (caught) {
      await refreshHedgeState().catch(() => undefined);
      setError(apiErrorMessage(caught, "The remaining fills could not be completed."));
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="terminal-shell">
      <header className="topbar">
        <div className="brand-block">
          <div className="brand-mark">FH</div>
          <div>
            <div className="brand-name">FLOWHEDGE</div>
            <div className="brand-subtitle">Institutional Crypto Sales Trading</div>
          </div>
        </div>

        <div className="ticker-block">
          <span className="eyebrow">BTC / USD</span>
          <strong>{liveMidPrice === null ? "—" : formatUsd(liveMidPrice)}</strong>
          <span className="live-market-label">KRAKEN SPOT</span>
          <span className={`market-status market-status-${marketStatus.toLowerCase()}`}>
            {marketStatus}
          </span>
        </div>

        <div className={`connection-state ${apiState === "offline" ? "offline" : ""}`}>
          <span />
          {apiState === "connecting"
            ? "API CONNECTING"
            : apiState === "online"
              ? "API ONLINE"
              : "API OFFLINE"}
        </div>

        <div className="mode-controls" aria-label="Future hedge mode preview">
          <button
            className={mode === "manual" ? "selected" : ""}
            onClick={() => setMode("manual")}
            type="button"
          >
            MANUAL TRADER
          </button>
          <button
            className={mode === "auto" ? "selected" : ""}
            onClick={() => setMode("auto")}
            type="button"
          >
            AUTO HEDGE
          </button>
        </div>
      </header>

      {(backendError ?? error) && (
        <div className="api-error" role="alert">{backendError ?? error}</div>
      )}
      {notice && <div className="api-notice" role="status">{notice}</div>}

      <section className="desk-strip" aria-label="Desk summary">
        <Metric label="Net delta" value={formatBtc(totalDelta)} />
        <Metric
          label="Delta notional"
          value={deltaNotional === null ? "—" : formatSignedCompactUsd(deltaNotional)}
        />
        <Metric label="Working delta" value={formatBtc(workingDelta)} />
        <Metric label="Projected delta" value={formatBtc(projectedDelta)} />
        <Metric label="Spot inventory" value={formatBtc(Number(deskState.spot_inventory_btc))} />
        <Metric label="Derivative delta" value={formatBtc(Number(deskState.derivative_delta_btc))} />
      </section>

      <section className="workspace-grid">
        <aside className="left-rail">
          <Panel
            title="Live Market Data · Multi-Venue"
            meta={marketSnapshot
              ? `SNAPSHOT v${marketSnapshot.snapshot_version} · ${marketStates.filter((market) => market.eligible).length}/${marketStates.length} ELIGIBLE`
              : "CONNECTING"}
          >
            {marketStates.length > 0 ? (
              <div className="live-market-panel">
                <div className="venue-market-list" aria-label="BTC market feeds">
                  {marketStates.map((market) => {
                    const identity = marketIdentity(market);
                    const status = marketPollFailed
                      ? "DISCONNECTED"
                      : market.connection.status;
                    return (
                      <button
                        aria-pressed={identity === marketIdentity(selectedMarketState)}
                        className={identity === marketIdentity(selectedMarketState) ? "selected" : ""}
                        key={identity}
                        onClick={() => setSelectedMarketId(identity)}
                        type="button"
                      >
                        <span>
                          <strong>{market.venue}</strong>
                          <small>{market.instrument_type === "PERPETUAL" ? "PERP · USDC" : "SPOT · USD"}</small>
                        </span>
                        <span className="venue-touch">
                          <strong>{market.book ? formatUsd(Number(market.book.mid_price)) : "—"}</strong>
                          <small>{market.book ? `${Number(market.book.spread_bps).toFixed(2)} bps` : market.exclusion_reason?.replaceAll("_", " ") ?? "WAITING"}</small>
                        </span>
                        <span className={`market-status market-status-${status.toLowerCase()}`}>{status}</span>
                      </button>
                    );
                  })}
                </div>

                {selectedBook ? (
                  <>
                    <div className="selected-market-heading">
                      <span>{selectedMarketState?.venue} · {selectedMarketState?.instrument_type}</span>
                      <strong>{selectedBook.venue_symbol}</strong>
                    </div>
                    <div className="top-of-book">
                      <div><small>BEST BID</small><strong className="bid">{formatUsd(Number(selectedBook.best_bid))}</strong></div>
                      <div><small>BEST ASK</small><strong className="ask">{formatUsd(Number(selectedBook.best_ask))}</strong></div>
                      <div><small>SPREAD</small><strong>{Number(selectedBook.spread_bps).toFixed(2)} bps</strong></div>
                    </div>
                    <table className="market-table order-book-table">
                      <thead><tr><th>BID QTY</th><th>BID</th><th>ASK</th><th>ASK QTY</th></tr></thead>
                      <tbody>
                        {selectedBook.bids.slice(0, 5).map((bidLevel, index) => {
                          const askLevel = selectedBook.asks[index];
                          return (
                            <tr key={`${bidLevel.price}-${askLevel?.price ?? index}`}>
                              <td>{formatBookQuantity(Number(bidLevel.quantity))}</td>
                              <td className="bid">{formatUsd(Number(bidLevel.price))}</td>
                              <td className="ask">{askLevel ? formatUsd(Number(askLevel.price)) : "—"}</td>
                              <td>{askLevel ? formatBookQuantity(Number(askLevel.quantity)) : "—"}</td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                    <div className="market-metadata">
                      <span>BOOK L2 · DEPTH {selectedBook.depth}</span>
                      <span>{selectedBook.checksum !== null ? `CRC ${selectedBook.checksum}` : `SEQ ${selectedBook.source_sequence ?? "—"}`}</span>
                      <span>
                        {selectedMarketState?.instrument
                          ? `TICK ${selectedMarketState.instrument.price_increment} · MIN ${selectedMarketState.instrument.quantity_min} BTC`
                          : "INSTRUMENT METADATA LOADING"}
                      </span>
                      {selectedMarketState?.instrument?.usd_conversion_assumption && (
                        <span>USDC≈USD · 1:1 DEMO ASSUMPTION</span>
                      )}
                      {selectedMarketState?.instrument_type === "PERPETUAL" && (
                        <span>FUNDING COST · DEFERRED</span>
                      )}
                    </div>
                  </>
                ) : (
                  <EmptyState
                    title={`${selectedMarketState?.venue ?? "Market"} ${selectedMarketStatus.toLowerCase()}`}
                    detail={selectedMarketState?.connection.last_error ?? "Waiting for the first normalized L2 snapshot."}
                  />
                )}
              </div>
            ) : (
              <EmptyState
                title={marketPollFailed ? "Market API disconnected" : "Waiting for market feeds"}
                detail={
                  "Kraken and Coinbase public adapters are connecting to their first normalized books."
                }
              />
            )}
          </Panel>

          <Panel
            title="RFQ Inbox"
            meta={`${pendingRfqs.length} pricing · ${completedFlowCount} filled`}
            grow
          >
            <div className="rfq-list">
              {pendingRfqs.map((pending) => (
                <div className="rfq-card active incoming-rfq" key={pending.rfq.rfq_id}>
                  <span className={`rfq-side ${pending.rfq.client_side === "BUY" ? "bid" : "ask"}`}>
                    {pending.rfq.client_side}
                  </span>
                  <strong>{formatQuantity(pending.rfq.quantity_btc)} BTC</strong>
                  <span className="status-tag status-pricing"><Spinner /> PRICING</span>
                  <small>{pending.rfq.client_id} · {formatCompactUsd(Number(pending.rfq.validated_notional_usd))}</small>
                  <time>{formatTime(pending.rfq.received_at)}</time>
                </div>
              ))}
              {[...completedScenarios].reverse().slice(0, 6).map((completed) => (
                <div className="rfq-card" key={completed.rfq.rfq_id}>
                  <span className={`rfq-side ${completed.rfq.client_side === "BUY" ? "bid" : "ask"}`}>
                    {completed.rfq.client_side}
                  </span>
                  <strong>{formatQuantity(completed.rfq.quantity_btc)} BTC</strong>
                  <span className="status-tag status-filled">✓ ACCEPTED</span>
                  <small>{completed.rfq.client_id} · {formatCompactUsd(Number(completed.rfq.validated_notional_usd))}</small>
                  <time>{formatTime(completed.rfq.received_at)}</time>
                </div>
              ))}
              {pendingRfqs.length === 0 && completedScenarios.length === 0 && (
                <EmptyState
                  title="Waiting for institutional flow"
                  detail="The backend will introduce valid institutional RFQs asynchronously."
                />
              )}
            </div>
          </Panel>

          <div className="flow-controls">
            <div>
              <span className="eyebrow">Simulated client flow</span>
              <strong className={flowActive ? "positive" : "warning"}>{flowActive ? "ACTIVE" : "PAUSED"}</strong>
              <small>Orders arrive asynchronously</small>
            </div>
            <button disabled={busy} type="button" onClick={handleFlowToggle}>
              {flowActive ? "PAUSE" : "RESUME"}
            </button>
            <button disabled={busy || apiState === "connecting"} type="button" className="reset-button" onClick={handleReset}>
              RESET DEMO
            </button>
          </div>
        </aside>

        <section className="center-stage">
          <Panel title="Demo Client Quote · Active RFQ" meta={activeRfqMeta(pendingRfq, scenario)}>
            {pendingRfq ? (
              <div className="active-rfq active-rfq-loading">
                <div>
                  <div className="active-rfq-size">
                    <span className={pendingRfq.rfq.client_side === "BUY" ? "bid" : "ask"}>
                      {pendingRfq.rfq.client_side}
                    </span>{" "}
                    {formatQuantity(pendingRfq.rfq.quantity_btc)} BTC
                  </div>
                  <p>{pendingRfq.rfq.client_id} · {formatCompactUsd(Number(pendingRfq.rfq.validated_notional_usd))} · preparing demo quote</p>
                </div>
                <span className="pricing-status" role="status"><Spinner /> PRICING</span>
              </div>
            ) : scenario ? (
              <>
                <div className="active-rfq">
                  <div>
                    <div className="active-rfq-size">
                      <span className={scenario.rfq.client_side === "BUY" ? "bid" : "ask"}>{scenario.rfq.client_side}</span>{" "}
                      {formatQuantity(scenario.rfq.quantity_btc)} BTC
                    </div>
                    <p>{scenario.rfq.client_id} · {clientTradeDescription(scenario)} · {formatCompactUsd(Number(scenario.rfq.validated_notional_usd))}</p>
                  </div>
                  <span className="auto-accepted"><span className="status-check">✓</span> AUTO-ACCEPTED</span>
                </div>

                <div className="quote-section">
                  <div className="quote-prices">
                    <span className="eyebrow">Accepted client quote</span>
                    <div className="price-pair single-price">
                      <div>
                        <small>{scenario.rfq.client_side === "BUY" ? "CLIENT ASK" : "CLIENT BID"} · TRADED</small>
                        <strong className={scenario.rfq.client_side === "BUY" ? "ask" : "bid"}>{formatUsd(Number(scenario.quote.quoted_price_usd))}</strong>
                      </div>
                    </div>
                  </div>
                  <div className="quote-breakdown">
                    <span className="eyebrow">Quote provenance</span>
                    <LineItem label="Reference price" value={formatUsd(Number(scenario.market_snapshot.reference_price_usd))} />
                    <LineItem label="Quote revision" value={`R${scenario.quote.revision}`} />
                    <LineItem label="Desk state used" value={`v${scenario.quote.desk_state_version}`} />
                    <LineItem label="Pricing source" value={formatPricingSource(scenario.quote.pricing_source)} />
                  </div>
                </div>
              </>
            ) : (
              <EmptyState title="No active client RFQ" detail="Slow client flow is running in the background; arrivals are intentionally not predicted." roomy />
            )}
          </Panel>

          <Panel
            title="Hedge Decision Workspace · Simulated Execution"
            meta={mode === "manual" ? "MANUAL MODE · STEP 6" : "AUTO MODE · DEFERRED"}
            grow
          >
            {!scenario ? (
              <EmptyState
                title="No exposure to hedge"
                detail="The slow client-flow simulator has not booked its first auto-accepted trade yet."
                roomy
              />
            ) : mode === "auto" ? (
              <UnavailableFeature
                title="Automatic hedging is intentionally deferred"
                detail="This step verifies execution accounting only. A future Risk Policy will decide how much to hedge, then the Hedge Optimizer will decide how to hedge it."
              />
            ) : (
              <div className="hedge-workspace">
                <div className="recommendation-grid">
                  <div>
                    <small>{hedgeOrdersCreated ? "CURRENT ORDERS PROJECT TO" : "DRAFT PROJECTED DELTA"}</small>
                    <strong>{formatBtc(hedgeOutcomeDelta)}</strong>
                  </div>
                  <div>
                    <small>ACTUAL DELTA</small>
                    <strong>{formatBtc(totalDelta)}</strong>
                  </div>
                  <div>
                    <small>WORKING DELTA</small>
                    <strong>{formatBtc(workingDelta)}</strong>
                  </div>
                  <div>
                    <small>PROJECTED IF FILLED</small>
                    <strong>{formatBtc(projectedDelta)}</strong>
                  </div>
                </div>

                <div className={`allocation-controls ${hedgeOrdersCreated ? "locked" : ""}`}>
                  <label>
                    <span>SPOT HEDGE · BTC · MAX 2 DECIMALS</span>
                    <input
                      aria-describedby="spot-allocation-limit"
                      aria-label="Spot hedge quantity in BTC"
                      disabled={busy || hedgeOrdersCreated}
                      inputMode="decimal"
                      min="0"
                      max={maximumSpotQuantity}
                      step="0.01"
                      type="number"
                      value={spotAllocation}
                      onBlur={() => {
                        if (spotAllocation !== "" && spotQuantityIsValid) {
                          setSpotAllocation(spotAllocationNumber.toFixed(2));
                        }
                      }}
                      onChange={(event) => {
                        const nextValue = event.target.value;
                        if (nextValue === "" || /^\d*(?:\.\d{0,2})?$/.test(nextValue)) {
                          setSpotAllocation(nextValue);
                        }
                      }}
                    />
                    <small className="allocation-limit" id="spot-allocation-limit">
                      MAX {maximumSpotQuantity.toFixed(2)} BTC WITH CURRENT PERP
                    </small>
                  </label>
                  <label>
                    <span>PERP HEDGE · BTC · MAX 2 DECIMALS</span>
                    <input
                      aria-describedby="perp-allocation-limit"
                      aria-label="Perpetual hedge quantity in BTC"
                      disabled={busy || hedgeOrdersCreated}
                      inputMode="decimal"
                      min="0"
                      max={maximumPerpQuantity}
                      step="0.01"
                      type="number"
                      value={perpAllocation}
                      onBlur={() => {
                        if (perpAllocation !== "" && perpQuantityIsValid) {
                          setPerpAllocation(perpAllocationNumber.toFixed(2));
                        }
                      }}
                      onChange={(event) => {
                        const nextValue = event.target.value;
                        if (nextValue === "" || /^\d*(?:\.\d{0,2})?$/.test(nextValue)) {
                          setPerpAllocation(nextValue);
                        }
                      }}
                    />
                    <small className="allocation-limit" id="perp-allocation-limit">
                      MAX {maximumPerpQuantity.toFixed(2)} BTC WITH CURRENT SPOT
                    </small>
                  </label>
                  <label>
                    <span>EXECUTION SOURCE</span>
                    <input readOnly value="FIXED STEP 4 SIMULATION" />
                  </label>
                </div>
                {overHedgeQuantity > 0 && (
                  <p className="allocation-warning" role="alert">
                    ALLOCATION EXCEEDS THE MAXIMUM BY {overHedgeQuantity.toFixed(2)} BTC
                  </p>
                )}

                <div className="allocation-summary">
                  <span>Current exposure {formatBtc(totalDelta)}</span>
                  <span>→</span>
                  <span>{hedgeOrdersCreated ? "Working hedge" : "Manual allocation"} <strong>{formatBtc(hedgeOrdersCreated ? requiredHedgeDelta : submittedHedgeDelta)}</strong></span>
                  <span>→</span>
                  <span>Projected <strong>{formatBtc(hedgeOutcomeDelta)}</strong></span>
                </div>
                <p className="demo-policy-note">
                  Enter Spot and Perp quantities independently. A partial hedge may intentionally leave residual exposure; the combined quantity cannot exceed the current exposure. This is a manual decision, not a Risk Policy or Hedge Optimizer recommendation. New client fills remain independent and can move projected delta while these orders are still working.
                </p>

                <div className={`market-candidate ${marketStatus !== "LIVE" ? "unavailable" : ""}`}>
                  <div className="market-candidate-heading">
                    <span>LIVE MARKET CANDIDATE · NOT OPTIMIZED</span>
                    <strong>KRAKEN SPOT</strong>
                  </div>
                  {marketStatus === "LIVE" && liveBook ? (
                    <div className="market-candidate-grid">
                      <div><small>SIDE</small><strong className={spotHedgeSide === "BUY" ? "bid" : "ask"}>{spotHedgeSide}</strong></div>
                      <div><small>MANUAL SPOT QTY</small><strong>{spotQuantityIsValid ? `${spotAllocationNumber.toFixed(2)} BTC` : "INVALID"}</strong></div>
                      <div><small>BEST {spotHedgeSide === "BUY" ? "ASK" : "BID"} REFERENCE</small><strong>{spotReferencePrice === null ? "—" : formatUsd(spotReferencePrice)}</strong></div>
                      <div><small>INDICATIVE NOTIONAL</small><strong>{spotQuantityIsValid && spotReferencePrice !== null ? formatCompactUsd(spotAllocationNumber * spotReferencePrice) : "—"}</strong></div>
                    </div>
                  ) : (
                    <p>
                      Live execution reference unavailable while Kraken is {marketStatus.toLowerCase()}.
                      Simulated hedge accounting remains isolated from market connectivity.
                    </p>
                  )}
                  <small className="candidate-disclaimer">
                    Market reference only — no venue comparison, order routing, optimizer, recommendation, or real order submission.
                  </small>
                </div>

                <div className="decision-actions">
                  <button
                    className="primary-action"
                    disabled={busy || hedgeOrdersCreated || !validAllocation || demoHedgeQuantity === 0}
                    onClick={handleCreateHedgeOrders}
                    type="button"
                  >
                    CREATE HEDGE ORDERS
                  </button>
                  <button
                    className="secondary-action"
                    disabled={busy || !canReviseAllocation}
                    onClick={handleCancelAndRevise}
                    type="button"
                  >
                    CANCEL &amp; EDIT ALLOCATION
                  </button>
                </div>

                <div className="simulation-controls">
                  <div className="simulation-controls-heading">
                    <strong>DEMO FILL CONTROLS</strong>
                    <span>Temporary controls — future fills arrive from exchange execution reports.</span>
                  </div>
                  <div className="decision-actions">
                    <button
                      className="secondary-action"
                      disabled={busy || !canSimulateHalfFill}
                      onClick={handleHalfFills}
                      type="button"
                    >
                      SIMULATE PARTIAL FILLS (~50%)
                    </button>
                    <button
                      className="secondary-action"
                      disabled={busy || !canFillRemainder}
                      onClick={handleFillRemainder}
                      type="button"
                    >
                      FILL REMAINDER
                    </button>
                  </div>
                </div>

                {allHedgeOrdersFilled && totalDelta === 0 && workingDelta === 0 && (
                  <div className="hedge-complete" role="status">
                    <span className="status-check">✓</span>
                    <div>
                      <strong>DELTA NEUTRAL</strong>
                      <p>
                        Actual total delta is 0.00 BTC. Spot inventory may remain non-zero when perpetuals carry part of the hedge.
                      </p>
                    </div>
                  </div>
                )}
              </div>
            )}
          </Panel>

          <Panel title="Live Event Tape" meta="Newest first">
            {visibleEvents.length > 0 ? (
              <div className="event-tape">
                {[...visibleEvents].reverse().map((event) => (
                  <div className="event-row" key={event.event_id}>
                    <time>{formatTime(event.occurred_at)}</time>
                    <span>{event.event_type}</span>
                    <p>{describeEvent(event)}</p>
                  </div>
                ))}
              </div>
            ) : (
              <EmptyState title="No events" detail={pendingRfq ? "Waiting for the backend pricing response." : "Waiting for the first automatic client RFQ."} />
            )}
          </Panel>
        </section>

        <aside className="right-rail">
          <Panel title="Position & Risk" meta="RISK POLICY NOT CONFIGURED">
            <div className="position-panel">
              <div className="position-heading">
                <div><span className="eyebrow">Current total delta</span><strong>{formatBtc(totalDelta)}</strong></div>
                <span className="neutral-tag">ACTUAL POSITION</span>
              </div>
              <div className="position-grid">
                <div><small>SPOT INVENTORY</small><strong>{formatBtc(Number(deskState.spot_inventory_btc))}</strong></div>
                <div><small>DERIVATIVE DELTA</small><strong>{formatBtc(Number(deskState.derivative_delta_btc))}</strong></div>
                <div><small>WORKING ORDER DELTA</small><strong>{formatBtc(Number(deskState.working_order_delta_btc))}</strong></div>
                <div><small>PROJECTED DELTA</small><strong>{formatBtc(projectedDelta)}</strong></div>
              </div>
              <p className="future-note">Desk state v{deskState.version}. Soft/hard limits and GREEN/YELLOW/RED logic are intentionally deferred.</p>
            </div>
          </Panel>

          <Panel title="Desk PnL" meta="NOT CALCULATED">
            <UnavailableFeature title="PnL accounting unavailable" detail="No spread capture, fees, MTM, funding, or total PnL is calculated in this step." compact />
          </Panel>

          <Panel title="Hedge Blotter" meta={`${hedgeOrders.length} orders · ${hedgeFills.length} fills`} grow>
            {hedgeOrders.length === 0 ? (
              <EmptyState title="No hedge orders" detail="Create a manual allocation after the client fill." />
            ) : (
              <div className="hedge-blotter">
                {hedgeOrders.map((order) => (
                  <div key={order.hedge_order_id}>
                    <span>
                      <strong>{order.instrument_type === "SPOT" ? "SPOT" : "PERP"} · {order.side}</strong>
                      <small>{order.venue} · SIMULATED</small>
                    </span>
                    <span className="blotter-progress">
                      <strong>{formatQuantity(order.filled_quantity_btc)} / {formatQuantity(order.quantity_btc)} BTC</strong>
                      <small className={order.status === "FILLED" ? "positive" : "warning"}>{order.status.replace("_", " ")}</small>
                    </span>
                  </div>
                ))}
                {hedgeFills.length > 0 && (
                  <div className="fill-summary">
                    <span>
                      <strong>EXECUTION LEDGER</strong>
                      <small>{hedgeFills.length} immutable simulated fill{hedgeFills.length === 1 ? "" : "s"}</small>
                    </span>
                    <span className="positive">{formatQuantity(String(hedgeFills.reduce((sum, fill) => sum + Number(fill.quantity_btc), 0)))} BTC</span>
                  </div>
                )}
              </div>
            )}
          </Panel>
        </aside>
      </section>

      <footer className="terminal-footer">
        <span>LIVE MARKET: KRAKEN + COINBASE SPOT/PERP · DEMO CLIENT QUOTE · SIMULATED HEDGE EXECUTION</span>
        <span>{flowActive ? "FLOW ACTIVE" : "FLOW PAUSED"} · {mode === "manual" ? "MANUAL HEDGE" : "AUTO DEFERRED"}</span>
      </footer>
    </main>
  );
}

function Panel({ title, meta, children, grow = false }: { title: string; meta: string; children: React.ReactNode; grow?: boolean }) {
  return <section className={`panel ${grow ? "panel-grow" : ""}`}><header><h2>{title}</h2><span>{meta}</span></header>{children}</section>;
}

function Metric({ label, value, tone }: { label: string; value: string; tone?: "positive" | "warning" }) {
  return <div className="metric"><span>{label}</span><strong className={tone}>{value}</strong></div>;
}

function LineItem({ label, value }: { label: string; value: string }) {
  return <div className="line-item"><span>{label}</span><strong>{value}</strong></div>;
}

function Spinner() {
  return <span className="status-spinner" aria-hidden="true" />;
}

function EmptyState({ title, detail, roomy = false }: { title: string; detail: string; roomy?: boolean }) {
  return <div className={`empty-state ${roomy ? "roomy" : ""}`}><strong>{title}</strong><span>{detail}</span></div>;
}

function UnavailableFeature({ title, detail, compact = false }: { title: string; detail: string; compact?: boolean }) {
  return <div className={`unavailable-feature ${compact ? "compact" : ""}`}><span className="unavailable-mark">—</span><div><strong>{title}</strong><p>{detail}</p></div></div>;
}

function activeRfqMeta(
  pending: PendingClientFlow | null,
  scenario: DemoScenarioResult | null,
): string {
  if (pending) return `${pending.rfq.rfq_id} · PRICING`;
  if (!scenario) return "NO ACTIVE RFQ";
  return `${scenario.rfq.rfq_id} · AUTO-ACCEPTED & FILLED`;
}

function marketIdentity(state: MarketStateView | null): string {
  return state
    ? `${state.venue}:${state.instrument_type}:${state.symbol}`
    : "";
}

function describeEvent(event: FlowEvent): string {
  switch (event.event_type) {
    case "RFQ_RECEIVED":
      return `${String(event.payload.client_side)} ${payloadNumber(event, "quantity_btc").toFixed(2)} BTC`;
    case "RFQ_VALIDATED":
      return `${formatCompactUsd(payloadNumber(event, "notional_usd"))} · notional > $500K`;
    case "QUOTE_GENERATED":
      return `Client quote ${formatUsd(payloadNumber(event, "quoted_price_usd"))}`;
    case "QUOTE_ACCEPTED":
      return "AUTO_ACCEPT · no client decision model";
    case "CLIENT_FILL":
      return `Desk spot change ${formatBtc(payloadNumber(event, "desk_spot_change_btc"))}`;
    case "HEDGE_ORDER_CREATED":
      return `${String(event.payload.instrument_type)} ${String(event.payload.side)} ${formatBtc(payloadNumber(event, "quantity_btc"))} · working only`;
    case "HEDGE_FILL":
      return `${String(event.payload.instrument_type)} ${String(event.payload.side)} ${formatBtc(payloadNumber(event, "quantity_btc"))} @ ${formatUsd(payloadNumber(event, "fill_price_usd"))}`;
    case "HEDGE_ORDER_UPDATED":
      return `${String(event.payload.status).replace("_", " ")} · ${formatBtc(payloadNumber(event, "remaining_quantity_btc"))} remaining`;
    case "HEDGE_ORDERS_CANCELLED":
      return "Unfilled hedge orders cancelled · allocation returned to draft";
    case "POSITION_UPDATED":
      return `Actual ${formatBtc(payloadNumber(event, "total_delta_btc"))} · working ${formatBtc(payloadNumber(event, "working_order_delta_btc"))} · state v${event.desk_state_version_after}`;
    default:
      return event.aggregate_id;
  }
}

function clientTradeDescription(scenario: DemoScenarioResult): string {
  return scenario.rfq.client_side === "BUY"
    ? "Client buys BTC from desk"
    : "Client sells BTC to desk";
}

function formatPricingSource(source: string): string {
  return source === "DEMO_KRAKEN_TOUCH_AUTO_ACCEPT"
    ? "DEMO KRAKEN TOUCH"
    : source.replaceAll("_", " ");
}

function signedHedgeOrderQuantity(order: HedgeOrder): number {
  const quantity = Number(order.remaining_quantity_btc);
  return order.side === "BUY" || order.side === "LONG" ? quantity : -quantity;
}

function payloadNumber(event: FlowEvent, key: string): number {
  const value = event.payload[key];
  if (typeof value === "number") return value;
  if (typeof value === "string") return Number(value);
  return 0;
}

function apiErrorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback;
}

function roundBtc(value: number): number {
  return Math.round((value + Number.EPSILON) * 100) / 100;
}

function formatQuantity(value: string): string {
  return Number(value).toFixed(2);
}

function formatBookQuantity(value: number): string {
  return new Intl.NumberFormat("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 4,
  }).format(value);
}

function formatBtc(value: number): string {
  const sign = value > 0 ? "+" : "";
  return `${sign}${value.toFixed(2)} BTC`;
}

function formatUsd(value: number): string {
  return new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 }).format(value);
}

function formatCompactUsd(value: number): string {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    notation: "compact",
    maximumFractionDigits: 1,
  }).format(value);
}

function formatSignedCompactUsd(value: number): string {
  const sign = value > 0 ? "+" : "";
  return `${sign}${formatCompactUsd(value)}`;
}

function formatTime(value: string): string {
  return new Intl.DateTimeFormat("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(new Date(value));
}
