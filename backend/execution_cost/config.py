"""Centralized Step 8A fee schedule for explicit demo desk assumptions."""

from ..config import demo_desk_config
from ..domain.models import InstrumentType
from ..market.models import MarketVenue
from .models import ExecutionFeeConfig, ExecutionFeeEntry


execution_fee_config = ExecutionFeeConfig(
    entries=tuple(
        ExecutionFeeEntry(
            venue=venue,
            instrument_type=instrument_type,
            fee_bps=demo_desk_config.taker_fee_bps,
            assumption_label=(
                f"{demo_desk_config.assumption_label} — "
                f"{demo_desk_config.fee_disclaimer}"
            ),
        )
        for venue in MarketVenue
        for instrument_type in InstrumentType
    )
)
