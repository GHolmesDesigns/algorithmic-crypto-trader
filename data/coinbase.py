"""Coinbase Advanced Trade public market-data adapter."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
from core.models import Candle
from core.resilience import CircuitBreaker, TokenBucketRateLimiter

COINBASE_REST_URL = "https://api.coinbase.com/api/v3/brokerage"
COINBASE_WS_URL = "wss://advanced-trade-ws.coinbase.com"
MAX_CANDLES_PER_REQUEST = 300

GRANULARITY_SECONDS: dict[str, int] = {
    "ONE_MINUTE": 60,
    "FIVE_MINUTE": 300,
    "FIFTEEN_MINUTE": 900,
    "THIRTY_MINUTE": 1800,
    "ONE_HOUR": 3600,
    "TWO_HOUR": 7200,
    "SIX_HOUR": 21600,
    "ONE_DAY": 86400,
}


class CoinbaseMarketDataError(RuntimeError):
    """Raised when Coinbase returns an unusable public market-data response."""


class CoinbaseProductNotFound(CoinbaseMarketDataError):
    """Raised when the public product lookup answers 404: no such product."""


@dataclass(frozen=True, slots=True)
class CoinbaseProduct:
    """The few public product facts a watchlist add is judged on."""

    product_id: str
    status: str
    trading_disabled: bool
    is_disabled: bool
    product_type: str

    @property
    def tradable(self) -> bool:
        return (
            self.status == "online"
            and not self.trading_disabled
            and not self.is_disabled
            and self.product_type == "SPOT"
        )


def _utc_from_epoch(value: str | int) -> datetime:
    return datetime.fromtimestamp(int(value), tz=UTC)


def normalize_coinbase_candle(
    product_id: str,
    raw: dict[str, Any],
    *,
    interval: str,
    received_at: datetime,
) -> Candle:
    """Convert one Coinbase candle into the provider-neutral canonical model."""

    opened_at = _utc_from_epoch(raw["start"])
    closed_at = _utc_from_epoch(
        raw.get("end", int(opened_at.timestamp()) + GRANULARITY_SECONDS[interval])
    )
    return Candle(
        symbol=product_id,
        interval=interval,
        opened_at=opened_at,
        closed_at=closed_at,
        open=Decimal(str(raw["open"])),
        high=Decimal(str(raw["high"])),
        low=Decimal(str(raw["low"])),
        close=Decimal(str(raw["close"])),
        volume=Decimal(str(raw["volume"])),
        source="coinbase-advanced-trade",
        as_of=closed_at,
        ingested_at=received_at,
    )


class CoinbaseRESTClient:
    """Bounded public REST client; it never accepts credentials or submits orders."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        rate_limiter: TokenBucketRateLimiter | None = None,
        circuit_breaker: CircuitBreaker | None = None,
        base_url: str = COINBASE_REST_URL,
    ) -> None:
        self._client = client or httpx.AsyncClient(timeout=15.0)
        self._owns_client = client is None
        self._base_url = base_url.rstrip("/")
        self._rate_limiter = rate_limiter or TokenBucketRateLimiter(5, 5)
        self._circuit_breaker = circuit_breaker or CircuitBreaker(failure_threshold=3)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def get_candles(
        self,
        product_id: str,
        start: datetime,
        end: datetime,
        *,
        granularity: str = "ONE_MINUTE",
    ) -> tuple[Candle, ...]:
        if start.tzinfo is None or end.tzinfo is None or end <= start:
            raise ValueError("candle bounds must be timezone-aware and end after start")
        if granularity not in GRANULARITY_SECONDS:
            raise ValueError(f"unsupported Coinbase granularity: {granularity}")
        if end - start > timedelta(
            seconds=GRANULARITY_SECONDS[granularity] * MAX_CANDLES_PER_REQUEST
        ):
            raise ValueError("candle request exceeds the bounded provider page size")
        self._circuit_breaker.before_request()
        await self._rate_limiter.acquire()
        try:
            response = await self._client.get(
                f"{self._base_url}/market/products/{product_id}/candles",
                params={
                    "start": int(start.timestamp()),
                    "end": int(end.timestamp()),
                    "granularity": granularity,
                },
            )
            response.raise_for_status()
            payload = response.json()
            raw_candles = payload.get("candles")
            if not isinstance(raw_candles, list):
                raise CoinbaseMarketDataError("Coinbase candle response omitted candles")
            received_at = datetime.now(UTC)
            normalized = tuple(
                normalize_coinbase_candle(
                    product_id,
                    raw,
                    interval=granularity,
                    received_at=received_at,
                )
                for raw in raw_candles
            )
            self._circuit_breaker.record_success()
            return tuple(sorted(normalized, key=lambda candle: candle.opened_at))
        except Exception:
            self._circuit_breaker.record_failure()
            raise

    async def get_product(self, product_id: str) -> CoinbaseProduct:
        """One public product lookup through the same limiter and breaker as candles."""

        self._circuit_breaker.before_request()
        await self._rate_limiter.acquire()
        try:
            response = await self._client.get(f"{self._base_url}/market/products/{product_id}")
            if response.status_code == 404:
                self._circuit_breaker.record_success()
                raise CoinbaseProductNotFound(product_id)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or payload.get("product_id") != product_id:
                raise CoinbaseMarketDataError("Coinbase product response did not match the request")
            product = CoinbaseProduct(
                product_id=product_id,
                status=str(payload.get("status", "")).lower(),
                trading_disabled=bool(payload.get("trading_disabled", False)),
                is_disabled=bool(payload.get("is_disabled", False)),
                product_type=str(payload.get("product_type", "")).upper(),
            )
            self._circuit_breaker.record_success()
            return product
        except CoinbaseProductNotFound:
            raise
        except Exception:
            self._circuit_breaker.record_failure()
            raise
