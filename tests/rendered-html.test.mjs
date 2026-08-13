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
  assert.match(html, /Hedge Decision Workspace/);
  assert.match(html, /RFQ Inbox/);
  assert.match(html, /Orders arrive asynchronously/);
  assert.doesNotMatch(html, /Next client RFQ|countdown|codex-preview/i);
});
