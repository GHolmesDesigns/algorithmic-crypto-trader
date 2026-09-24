"""Coinbase Advanced Trade broker adapter.

The adapter owns provider-specific payloads and authentication.  Callers only
see the provider-neutral domain models and the unchanged ``BrokerInterface``.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from collections.abc import AsyncIterator, Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse
from uuid import UUID, uuid5

import httpx
from core.models import (
    Balance,
    Candle,
    Fill,
    Order,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    Quote,
    RiskApproval,
    utc_now,
)
from core.resilience import (
    CircuitBreaker,
    CircuitOpen,
    RateLimitExceeded,
    TokenBucketRateLimiter,
)
from data.coinbase import (
    COINBASE_REST_URL,
    COINBASE_WS_URL,
    GRANULARITY_SECONDS,
    normalize_coinbase_candle,
)

from brokers.http import (
    AmbiguousSubmissionError,
    AuthenticatedOrderEvent,
    ProviderError,
    ProviderHTTPClient,
    ProviderHTTPError,
    ProviderOrderRejectedError,
    ProviderTimeoutError,
)
from brokers.interface import BrokerCapabilities, BrokerInterface

COINBASE_ORDER_NAMESPACE = UUID("8c3c7e68-982d-4e4f-93c1-6d5e7d2c52a1")
COINBASE_SANDBOX_REST_URL = "https://api-sandbox.coinbase.com/api/v3/brokerage"
# Page-size maximum documented for List Accounts, List Orders, and List Fills.
PAGE_LIMIT = 250
# Bounded request budgets: a listing that keeps paginating past these fails closed.
MAX_ACCOUNT_PAGES = 40
MAX_FILL_PAGES = 20
ORDER_SEARCH_PAGES = 10


class CoinbaseJWTProvider:
    """Short-lived REST and WebSocket JWTs with explicit invalidation.

    Coinbase binds each JWT to one request through its ``uri`` claim, written as
    ``"GET api.coinbase.com/api/v3/brokerage/accounts"``, so tokens are cached per
    method and path. WebSocket JWTs intentionally omit ``uri``. Both token types
    carry the key name as ``kid`` and a random ``nonce`` in the header.
    """

    def __init__(
        self,
        *,
        api_key: str | None,
        private_key: str | None,
        token_provider: Callable[[str, str], str] | None = None,
        ttl_seconds: int = 120,
        host: str = "api.coinbase.com",
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("JWT TTL must be positive")
        if (api_key is None) != (private_key is None):
            raise ValueError("Coinbase api_key and private_key must be supplied together")
        if token_provider is None and api_key is None:
            raise ValueError("Coinbase credentials or a token_provider are required")
        self.api_key = api_key
        self.private_key = private_key
        self.token_provider = token_provider
        self.ttl_seconds = ttl_seconds
        self.host = host
        self._cached: dict[tuple[str, str], tuple[str, float]] = {}

    def invalidate(self) -> None:
        self._cached.clear()

    def token(self, method: str, path: str, *, force_refresh: bool = False) -> str:
        """Return a JWT for ``method`` on the full request ``path`` (``/api/v3/...``)."""

        return self._issue(
            method=method,
            path=path,
            uri=f"{method.upper()} {self.host}{path}",
            force_refresh=force_refresh,
        )

    def websocket_token(self, *, force_refresh: bool = False) -> str:
        """Return a JWT for a WebSocket subscription, which has no ``uri`` claim."""

        return self._issue(
            method="WS",
            path="",
            uri=None,
            force_refresh=force_refresh,
        )

    def _issue(
        self,
        *,
        method: str,
        path: str,
        uri: str | None,
        force_refresh: bool,
    ) -> str:

        now = time.time()
        key = (method.upper(), path)
        cached = self._cached.get(key)
        if not force_refresh and cached is not None and cached[1] > now + 5:
            return cached[0]
        if self.token_provider is not None:
            token = self.token_provider(method, path)
        else:
            assert self.api_key is not None and self.private_key is not None
            try:
                import jwt
            except ImportError as exc:  # pragma: no cover - exercised only without optional deps
                raise RuntimeError("PyJWT is required for Coinbase private requests") from exc
            issued = int(now)
            claims: dict[str, str | int] = {
                "sub": self.api_key,
                "iss": "cdp",
                "nbf": issued,
                "exp": issued + self.ttl_seconds,
            }
            if uri is not None:
                claims["uri"] = uri
            token = jwt.encode(
                claims,
                self.private_key,
                algorithm="ES256",
                headers={"kid": self.api_key, "nonce": secrets.token_hex(16)},
            )
        self._cached[key] = (token, now + self.ttl_seconds)
        return token


class CoinbaseBroker(BrokerInterface):
    """Production Coinbase Advanced Trade adapter with bounded provider access."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        private_key: str | None = None,
        token_provider: Callable[[str, str], str] | None = None,
        auth_token: str | None = None,
        client: httpx.AsyncClient | None = None,
        base_url: str = COINBASE_REST_URL,
        rate_limiter: TokenBucketRateLimiter | None = None,
        circuit_breaker: CircuitBreaker | None = None,
        max_429_retries: int = 2,
        backoff_base_seconds: float = 0.25,
    ) -> None:
        if auth_token is not None and any((api_key, private_key, token_provider)):
            raise ValueError("use auth_token or Coinbase credentials, not both")
        if not base_url.startswith("https://") and client is None:
            raise ValueError("Coinbase broker requires an HTTPS base URL")
        self._base_url = base_url.rstrip("/")
        parsed = urlparse(self._base_url)
        # JWT uri claims name the full request path, e.g. /api/v3/brokerage/accounts.
        self._path_prefix = parsed.path
        self._auth_token = auth_token
        self._jwt = (
            None
            if auth_token is not None
            else CoinbaseJWTProvider(
                api_key=api_key,
                private_key=private_key,
                token_provider=token_provider,
                host=parsed.hostname or "api.coinbase.com",
            )
        )
        self._http = ProviderHTTPClient(
            client=client,
            rate_limiter=rate_limiter,
            circuit_breaker=circuit_breaker,
            max_429_retries=max_429_retries,
            backoff_base_seconds=backoff_base_seconds,
        )
        self._requests: dict[str, OrderRequest] = {}
        self._orders: dict[str, Order] = {}
        self._provider_order_ids: dict[str, str] = {}

    @property
    def capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities(
            provider="coinbase-advanced-trade",
            environment="production",
            streaming=True,
            historical_candles=True,
            native_order_edit=True,
            preview_orders=False,
            order_types=("market", "limit"),
            price_increment=Decimal("0.01"),
            quantity_increment=Decimal("0.00000001"),
            max_quote_age_seconds=60,
        )

    @property
    def healthy(self) -> bool:
        return self._http.healthy

    async def close(self) -> None:
        await self._http.close()

    async def get_quote(self, symbol: str) -> Quote:
        payload = await self._public("GET", f"/products/{symbol}/ticker")
        raw = _unwrap(payload, "ticker")
        return Quote(
            symbol=symbol,
            bid=_decimal(raw.get("best_bid")),
            ask=_decimal(raw.get("best_ask")),
            as_of=_timestamp(raw.get("timestamp")),
            source="coinbase-advanced-trade",
        )

    async def get_balances(self) -> tuple[Balance, ...]:
        # List Accounts returns 49 accounts per page by default; read every page.
        rows = await self._paginate("/accounts", "accounts", max_pages=MAX_ACCOUNT_PAGES)
        now = utc_now()
        result: list[Balance] = []
        for row in rows:
            currency = str(row.get("currency") or row.get("asset") or "").upper()
            if not currency:
                continue
            available = _decimal(_nested(row, "available_balance", "value"), default=Decimal("0"))
            hold = _decimal(_nested(row, "hold", "value"), default=Decimal("0"))
            if available or hold:
                result.append(Balance(asset=currency, available=available, hold=hold, as_of=now))
        return tuple(sorted(result, key=lambda item: item.asset))

    async def get_positions(self) -> tuple[Position, ...]:
        balances = await self.get_balances()
        now = utc_now()
        return tuple(
            Position(
                symbol=f"{balance.asset}-USD",
                quantity=balance.available,
                average_price=Decimal("0"),
                as_of=now,
            )
            for balance in balances
            if balance.asset != "USD" and balance.available
        )

    async def get_products(self, *, product_type: str = "SPOT") -> tuple[dict[str, Any], ...]:
        payload = await self._public("GET", "/products", params={"product_type": product_type})
        return tuple(dict(row) for row in _rows(payload, "products"))

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
        payload = await self._public(
            "GET",
            f"/products/{product_id}/candles",
            params={
                "start": int(start.timestamp()),
                "end": int(end.timestamp()),
                "granularity": granularity,
            },
        )
        received_at = utc_now()
        candles = tuple(
            normalize_coinbase_candle(
                product_id,
                dict(raw),
                interval=granularity,
                received_at=received_at,
            )
            for raw in _rows(payload, "candles")
        )
        return tuple(sorted(candles, key=lambda candle: candle.opened_at))

    async def submit_order(self, request: OrderRequest, approval: RiskApproval) -> Order:
        _require_approval(request, approval)
        key = str(request.client_order_id)
        existing = self._orders.get(key)
        if existing is not None and existing.status not in {
            OrderStatus.UNKNOWN,
            OrderStatus.PENDING_SUBMIT,
        }:
            return existing
        if existing is not None:
            recovered = await self.get_order(key)
            if recovered is not None and recovered.status is not OrderStatus.UNKNOWN:
                return recovered
        self._requests[key] = request
        unknown = Order(
            order_id=request.client_order_id, request=request, status=OrderStatus.UNKNOWN
        )
        body = {
            "client_order_id": key,
            "product_id": request.symbol,
            "side": request.side.value.upper(),
            "order_configuration": _coinbase_order_configuration(request),
        }
        try:
            payload = await self._private("POST", "/orders", json=body)
        except ProviderTimeoutError as exc:
            self._orders[key] = unknown
            raise AmbiguousSubmissionError(unknown) from exc
        except ProviderHTTPError as exc:
            # A 400/422 (e.g. INVALID_ARGUMENT) means Coinbase did not create the order.
            if exc.status_code in {400, 422}:
                rejected = unknown.model_copy(update={"status": OrderStatus.REJECTED})
                self._orders[key] = rejected
                raise ProviderOrderRejectedError(rejected, _error_message(exc.payload)) from exc
            raise
        if payload.get("success") is False:
            rejected = unknown.model_copy(update={"status": OrderStatus.REJECTED})
            self._orders[key] = rejected
            raise ProviderOrderRejectedError(rejected, _error_message(payload))
        order = self._order_from_payload(payload, request=request)
        self._orders[key] = order
        # Create Order's success_response carries identifiers only, not fill state.
        # Read the order back; if that read fails, the accepted order stays OPEN and
        # reconciliation settles its state.
        try:
            refreshed = await self.get_order(key)
        except (ProviderError, CircuitOpen, RateLimitExceeded):
            return order
        return refreshed or order

    async def get_order(self, client_order_id: str) -> Order | None:
        cached = self._orders.get(client_order_id)
        if cached is not None and cached.status in {
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
            OrderStatus.REJECTED,
        }:
            return cached
        provider_id = self._provider_order_ids.get(client_order_id)
        raw: Mapping[str, Any] | None
        if provider_id is not None:
            try:
                payload = await self._private("GET", f"/orders/historical/{provider_id}")
            except ProviderHTTPError as exc:
                if exc.status_code == 404:
                    return None
                raise
            raw = None if _is_missing(payload) else payload
        else:
            # A timed-out submission has no venue order ID yet. Coinbase documents no
            # lookup by client_order_id, so search recent orders for it.
            raw = await self._find_by_client_order_id(client_order_id)
        if raw is None:
            return None
        request = self._requests.get(client_order_id)
        order = self._order_from_payload(raw, request=request, client_order_id=client_order_id)
        self._orders[client_order_id] = order
        return order

    async def get_fills(self, client_order_id: str) -> tuple[Fill, ...]:
        provider_id = self._provider_order_ids.get(client_order_id)
        if provider_id is None:
            order = await self.get_order(client_order_id)
            if order is None:
                return ()
            provider_id = self._provider_order_ids.get(client_order_id)
        if provider_id is None:
            return ()
        rows = await self._paginate(
            "/orders/historical/fills",
            "fills",
            params={"order_ids": [provider_id]},
            max_pages=MAX_FILL_PAGES,
        )
        order = self._orders.get(client_order_id)
        if order is None:
            order = await self.get_order(client_order_id)
        if order is None:
            return ()
        return tuple(_coinbase_fill(row, order) for row in rows)

    async def key_permissions(self) -> dict[str, bool]:
        """Report whether this key can view, trade, and transfer funds."""

        payload = await self._private("GET", "/key_permissions")
        if not isinstance(payload, Mapping):
            raise ProviderHTTPError(502, "key permissions response was not an object")
        return {
            name: payload.get(name) is True for name in ("can_view", "can_trade", "can_transfer")
        }

    async def _find_by_client_order_id(self, client_order_id: str) -> Mapping[str, Any] | None:
        request = self._requests.get(client_order_id)
        params: dict[str, Any] = {"limit": PAGE_LIMIT}
        if request is not None:
            params["product_ids"] = [request.symbol]
        cursor = ""
        for _ in range(ORDER_SEARCH_PAGES):
            page_params = dict(params, cursor=cursor) if cursor else params
            payload = await self._private("GET", "/orders/historical/batch", params=page_params)
            for row in _rows(payload, "orders"):
                if str(row.get("client_order_id")) == client_order_id:
                    return row
            cursor = _next_cursor(payload, cursor)
            if not cursor:
                return None
        # Not found within the search budget. Resubmitting is still safe: Coinbase returns
        # the existing order for a reused client_order_id instead of creating a second one.
        return None

    async def _paginate(
        self,
        path: str,
        key: str,
        *,
        params: Mapping[str, Any] | None = None,
        max_pages: int,
    ) -> list[Mapping[str, Any]]:
        rows: list[Mapping[str, Any]] = []
        cursor = ""
        base = dict(params or {}, limit=PAGE_LIMIT)
        for _ in range(max_pages):
            payload = await self._private(
                "GET", path, params=dict(base, cursor=cursor) if cursor else base
            )
            page = _rows(payload, key)
            rows.extend(page)
            cursor = _next_cursor(payload, cursor) if page else ""
            if not cursor:
                return rows
        raise ProviderHTTPError(502, f"{key} pagination exceeded {max_pages} pages")

    async def cancel_order(self, client_order_id: str) -> Order | None:
        provider_id = self._provider_order_ids.get(client_order_id)
        current = self._orders.get(client_order_id) or await self.get_order(client_order_id)
        if current is None:
            return None
        provider_id = provider_id or str(current.order_id)
        payload = await self._private(
            "POST", "/orders/batch_cancel", json={"order_ids": [provider_id]}
        )
        # Cancel Orders reports each order in "results"; a failed cancel keeps the order live.
        results = _rows(payload, "results")
        if not any(row.get("success") is True for row in results):
            reason = next((row.get("failure_reason") for row in results), None)
            raise ProviderHTTPError(409, f"cancel failed: {reason or 'no result'}")
        # A successful response only acknowledges the cancel request. The order
        # can still be OPEN/CANCEL_QUEUED or win the race and fill, so read the
        # authoritative state instead of manufacturing a terminal cache entry.
        refreshed = await self.get_order(client_order_id)
        return refreshed or current

    async def edit_order(
        self,
        client_order_id: str,
        *,
        quantity: Decimal,
        limit_price: Decimal,
        approval: RiskApproval | None = None,
    ) -> Order:
        if quantity <= 0 or limit_price <= 0:
            raise ValueError("edited quantity and limit_price must be positive")
        current = self._orders.get(client_order_id) or await self.get_order(client_order_id)
        if current is None:
            raise KeyError(client_order_id)
        if current.status in {OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED}:
            raise ValueError("terminal orders cannot be edited")
        if current.request.order_type is not OrderType.LIMIT:
            raise ValueError("only open limit orders can be edited")
        if (
            current.request.order_type is OrderType.LIMIT
            and current.status in {OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED}
            and quantity >= current.filled_quantity
        ):
            provider_id = self._provider_order_ids.get(client_order_id, str(current.order_id))
            payload = await self._private(
                "POST",
                "/orders/edit",
                json={"order_id": provider_id, "size": str(quantity), "price": str(limit_price)},
            )
            # Edit Order returns only success and errors; a failed edit leaves the order as is.
            if not isinstance(payload, Mapping) or payload.get("success") is not True:
                raise ProviderHTTPError(409, f"edit failed: {_edit_errors(payload)}")
            self._requests[client_order_id] = current.request.model_copy(
                update={"quantity": quantity, "limit_price": limit_price}
            )
            self._orders.pop(client_order_id, None)
            updated = await self.get_order(client_order_id)
            if updated is None:
                raise KeyError(client_order_id)
            return updated
        if approval is None:
            raise PermissionError("cancel-and-replace requires a fresh RiskApproval")
        canceled = await self.cancel_order(client_order_id)
        if canceled is None:
            raise KeyError(client_order_id)
        replacement = current.request.model_copy(
            update={
                "quantity": quantity,
                "limit_price": limit_price,
                "client_order_id": uuid5(
                    COINBASE_ORDER_NAMESPACE, f"{client_order_id}|{quantity}|{limit_price}"
                ),
            }
        )
        return await self.submit_order(replacement, approval)

    async def poll_order(
        self,
        client_order_id: str,
        *,
        timeout_seconds: float = 30.0,
        interval_seconds: float = 1.0,
    ) -> Order | None:
        if timeout_seconds <= 0 or interval_seconds <= 0:
            raise ValueError("poll timeout and interval must be positive")
        deadline = time.monotonic() + timeout_seconds
        while True:
            order = await self.get_order(client_order_id)
            if order is None or order.status in {
                OrderStatus.FILLED,
                OrderStatus.CANCELED,
                OrderStatus.REJECTED,
            }:
                return order
            if time.monotonic() >= deadline:
                return order
            await asyncio.sleep(min(interval_seconds, max(0.0, deadline - time.monotonic())))

    async def authenticated_order_events(
        self,
        *,
        stop: asyncio.Event,
        connect: Callable[[str], Any] | None = None,
    ) -> AsyncIterator[AuthenticatedOrderEvent]:
        """Yield authenticated user-order events; polling remains the recovery path."""

        if self._jwt is None and self._auth_token is None:
            raise PermissionError("Coinbase user-order events require credentials")
        if connect is None:
            import websockets

            connect = websockets.connect
        websocket = await connect(COINBASE_WS_URL)
        try:
            token = self._websocket_token()
            await websocket.send(
                json.dumps(
                    {
                        "type": "subscribe",
                        "channel": "user",
                        "jwt": token,
                    },
                    separators=(",", ":"),
                )
            )
            while not stop.is_set():
                raw = await asyncio.wait_for(websocket.recv(), timeout=30)
                payload = json.loads(raw)
                for event in _coinbase_events(payload):
                    yield event
        finally:
            await websocket.close()

    async def _public(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
    ) -> Any:
        return await self._http.request_json(
            method,
            f"{self._base_url}{path}",
            params=params,
        )

    async def _private(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
    ) -> Any:
        return await self._http.request_json(
            method,
            f"{self._base_url}{path}",
            params=params,
            json=json,
            auth_headers=lambda force: {
                "Authorization": f"Bearer {self._token(method, path, force=force)}"
            },
            refresh_auth=self._invalidate_token,
        )

    def _token(self, method: str, path: str, *, force: bool = False) -> str:
        if self._auth_token is not None:
            return self._auth_token
        assert self._jwt is not None
        return self._jwt.token(method, f"{self._path_prefix}{path}", force_refresh=force)

    def _websocket_token(self) -> str:
        if self._auth_token is not None:
            return self._auth_token
        assert self._jwt is not None
        return self._jwt.websocket_token()

    def _invalidate_token(self) -> None:
        if self._jwt is not None:
            self._jwt.invalidate()

    def _order_from_payload(
        self,
        payload: Mapping[str, Any],
        *,
        request: OrderRequest | None = None,
        client_order_id: str | None = None,
    ) -> Order:
        raw = _unwrap(payload, "order", "success_response")
        client_id = str(
            raw.get("client_order_id") or client_order_id or request and request.client_order_id
        )
        request = request or self._requests.get(client_id) or _recovered_request(raw, client_id)
        provider_id = str(raw.get("order_id") or raw.get("id") or client_id)
        self._provider_order_ids[client_id] = provider_id
        status = _order_status(raw, request.quantity)
        return Order(
            order_id=_provider_uuid(provider_id),
            request=request,
            status=status,
            filled_quantity=_decimal(
                raw.get("filled_size") or raw.get("filled_quantity") or raw.get("executed_size"),
                default=Decimal("0"),
            ),
            average_fill_price=_optional_decimal(
                raw.get("average_filled_price") or raw.get("average_fill_price")
            ),
            created_at=_timestamp(raw.get("created_time") or raw.get("created_at")),
            updated_at=utc_now(),
        )


