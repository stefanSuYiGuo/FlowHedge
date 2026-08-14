"""Public OKX BTC/USDT Spot and linear Perpetual market-data adapter."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Awaitable, Callable
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from ..config import stablecoin_quote_config
from ..domain.models import InstrumentType
from .base import MarketDataAdapter
from .book import decimal_value, normalized_books_from_levels
from .coinbase import decimal_precision
from .models import (
    ContractStructure,
    DerivativeMarketContext,
    InstrumentRules,
    MarketConnectionStatus,
    MarketVenue,
)
from .store import InMemoryMarketStateStore, utc_now


logger = logging.getLogger(__name__)

OKX_PUBLIC_REST_ENDPOINT = "https://www.okx.com/api/v5"
OKX_SPOT_PRODUCT = "BTC-USDT"
OKX_PERP_PRODUCT = "BTC-USDT-SWAP"
CANONICAL_SYMBOL = "BTC-USD"
POLL_INTERVAL_SECONDS = 1
CONTEXT_REFRESH_SECONDS = 5
METADATA_REFRESH_SECONDS = 300
RECONNECT_BACKOFF_SECONDS = (1, 2, 5, 10)


def okx_url(path: str, **params: str) -> str:
    return f"{OKX_PUBLIC_REST_ENDPOINT}/{path}?{urlencode(params)}"


async def fetch_public_json(url: str) -> dict[str, Any]:
    def fetch() -> dict[str, Any]:
        request = Request(
            url,
            headers={
                "Accept": "application/json",
                "Cache-Control": "no-cache",
                "User-Agent": "FlowHedge/0.1 public-market-data",
            },
        )
        with urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"), parse_float=Decimal)
        if payload.get("code") != "0":
            raise ValueError(payload.get("msg") or "OKX public request failed")
        return payload

    return await asyncio.to_thread(fetch)


def timestamp_from_milliseconds(value: object) -> datetime:
    return datetime.fromtimestamp(float(value) / 1000, tz=timezone.utc)


def instrument_rules_from_okx(
    instrument: dict[str, Any],
    instrument_type: InstrumentType,
    *,
    received_at: datetime,
) -> InstrumentRules:
    is_perpetual = instrument_type is InstrumentType.PERPETUAL
    if is_perpetual:
        underlying = str(instrument["uly"])
        base_asset, quote_asset = underlying.split("-", 1)
        contract_structure = (
            ContractStructure.INVERSE
            if instrument.get("ctType") == "inverse"
            else ContractStructure.LINEAR
        )
        contract_multiplier = decimal_value(instrument["ctVal"]) * decimal_value(
            instrument.get("ctMult") or "1"
        )
        contract_value_currency = str(instrument.get("ctValCcy") or base_asset)
        native_quantity_unit = "CONTRACTS"
        settlement_asset = str(instrument["settleCcy"])
    else:
        base_asset = str(instrument["baseCcy"])
        quote_asset = str(instrument["quoteCcy"])
        contract_structure = ContractStructure.SPOT
        contract_multiplier = Decimal("1")
        contract_value_currency = None
        native_quantity_unit = base_asset
        settlement_asset = quote_asset
    tick_size = decimal_value(instrument["tickSz"])
    quantity_increment = decimal_value(instrument["lotSz"])
    assumption = (
        stablecoin_quote_config.usdt_assumption_label
        if quote_asset == "USDT"
        else None
    )
    return InstrumentRules(
        venue=MarketVenue.OKX,
        symbol=CANONICAL_SYMBOL,
        venue_symbol=str(instrument["instId"]),
        instrument_type=instrument_type,
        base_asset=base_asset,
        quote_asset=quote_asset,
        price_increment=tick_size,
        quantity_increment=quantity_increment,
        quantity_min=decimal_value(instrument["minSz"]),
        price_precision=decimal_precision(tick_size),
        quantity_precision=decimal_precision(quantity_increment),
        status=str(instrument["state"]).upper(),
        contract_structure=contract_structure,
        contract_multiplier=contract_multiplier,
        contract_value_currency=contract_value_currency,
        native_quantity_unit=native_quantity_unit,
        settlement_asset=settlement_asset,
        usd_conversion_rate=stablecoin_quote_config.usdt_usd_rate,
        usd_conversion_assumption=assumption,
        received_at=received_at,
    )


JsonFetcher = Callable[[str], Awaitable[dict[str, Any]]]


class OKXMarketDataAdapter(MarketDataAdapter):
    feed_id = "okx-public-spot-perpetual"
    venue = MarketVenue.OKX
    endpoint = OKX_PUBLIC_REST_ENDPOINT
    markets = (
        (CANONICAL_SYMBOL, InstrumentType.SPOT),
        (CANONICAL_SYMBOL, InstrumentType.PERPETUAL),
    )

    def __init__(
        self,
        store: InMemoryMarketStateStore,
        *,
        fetcher: JsonFetcher = fetch_public_json,
    ) -> None:
        self.store = store
        self.fetcher = fetcher
        self._rules: dict[InstrumentType, InstrumentRules] = {}
        self._poll_count = 0

    async def run(self) -> None:
        reconnect_attempt = 0
        try:
            while True:
                status = (
                    MarketConnectionStatus.CONNECTING
                    if reconnect_attempt == 0
                    else MarketConnectionStatus.RECONNECTING
                )
                await self.store.update_connection(
                    self.feed_id,
                    status=status,
                    reconnect_attempt=reconnect_attempt,
                    clear_error=reconnect_attempt == 0,
                )
                try:
                    await self._refresh_metadata()
                    await self.store.update_connection(
                        self.feed_id,
                        status=status,
                        connected_at=utc_now(),
                        clear_error=True,
                    )
                    reconnect_attempt = 0
                    while True:
                        await self.poll_once()
                        await asyncio.sleep(POLL_INTERVAL_SECONDS)
                except (OSError, ValueError, KeyError, TypeError) as error:
                    reconnect_attempt += 1
                    await self.store.update_connection(
                        self.feed_id,
                        status=MarketConnectionStatus.DISCONNECTED,
                        last_error=str(error),
                        reconnect_attempt=reconnect_attempt,
                    )
                    logger.warning("OKX public feed disconnected: %s", error)
                    delay = RECONNECT_BACKOFF_SECONDS[
                        min(reconnect_attempt - 1, len(RECONNECT_BACKOFF_SECONDS) - 1)
                    ]
                    await asyncio.sleep(delay)
        finally:
            await self.store.update_connection(
                self.feed_id,
                status=MarketConnectionStatus.DISCONNECTED,
                last_error="market data service stopped",
            )

    async def _refresh_metadata(self) -> None:
        requests = (
            (
                InstrumentType.SPOT,
                okx_url(
                    "public/instruments", instType="SPOT", instId=OKX_SPOT_PRODUCT
                ),
            ),
            (
                InstrumentType.PERPETUAL,
                okx_url(
                    "public/instruments", instType="SWAP", instId=OKX_PERP_PRODUCT
                ),
            ),
        )
        results = await asyncio.gather(
            *(self.fetcher(url) for _, url in requests), return_exceptions=True
        )
        successes = 0
        for (instrument_type, _), result in zip(requests, results):
            if isinstance(result, Exception):
                continue
            rules = instrument_rules_from_okx(
                result["data"][0], instrument_type, received_at=utc_now()
            )
            self._rules[instrument_type] = rules
            await self.store.replace_instrument(rules)
            successes += 1
        if successes == 0:
            raise ValueError("OKX Spot and Perpetual metadata are unavailable")

    async def poll_once(self) -> None:
        if len(self._rules) < 2:
            await self._refresh_metadata()
        book_requests = (
            (InstrumentType.SPOT, OKX_SPOT_PRODUCT),
            (InstrumentType.PERPETUAL, OKX_PERP_PRODUCT),
        )
        results = await asyncio.gather(
            *(
                self.fetcher(okx_url("market/books", instId=product, sz="400"))
                for _, product in book_requests
            ),
            return_exceptions=True,
        )
        received_at = utc_now()
        successes = 0
        errors: list[str] = []
        for (instrument_type, _), result in zip(book_requests, results):
            if isinstance(result, Exception):
                errors.append(f"{instrument_type.value}: {result}")
                continue
            payload = result["data"][0]
            display, executable = normalized_books_from_levels(
                rules=self._rules[instrument_type],
                bids=((level[0], level[1]) for level in payload["bids"]),
                asks=((level[0], level[1]) for level in payload["asks"]),
                exchange_timestamp=timestamp_from_milliseconds(payload["ts"]),
                received_at=received_at,
                source_sequence=int(payload["seqId"]),
            )
            await self.store.replace_books(display, executable)
            successes += 1

        self._poll_count += 1
        if self._poll_count % CONTEXT_REFRESH_SECONDS == 0:
            await self._refresh_derivative_context()
        if self._poll_count % METADATA_REFRESH_SECONDS == 0:
            await self._refresh_metadata()
        if successes == 0:
            raise ValueError("; ".join(errors) or "OKX books unavailable")
        await self.store.update_connection(
            self.feed_id,
            status=MarketConnectionStatus.LIVE,
            last_message_at=received_at,
            last_book_update_at=received_at,
            last_error="; ".join(errors) if errors else None,
            reconnect_attempt=0,
            clear_error=not errors,
        )

    async def _refresh_derivative_context(self) -> None:
        urls = (
            okx_url("public/mark-price", instType="SWAP", instId=OKX_PERP_PRODUCT),
            okx_url("market/index-tickers", instId="BTC-USDT"),
            okx_url("public/open-interest", instType="SWAP", instId=OKX_PERP_PRODUCT),
            okx_url("public/funding-rate", instId=OKX_PERP_PRODUCT),
        )
        mark_result, index_result, oi_result, funding_result = await asyncio.gather(
            *(self.fetcher(url) for url in urls), return_exceptions=True
        )
        now = utc_now()
        mark_data = None if isinstance(mark_result, Exception) else mark_result["data"][0]
        index_data = None if isinstance(index_result, Exception) else index_result["data"][0]
        oi_data = None if isinstance(oi_result, Exception) else oi_result["data"][0]
        funding_data = (
            None if isinstance(funding_result, Exception) else funding_result["data"][0]
        )
        if all(item is None for item in (mark_data, index_data, oi_data, funding_data)):
            return
        mark = decimal_value(mark_data["markPx"]) if mark_data else None
        index = decimal_value(index_data["idxPx"]) if index_data else None
        open_interest = decimal_value(oi_data["oi"]) if oi_data else None
        rules = self._rules[InstrumentType.PERPETUAL]
        reference = mark or index
        open_interest_btc = (
            rules.quantity_to_btc_equivalent(open_interest, price=reference)
            if open_interest is not None and reference is not None
            else None
        )
        funding_time = (
            timestamp_from_milliseconds(funding_data["fundingTime"])
            if funding_data and funding_data.get("fundingTime")
            else None
        )
        next_funding_time = (
            timestamp_from_milliseconds(funding_data["nextFundingTime"])
            if funding_data and funding_data.get("nextFundingTime")
            else None
        )
        interval_seconds = (
            int((next_funding_time - funding_time).total_seconds())
            if funding_time and next_funding_time
            else None
        )
        context = DerivativeMarketContext(
            venue=MarketVenue.OKX,
            symbol=CANONICAL_SYMBOL,
            venue_symbol=OKX_PERP_PRODUCT,
            mark_price=mark,
            index_price=index,
            current_funding_rate=(
                decimal_value(funding_data["fundingRate"])
                if funding_data and funding_data.get("fundingRate")
                else None
            ),
            predicted_funding_rate=(
                decimal_value(funding_data["nextFundingRate"])
                if funding_data and funding_data.get("nextFundingRate")
                else None
            ),
            next_funding_time=funding_time,
            funding_interval_seconds=interval_seconds,
            open_interest=open_interest,
            open_interest_unit="CONTRACTS" if open_interest is not None else None,
            open_interest_btc_equivalent=open_interest_btc,
            open_interest_usd=(
                open_interest_btc * reference
                if open_interest_btc is not None and reference is not None
                else None
            ),
            mark_price_captured_at=(
                timestamp_from_milliseconds(mark_data["ts"]) if mark_data else None
            ),
            index_price_captured_at=(
                timestamp_from_milliseconds(index_data["ts"]) if index_data else None
            ),
            funding_captured_at=(
                timestamp_from_milliseconds(funding_data["ts"])
                if funding_data
                else None
            ),
            open_interest_captured_at=(
                timestamp_from_milliseconds(oi_data["ts"]) if oi_data else None
            ),
            received_at=now,
            source="OKX_PUBLIC_REST",
        )
        await self.store.replace_derivative_context(context)
