# FlowHedge

FlowHedge is an institutional crypto sales-trading simulator. The current
checkpoint contains the reviewable trading-terminal layout, a backend-driven
institutional client-flow simulator, and an accounting chain from RFQ through
client fill, manual Spot/Perp hedge orders, simulated fills, and desk-state
updates. RiskPolicy v1 is active, and the public market universe now contains
six Spot/Perpetual candidates across Kraken, Coinbase, and OKX. Production
pricing, hedge optimization, smart routing, real execution, and PnL logic
remain deferred.

## Current layout

- **Header:** instrument, live Kraken Spot mid-price, independent API/market connectivity, and manual/auto hedge mode.
- **Desk strip:** actual, working, and projected delta plus Spot and derivative positions.
- **Left rail:** selectable Kraken/Coinbase/OKX Spot and Perp compact books, derivatives context, multi-order RFQ inbox, and backend flow pause/resume controls.
- **Center stage:** pending/accepted demo client quotes, live-but-not-optimized Kraken hedge reference, manual hedge allocation and simulated fill controls, and event tape.
- **Right rail:** reconciled desk positions, live RiskPolicy assessment, deferred PnL, and the hedge order/fill blotter.

No countdown or prediction of the next client RFQ is shown. Orders are modeled
as asynchronous arrivals.

## Run the frontend

Prerequisites: Node.js 22.13 or newer.

```bash
cd FlowHedge
npm install
npm run dev
```

Open <http://localhost:3000> in a browser. The development server refreshes the
page automatically when frontend files change. Stop it with `Control-C`.

## Run the backend shell

Open a second terminal:

```bash
cd FlowHedge
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r backend/requirements.txt
python -m uvicorn backend.main:app --port 8000
```

Then open:

- API health: <http://localhost:8000/health>
- Interactive API documentation: <http://localhost:8000/docs>

The frontend reads demo state and actions from this API.

For a stable demo, use the command above without the development reloader. If
the page remains in `API CONNECTING`, stop any older backend with `Control-C`,
start this command again, and confirm that `/health` returns before refreshing
the page. Frontend requests time out after five seconds and recover
automatically when the backend becomes available.

The Kraken, Coinbase, and OKX adapters use public market data only. No account,
API key, or real-order permission is required.

## Step 2 accounting demo

The backend exposes a fixed, repeatable scenario for checking the accounting
rules before the UI is connected:

- `POST /demo/reset` returns the desk to a flat version-zero state.
- `POST /demo/run-client-trade` processes a valid USD 590,000 client BUY RFQ,
  auto-accepts a clearly marked fixture quote, records one immutable client
  trade, and moves desk spot inventory and total delta from `0` to `-5 BTC`.
- Repeating the same demo call returns an idempotent replay and does not book the
  trade twice.
- `POST /rfqs/validate` checks that RFQ notional is strictly greater than USD
  500,000 using the supplied reference price.
- `GET /desk/state` and `GET /events` expose the resulting aggregate state and
  causal event sequence.

The fixture quote is not the future pricing engine.

## Step 3 frontend integration

The React terminal reads scenario and accounting state from FastAPI instead of
showing invented trading results. The original deterministic endpoint remains
available as a regression-test seam for this event chain:

`PRICING → AUTO-ACCEPTED → CLIENT FILLED → POSITION UPDATED`

The deterministic test run moves the desk from flat to `-5 BTC` spot inventory
and total delta. A repeated run is identified as a replay and cannot book the
same client trade twice. The normal page no longer exposes a manual Inject RFQ
button. **Reset Demo** clears all current client and hedge state.

The later Step 7 adds RiskPolicy without changing this fill-based accounting
chain. Automatic hedge recommendations and PnL remain unavailable.

The frontend defaults to `http://127.0.0.1:8000`. To use a different local API,
set `NEXT_PUBLIC_FLOWHEDGE_API_URL` before starting the frontend.

## Step 4 hedge execution demo

Manual mode exposes an explicit reference target of `0 BTC`. Spot and Perp are
independently editable with at most two decimal places. A trader can fully hedge
to the reference target or submit a smaller allocation and intentionally retain
residual exposure; the combined allocation cannot exceed the current exposure.

- `POST /demo/hedge-orders` validates and records both manual Spot and Perp
  instructions. Actual positions do not change until fills; only working and
  projected delta change.
- `POST /demo/hedge-orders/{order_id}/fills` records an immutable simulated
  fill. Only these fills change Spot inventory or derivative delta.
