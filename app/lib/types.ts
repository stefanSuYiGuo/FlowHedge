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
export type HedgeOrderOrigin = "MANUAL";
export type HedgeSide = "BUY" | "SELL" | "LONG" | "SHORT";
export type HedgeOrderStatus = "OPEN" | "PARTIALLY_FILLED" | "FILLED";
export type MarketConnectionStatus =
  | "CONNECTING"
  | "LIVE"
  | "STALE"
  | "DISCONNECTED"
  | "RECONNECTING";
export type JsonValue =
  | string
  | number
  | boolean
  | null
  | JsonValue[]
  | { [key: string]: JsonValue };

export interface MarketObservation {
  venue: string;
  instrument_id: string;
  instrument_type: "SPOT" | "PERPETUAL";
  bid: string;
  ask: string;
  observed_at: string;
}

export interface MarketLevel {
  price: string;
  quantity: string;
}

export interface InstrumentRules {
  venue: string;
  symbol: string;
  venue_symbol: string;
  instrument_type: "SPOT" | "PERPETUAL";
  base_asset: string;
  quote_asset: string;
  price_increment: string;
  quantity_increment: string;
  quantity_min: string;
  price_precision: number;
  quantity_precision: number;
  status: string;
  contract_structure: "SPOT" | "LINEAR" | "INVERSE";
  contract_multiplier: string;
  contract_value_currency: string | null;
  native_quantity_unit: string;
  settlement_asset: string;
  usd_conversion_rate: string;
  usd_conversion_assumption: string | null;
  received_at: string;
}

export interface DerivativeMarketContext {
  venue: string;
  symbol: string;
  venue_symbol: string;
  mark_price: string | null;
  index_price: string | null;
  current_funding_rate: string | null;
  predicted_funding_rate: string | null;
  next_funding_time: string | null;
  funding_interval_seconds: number | null;
  open_interest: string | null;
  open_interest_unit: string | null;
  open_interest_btc_equivalent: string | null;
  open_interest_usd: string | null;
  mark_price_captured_at: string | null;
  index_price_captured_at: string | null;
  funding_captured_at: string | null;
  open_interest_captured_at: string | null;
  received_at: string;
  source: string;
  basis_bps: string | null;
  basis_reference_price_usd: string | null;
  basis_captured_at: string | null;
}

export interface NormalizedOrderBook {
  venue: string;
  symbol: string;
  venue_symbol: string;
  instrument_type: "SPOT" | "PERPETUAL";
  depth: number;
  bids: MarketLevel[];
  asks: MarketLevel[];
  best_bid: string;
  best_ask: string;
  mid_price: string;
  spread: string;
  spread_bps: string;
  exchange_timestamp: string;
  received_at: string;
  checksum: number | null;
  source_sequence: number | null;
}

export interface MarketConnectionState {
  feed_id: string;
  venue: string;
  status: MarketConnectionStatus;
  endpoint: string;
  connected_at: string | null;
  last_message_at: string | null;
  last_book_update_at: string | null;
  last_error: string | null;
  reconnect_attempt: number;
}

export interface MarketStateView {
  venue: string;
  symbol: string;
  instrument_type: "SPOT" | "PERPETUAL";
  connection: MarketConnectionState;
  book: NormalizedOrderBook | null;
  instrument: InstrumentRules | null;
  derivatives: DerivativeMarketContext | null;
  executable_bid_levels: number;
  executable_ask_levels: number;
  book_data_age_ms: number | null;
  derivative_data_age_ms: number | null;
  derivative_data_stale: boolean | null;
  eligible: boolean;
  exclusion_reason: string | null;
  as_of: string;
}

export interface UnifiedMarketSnapshot {
  snapshot_version: number;
  captured_at: string;
  base_asset: string;
  markets: MarketStateView[];
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

export interface HedgeOrder {
  hedge_order_id: string;
  batch_id: string;
  origin: HedgeOrderOrigin;
  venue: string;
  instrument_id: string;
  instrument_type: "SPOT" | "PERPETUAL";
  side: HedgeSide;
  quantity_btc: string;
  filled_quantity_btc: string;
  remaining_quantity_btc: string;
  status: HedgeOrderStatus;
  created_at: string;
  created_desk_state_version: number;
}

export interface HedgeFill {
  hedge_fill_id: string;
  hedge_order_id: string;
  instrument_id: string;
  instrument_type: "SPOT" | "PERPETUAL";
  side: HedgeSide;
  quantity_btc: string;
  fill_price_usd: string;
  filled_at: string;
  execution_source: string;
}

export interface FlowEvent {
  event_id: string;
  event_type: string;
  occurred_at: string;
  aggregate_id: string;
  correlation_id: string;
  desk_state_version_before: number;
  desk_state_version_after: number;
  payload: Record<string, JsonValue>;
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

export interface PendingClientFlow {
  correlation_id: string;
  market_snapshot: MarketSnapshot;
  rfq: RFQ;
}

export interface ClientFlowState {
  active: boolean;
  mode: "MANUAL_SLOW_FLOW";
  pending_rfqs: PendingClientFlow[];
  completed_scenarios: DemoScenarioResult[];
  completed_count: number;
}

export interface DemoWorkspaceState {
  client_flow: ClientFlowState;
  desk_state: DeskState;
  risk_assessment: RiskAssessment;
  hedge_orders: HedgeOrder[];
  hedge_fills: HedgeFill[];
  events: FlowEvent[];
}

export type RiskBand = "GREEN" | "YELLOW" | "RED" | "UNAVAILABLE";
export type RiskAction = "WAREHOUSE" | "PARTIAL_HEDGE" | "IMMEDIATE_HEDGE" | "HOLD";

export interface RiskAssessment {
  assessment_id: string;
  assessed_at: string;
  policy_version: string;
  assumption_label: "DEMO DESK ASSUMPTIONS";
  desk_state_version: number;
  market_snapshot_version: number;
  reference_price_usd: string | null;
  reference_price_degraded: boolean;
  reference_price_source: string;
  actual_delta_btc: string;
  signed_delta_notional_usd: string | null;
  absolute_delta_exposure_usd: string | null;
  risk_band: RiskBand;
  action: RiskAction;
  target_delta_btc: string | null;
  gross_required_hedge_delta_btc: string | null;
  working_order_delta_btc: string;
  projected_delta_btc: string;
  remaining_hedge_requirement_btc: string | null;
  working_order_conflict: boolean;
  working_order_overhedge: boolean;
  hard_breach_id: string | null;
  hard_breach_started_at: string | null;
  hard_breach_seconds_remaining: string | null;
  auto_hedge_required: boolean;
  auto_hedge_blocked: boolean;
  auto_hedge_blocked_reasons: string[];
  inventory_or_settlement_state: "NOT_EVALUATED";
}

export interface HedgeOrderBatchResult {
  replayed: boolean;
  batch_id: string;
  demo_target_total_delta_btc: string;
  required_hedge_delta_btc: string;
  submitted_hedge_delta_btc: string;
  projected_total_delta_btc: string;
  orders: HedgeOrder[];
  desk_state_before: DeskState;
  desk_state_after: DeskState;
  events: FlowEvent[];
}

export interface HedgeFillResult {
  replayed: boolean;
  fill: HedgeFill;
  order: HedgeOrder;
  desk_state_before: DeskState;
  desk_state_after: DeskState;
  events: FlowEvent[];
}

export interface HedgeCancellationResult {
  cancelled_hedge_order_ids: string[];
  desk_state_before: DeskState;
  desk_state_after: DeskState;
  events: FlowEvent[];
}
