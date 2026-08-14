import type { DemoScenarioResult, DeskState } from "./types";

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
    throw new Error(
      body || `FlowHedge API request failed with status ${response.status}`,
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
