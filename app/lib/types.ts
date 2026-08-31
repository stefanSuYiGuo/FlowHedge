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
export type HedgeOrderOrigin = "MANUAL" | "SYSTEM_ADVISORY" | "AUTO_RISK";
export type HedgeSide = "BUY" | "SELL" | "LONG" | "SHORT";
export type HedgeOrderStatus = "OPEN" | "PARTIALLY_FILLED" | "FILLED" | "CANCELLED";
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

export type PricingStatus =
  | "OK"
  | "NO_ELIGIBLE_SPOT_MARKETS"
  | "INSUFFICIENT_LIQUIDITY"
  | "INVALID_REQUEST";

export interface PricingAdjustment {
  adjustment_type:
    | "EXPECTED_TAKER_FEE"
    | "BASE_CLIENT_MARGIN"
    | "CLIENT_PRICE_ROUNDING"
    | "INVENTORY_SKEW";
  amount_bps: string | null;
  amount_usd: string;
  assumption_label: string;
}

export interface PricingLiquidityLeg {
  venue: string;
  instrument_id: string;
  instrument_type: "SPOT" | "PERPETUAL";
  quantity_btc: string;
  execution_vwap_usd: string;
  executed_notional_usd: string;
  expected_taker_fee_bps: string;
  expected_fee_usd: string;
  usd_conversion_rate: string;
  usd_conversion_assumption: string;
}

