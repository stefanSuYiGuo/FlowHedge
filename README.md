# FlowHedge

FlowHedge is an institutional crypto sales-trading simulator. The current
checkpoint contains the reviewable trading-terminal layout and a deterministic
backend accounting chain from RFQ through client fill and desk-state update.
Exchange connectivity, production pricing, hedge optimization, execution
simulation, risk, and PnL logic remain deferred.

## Current layout

- **Header:** instrument, reference price, venue connectivity, and manual/auto hedge mode.
- **Desk strip:** net delta, risk state, PnL, client response model, and flow state.
- **Left rail:** executable multi-venue market view, asynchronous RFQ inbox, flow pause/resume, and manual RFQ injection.
- **Center stage:** active RFQ, client quote construction, hedge recommendation, trader allocation controls, and event tape.
- **Right rail:** desk inventory and risk, PnL attribution, and hedge blotter.

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
python -m uvicorn backend.main:app --reload --port 8000
```

Then open:

- API health: <http://localhost:8000/health>
- Interactive API documentation: <http://localhost:8000/docs>

The frontend currently uses static placeholder state and does not call this API.

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

The React terminal now reads the fixed scenario from FastAPI instead of showing
invented trading results. Use **Inject RFQ** to replay the backend event chain:

`PRICING → AUTO-ACCEPTED → CLIENT FILLED → POSITION UPDATED`

The first run moves the desk from flat to `-5 BTC` spot inventory and total
delta. A repeated run is identified as a replay and cannot book the same client
trade twice. **Reset Demo** returns the backend and UI to version zero.

Risk state, hedge recommendations, hedge orders, and PnL remain visibly marked
as unavailable until their accounting steps are implemented.

The frontend defaults to `http://127.0.0.1:8000`. To use a different local API,
set `NEXT_PUBLIC_FLOWHEDGE_API_URL` before starting the frontend.

## Validate

```bash
npm run build
npm test
source .venv/bin/activate
python -m pytest
```
