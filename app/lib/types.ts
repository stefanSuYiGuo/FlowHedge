export type ClientSide = "BUY" | "SELL";
export type RFQStatus =
  | "RECEIVED"
  | "PRICING"
  | "QUOTED"
  | "FILLED"
  | "EXPIRED"
  | "REJECTED";
export type QuoteStatus =
  | "ACTIVE"
  | "ACCEPTED"
  | "EXPIRED"
  | "SUPERSEDED"
  | "REJECTED";

export interface MarketObservation {
  venue: string;
  instrument_id: string;
  instrument_type: "SPOT" | "PERPETUAL";
  bid: string;
  ask: string;
  observed_at: string;
}

export interface MarketSnapshot {
  market_snapshot_id: string;
  version: number;
  captured_at: string;
  base_asset: string;
  quote_currency: string;
  reference_price_usd: string;
  observations: MarketObservation[];
}

export interface RFQ {
  rfq_id: string;
  client_id: string;
  instrument_id: string;
  client_side: ClientSide;
  quantity_btc: string;
  received_at: string;
  status: RFQStatus;
  validation_market_snapshot_id: string;
  validation_reference_price_usd: string;
  validated_notional_usd: string;
}

export interface Quote {
  quote_id: string;
  rfq_id: string;
  revision: number;
  quoted_price_usd: string;
  quantity_btc: string;
  created_at: string;
  expires_at: string;
  status: QuoteStatus;
  market_snapshot_id: string;
  desk_state_version: number;
  pricing_source: string;
}

export interface ClientTrade {
  client_trade_id: string;
  rfq_id: string;
  quote_id: string;
  client_id: string;
  instrument_id: string;
  client_side: ClientSide;
  quantity_btc: string;
  trade_price_usd: string;
  traded_at: string;
}

export interface DeskState {
  version: number;
  as_of: string;
  spot_inventory_btc: string;
  derivative_delta_btc: string;
  total_delta_btc: string;
  open_hedge_order_ids: string[];
  working_order_delta_btc: string;
}

export interface FlowEvent {
  event_id: string;
  event_type: string;
  occurred_at: string;
  aggregate_id: string;
  correlation_id: string;
  desk_state_version_before: number;
  desk_state_version_after: number;
  payload: Record<string, string | number | boolean | null>;
}

export interface DemoScenarioResult {
  replayed: boolean;
  market_snapshot: MarketSnapshot;
  rfq: RFQ;
  quote: Quote;
  client_trade: ClientTrade;
  desk_state_before: DeskState;
  desk_state_after: DeskState;
  events: FlowEvent[];
}
