"""Pure RiskPolicy v1.1 calculations and independent USD Spot reference price."""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_DOWN, Decimal

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
AUTO_HEDGE_TARGET_QUANTUM_BTC = Decimal("0.00000001")


def build_risk_reference_price(
    snapshot: UnifiedMarketSnapshot,
) -> RiskReferencePrice:
    """Use only fresh Kraken/Coinbase USD Spot mids for RiskPolicy v1.1."""

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
        auto_target_notional = (
            self.config.soft_delta_limit_usd
            * self.config.auto_hedge_target_ratio_of_soft
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
                advisory_target_delta_btc=None,
                advisory_gross_required_hedge_delta_btc=None,
                advisory_remaining_hedge_requirement_btc=None,
                target_delta_btc=None,
                gross_required_hedge_delta_btc=None,
                remaining_hedge_requirement_btc=None,
                auto_hedge_target_ratio_of_soft=(
                    self.config.auto_hedge_target_ratio_of_soft
                ),
                auto_hedge_target_notional_usd=auto_target_notional,
                auto_hedge_target_delta_btc=None,
                auto_gross_required_hedge_delta_btc=None,
                auto_qualifying_working_order_delta_btc=None,
                auto_remaining_hedge_requirement_btc=None,
                auto_working_order_conflict=False,
                auto_working_order_overhedge=False,
                working_order_delta_btc=working,
                projected_delta_btc=projected,
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
            advisory_target = actual
        elif absolute_exposure <= self.config.hard_delta_limit_usd:
            band = RiskBand.YELLOW
            action = RiskAction.PARTIAL_HEDGE
            direction = Decimal("1") if actual > 0 else Decimal("-1")
            advisory_target = (
                direction * self.config.soft_delta_limit_usd / price
            )
        else:
            band = RiskBand.RED
            action = RiskAction.IMMEDIATE_HEDGE
            direction = Decimal("1") if actual > 0 else Decimal("-1")
            advisory_target = (
                direction * self.config.soft_delta_limit_usd / price
            )

        advisory_gross = advisory_target - actual
        advisory_remaining, advisory_conflict, advisory_overhedge = (
            self._remaining_requirement(advisory_gross, working)
        )
        advisory_blocked_reasons = self._blocked_reasons(
            advisory_gross,
            advisory_remaining,
            advisory_conflict,
            advisory_overhedge,
        )

        assessment = RiskAssessment(
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
            advisory_target_delta_btc=advisory_target,
            advisory_gross_required_hedge_delta_btc=advisory_gross,
            advisory_remaining_hedge_requirement_btc=advisory_remaining,
            target_delta_btc=advisory_target,
            gross_required_hedge_delta_btc=advisory_gross,
            remaining_hedge_requirement_btc=advisory_remaining,
            auto_hedge_target_ratio_of_soft=(
                self.config.auto_hedge_target_ratio_of_soft
            ),
            auto_hedge_target_notional_usd=auto_target_notional,
            auto_hedge_target_delta_btc=None,
            auto_gross_required_hedge_delta_btc=None,
            auto_qualifying_working_order_delta_btc=None,
            auto_remaining_hedge_requirement_btc=None,
            auto_working_order_conflict=False,
            auto_working_order_overhedge=False,
            working_order_delta_btc=working,
            projected_delta_btc=projected,
            working_order_conflict=advisory_conflict,
            working_order_overhedge=advisory_overhedge,
            auto_hedge_blocked=bool(advisory_blocked_reasons),
            auto_hedge_blocked_reasons=tuple(advisory_blocked_reasons),
            inventory_or_settlement_state=InventoryOrSettlementState.NOT_EVALUATED,
        )
        return self.apply_auto_target(assessment) if band is RiskBand.RED else assessment

    def apply_auto_target(self, assessment: RiskAssessment) -> RiskAssessment:
        """Attach the buffered target used only by an active/armed auto path."""

        price = assessment.reference_price_usd
        if price is None:
            reasons = tuple(
                dict.fromkeys(
                    assessment.auto_hedge_blocked_reasons
                    + ("RISK_REFERENCE_PRICE_UNAVAILABLE",)
                )
            )
            return assessment.model_copy(
                update={
                    "auto_hedge_blocked": True,
                    "auto_hedge_blocked_reasons": reasons,
                }
            )

        actual = assessment.actual_delta_btc
        if actual == 0:
            auto_target = Decimal("0")
        else:
            direction = Decimal("1") if actual > 0 else Decimal("-1")
            raw_auto_target = (
                direction * assessment.auto_hedge_target_notional_usd / price
            )
            # Keep the policy target on the desk's BTC accounting grid and round
            # toward zero. The resulting notional is never above the $900K cap,
            # and the execution layer is not left chasing a sub-satoshi residual.
            auto_target = raw_auto_target.quantize(
                AUTO_HEDGE_TARGET_QUANTUM_BTC,
                rounding=ROUND_DOWN,
            )
        auto_gross = auto_target - actual
        auto_remaining, auto_conflict, auto_overhedge = self._remaining_requirement(
            auto_gross,
            assessment.working_order_delta_btc,
        )
        auto_blocked_reasons = self._blocked_reasons(
            auto_gross,
            auto_remaining,
            auto_conflict,
            auto_overhedge,
        )
        qualifying_working = (
            Decimal("0")
            if auto_conflict
            else assessment.working_order_delta_btc
        )
        return assessment.model_copy(
            update={
                "auto_hedge_target_delta_btc": auto_target,
                "auto_gross_required_hedge_delta_btc": auto_gross,
                "auto_qualifying_working_order_delta_btc": qualifying_working,
                "auto_remaining_hedge_requirement_btc": auto_remaining,
                "auto_working_order_conflict": auto_conflict,
                "auto_working_order_overhedge": auto_overhedge,
                "auto_hedge_blocked": bool(auto_blocked_reasons),
                "auto_hedge_blocked_reasons": tuple(auto_blocked_reasons),
            }
        )

    @staticmethod
    def _blocked_reasons(
        gross: Decimal,
        remaining: Decimal,
        conflict: bool,
        overhedge: bool,
    ) -> list[str]:
        blocked_reasons: list[str] = []
        if conflict:
            blocked_reasons.append("WORKING_ORDER_CONFLICT")
        if overhedge:
            blocked_reasons.append("WORKING_ORDER_OVERHEDGE")
        if remaining == 0 and gross != 0:
            blocked_reasons.append("NO_REMAINING_HEDGE_REQUIREMENT")
        return blocked_reasons

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