- `POST /demo/hedge-orders/cancel` cancels an untouched hedge batch so its
  allocation can be edited. Once any fill exists, history cannot be rewritten.
- Clearly labelled demo controls can apply partial fills, then fill the
  remainder so actual and working delta move in opposite directions while
  projected exposure remains reconciled. A completed batch does not prevent a
  later client exposure from receiving a new manual hedge batch, and completed
  orders remain visible in the hedge blotter.
- `GET /demo/hedge-orders` and `GET /demo/hedge-fills` restore the blotter after
  a page refresh. Reset clears client trades, hedge orders, fills, and events.

The UI shows the live maximum available beside each input after accounting for
the other input, while the API independently rejects over-hedging. The zero
reference is a labeled demo assumption, not a Risk Policy output. Manual
allocation is not presented as a Hedge Optimizer recommendation, and the fixed
fill prices and fill controls are not real market execution.

## Step 5 Kraken public market data

FastAPI starts a Kraken Spot WebSocket v2 adapter with its application
lifecycle and subscribes to the public `BTC/USD` level-2 book at depth 25 plus
instrument metadata. Every exchange update is applied in event order and
validated with Kraken's top-10 CRC32 checksum before it can replace the latest
normalized book.

- `GET /market/books/KRAKEN/BTC-USD` returns the latest book, instrument rules,
  data age, and connection state.
- `GET /market/connections` returns venue-adapter connection states.
- The backend marks connecting, live, stale, disconnected, and reconnecting
  states and retries with bounded backoff after a connection failure.
- The frontend polls the latest backend view every 250ms. It does not request
  Kraken directly.
- In-memory market state is bounded to the current book and metadata for each
  venue/symbol. It stores no historical tick stream and does not grow with the
  number of updates.

The live Kraken best bid or ask can be displayed as a manual Spot market candidate,
but it is explicitly not a hedge recommendation and does not change the fixed
client quote or simulated fill accounting. The adapter interface, canonical
symbols, normalized models, registry, and keyed state store leave room for
additional venues and instruments without coupling them to the UI.

This step intentionally does not include a second venue, Kraken Futures/Perp,
private API keys, real orders, Risk Policy, Hedge Optimizer, smart order
routing, Pricing Engine, fees, funding, margin, PnL, or historical market-data
storage.

## Step 5 slow automatic client flow

The normal demo is now driven by a backend Client Flow Simulator rather than a
page button. In manual-trader mode it waits a randomly jittered slow interval of
75–105 seconds between arrivals, centered around roughly 90 seconds. The next
arrival time is intentionally not exposed to the UI.

- Every generated RFQ uses the captured Kraken mid-price to calculate a varied
  two-decimal BTC quantity whose notional is strictly greater than USD 500,000.
  The generator produces both whole and fractional quantities and both client
  BUY and SELL sides.
- A new RFQ first remains in `PRICING` so the page can show a spinner. The demo
  then quotes at the captured Kraken touch, auto-accepts the quote, records the
  client trade, and updates inventory, delta, notional, RFQ history, and events.
- This touch-price rule is labelled `DEMO_KRAKEN_TOUCH_AUTO_ACCEPT`; it is a
  transparent simulation rule, not the future Pricing Engine.
- New client trades do not wait for previous hedge orders to finish. Existing
  working hedge delta remains intact while each accepted client fill changes
  actual inventory and projected exposure.
- `GET /demo/workspace` gives the frontend one coherent polling view. Pause and
  resume affect the backend arrival process, while Reset clears the session and
  restarts its slow schedule.
- `POST /demo/client-flow/generate` remains available only as a test-support
  seam; the normal page never calls or displays it.

## Step 6 unified multi-venue market state

At the Step 6 checkpoint, the normalized market layer supported multiple venues and multiple
instrument types without allowing Spot and Perpetual books with the same
canonical symbol to collide. The live universe for that checkpoint was:

- Kraken `BTC/USD` Spot.
- Coinbase `BTC-USD` Spot.
- Coinbase International `BTC-PERP-INTX` linear Perpetual.

Coinbase Spot and Perpetual L2 data come from the public Advanced Trade market
data WebSocket. Product rules come from Coinbase's public product endpoint. No
Coinbase account, private API key, or order permission is used.

- `GET /market/books/{venue}/{instrument_type}/{symbol}` returns one explicitly
  typed market. The original Spot route remains available for compatibility.
