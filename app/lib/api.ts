import type {
  AdvisoryHedgeRecommendation,
  ClientFlowState,
  DemoScenarioResult,
  DemoWorkspaceState,
  DeskState,
  FlowEvent,
  HedgeCancellationResult,
  HedgeFill,
  HedgeFillResult,
  HedgeOrder,
  HedgeOrderBatchResult,
  ExecutionBatchMetrics,
  ManualHedgeLegRequest,
  ManualHedgePreview,
  ManualHedgeSubmission,
  MarketStateView,
  UnifiedMarketSnapshot,
} from "./types";

const configuredBaseUrl = process.env.NEXT_PUBLIC_FLOWHEDGE_API_URL;
const REQUEST_TIMEOUT_MS = 5_000;

export const API_BASE_URL = (
  configuredBaseUrl && configuredBaseUrl.trim().length > 0
    ? configuredBaseUrl
    : "http://127.0.0.1:8000"
).replace(/\/$/, "");

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const controller = new AbortController();
  const timeout = globalThis.setTimeout(
    () => controller.abort(),
    REQUEST_TIMEOUT_MS,
  );
  let response: Response;

  try {
    response = await fetch(`${API_BASE_URL}${path}`, {
      cache: "no-store",
      ...init,
      headers: {
        Accept: "application/json",
        ...init?.headers,
      },
      signal: controller.signal,
    });
  } catch (error) {
    if (controller.signal.aborted) {
      throw new Error(`FlowHedge API request timed out after ${REQUEST_TIMEOUT_MS}ms`);
    }
    throw error;
  } finally {
    globalThis.clearTimeout(timeout);
  }

  if (!response.ok) {
    const body = await response.text();
    let message = body;
    try {
      const parsed = JSON.parse(body) as { detail?: string | { message?: string } };
      if (typeof parsed.detail === "string") message = parsed.detail;
      if (
        typeof parsed.detail === "object" &&
        parsed.detail !== null &&
        typeof parsed.detail.message === "string"
      ) {
        message = parsed.detail.message;
      }
    } catch {
      // Preserve a non-JSON backend response for diagnostics.
    }
    throw new Error(
      message || `FlowHedge API request failed with status ${response.status}`,
    );
  }

  return response.json() as Promise<T>;
}

export function getDeskState(): Promise<DeskState> {
  return request<DeskState>("/desk/state");
}

export function getDemoScenario(): Promise<DemoScenarioResult | null> {
  return request<DemoScenarioResult | null>("/demo/scenario");
}

export function getDemoWorkspace(): Promise<DemoWorkspaceState> {
  return request<DemoWorkspaceState>("/demo/workspace");
}

export function pauseClientFlow(): Promise<ClientFlowState> {
  return request<ClientFlowState>("/demo/client-flow/pause", { method: "POST" });
}

export function resumeClientFlow(): Promise<ClientFlowState> {
  return request<ClientFlowState>("/demo/client-flow/resume", { method: "POST" });
}

export function runDemoClientTrade(): Promise<DemoScenarioResult> {
  return request<DemoScenarioResult>("/demo/run-client-trade", {
    method: "POST",
  });
}

export function resetDemo(): Promise<DeskState> {
  return request<DeskState>("/demo/reset", { method: "POST" });
}

export function getEvents(): Promise<FlowEvent[]> {
  return request<FlowEvent[]>("/events");
}

export function getHedgeOrders(): Promise<HedgeOrder[]> {
  return request<HedgeOrder[]>("/demo/hedge-orders");
}

export function getHedgeFills(): Promise<HedgeFill[]> {
  return request<HedgeFill[]>("/demo/hedge-fills");
}

export function getMarketState(
  venue: string,
  instrumentType: "SPOT" | "PERPETUAL",
  symbol: string,
): Promise<MarketStateView> {
  return request<MarketStateView>(
    `/market/books/${encodeURIComponent(venue)}/${encodeURIComponent(instrumentType)}/${encodeURIComponent(symbol)}`,
  );
}

export function getUnifiedMarketSnapshot(
  baseAsset: string,
): Promise<UnifiedMarketSnapshot> {
  return request<UnifiedMarketSnapshot>(
    `/market/snapshots/${encodeURIComponent(baseAsset)}`,
  );
}

export function createManualHedgeOrders(
  spotQuantityBtc: string,
  perpQuantityBtc: string,
  batchId: string,
): Promise<HedgeOrderBatchResult> {
  return request<HedgeOrderBatchResult>("/demo/hedge-orders", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      batch_id: batchId,
      spot_quantity_btc: spotQuantityBtc,
      perp_quantity_btc: perpQuantityBtc,
    }),
  });
}

export function previewManualHedge(
  requestId: string,
  legs: ManualHedgeLegRequest[],
): Promise<ManualHedgePreview> {
  return request<ManualHedgePreview>("/demo/manual-hedges/preview", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ request_id: requestId, legs }),
  });
}

export function submitManualHedge(
  previewId: string,
): Promise<ManualHedgeSubmission> {
  return request<ManualHedgeSubmission>("/demo/manual-hedges/submit", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ preview_id: previewId }),
  });
}

export function executeHedgeBatch(
  batchId: string,
  executionId: string,
): Promise<ExecutionBatchMetrics> {
  return request<ExecutionBatchMetrics>(
    `/demo/execution-batches/${encodeURIComponent(batchId)}/execute`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ execution_id: executionId }),
    },
  );
}

export function acceptAdvisoryHedgePlan(
  planId: string,
): Promise<HedgeOrderBatchResult> {
  return request<HedgeOrderBatchResult>(
    `/demo/advisory-hedge-plans/${encodeURIComponent(planId)}/accept`,
    { method: "POST" },
  );
}

export function rejectAdvisoryHedgePlan(
  planId: string,
): Promise<AdvisoryHedgeRecommendation> {
  return request<AdvisoryHedgeRecommendation>(
    `/demo/advisory-hedge-plans/${encodeURIComponent(planId)}/reject`,
    { method: "POST" },
  );
}

export function cancelUnfilledHedgeOrders(): Promise<HedgeCancellationResult> {
  return request<HedgeCancellationResult>("/demo/hedge-orders/cancel", {
    method: "POST",
  });
}

export function simulateHedgeFill(
  orderId: string,
  quantityBtc: string | number,
  fillId: string,
): Promise<HedgeFillResult> {
  return request<HedgeFillResult>(`/demo/hedge-orders/${orderId}/fills`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      hedge_fill_id: fillId,
      quantity_btc: String(quantityBtc),
    }),
  });
}
