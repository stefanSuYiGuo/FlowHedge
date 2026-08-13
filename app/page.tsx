"use client";

import { useMemo, useState } from "react";

type TradingMode = "manual" | "auto";

const venues = [
  { name: "Binance", bid: "118,010", ask: "118,015", depth: "8.4" },
  { name: "OKX", bid: "118,006", ask: "118,010", depth: "7.1" },
  { name: "Kraken", bid: "118,009", ask: "118,018", depth: "4.2" },
  { name: "Coinbase", bid: "118,012", ask: "118,023", depth: "5.8" },
];

const rfqs = [
  { side: "BUY", size: "12.00", client: "INST-042", tier: "Tier 1", status: "FILLED", time: "14:32:08" },
  { side: "SELL", size: "4.00", client: "INST-018", tier: "Tier 2", status: "PRICING", time: "14:32:13" },
  { side: "BUY", size: "7.50", client: "INST-027", tier: "Tier 1", status: "QUEUED", time: "14:32:17" },
];

const initialEvents = [
  { time: "14:32:18", type: "HEDGE_PARTIAL", detail: "Binance Spot +3.00 / +8.00 BTC" },
  { time: "14:32:17", type: "RFQ_RECEIVED", detail: "INST-027 BUY 7.50 BTC · queued" },
  { time: "14:32:13", type: "RFQ_RECEIVED", detail: "INST-018 SELL 4.00 BTC · pricing" },
  { time: "14:32:09", type: "CLIENT_FILL", detail: "INST-042 BUY 12.00 @ 118,087" },
  { time: "14:32:08", type: "QUOTE_SENT", detail: "Bid 118,002 / Ask 118,087" },
];