- `GET /market/snapshots/BTC` atomically returns every registered BTC market,
  one snapshot version, eligibility, stale/unavailable reasons, current books,
  product rules, and feed connection state.
- Connection identity belongs to a specific feed rather than only a venue, so
  future Spot and derivatives adapters can fail independently when they use
  separate connections.
- Coinbase retains a bounded 2,000-level working buffer per side and publishes
  only the current normalized depth-25 book. No historical updates accumulate.
- The page polls the unified snapshot and lets the trader inspect each live
  market while the accepted Step 5 RFQ and manual hedge behavior continues to
  use Kraken Spot.

Coinbase Perpetual is quoted and settled in USDC. This checkpoint explicitly
uses `USDC ≈ USD` as a configurable `1:1 demo assumption`; it does not silently
rename the quote currency. Contract structure, multiplier, settlement asset,
tick, quantity increment, and minimum quantity are preserved for the future
Cost Engine. Funding cost is intentionally deferred and is not included in any
calculation.

This step does not yet compare execution costs, recommend hedges, route orders,
or change RFQ pricing. Those remain responsibilities of the later Cost Engine,
Hedge Optimizer, SOR, and Pricing Engine steps.

## Step 7 RiskPolicy v1

RiskPolicy answers only whether directional delta should be hedged and how much
exposure should be removed. It does not choose Spot versus Perpetual, select a
venue, optimize a route, or create hedge orders.

The configurable values below are visibly labelled **DEMO DESK ASSUMPTIONS**.
They are not OSL internal risk limits:

- Soft delta limit: USD 1,000,000.
- Hard delta limit: USD 3,000,000.
- RED target: flat.
- Hard-breach grace period: five seconds.

GREEN warehouses actual exposure at or below the soft limit. YELLOW targets the
signed soft-limit boundary. RED targets flat. Classification uses actual,
fill-based delta; working orders only reduce the remaining hedge requirement and
contribute to projected delta. Conflict and overhedge guards protect future auto
execution from unsafe working-order state.

The independent BTC risk reference is the median of fresh Kraken and Coinbase
USD Spot mids. One healthy source is accepted in degraded mode. If neither is
available, RiskPolicy returns `UNAVAILABLE / HOLD` rather than silently returning
GREEN or inventing a price.

A RED breach owns a stable breach ID and five-second timer. Market ticks, desk
version changes, API polling, and browser refreshes do not reset it. Exiting RED
cancels the countdown; remaining RED emits one idempotent
`AUTO_HEDGE_REQUIRED` event. Step 7 deliberately does not create a fake optimal
hedge or any automatic order.

- `GET /risk/assessment` returns the current assessment and breach lifecycle.
- `GET /demo/workspace` includes the same assessment beside desk, RFQ, hedge,
  and event state.

## Step 7.5 executable and derivatives market data

The public candidate universe is now six markets:

- Kraken BTC/USD Spot and `PI_XBTUSD` inverse Perpetual.
- Coinbase BTC/USD Spot and `BTC-PERP-INTX` linear Perpetual.
- OKX BTC/USDT Spot and `BTC-USDT-SWAP` linear Perpetual.

The page renders only the top five levels. The backend separately retains up to
200 real L2 levels per side where the venue supplies them; it never synthesizes
or extrapolates missing liquidity. `GET
/market/executable-books/{venue}/{instrument_type}/{symbol}` exposes that bounded
book for the future Cost Engine.

Derivative source quantities are converted into BTC equivalent inside the
market layer from live instrument metadata. Linear contract quantities use the
venue contract value; Kraken inverse quantities are converted at each price
level. No venue contract multiplier is hard-coded in optimizer-facing data.

Where public data exists, the normalized derivative context retains mark,
index, current/predicted funding, funding timing, open interest in native/BTC/USD
units, basis, and separate observation timestamps. Unavailable fields remain
null. A bounded five-second observation series covers roughly one hour for
future 5-minute/1-hour open-interest changes without becoming an unbounded tick
database. Funding is data only: there is no expected funding-cost calculation
until a future hedge horizon is defined.

USDT≈USD and USDC≈USD are centralized, explicit 1:1 demo assumptions. Stale or
disconnected books are ineligible, and each adapter fails independently so one
venue cannot terminate the others.

## Validate

```bash
npm run build
npm test
source .venv/bin/activate
python -m pytest
```