def _require_approval(request: OrderRequest, approval: RiskApproval) -> None:
    if request.signal_id != approval.signal_id:
        raise ValueError("risk approval does not belong to order request")
    if not approval.approved:
        raise PermissionError("risk approval is not approved")


def _coinbase_order_configuration(request: OrderRequest) -> dict[str, dict[str, str]]:
    if request.order_type is OrderType.MARKET:
        return {"market_market_ioc": {"base_size": str(request.quantity)}}
    assert request.limit_price is not None
    return {
        "limit_limit_gtc": {
            "base_size": str(request.quantity),
            "limit_price": str(request.limit_price),
            "post_only": "false",
        }
    }


def _coinbase_fill(raw: Mapping[str, Any], order: Order) -> Fill:
    # entry_id is unique per fill; trade_id repeats for adjusted fills.
    fill_id = str(
        raw.get("entry_id")
        or raw.get("trade_id")
        or raw.get("fill_id")
        or f"{order.order_id}-{raw.get('price')}"
    )
    return Fill(
        fill_id=fill_id,
        order_id=order.order_id,
        symbol=order.request.symbol,
        side=order.request.side,
        quantity=_decimal(raw.get("size") or raw.get("quantity"), default=Decimal("0.00000001")),
        price=_decimal(raw.get("price"), default=Decimal("0.00000001")),
        fee=_decimal(raw.get("commission") or raw.get("fee"), default=Decimal("0")),
        fee_asset=str(raw.get("commission_asset") or raw.get("fee_asset") or "USD"),
        occurred_at=_timestamp(raw.get("trade_time") or raw.get("timestamp")),
    )


