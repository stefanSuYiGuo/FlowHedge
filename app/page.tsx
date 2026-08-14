"use client";

import { useEffect, useMemo, useState } from "react";

import {
  getDemoScenario,
  getDeskState,
  resetDemo,
  runDemoClientTrade,
} from "./lib/api";
import type {
  DemoScenarioResult,
  DeskState,
  FlowEvent,
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
        const [currentDeskState, currentScenario] = await Promise.all([
          getDeskState(),
          getDemoScenario(),
        ]);
        if (!active) return;
        setDeskState(currentDeskState);
        setScenario(currentScenario);
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
  const deltaNotional = referencePrice === null ? null : totalDelta * referencePrice;
  const visibleEvents = useMemo(() => {
    if (!scenario) return [];
    if (stage === "accepted") return scenario.events.slice(0, 4);
    if (stage === "filled") return scenario.events;
    return [];
  }, [scenario, stage]);

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
        setDeskState(result.desk_state_after);
        setStage("filled");
        setNotice("Replay detected — the existing client trade was not booked twice.");
        return;
      }

      setDeskState(result.desk_state_before);
      setStage("accepted");
      await delay(800);
      setDeskState(result.desk_state_after);
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
      setStage("idle");
      setApiState("online");
    } catch {
      setApiState("offline");
      setError("The demo could not be reset because the backend is unavailable.");
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
        <Metric label="Spot inventory" value={formatBtc(Number(deskState.spot_inventory_btc))} />
        <Metric label="Derivative delta" value={formatBtc(Number(deskState.derivative_delta_btc))} />
        <Metric label="Client response" value="AUTO-ACCEPT" tone="positive" />
        <Metric label="Desk state" value={`VERSION ${deskState.version}`} />
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

          <Panel title="Hedge Decision Workspace" meta={`${mode.toUpperCase()} MODE · FUTURE STEP`} grow>
            <UnavailableFeature
              title="Hedge plan not generated"
              detail="Spot/perpetual allocation, hedge candidates, trader override, and execution belong to Step 4. No recommendation is being inferred from the current delta."
            />
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
                <span className="neutral-tag">NO RISK STATE</span>
              </div>
              <div className="position-grid">
                <div><small>SPOT INVENTORY</small><strong>{formatBtc(Number(deskState.spot_inventory_btc))}</strong></div>
                <div><small>DERIVATIVE DELTA</small><strong>{formatBtc(Number(deskState.derivative_delta_btc))}</strong></div>
                <div><small>WORKING ORDER DELTA</small><strong>{formatBtc(Number(deskState.working_order_delta_btc))}</strong></div>
                <div><small>STATE VERSION</small><strong>v{deskState.version}</strong></div>
              </div>
              <p className="future-note">Soft/hard limits and GREEN/YELLOW/RED logic are intentionally deferred.</p>
            </div>
          </Panel>

          <Panel title="Desk PnL" meta="NOT CALCULATED">
            <UnavailableFeature title="PnL accounting unavailable" detail="No spread capture, fees, MTM, funding, or total PnL is calculated in this step." compact />
          </Panel>

          <Panel title="Hedge Blotter" meta="0 orders" grow>
            <UnavailableFeature title="No hedge orders" detail="Recommendations and orders do not exist until the hedge workflow is implemented." compact />
          </Panel>
        </aside>
      </section>

      <footer className="terminal-footer">
        <span>SIMULATION · FIXED MARKET FIXTURE / API-BOOKED CLIENT FILL</span>
        <span>{flowActive ? "FLOW ACTIVE" : "FLOW PAUSED"} · {mode === "manual" ? "MANUAL" : "AUTO"} HEDGE PREVIEW</span>
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
    case "POSITION_UPDATED":
      return `Total delta ${formatBtc(Number(scenario.desk_state_after.total_delta_btc))} · state v${scenario.desk_state_after.version}`;
    default:
      return event.aggregate_id;
  }
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
