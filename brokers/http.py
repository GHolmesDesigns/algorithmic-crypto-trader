"""Shared HTTP, retry, and provider error primitives for broker adapters."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from core.resilience import CircuitBreaker, TokenBucketRateLimiter


class ProviderError(RuntimeError):
    """Base class for errors returned by or raised while contacting a venue."""


class ProviderHTTPError(ProviderError):
    """An HTTP response outside the successful range."""

    def __init__(self, status_code: int, message: str, *, payload: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


class ProviderTimeoutError(ProviderError):
    """The provider did not acknowledge a request within the transport timeout."""


class AmbiguousSubmissionError(ProviderTimeoutError):
    """A submission may have reached the venue and must be queried before retrying."""

    def __init__(self, order: Any) -> None:
        super().__init__(
            "provider submission timed out; query the persisted client_order_id before retrying"
        )
        self.order = order


class ProviderOrderRejectedError(ProviderError):
    """The provider definitively rejected an order."""

    def __init__(self, order: Any, message: str = "provider rejected the order") -> None:
        super().__init__(message)
        self.order = order


@dataclass(frozen=True, slots=True)
class AuthenticatedOrderEvent:
    """Provider-neutral order event emitted by an authenticated stream."""

    client_order_id: str
    status: str
    provider_order_id: str | None
    filled_quantity: str
    observed_at: str | None = None


AuthHeaders = Callable[[bool], Mapping[str, str]]
AuthRefresh = Callable[[], None]


def retry_after_seconds(response: httpx.Response, *, fallback: float, maximum: float) -> float:
    """Return a bounded Retry-After delay without trusting an unbounded header."""

    raw = response.headers.get("retry-after")
    if raw:
        try:
            delay = float(raw)
        except ValueError:
            try:
                parsed = parsedate_to_datetime(raw)
                delay = max(0.0, parsed.timestamp() - time.time())
            except (TypeError, ValueError, OverflowError):
                delay = fallback
    else:
        delay = fallback
    return min(maximum, max(0.0, delay))


class ProviderHTTPClient:
    """A bounded async HTTP client with rate limiting, 429 backoff, and a circuit."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        rate_limiter: TokenBucketRateLimiter | None = None,
        circuit_breaker: CircuitBreaker | None = None,
        max_429_retries: int = 2,
        backoff_base_seconds: float = 0.25,
        max_backoff_seconds: float = 2.0,
    ) -> None:
        if max_429_retries < 0 or backoff_base_seconds < 0 or max_backoff_seconds < 0:
            raise ValueError("retry settings must not be negative")
        self.client = client or httpx.AsyncClient(timeout=15.0)
        self.owns_client = client is None
        self.rate_limiter = rate_limiter or TokenBucketRateLimiter(5, 5)
        self.circuit_breaker = circuit_breaker or CircuitBreaker(failure_threshold=3)
        self.max_429_retries = max_429_retries
        self.backoff_base_seconds = backoff_base_seconds
        self.max_backoff_seconds = max_backoff_seconds

    @property
    def healthy(self) -> bool:
        return self.circuit_breaker.allow_request()

    async def close(self) -> None:
        if self.owns_client:
            await self.client.aclose()

    async def request_json(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        auth_headers: AuthHeaders | None = None,
        refresh_auth: AuthRefresh | None = None,
    ) -> Any:
        """Make one bounded request; refresh a rejected JWT once and retry 429s."""

        auth_attempt = 0
        for _ in range(2 if auth_headers is not None else 1):
            request_headers = dict(headers or {})
            if auth_headers is not None:
                request_headers.update(auth_headers(auth_attempt > 0))
            try:
                return await self._request_once(
                    method,
                    url,
                    headers=request_headers,
                    params=params,
                    json=json,
                )
            except ProviderHTTPError as exc:
                if exc.status_code != 401 or auth_headers is None or auth_attempt:
                    raise
                auth_attempt += 1
                if refresh_auth is not None:
                    refresh_auth()
        raise AssertionError("unreachable")

    async def _request_once(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        params: Mapping[str, Any] | None,
        json: Any,
    ) -> Any:
        for attempt in range(self.max_429_retries + 1):
            self.circuit_breaker.before_request()
            try:
                await self.rate_limiter.acquire()
                response = await self.client.request(
                    method, url, headers=headers, params=params, json=json
                )
            except (httpx.TimeoutException, TimeoutError) as exc:
                self.circuit_breaker.record_failure()
                raise ProviderTimeoutError("provider request timed out") from exc
            except httpx.HTTPError as exc:
                self.circuit_breaker.record_failure()
                raise ProviderError("provider transport failed") from exc

            if response.status_code == 429:
                if attempt >= self.max_429_retries:
                    self.circuit_breaker.record_failure()
                    raise ProviderHTTPError(
                        429, "provider rate limit persisted", payload=_json(response)
                    )
                delay = retry_after_seconds(
                    response,
                    fallback=self.backoff_base_seconds * (2**attempt),
                    maximum=self.max_backoff_seconds,
                )
                await asyncio.sleep(delay)
                continue

            payload = _json(response)
            if response.status_code >= 500:
                self.circuit_breaker.record_failure()
            elif response.status_code < 400:
                self.circuit_breaker.record_success()
            if response.status_code >= 400:
                raise ProviderHTTPError(
                    response.status_code,
                    f"provider returned HTTP {response.status_code}",
                    payload=payload,
                )
            return payload
        raise AssertionError("unreachable")


def _json(response: httpx.Response) -> Any:
    if not response.content:
        return {}
    try:
        return response.json()
    except ValueError:
        return {"text": response.text}