export default function Home() {
  const [mode, setMode] = useState<TradingMode>("manual");
  const [flowActive, setFlowActive] = useState(true);
  const [spotQty, setSpotQty] = useState(4.5);

  const perpQty = useMemo(() => Math.max(0, 7 - spotQty), [spotQty]);

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
          <strong>118,024.50</strong>
          <span className="positive">+0.42%</span>
        </div>

        <div className="connection-state"><span />4 VENUES LIVE</div>

        <div className="mode-controls" aria-label="Trading mode">
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

      <section className="desk-strip" aria-label="Desk summary">
        <Metric label="Net delta" value="-7.00 BTC" />
        <Metric label="Delta notional" value="-$826.2K" />
        <Metric label="Risk state" value="YELLOW · PARTIAL HEDGE" tone="warning" />
        <Metric label="Desk PnL" value="+$9,390" tone="positive" />
        <Metric label="Client response" value="AUTO-ACCEPT" tone="positive" />
        <Metric label="Flow state" value={flowActive ? "ACTIVE" : "PAUSED"} tone={flowActive ? "positive" : "warning"} />
      </section>

      <section className="workspace-grid">
        <aside className="left-rail">
          <Panel title="Executable Market" meta="20 BTC VWAP">
            <table className="market-table">
              <thead><tr><th>VENUE</th><th>BID</th><th>ASK</th><th>DEPTH</th></tr></thead>
              <tbody>
                {venues.map((venue) => (
                  <tr key={venue.name}>
                    <td>{venue.name}</td><td className="bid">{venue.bid}</td><td className="ask">{venue.ask}</td><td>{venue.depth}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Panel>

          <Panel title="RFQ Inbox" meta={`${rfqs.length} active`} grow>
            <div className="rfq-list">
              {rfqs.map((rfq, index) => (
                <button className={`rfq-card ${index === 0 ? "active" : ""}`} key={`${rfq.client}-${rfq.time}`} type="button">
                  <span className={`rfq-side ${rfq.side === "BUY" ? "bid" : "ask"}`}>{rfq.side}</span>
                  <strong>{rfq.size} BTC</strong>
                  <span className={`status-tag status-${rfq.status.toLowerCase()}`}>{rfq.status}</span>
                  <small>{rfq.client} · {rfq.tier}</small>
                  <time>{rfq.time}</time>
                </button>
              ))}
            </div>
          </Panel>

          <div className="flow-controls">
            <div>
              <span className="eyebrow">Simulated client flow</span>
              <strong className={flowActive ? "positive" : "warning"}>{flowActive ? "ACTIVE" : "PAUSED"}</strong>
              <small>Orders arrive asynchronously</small>
            </div>
            <button type="button" onClick={() => setFlowActive((active) => !active)}>{flowActive ? "PAUSE" : "RESUME"}</button>
            <button type="button" className="accent-outline">INJECT RFQ</button>
          </div>
        </aside>

        <section className="center-stage">
          <Panel title="Active Client RFQ" meta="RFQ-10842 · QUOTE FILLED">
            <div className="active-rfq">
              <div>
                <div className="active-rfq-size"><span className="bid">BUY</span> 12.00 BTC</div>
                <p>INST-042 · Tier 1 · Client buys BTC from desk</p>
              </div>
              <span className="auto-accepted">AUTO-ACCEPTED · 380ms</span>
            </div>

            <div className="quote-section">
              <div className="quote-prices">
                <span className="eyebrow">Client quote</span>
                <div className="price-pair">
                  <div><small>BID</small><strong className="bid">118,002</strong></div>
                  <div><small>ASK · TRADED</small><strong className="ask">118,087</strong></div>
                </div>
              </div>
              <div className="quote-breakdown">
                <span className="eyebrow">Ask construction</span>
                <LineItem label="Hedge VWAP" value="118,020" />
                <LineItem label="Fees + latency" value="+21" />
                <LineItem label="Inventory skew" value="+18" />
                <LineItem label="Target edge" value="+28" />
              </div>
            </div>
          </Panel>

          <Panel title="Hedge Decision Workspace" meta={mode === "manual" ? "TRADER CONTROLLED" : "SYSTEM AUTO-EXECUTION"} grow>
            <div className="hedge-workspace">
              <span className="eyebrow">System recommendation · snapshot v1842</span>
              <div className="recommendation-grid">
                <div><small>TARGET HEDGE</small><strong>BUY 7.00 BTC</strong></div>
                <div><small>SPOT</small><strong>4.50</strong></div>
                <div><small>PERP</small><strong>2.50</strong></div>
                <div><small>EST. COST</small><strong>1.8 bps</strong></div>
              </div>

              <div className={`allocation-controls ${mode === "auto" ? "disabled" : ""}`}>
                <label>
                  <span>SPOT QUANTITY · BTC</span>
                  <input
                    disabled={mode === "auto"}
                    max="7"
                    min="0"
                    onChange={(event) => setSpotQty(Math.min(7, Math.max(0, Number(event.target.value))))}
                    step="0.1"
                    type="number"
                    value={spotQty}
                  />
                </label>
                <label>
                  <span>PERP REMAINDER · AUTO</span>
                  <input disabled type="number" value={perpQty.toFixed(1)} readOnly />
                </label>
                <label>
                  <span>SPOT EXECUTION</span>
                  <select disabled={mode === "auto"} defaultValue="smart">
                    <option value="smart">Smart route · 3 venues</option>
                    <option value="binance">Binance only</option>
                    <option value="okx">OKX only</option>
                    <option value="coinbase">Coinbase only</option>
                  </select>
                </label>
              </div>

              <div className="allocation-summary">
                <span>Spot {spotQty.toFixed(2)} BTC</span><span>+</span><span>Long Perp {perpQty.toFixed(2)} BTC</span><span>→</span><strong>Residual 0.00 BTC</strong>
              </div>

              {mode === "manual" ? (
                <div className="decision-actions"><button type="button" className="secondary-action">USE SYSTEM PLAN</button><button type="button" className="primary-action">EXECUTE MANUAL HEDGE</button></div>
              ) : (
                <div className="auto-mode-message"><span className="pulse-dot" />System will validate the current desk snapshot and execute the recommended allocation automatically.</div>
              )}
            </div>
          </Panel>

          <Panel title="Live Event Tape" meta="Newest first">
            <div className="event-tape">
              {initialEvents.map((event) => (
                <div className="event-row" key={`${event.time}-${event.type}`}><time>{event.time}</time><span>{event.type}</span><p>{event.detail}</p></div>
              ))}
            </div>
          </Panel>
        </section>

        <aside className="right-rail">
          <Panel title="Position & Risk" meta="BTC desk book">
            <div className="risk-panel">
              <div className="risk-heading"><div><span className="eyebrow">Current delta</span><strong>-7.00 BTC</strong></div><span className="warning">YELLOW</span></div>
              <div className="risk-track"><span /></div>
              <div className="risk-scale"><span>-15 hard</span><span>-5 soft</span><span>0</span><span>+5 soft</span><span>+15 hard</span></div>
              <LineItem label="Gross client flow" value="28.0 BTC" />
              <LineItem label="Internalized" value="6.0 BTC" />
              <LineItem label="Open hedge orders" value="5.0 BTC" />
            </div>
          </Panel>

          <Panel title="Desk PnL" meta="USD">
            <div className="pnl-list">
              <LineItem label="Client spread capture" value="+$12,430" tone="positive" />
              <LineItem label="Hedge execution" value="-$3,820" tone="negative" />
              <LineItem label="Fees" value="-$1,140" tone="negative" />
              <LineItem label="Inventory MTM" value="+$2,340" tone="positive" />
              <LineItem label="Funding" value="-$420" tone="negative" />
              <div className="pnl-total"><span>Total desk PnL</span><strong className="positive">+$9,390</strong></div>
            </div>
          </Panel>

          <Panel title="Hedge Blotter" meta="2 open" grow>
            <div className="hedge-blotter">
              <div><div><strong>BUY 8.00 BTC · SPOT</strong><small>Binance / OKX · SOR</small></div><span className="warning">3.00 / 8.00</span></div>
              <div><div><strong>BUY 4.00 BTC · PERP</strong><small>OKX · limit</small></div><span>QUEUED</span></div>
            </div>
          </Panel>
        </aside>
      </section>

      <footer className="terminal-footer">
        <span>SIMULATION · LIVE PUBLIC MARKET DATA / SIMULATED FILLS</span>
        <span>{flowActive ? "FLOW ACTIVE" : "FLOW PAUSED"} · {mode === "manual" ? "MANUAL HEDGE" : "AUTO HEDGE"}</span>
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

function LineItem({ label, value, tone }: { label: string; value: string; tone?: "positive" | "negative" }) {
  return <div className="line-item"><span>{label}</span><strong className={tone}>{value}</strong></div>;
}
