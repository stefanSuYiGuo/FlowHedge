import type {
  DemoScenarioResult,
  DeskState,
  FlowEvent,
  HedgeCancellationResult,
  HedgeFill,
  HedgeFillResult,
  HedgeOrder,
  HedgeOrderBatchResult,
} from "./types";

const configuredBaseUrl = process.env.NEXT_PUBLIC_FLOWHEDGE_API_URL;

export const API_BASE_URL = (
  configuredBaseUrl && configuredBaseUrl.trim().length > 0
    ? configuredBaseUrl
    : "http://127.0.0.1:8000"
).replace(/\/$/, "");

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    cache: "no-store",
    ...init,
    headers: {
      Accept: "application/json",
      ...init?.headers,
    },
  });

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

export function createManualHedgeOrders(
  spotQuantityBtc: string,
): Promise<HedgeOrderBatchResult> {
  return request<HedgeOrderBatchResult>("/demo/hedge-orders", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      batch_id: "step4-manual-hedge",
      spot_quantity_btc: spotQuantityBtc,
    }),
  });
}

export function cancelUnfilledHedgeOrders(): Promise<HedgeCancellationResult> {
  return request<HedgeCancellationResult>("/demo/hedge-orders/cancel", {
    method: "POST",
  });
}

export function simulateHedgeFill(
  orderId: string,
  quantityBtc: number,
  fillId: string,
): Promise<HedgeFillResult> {
  return request<HedgeFillResult>(`/demo/hedge-orders/${orderId}/fills`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      hedge_fill_id: fillId,
      quantity_btc: quantityBtc.toString(),
    }),
  });
}