export interface PricingResult {
  pricing_result_id: string;
  request_id: string;
  rfq_id: string;
  model_version: string;
  status: PricingStatus;
  status_reason: string | null;
  client_side: ClientSide;
  requested_quantity_btc: string;
  priced_quantity_btc: string;
  unpriced_quantity_btc: string;
  market_snapshot_version: number;
  snapshot_captured_at: string;
  reference_mid_usd: string | null;
  reference_source: string;
  executable_replacement_vwap_usd: string | null;
  executed_notional_usd: string;
  expected_market_impact_bps: string | null;
  expected_market_impact_usd: string | null;
  expected_fee_bps: string | null;
  expected_fee_usd: string;
  client_margin_bps: string;
  client_margin_usd: string;
  rounding_adjustment_usd: string;
  expected_gross_edge_usd: string | null;
  final_quote_price_usd: string | null;
  client_price_increment_usd: string;
  quote_validity_seconds: number;
  assumption_label: string;
  economics_disclosure: string;
  liquidity_legs: PricingLiquidityLeg[];
  adjustments: PricingAdjustment[];
  excluded_markets: string[];
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
  pricing_result_id: string | null;
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
  source_plan_id: string | null;
  source_intervention_id: string | null;
  source_breach_id: string | null;
  native_quantity: string | null;
  native_quantity_unit: string | null;
  market_snapshot_version: number | null;
  expected_vwap_usd: string | null;
  arrival_mid_usd: string | null;
  expected_taker_fee_bps: string | null;
  expected_fee_usd: string | null;
  expected_price_cost_usd: string | null;
  expected_all_in_cost_usd: string | null;
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
  venue: string | null;
  market_snapshot_version: number | null;
  arrival_mid_usd: string | null;
  expected_vwap_usd: string | null;
  filled_notional_usd: string | null;
  taker_fee_bps: string | null;
  fee_usd: string | null;
  slippage_vs_expected_usd: string | null;
  implementation_shortfall_usd: string | null;
  all_in_cost_usd: string | null;
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
  pricing_result: PricingResult | null;
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

export type PnLStatus = "COMPLETE" | "PARTIAL" | "UNRECONCILED";
export type PnLAttributionStatus = "COMPLETE" | "PARTIAL";

export interface PnLPosition {
  bucket_id: string;
  instrument_type: "SPOT" | "PERPETUAL";
  venue: string | null;
  instrument_id: string;
  signed_quantity_btc: string;
  average_entry_price_usd: string | null;
  mark_price_usd: string | null;
  gross_realized_pnl_usd: string;
  unrealized_mtm_usd: string | null;
  valuation_status: "FLAT" | "VALUED" | "MARK_UNAVAILABLE";
  valuation_method: string;
  data_quality_flags: string[];
}

export interface PnLSnapshot {
  status: PnLStatus;
  as_of: string;
  desk_state_version: number;
  market_snapshot_version: number | null;
  currency: "USD";
  valuation_method: "AVERAGE_COST_BTC_EQUIVALENT_LINEARIZED_USD";
  spot_mark_usd: string | null;
  gross_realized_pnl_usd: string;
  trading_fees_usd: string | null;
  net_realized_pnl_usd: string | null;
  spot_unrealized_mtm_usd: string | null;
  perp_unrealized_mtm_usd: string | null;
  total_desk_pnl_usd: string | null;
  client_spread_capture_usd: string;
  hedge_slippage_vs_expected_usd: string | null;
  hedge_implementation_shortfall_usd: string | null;
  inventory_market_movement_usd: string | null;
  reconciliation_difference_usd: string | null;
  reconciled: boolean;
  attribution_status: PnLAttributionStatus;
  positions: PnLPosition[];
  data_quality_flags: string[];
}

export interface DemoWorkspaceState {
  client_flow: ClientFlowState;
  desk_state: DeskState;
  risk_assessment: RiskAssessment;
  advisory_recommendation: AdvisoryHedgeRecommendation;
  auto_hedge_intervention: AutoHedgeIntervention | null;
  hedge_orders: HedgeOrder[];
  hedge_fills: HedgeFill[];
  execution_batches: ExecutionBatchMetrics[];
  events: FlowEvent[];
  pnl_snapshot: PnLSnapshot;
}

export type AutoHedgeInterventionStatus =
  | "STARTING"
  | "EXECUTING"
  | "REOPTIMIZING"
  | "INCOMPLETE"
  | "BLOCKED"
  | "COMPLETE"
  | "CANCELLED";

export interface AutoHedgeIntervention {
  intervention_id: string;
  breach_id: string;
  status: AutoHedgeInterventionStatus;
  started_at: string;
  completed_at: string | null;
  target_notional_usd: string;
  latest_risk_assessment_id: string;
  current_exposure_usd: string | null;
  latest_auto_remaining_hedge_btc: string | null;
  active_plan_id: string | null;
  active_plan: HedgePlan | null;
  generated_plan_ids: string[];
  auto_order_ids: string[];
  planned_quantity_btc: string;
  filled_quantity_btc: string;
  reason_codes: string[];
}

export type HedgePlanStatus =
  | "FULLY_FEASIBLE"
  | "PARTIALLY_FEASIBLE"
  | "NO_FEASIBLE_HEDGE"
  | "OPTIMIZATION_BLOCKED"
  | "NO_HEDGE_REQUIRED";

export interface SimulatedExecutionFill {
  price: string;
  quantity_btc: string;
}

export interface HedgeLeg {
  leg_id: string;
  candidate_id: string;
  venue: "KRAKEN" | "COINBASE" | "OKX";
  instrument_id: string;
  instrument_type: "SPOT" | "PERPETUAL";
  side: "BUY" | "SELL";
  quantity_btc: string;
  native_quantity: string;
  native_quantity_unit: string;
  expected_vwap: string;
  expected_notional_usd: string;
  expected_immediate_cost_bps: string;
  expected_immediate_cost_usd: string;
  funding_applicability: "APPLIED" | "NOT_APPLICABLE";
  expected_funding_cost_bps: string;
  expected_funding_cost_usd: string;
  expected_total_cost_bps: string;
  expected_total_cost_usd: string;
  market_snapshot_version: number;
  expected_fills: SimulatedExecutionFill[];
  data_quality_flags: string[];
}

export interface MarginalSelectionFact {
  sequence: number;
  candidate_id: string;
  venue: "KRAKEN" | "COINBASE" | "OKX";
  instrument_type: "SPOT" | "PERPETUAL";
  quantity_btc: string;
  expected_marginal_cost_usd_per_btc: string;
  reason_code: string;
}

export interface CandidateExclusionFact {
  candidate_id: string;
  venue: "KRAKEN" | "COINBASE" | "OKX";
  instrument_type: "SPOT" | "PERPETUAL";
  reason: string;
}

export interface HedgePlan {
  plan_id: string;
  optimization_id: string;
  mode: "ADVISORY" | "AUTO_RISK";
  status: HedgePlanStatus;
  generated_at: string;
  desk_state_version: number;
  risk_assessment_id: string;
  market_snapshot_version: number;
  actual_delta_btc: string;
  target_delta_btc: string;
  qualifying_working_order_delta_btc: string;
  requested_hedge_delta_btc: string;
  allocated_hedge_delta_btc: string;
  residual_unallocated_delta_btc: string;
  expected_holding_seconds: number | null;
  legs: HedgeLeg[];
  total_expected_cost_usd: string | null;
  total_expected_cost_bps: string | null;
  projected_delta_btc: string;
  projected_delta_notional_usd: string | null;
  fully_feasible: boolean;
  data_quality_flags: string[];
  explanation_data: {
    allocator_method: string;
    selection_facts: MarginalSelectionFact[];
    excluded_candidate_facts: CandidateExclusionFact[];
    residual_reason: string | null;
  };
}

export type AdvisoryLifecycleStatus =
  | "NOT_REQUIRED"
  | "AVAILABLE"
  | "PARTIALLY_FEASIBLE"
  | "NO_FEASIBLE_HEDGE"
  | "BLOCKED"
  | "REJECTED"
  | "AUTO_HANDOFF_PENDING";

export interface AdvisoryHedgeRecommendation {
  lifecycle_status: AdvisoryLifecycleStatus;
  plan: HedgePlan | null;
  can_use_system_plan: boolean;
  reason_codes: string[];
  expected_holding_seconds: number | null;
  holding_horizon_status: "CONFIGURED" | "UNAVAILABLE_SPOT_ONLY";
  demo_taker_fee_bps: string | null;
  economics_assumption_label: string | null;
  fee_disclaimer: string | null;
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
  advisory_target_delta_btc: string | null;
  advisory_gross_required_hedge_delta_btc: string | null;
  advisory_remaining_hedge_requirement_btc: string | null;
  /** Compatibility aliases for the advisory fields above. */
  target_delta_btc: string | null;
  gross_required_hedge_delta_btc: string | null;
  remaining_hedge_requirement_btc: string | null;
  auto_hedge_target_ratio_of_soft: string;
  auto_hedge_target_notional_usd: string;
  auto_hedge_target_delta_btc: string | null;
  auto_gross_required_hedge_delta_btc: string | null;
  auto_qualifying_working_order_delta_btc: string | null;
  auto_remaining_hedge_requirement_btc: string | null;
  auto_working_order_conflict: boolean;
  auto_working_order_overhedge: boolean;
  working_order_delta_btc: string;
  projected_delta_btc: string;
  working_order_conflict: boolean;
  working_order_overhedge: boolean;
  hard_breach_id: string | null;
  hard_breach_started_at: string | null;
  hard_breach_seconds_remaining: string | null;
  auto_hedge_required: boolean;
  auto_hedge_active: boolean;
  auto_hedge_complete: boolean;
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

export interface ManualHedgeLegRequest {
  venue: "COINBASE" | "KRAKEN" | "OKX";
  instrument_type: "SPOT" | "PERPETUAL";
  quantity_btc: string;
}

export interface ManualExecutionLegPreview {
  venue: "COINBASE" | "KRAKEN" | "OKX";
  instrument_id: string;
  instrument_type: "SPOT" | "PERPETUAL";
  side: "BUY" | "SELL";
  requested_quantity_btc: string;
  executable_quantity_btc: string;
  unfilled_quantity_btc: string;
  status: string;
  status_reason: string | null;
  market_snapshot_version: number;
  arrival_mid_usd: string | null;
  expected_vwap_usd: string | null;
  spread_cost_bps: string | null;
  depth_impact_bps: string | null;
  taker_fee_bps: string | null;
  expected_fee_usd: string | null;
  expected_price_cost_usd: string | null;
  expected_all_in_cost_usd: string | null;
}

export interface ManualHedgePreview {
  preview_id: string;
  request_id: string;
  created_at: string;
  expires_at: string;
  desk_state_version: number;
  market_snapshot_version: number;
  actual_delta_btc: string;
  advisory_target_delta_btc: string | null;
  maximum_hedge_quantity_btc: string;
  submitted_hedge_delta_btc: string;
  projected_delta_btc: string;
  can_submit: boolean;
  reason_codes: string[];
  legs: ManualExecutionLegPreview[];
  total_expected_fee_usd: string | null;
  total_expected_all_in_cost_usd: string | null;
}

export interface ExecutionOrderMetrics {
  hedge_order_id: string;
  venue: string;
  instrument_id: string;
  instrument_type: "SPOT" | "PERPETUAL";
  side: string;
  execution_source: string;
  status: string;
  market_snapshot_version: number | null;
  ordered_quantity_btc: string;
  filled_quantity_btc: string;
  remaining_quantity_btc: string;
  expected_vwap_usd: string | null;
  realized_vwap_usd: string | null;
  arrival_mid_usd: string | null;
  slippage_vs_expected_usd: string;
  implementation_shortfall_usd: string;
  taker_fee_bps: string | null;
  fee_usd: string;
  filled_notional_usd: string;
  all_in_cost_usd: string;
}

export interface ExecutionBatchMetrics {
  execution_id: string;
  batch_id: string;
  origin: HedgeOrderOrigin;
  executed_at: string;
  status: "FILLED" | "PARTIALLY_FILLED" | "UNFILLED";
  market_snapshot_version: number;
  requested_quantity_btc: string;
  filled_quantity_btc: string;
  remaining_quantity_btc: string;
  expected_vwap_usd: string | null;
  realized_vwap_usd: string | null;
  filled_notional_usd: string;
  implementation_shortfall_usd: string;
  slippage_vs_expected_usd: string;
  fee_usd: string;
  all_in_cost_usd: string;
  orders: ExecutionOrderMetrics[];
}

export interface ManualHedgeSubmission {
  order_batch: HedgeOrderBatchResult;
  preview: ManualHedgePreview;
}