def _coinbase_events(payload: Mapping[str, Any]) -> tuple[AuthenticatedOrderEvent, ...]:
    result: list[AuthenticatedOrderEvent] = []
    for event in payload.get("events", []):
        for raw in event.get("orders", []):
            result.append(
                AuthenticatedOrderEvent(
                    client_order_id=str(raw.get("client_order_id") or raw.get("client_order_id")),
                    status=str(raw.get("status") or raw.get("order_status") or "UNKNOWN").lower(),
                    provider_order_id=str(raw.get("order_id")) if raw.get("order_id") else None,
                    filled_quantity=str(
                        raw.get("filled_size") or raw.get("cumulative_quantity") or "0"
                    ),
                    observed_at=str(raw.get("timestamp")) if raw.get("timestamp") else None,
                )
            )
    return tuple(result)


def _order_status(raw: Mapping[str, Any], requested: Decimal) -> OrderStatus:
    value = str(raw.get("status") or raw.get("order_status") or "").lower()
    if value == "unknown_order_status":
        return OrderStatus.UNKNOWN
    if value in {"cancelled", "canceled", "expired"}:
        return OrderStatus.CANCELED
    if value in {"rejected", "failed"} or raw.get("success") is False:
        return OrderStatus.REJECTED
    filled = _decimal(raw.get("filled_size") or raw.get("filled_quantity"), default=Decimal("0"))
    if value == "filled" or filled >= requested:
        return OrderStatus.FILLED
    if filled > 0 or value in {"partially_filled", "partial"}:
        return OrderStatus.PARTIALLY_FILLED
    if value in {"pending", "pending_submit"}:
        return OrderStatus.PENDING_SUBMIT
    return OrderStatus.OPEN


