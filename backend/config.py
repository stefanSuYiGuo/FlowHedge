"""Centralized demo configuration with explicit responsibility boundaries."""

from __future__ import annotations

from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class RedTargetMode(str, Enum):
    FLAT = "FLAT"


class ClientFlowConfig(BaseModel):
    """RFQ admission/simulation rules; deliberately separate from risk appetite."""

    model_config = ConfigDict(frozen=True)

    minimum_rfq_notional_usd: Decimal = Field(default=Decimal("500000"), gt=0)


class RiskPolicyConfig(BaseModel):
    """Configurable demo assumptions, not actual OSL internal risk limits."""

    model_config = ConfigDict(frozen=True)

    policy_version: str = "RISK_POLICY_V1"
    assumption_label: str = "DEMO DESK ASSUMPTIONS"
    soft_delta_limit_usd: Decimal = Field(default=Decimal("1000000"), gt=0)
    hard_delta_limit_usd: Decimal = Field(default=Decimal("3000000"), gt=0)
    red_target_mode: RedTargetMode = RedTargetMode.FLAT
    hard_breach_grace_seconds: Decimal = Field(default=Decimal("5"), gt=0)


class StablecoinQuoteConfig(BaseModel):
    """Explicit MVP conversion assumptions; never silently rename stablecoins."""

    model_config = ConfigDict(frozen=True)

    usdt_usd_rate: Decimal = Field(default=Decimal("1"), gt=0)
    usdc_usd_rate: Decimal = Field(default=Decimal("1"), gt=0)
    usdt_assumption_label: str = "USDT_USD_1_TO_1_DEMO"
    usdc_assumption_label: str = "USDC_USD_1_TO_1_DEMO"


client_flow_config = ClientFlowConfig()
risk_policy_config = RiskPolicyConfig()
stablecoin_quote_config = StablecoinQuoteConfig()
