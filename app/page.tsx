"use client";

import { useEffect, useMemo, useState } from "react";

import {
  createManualHedgeOrders,
  getDemoScenario,
  getDeskState,
  getEvents,
  getHedgeFills,
  getHedgeOrders,
  resetDemo,
  runDemoClientTrade,
  simulateHedgeFill,
} from "./lib/api";
import type {
  DemoScenarioResult,
  DeskState,
  FlowEvent,
  HedgeFill,
  HedgeOrder,
} from "./lib/types";

type TradingMode = "manual" | "auto";
type DemoStage = "idle" | "pricing" | "accepted" | "filled";

const flatDeskState: DeskState = {
  version: 0,
  as_of: "",
  spot_inventory_btc: "0",
  derivative_delta_btc: "0",
  total_delta_btc: "0",
  open_hedge_order_ids: [],
  working_order_delta_btc: "0",
};

const delay = (milliseconds: number) =>
  new Promise((resolve) => window.setTimeout(resolve, milliseconds));

export default function Home() {
  const [mode, setMode] = useState<TradingMode>("manual");
  const [flowActive, setFlowActive] = useState(true);
  const [stage, setStage] = useState<DemoStage>("idle");
  const [scenario, setScenario] = useState<DemoScenarioResult | null>(null);
  const [deskState, setDeskState] = useState<DeskState>(flatDeskState);
  const [events, setEvents] = useState<FlowEvent[]>([]);
  const [hedgeOrders, setHedgeOrders] = useState<HedgeOrder[]>([]);
  const [hedgeFills, setHedgeFills] = useState<HedgeFill[]>([]);
  const [spotAllocation, setSpotAllocation] = useState("3.00");
  const [busy, setBusy] = useState(false);
  const [apiState, setApiState] = useState<"connecting" | "online" | "offline">(
    "connecting",
  );
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    let active = true;

    async function loadBackendState() {
      try {
        const [
          currentDeskState,
          currentScenario,
          currentEvents,
          currentHedgeOrders,
          currentHedgeFills,
        ] = await Promise.all([
          getDeskState(),
          getDemoScenario(),
          getEvents(),
          getHedgeOrders(),
          getHedgeFills(),
        ]);
        if (!active) return;
        setDeskState(currentDeskState);
        setScenario(currentScenario);
        setEvents(currentEvents);
        setHedgeOrders(currentHedgeOrders);
        setHedgeFills(currentHedgeFills);
        if (currentHedgeOrders.length > 0) {
          const existingSpotOrder = currentHedgeOrders.find(
            (order) => order.instrument_type === "SPOT",
          );
          setSpotAllocation(existingSpotOrder?.quantity_btc ?? "0");
        }
        setStage(currentScenario ? "filled" : "idle");
        setApiState("online");
      } catch {
        if (!active) return;
        setApiState("offline");
        setError(
          "Backend unavailable. Start the FastAPI service on port 8000, then refresh.",
        );
      }
    }

    void loadBackendState();
    return () => {
      active = false;
    };
  }, []);

  const referencePrice = scenario
    ? Number(scenario.market_snapshot.reference_price_usd)
    : null;
  const totalDelta = Number(deskState.total_delta_btc);
  const workingDelta = Number(deskState.working_order_delta_btc);
  const projectedDelta = totalDelta + workingDelta;
  const deltaNotional = referencePrice === null ? null : totalDelta * referencePrice;
  const demoHedgeQuantity = scenario
    ? Math.abs(Number(scenario.desk_state_after.total_delta_btc))
    : 0;
  const spotAllocationNumber =
    spotAllocation.trim() === "" ? Number.NaN : Number(spotAllocation);
  const perpAllocation = Number.isFinite(spotAllocationNumber)
    ? Number((demoHedgeQuantity - spotAllocationNumber).toFixed(8))
    : Number.NaN;
  const validAllocation =
    Number.isFinite(spotAllocationNumber) &&
    spotAllocationNumber >= 0 &&
    spotAllocationNumber <= demoHedgeQuantity;
  const hedgeOrdersCreated = hedgeOrders.length > 0;
  const allHedgeOrdersFilled =
    hedgeOrdersCreated && hedgeOrders.every((order) => order.status === "FILLED");
  const canSimulateHalfFill = hedgeOrders.some(
    (order) => Number(order.filled_quantity_btc) === 0,
  );
  const canFillRemainder = hedgeOrders.some(
    (order) => Number(order.remaining_quantity_btc) > 0,
  );
  const visibleEvents = useMemo(() => {
    if (!scenario) return [];
    if (stage === "accepted") return scenario.events.slice(0, 4);
    if (stage === "filled") return events;
    return [];
  }, [events, scenario, stage]);

  async function refreshHedgeState() {
    const [currentDeskState, currentHedgeOrders, currentHedgeFills, currentEvents] =
      await Promise.all([
        getDeskState(),
        getHedgeOrders(),
        getHedgeFills(),
        getEvents(),
      ]);
    setDeskState(currentDeskState);
    setHedgeOrders(currentHedgeOrders);
    setHedgeFills(currentHedgeFills);
    setEvents(currentEvents);
  }

  async function handleInjectRfq() {
    if (busy) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    setStage("pricing");

    try {
      const [result] = await Promise.all([runDemoClientTrade(), delay(650)]);
      setScenario(result);
      setApiState("online");

      if (result.replayed) {
        await refreshHedgeState();
        setStage("filled");
        setNotice("Replay detected — the existing client trade was not booked twice.");
        return;
      }

      setDeskState(result.desk_state_before);
      setStage("accepted");
      await delay(800);
      setDeskState(result.desk_state_after);
      setEvents(result.events);
      setStage("filled");
    } catch {
      setStage(scenario ? "filled" : "idle");
      setApiState("offline");
      setError("The RFQ could not be processed because the backend is unavailable.");
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
      setScenario(null);
      setEvents([]);
      setHedgeOrders([]);
      setHedgeFills([]);
      setSpotAllocation("3.00");
      setStage("idle");
      setApiState("online");
    } catch {
      setApiState("offline");
      setError("The demo could not be reset because the backend is unavailable.");
    } finally {
      setBusy(false);
    }
  }

  async function handleCreateHedgeOrders() {
    if (busy || !scenario || !validAllocation || hedgeOrdersCreated) return;
    setBusy(true);
    setError(null);
    setNotice(null);

    try {
      await createManualHedgeOrders(spotAllocationNumber, perpAllocation);
      await refreshHedgeState();
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

  async function handleHalfFills() {
    if (busy || !canSimulateHalfFill) return;
    setBusy(true);
    setError(null);
    setNotice(null);

    try {
      for (const order of hedgeOrders) {
        if (Number(order.filled_quantity_btc) !== 0) continue;
        await simulateHedgeFill(
          order.hedge_order_id,
          Number(order.quantity_btc) / 2,
          `${order.hedge_order_id}-half`,
        );
      }
      await refreshHedgeState();
      setApiState("online");
      setNotice("50% simulated fills applied — actual and working delta moved together.");
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
        "All hedge orders filled — actual total delta now matches the Step 4 demo target.",
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
          <strong>{referencePrice === null ? "—" : formatUsd(referencePrice)}</strong>
          <span className="fixture-label">FIXTURE</span>
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

      {error && <div className="api-error" role="alert">{error}</div>}
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
            title="Market Snapshot"
            meta={scenario ? `v${scenario.market_snapshot.version}` : "awaiting fixture"}
          >
            {scenario ? (
              <table className="market-table">
                <thead><tr><th>VENUE</th><th>TYPE</th><th>BID</th><th>ASK</th></tr></thead>
                <tbody>
                  {scenario.market_snapshot.observations.map((observation) => (
                    <tr key={`${observation.venue}-${observation.instrument_id}`}>
                      <td>{observation.venue}</td>
                      <td>{observation.instrument_type}</td>
                      <td className="bid">{formatUsd(Number(observation.bid))}</td>
                      <td className="ask">{formatUsd(Number(observation.ask))}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <EmptyState title="No market snapshot" detail="Inject the fixed RFQ to load the Step 3 fixture." />
            )}
          </Panel>

          <Panel title="RFQ Inbox" meta={stage === "idle" ? "0 active" : "1 active"} grow>
            <div className="rfq-list">
              {stage === "pricing" ? (
                <div className="rfq-card active incoming-rfq">
                  <span className="rfq-side">INCOMING</span>
                  <strong>Institutional RFQ</strong>
                  <span className="status-tag status-pricing"><Spinner /> PRICING</span>
                  <small>Validating notional and generating quote</small>
                </div>
              ) : scenario ? (
                <div className="rfq-card active">
                  <span className={`rfq-side ${scenario.rfq.client_side === "BUY" ? "bid" : "ask"}`}>
                    {scenario.rfq.client_side}
                  </span>
                  <strong>{formatQuantity(scenario.rfq.quantity_btc)} BTC</strong>
                  <span className={`status-tag ${stage === "accepted" ? "status-accepted" : "status-filled"}`}>
                    {stage === "accepted" ? "✓ ACCEPTED" : "FILLED"}
                  </span>
                  <small>{scenario.rfq.client_id} · {formatCompactUsd(Number(scenario.rfq.validated_notional_usd))}</small>
                  <time>{formatTime(scenario.rfq.received_at)}</time>
                </div>
              ) : (
                <EmptyState title="No client RFQs" detail="Use Inject RFQ to run the deterministic backend chain." />
              )}
            </div>
          </Panel>

          <div className="flow-controls">
            <div>
              <span className="eyebrow">Simulated client flow</span>
              <strong className={flowActive ? "positive" : "warning"}>{flowActive ? "ACTIVE" : "PAUSED"}</strong>
              <small>Orders arrive asynchronously</small>
            </div>
            <button disabled={busy} type="button" onClick={() => setFlowActive((active) => !active)}>
              {flowActive ? "PAUSE" : "RESUME"}
            </button>
            <button disabled={busy || apiState === "connecting"} type="button" className="accent-outline" onClick={handleInjectRfq}>
              {busy ? "PROCESSING" : "INJECT RFQ"}
            </button>
            <button disabled={busy || apiState === "connecting"} type="button" className="reset-button" onClick={handleReset}>
              RESET DEMO
            </button>
          </div>
        </aside>

        <section className="center-stage">
          <Panel title="Active Client RFQ" meta={activeRfqMeta(stage, scenario)}>
            {stage === "pricing" ? (
              <div className="active-rfq active-rfq-loading">
                <div><div className="active-rfq-size">Pricing incoming RFQ</div><p>Notional validation passed before the trade can enter the flow.</p></div>
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
                    <p>{scenario.rfq.client_id} · Client buys BTC from desk · {formatCompactUsd(Number(scenario.rfq.validated_notional_usd))}</p>
                  </div>
                  <span className="auto-accepted"><span className="status-check">✓</span> AUTO-ACCEPTED</span>
                </div>

                <div className="quote-section">
                  <div className="quote-prices">
                    <span className="eyebrow">Accepted client quote</span>
                    <div className="price-pair single-price">
                      <div><small>CLIENT ASK · TRADED</small><strong className="ask">{formatUsd(Number(scenario.quote.quoted_price_usd))}</strong></div>
                    </div>
                  </div>
                  <div className="quote-breakdown">
                    <span className="eyebrow">Quote provenance</span>
                    <LineItem label="Reference price" value={formatUsd(Number(scenario.market_snapshot.reference_price_usd))} />
                    <LineItem label="Quote revision" value={`R${scenario.quote.revision}`} />
                    <LineItem label="Desk state used" value={`v${scenario.quote.desk_state_version}`} />
                    <LineItem label="Pricing source" value="STEP 2 FIXTURE" />
                  </div>
                </div>
              </>
            ) : (
              <EmptyState title="No active client RFQ" detail="The backend is flat and ready for a deterministic client trade." roomy />
            )}
          </Panel>

          <Panel
            title="Hedge Decision Workspace"
            meta={mode === "manual" ? "MANUAL MODE · STEP 4" : "AUTO MODE · DEFERRED"}
            grow
          >
            {!scenario ? (
              <EmptyState
                title="No exposure to hedge"
                detail="Book the fixed client trade before creating a manual hedge allocation."
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
                    <small>STEP 4 DEMO TARGET</small>
                    <strong>0.00 BTC</strong>
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
                    <span>SPOT HEDGE · BTC</span>
                    <input
                      aria-label="Spot hedge quantity in BTC"
                      disabled={busy || hedgeOrdersCreated}
                      min="0"
                      max={demoHedgeQuantity}
                      step="0.25"
                      type="number"
                      value={spotAllocation}
                      onChange={(event) => setSpotAllocation(event.target.value)}
                    />
                  </label>
                  <label>
                    <span>PERP REMAINDER · BTC</span>
                    <input
                      aria-label="Calculated perpetual hedge quantity in BTC"
                      readOnly
                      value={validAllocation ? perpAllocation.toFixed(2) : "INVALID"}
                    />
                  </label>
                  <label>
                    <span>EXECUTION SOURCE</span>
                    <input readOnly value="FIXED STEP 4 SIMULATION" />
                  </label>
                </div>

                <div className="allocation-summary">
                  <span>Client fill creates {formatBtc(Number(scenario.desk_state_after.total_delta_btc))}</span>
                  <span>→</span>
                  <span>Manual hedge requires <strong>+{demoHedgeQuantity.toFixed(2)} BTC</strong></span>
                  <span>→</span>
                  <span>Target <strong>0.00 BTC</strong></span>
                </div>
                <p className="demo-policy-note">
                  This is an explicit demo target, not a Risk Policy recommendation. Orders affect working and projected delta; only fills affect actual positions.
                </p>

                <div className="decision-actions three-actions">
                  <button
                    className="primary-action"
                    disabled={busy || hedgeOrdersCreated || !validAllocation}
                    onClick={handleCreateHedgeOrders}
                    type="button"
                  >
                    CREATE HEDGE ORDERS
                  </button>
                  <button
                    className="secondary-action"
                    disabled={busy || !canSimulateHalfFill}
                    onClick={handleHalfFills}
                    type="button"
                  >
                    SIMULATE 50% FILLS
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

                {allHedgeOrdersFilled && (
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
                    <p>{describeEvent(event, scenario)}</p>
                  </div>
                ))}
              </div>
            ) : (
              <EmptyState title="No events" detail={stage === "pricing" ? "Waiting for the backend response." : "Run the client-trade demo to create the causal event sequence."} />
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
        <span>SIMULATION · FIXED MARKET / CLIENT FILL / HEDGE FILLS</span>
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

function activeRfqMeta(stage: DemoStage, scenario: DemoScenarioResult | null): string {
  if (stage === "pricing") return "PRICING";
  if (!scenario) return "NO ACTIVE RFQ";
  if (stage === "accepted") return `${scenario.rfq.rfq_id} · QUOTE ACCEPTED`;
  return `${scenario.rfq.rfq_id} · CLIENT FILLED`;
}

function describeEvent(event: FlowEvent, scenario: DemoScenarioResult | null): string {
  if (!scenario) return event.aggregate_id;
  switch (event.event_type) {
    case "RFQ_RECEIVED":
      return `${scenario.rfq.client_id} ${scenario.rfq.client_side} ${formatQuantity(scenario.rfq.quantity_btc)} BTC`;
    case "RFQ_VALIDATED":
      return `${formatCompactUsd(Number(scenario.rfq.validated_notional_usd))} · notional > $500K`;
    case "QUOTE_GENERATED":
      return `Client quote ${formatUsd(Number(scenario.quote.quoted_price_usd))}`;
    case "QUOTE_ACCEPTED":
      return "AUTO_ACCEPT · no client decision model";
    case "CLIENT_FILL":
      return `Desk spot change ${formatBtc(-Number(scenario.client_trade.quantity_btc))}`;
    case "HEDGE_ORDER_CREATED":
      return `${String(event.payload.instrument_type)} ${String(event.payload.side)} ${formatBtc(payloadNumber(event, "quantity_btc"))} · working only`;
    case "HEDGE_FILL":
      return `${String(event.payload.instrument_type)} ${String(event.payload.side)} ${formatBtc(payloadNumber(event, "quantity_btc"))} @ ${formatUsd(payloadNumber(event, "fill_price_usd"))}`;
    case "HEDGE_ORDER_UPDATED":
      return `${String(event.payload.status).replace("_", " ")} · ${formatBtc(payloadNumber(event, "remaining_quantity_btc"))} remaining`;
    case "POSITION_UPDATED":
      return `Actual ${formatBtc(payloadNumber(event, "total_delta_btc"))} · working ${formatBtc(payloadNumber(event, "working_order_delta_btc"))} · state v${event.desk_state_version_after}`;
    default:
      return event.aggregate_id;
  }
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

function formatQuantity(value: string): string {
  return Number(value).toFixed(2);
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
