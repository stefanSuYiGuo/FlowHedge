"""Pure RiskPolicy v1 calculations and independent USD Spot reference price."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from ..config import RiskPolicyConfig, risk_policy_config
from ..domain.models import DeskState, InstrumentType
from ..market.models import MarketVenue, UnifiedMarketSnapshot
from .models import (
    InventoryOrSettlementState,
    RiskAction,
    RiskAssessment,
    RiskBand,
    RiskReferencePrice,
)


RISK_REFERENCE_VENUES = (MarketVenue.KRAKEN, MarketVenue.COINBASE)


def build_risk_reference_price(
    snapshot: UnifiedMarketSnapshot,
) -> RiskReferencePrice:
    """Use only fresh Kraken/Coinbase USD Spot mids for RiskPolicy v1."""

    eligible = []
    for market in snapshot.markets:
        if (
            market.venue in RISK_REFERENCE_VENUES
            and market.instrument_type is InstrumentType.SPOT
            and market.eligible
            and market.book is not None
            and market.instrument is not None
            and market.instrument.quote_asset == "USD"
        ):
            eligible.append(market)

    eligible.sort(key=lambda market: market.venue.value)
    if not eligible:
        return RiskReferencePrice(
            asset=snapshot.base_asset,
            captured_at=snapshot.captured_at,
            source="UNAVAILABLE",
            market_snapshot_version=snapshot.snapshot_version,
            eligible=False,
            degraded=True,
        )

    prices = sorted(market.book.mid_price for market in eligible if market.book)
    if len(prices) == 1:
        price = prices[0]
    else:
        midpoint = len(prices) // 2
        price = (
            prices[midpoint]
            if len(prices) % 2
            else (prices[midpoint - 1] + prices[midpoint]) / Decimal("2")
        )
    source = "MEDIAN(" + ",".join(
        f"{market.venue.value}:{market.symbol}:SPOT" for market in eligible
    ) + ")"
    return RiskReferencePrice(
        asset=snapshot.base_asset,
        price_usd=price,
        captured_at=max(
            market.book.exchange_timestamp for market in eligible if market.book
        ),
        source=source,
        market_snapshot_version=snapshot.snapshot_version,
        eligible=True,
        degraded=len(eligible) < len(RISK_REFERENCE_VENUES),
    )


class RiskPolicy:
    """Decide whether to hedge and how much delta to remove, never how to hedge."""

    def __init__(self, config: RiskPolicyConfig = risk_policy_config) -> None:
        self.config = config

    def evaluate(
        self,
        desk_state: DeskState,
        reference: RiskReferencePrice,
        *,
        assessed_at: datetime,
    ) -> RiskAssessment:
        actual = desk_state.total_delta_btc
        working = desk_state.working_order_delta_btc
        projected = actual + working
        assessment_id = (
            f"risk-{self.config.policy_version.lower()}-"
            f"d{desk_state.version}-m{reference.market_snapshot_version}"
        )

        if not reference.eligible or reference.price_usd is None:
            return RiskAssessment(
                assessment_id=assessment_id,
                assessed_at=assessed_at,
                policy_version=self.config.policy_version,
                assumption_label=self.config.assumption_label,
                desk_state_version=desk_state.version,
                market_snapshot_version=reference.market_snapshot_version,
                reference_price_usd=None,
                reference_price_degraded=True,
                reference_price_source=reference.source,
                actual_delta_btc=actual,
                signed_delta_notional_usd=None,
                absolute_delta_exposure_usd=None,
                risk_band=RiskBand.UNAVAILABLE,
                action=RiskAction.HOLD,
                target_delta_btc=None,
                gross_required_hedge_delta_btc=None,
                working_order_delta_btc=working,
                projected_delta_btc=projected,
                remaining_hedge_requirement_btc=None,
                working_order_conflict=False,
                working_order_overhedge=False,
                auto_hedge_blocked=True,
                auto_hedge_blocked_reasons=("RISK_REFERENCE_PRICE_UNAVAILABLE",),
                inventory_or_settlement_state=InventoryOrSettlementState.NOT_EVALUATED,
            )

        price = reference.price_usd
        signed_notional = actual * price
        absolute_exposure = abs(signed_notional)

        if absolute_exposure <= self.config.soft_delta_limit_usd:
            band = RiskBand.GREEN
            action = RiskAction.WAREHOUSE
            target = actual
        elif absolute_exposure <= self.config.hard_delta_limit_usd:
            band = RiskBand.YELLOW
            action = RiskAction.PARTIAL_HEDGE
            direction = Decimal("1") if actual > 0 else Decimal("-1")
            target = direction * self.config.soft_delta_limit_usd / price
        else:
            band = RiskBand.RED
            action = RiskAction.IMMEDIATE_HEDGE
            target = Decimal("0")

        gross = target - actual
        remaining, conflict, overhedge = self._remaining_requirement(gross, working)
        blocked_reasons: list[str] = []
        if conflict:
            blocked_reasons.append("WORKING_ORDER_CONFLICT")
        if overhedge:
            blocked_reasons.append("WORKING_ORDER_OVERHEDGE")
        if remaining == 0 and gross != 0:
            blocked_reasons.append("NO_REMAINING_HEDGE_REQUIREMENT")

        return RiskAssessment(
            assessment_id=assessment_id,
            assessed_at=assessed_at,
            policy_version=self.config.policy_version,
            assumption_label=self.config.assumption_label,
            desk_state_version=desk_state.version,
            market_snapshot_version=reference.market_snapshot_version,
            reference_price_usd=price,
            reference_price_degraded=reference.degraded,
            reference_price_source=reference.source,
            actual_delta_btc=actual,
            signed_delta_notional_usd=signed_notional,
            absolute_delta_exposure_usd=absolute_exposure,
            risk_band=band,
            action=action,
            target_delta_btc=target,
            gross_required_hedge_delta_btc=gross,
            working_order_delta_btc=working,
            projected_delta_btc=projected,
            remaining_hedge_requirement_btc=remaining,
            working_order_conflict=conflict,
            working_order_overhedge=overhedge,
            auto_hedge_blocked=bool(blocked_reasons),
            auto_hedge_blocked_reasons=tuple(blocked_reasons),
            inventory_or_settlement_state=InventoryOrSettlementState.NOT_EVALUATED,
        )

    @staticmethod
    def _remaining_requirement(
        gross: Decimal, working: Decimal
    ) -> tuple[Decimal, bool, bool]:
        if gross == 0:
            return Decimal("0"), False, working != 0
        if working == 0:
            return gross, False, False
        same_direction = (gross > 0 and working > 0) or (gross < 0 and working < 0)
        if not same_direction:
            return gross, True, False
        if abs(working) > abs(gross):
            return Decimal("0"), False, True
        return gross - working, False, False
