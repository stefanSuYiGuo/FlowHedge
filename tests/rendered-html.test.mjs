import assert from "node:assert/strict";
import test from "node:test";

async function render() {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);

  return worker.fetch(
    new Request("http://localhost/", { headers: { accept: "text/html" } }),
    { ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) } },
    { waitUntil() {}, passThroughOnException() {} },
  );
}

test("server-renders the FlowHedge terminal shell", async () => {
  const response = await render();
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i);

  const html = await response.text();
  assert.match(html, /<title>FlowHedge — Institutional Crypto Sales Trading<\/title>/i);
  assert.match(html, /FLOWHEDGE/);
  assert.match(html, /Hedge Decision Workspace · Simulated Execution/);
  assert.match(html, /Live Market Data · Multi-Venue/);
  assert.match(html, /Kraken, Coinbase, and OKX public adapters/);
  assert.match(html, /MARKET LOADING/);
  assert.match(html, /Demo Client Quote · Active RFQ/);
  assert.match(html, /RFQ Inbox/);
  assert.match(html, /Orders arrive asynchronously/);
  assert.match(html, /RISK REFERENCE LOADING/);
  assert.match(html, /DEMO DESK ASSUMPTIONS/);
  assert.match(html, /MANUAL MODE · STEP 7/);
  assert.match(html, /No exposure to hedge/);
  assert.match(html, /PnL accounting unavailable/);
  assert.match(html, /No hedge orders/);
  assert.match(html, /RESET DEMO/);
  assert.match(html, /Waiting for institutional flow/);
  assert.match(html, /introduce valid institutional RFQs asynchronously/);
  assert.doesNotMatch(html, /INJECT RFQ/);
  assert.doesNotMatch(html, /value="0\.10"/);
  assert.doesNotMatch(html, /\+\$9,390/);
  assert.doesNotMatch(html, /FUTURE STEP/);
  assert.doesNotMatch(html, /Next client RFQ|countdown|codex-preview/i);
});