def _recovered_request(raw: Mapping[str, Any], client_order_id: str) -> OrderRequest:
    side = OrderSide(str(raw.get("side") or "buy").lower())
    order_type = OrderType.LIMIT if raw.get("limit_price") or raw.get("price") else OrderType.MARKET
    quantity = _decimal(
        raw.get("original_size") or raw.get("size") or raw.get("filled_size"),
        default=Decimal("0.00000001"),
    )
    symbol = str(raw.get("product_id") or "BTC-USD")
    return OrderRequest(
        signal_id=uuid5(COINBASE_ORDER_NAMESPACE, f"signal|{client_order_id}"),
        strategy_version="provider-recovery",
        symbol=symbol,
        side=side,
        order_type=order_type,
        quantity=max(quantity, Decimal("0.00000001")),
        limit_price=_optional_decimal(raw.get("limit_price") or raw.get("price"))
        if order_type is OrderType.LIMIT
        else None,
        client_order_id=UUID(client_order_id),
        correlation_id=uuid5(COINBASE_ORDER_NAMESPACE, f"correlation|{client_order_id}"),
    )


def _provider_uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        return uuid5(COINBASE_ORDER_NAMESPACE, value)


def _unwrap(payload: Any, *keys: str) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        return {}
    for key in keys:
        value = payload.get(key)
        if isinstance(value, Mapping):
            return value
    return payload


