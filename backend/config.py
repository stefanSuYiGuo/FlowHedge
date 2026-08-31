"""Centralized demo configuration with explicit responsibility boundaries."""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class ClientFlowConfig(BaseModel):
    """RFQ admission/simulation rules; deliberately separate from risk appetite."""

    model_config = ConfigDict(frozen=True)

    minimum_rfq_notional_usd: Decimal = Field(default=Decimal("500000"), gt=0)


class RiskPolicyConfig(BaseModel):
    """Configurable demo assumptions, not actual OSL internal risk limits."""

    model_config = ConfigDict(frozen=True)

    policy_version: str = "RISK_POLICY_V1_1"
    assumption_label: str = "DEMO DESK ASSUMPTIONS"
    soft_delta_limit_usd: Decimal = Field(default=Decimal("1000000"), gt=0)
    hard_delta_limit_usd: Decimal = Field(default=Decimal("3000000"), gt=0)
    auto_hedge_target_ratio_of_soft: Decimal = Field(
        default=Decimal("0.90"), gt=0, lt=1
    )
    hard_breach_grace_seconds: Decimal = Field(default=Decimal("5"), gt=0)


class DemoDeskConfig(BaseModel):
    """Explicit economics assumptions for the interview demo runtime."""

    model_config = ConfigDict(frozen=True)

    assumption_label: str = "DEMO DESK ASSUMPTION"
    fee_disclaimer: str = (
        "NOT ACTUAL OSL OR VENUE INSTITUTIONAL FEES"
    )
    taker_fee_bps: Decimal = Field(default=Decimal("2.0"), ge=0)
    default_expected_hedge_horizon_seconds: int = Field(
        default=14400,
        gt=0,
    )


class StablecoinQuoteConfig(BaseModel):
    """Explicit MVP conversion assumptions; never silently rename stablecoins."""

    model_config = ConfigDict(frozen=True)

    usdt_usd_rate: Decimal = Field(default=Decimal("1"), gt=0)
    usdc_usd_rate: Decimal = Field(default=Decimal("1"), gt=0)
    usdt_assumption_label: str = "USDT_USD_1_TO_1_DEMO"
    usdc_assumption_label: str = "USDC_USD_1_TO_1_DEMO"


class PricingConfig(BaseModel):
    """Explicit Step 12 RFQ pricing assumptions for the interview demo."""

    model_config = ConfigDict(frozen=True)

    model_version: str = "EXECUTABLE_MULTI_VENUE_SPOT_L2_V1"
    assumption_label: str = "DEMO PRICING ASSUMPTION"
    base_client_margin_bps: Decimal = Field(default=Decimal("5.0"), ge=0)
    client_price_increment_usd: Decimal = Field(default=Decimal("0.10"), gt=0)
    quote_validity_seconds: int = Field(default=5, gt=0)
    reference_source: str = "ELIGIBLE_SPOT_MEDIAN"


client_flow_config = ClientFlowConfig()
risk_policy_config = RiskPolicyConfig()
demo_desk_config = DemoDeskConfig()
stablecoin_quote_config = StablecoinQuoteConfig()
pricing_config = PricingConfig()
