# FlowHedge

FlowHedge is an institutional crypto sales-trading simulator. This first
checkpoint contains the reviewable trading-terminal layout and a minimal
FastAPI shell. Exchange connectivity, client-flow generation, pricing, hedge
optimization, execution simulation, inventory, risk, and PnL logic are
intentionally deferred until the layout is approved.

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

## Validate

```bash
npm run build
npm test
```