def _rows(payload: Any, key: str) -> list[Mapping[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, Mapping)]
    if isinstance(payload, Mapping):
        value = payload.get(key, [])
        if isinstance(value, list):
            return [row for row in value if isinstance(row, Mapping)]
    return []


def _nested(payload: Mapping[str, Any], *keys: str) -> Any:
    value: Any = payload
    for key in keys:
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return value


def _decimal(value: Any, *, default: Decimal | None = None) -> Decimal:
    if value is None:
        if default is not None:
            return default
        raise ValueError("provider response omitted a required decimal")
    if isinstance(value, Mapping):
        value = value.get("value")
    return Decimal(str(value))


def _optional_decimal(value: Any) -> Decimal | None:
    return None if value is None else _decimal(value)


def _timestamp(value: Any) -> datetime:
    if value is None:
        return utc_now()
    if isinstance(value, (int, float)) or str(value).isdigit():
        return datetime.fromtimestamp(float(value), tz=UTC)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _error_message(payload: Any) -> str:
    if isinstance(payload, Mapping):
        return str(
            payload.get("error_response")
            or payload.get("error")
            or payload.get("message")
            or "provider rejected request"
        )
    return "provider rejected request"


def _edit_errors(payload: Any) -> str:
    if isinstance(payload, Mapping):
        reasons = [
            str(row.get("edit_failure_reason") or row.get("preview_failure_reason"))
            for row in _rows(payload, "errors")
        ]
        if reasons:
            return ", ".join(reasons)
    return "provider rejected the edit"


def _next_cursor(payload: Any, previous: str) -> str:
    """Return the next page cursor, or "" when the listing is complete."""

    if not isinstance(payload, Mapping) or payload.get("has_next") is False:
        return ""
    cursor = str(payload.get("cursor") or "")
    if cursor and cursor == previous:
        raise ProviderHTTPError(502, "pagination cursor did not advance")
    return cursor


def _is_missing(payload: Any) -> bool:
    return isinstance(payload, Mapping) and str(payload.get("error") or "").lower() in {
        "not_found",
        "order_not_found",
    }
