"""Gemini Sandbox broker adapter with an immutable host safety boundary."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse
from uuid import UUID, uuid5

import httpx
from core.models import (
    Balance,
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
from core.resilience import CircuitBreaker, TokenBucketRateLimiter

from brokers.http import (
    AmbiguousSubmissionError,
    ProviderHTTPClient,
    ProviderHTTPError,
    ProviderOrderRejectedError,
    ProviderTimeoutError,
)
from brokers.interface import BrokerCapabilities, BrokerInterface

GEMINI_SANDBOX_REST_URL = "https://api.sandbox.gemini.com"
GEMINI_SANDBOX_WS_URL = "wss://api.sandbox.gemini.com/v2/marketdata"
GEMINI_ORDER_NAMESPACE = UUID("e2fd1e2e-89a0-4c8f-86a9-9ad7f54ee32b")


class GeminiBroker(BrokerInterface):
    """Gemini adapter that can only address a ``*.sandbox.gemini.com`` host."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_secret: str | None = None,
        client: httpx.AsyncClient | None = None,
        base_url: str = GEMINI_SANDBOX_REST_URL,
        rate_limiter: TokenBucketRateLimiter | None = None,
        circuit_breaker: CircuitBreaker | None = None,
        max_429_retries: int = 2,
        backoff_base_seconds: float = 0.25,
    ) -> None:
        self._validate_sandbox_host(base_url)
        if (api_key is None) != (api_secret is None):
            raise ValueError("Gemini api_key and api_secret must be supplied together")
        self._base_url = GEMINI_SANDBOX_REST_URL
        self._api_key = api_key
        self._api_secret = api_secret
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
        self._raw_fills: dict[str, tuple[Mapping[str, Any], ...]] = {}
        self._nonce = int(time.time() * 1000)

    @staticmethod
    def _validate_sandbox_host(base_url: str) -> None:
        hostname = (urlparse(base_url).hostname or "").lower().rstrip(".")
        if not hostname.endswith(".sandbox.gemini.com"):
            raise ValueError("GeminiBroker only permits *.sandbox.gemini.com hosts")

    @property
    def capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities(
            provider="gemini",
            environment="sandbox",
            streaming=True,
            historical_candles=False,
            native_order_edit=False,
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
        raw = await self._public("GET", f"/v2/ticker/{_gemini_symbol(symbol)}")
        if not isinstance(raw, Mapping):
            raise ProviderHTTPError(502, "Gemini ticker response was not an object", payload=raw)
        return Quote(
            symbol=symbol,
            bid=_decimal(raw.get("bid")),
            ask=_decimal(raw.get("ask")),
            as_of=utc_now(),
            source="gemini-sandbox",
        )

    async def get_balances(self) -> tuple[Balance, ...]:
        payload = await self._private("POST", "/v1/balances", {})
        if not isinstance(payload, list):
            raise ProviderHTTPError(502, "Gemini balances response was not a list", payload=payload)
        now = utc_now()
        result = []
        for row in payload:
            if not isinstance(row, Mapping):
                continue
            available = _decimal(row.get("available"), default=Decimal("0"))
            hold = _decimal(row.get("available_for_withdrawal"), default=available)
            hold = max(Decimal("0"), available - hold)
            if available or hold:
                result.append(
                    Balance(
                        asset=str(row.get("currency") or "").upper(),
                        available=available,
                        hold=hold,
                        as_of=now,
                    )
                )
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
        body: dict[str, Any] = {
            "symbol": _gemini_symbol(request.symbol),
            "amount": str(request.quantity),
            "side": request.side.value,
            "type": "exchange market"
            if request.order_type is OrderType.MARKET
            else "exchange limit",
            "client_order_id": key,
        }
        if request.limit_price is not None:
            body["price"] = str(request.limit_price)
        try:
            payload = await self._private("POST", "/v1/order/new", body)
        except ProviderTimeoutError as exc:
            self._orders[key] = unknown
            raise AmbiguousSubmissionError(unknown) from exc
        except ProviderHTTPError as exc:
            if exc.status_code in {400, 409, 422}:
                rejected = unknown.model_copy(update={"status": OrderStatus.REJECTED})
                self._orders[key] = rejected
                raise ProviderOrderRejectedError(rejected, str(exc)) from exc
            raise
        if (
            isinstance(payload, Mapping)
            and payload.get("is_cancelled")
            and not payload.get("order_id")
        ):
            rejected = unknown.model_copy(update={"status": OrderStatus.REJECTED})
            self._orders[key] = rejected
            raise ProviderOrderRejectedError(
                rejected, str(payload.get("reason") or "Gemini rejected order")
            )
        order = self._order_from_payload(payload, request=request)
        self._orders[key] = order
        return order

    async def get_order(self, client_order_id: str) -> Order | None:
        cached = self._orders.get(client_order_id)
        if cached is not None and cached.status in {
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
            OrderStatus.REJECTED,
        }:
            return cached
        request = self._requests.get(client_order_id)
        body = {"client_order_id": client_order_id}
        provider_id = self._provider_order_ids.get(client_order_id)
        if provider_id is not None:
            body = {"order_id": provider_id}
        try:
            payload = await self._private("POST", "/v1/order/status", body)
        except ProviderHTTPError as exc:
            if exc.status_code == 404:
                return None
            raise
        if not payload:
            return None
        order = self._order_from_payload(payload, request=request, client_order_id=client_order_id)
        self._orders[client_order_id] = order
        return order

    async def get_fills(self, client_order_id: str) -> tuple[Fill, ...]:
        order = self._orders.get(client_order_id) or await self.get_order(client_order_id)
        if order is None:
            return ()
        raw_fills = self._raw_fills.get(client_order_id, ())
        return tuple(_gemini_fill(row, order) for row in raw_fills)

    async def cancel_order(self, client_order_id: str) -> Order | None:
        current = self._orders.get(client_order_id) or await self.get_order(client_order_id)
        if current is None:
            return None
        provider_id = self._provider_order_ids.get(client_order_id)
        if provider_id is None:
            raise ValueError("cannot cancel an order without a provider order id")
        payload = await self._private("POST", "/v1/order/cancel", {"order_id": provider_id})
        canceled = self._order_from_payload(payload, request=current.request)
        if canceled.status not in {OrderStatus.CANCELED, OrderStatus.FILLED}:
            canceled = canceled.model_copy(update={"status": OrderStatus.CANCELED})
        self._orders[client_order_id] = canceled
        return canceled

    async def edit_order(
        self,
        client_order_id: str,
        *,
        quantity: Decimal,
        limit_price: Decimal,
        approval: RiskApproval | None = None,
    ) -> Order:
        if approval is None:
            raise PermissionError(
                "Gemini uses cancel-and-replace and requires a fresh RiskApproval"
            )
        current = self._orders.get(client_order_id) or await self.get_order(client_order_id)
        if current is None:
            raise KeyError(client_order_id)
        canceled = await self.cancel_order(client_order_id)
        if canceled is None:
            raise KeyError(client_order_id)
        replacement = current.request.model_copy(
            update={
                "quantity": quantity,
                "limit_price": limit_price,
                "client_order_id": uuid5(
                    GEMINI_ORDER_NAMESPACE, f"{client_order_id}|{quantity}|{limit_price}"
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

    async def _public(self, method: str, path: str) -> Any:
        return await self._http.request_json(method, f"{self._base_url}{path}")

    async def _private(self, method: str, path: str, body: Mapping[str, Any]) -> Any:
        if self._api_key is None or self._api_secret is None:
            raise PermissionError("Gemini private requests require Sandbox credentials")
        nonce = self._next_nonce()
        payload = dict(body)
        payload["request"] = path
        payload["nonce"] = nonce
        encoded = base64.b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()
        signature = hmac.new(
            self._api_secret.encode(), encoded.encode(), hashlib.sha384
        ).hexdigest()
        return await self._http.request_json(
            method,
            f"{self._base_url}{path}",
            headers={
                "X-GEMINI-APIKEY": self._api_key,
                "X-GEMINI-PAYLOAD": encoded,
                "X-GEMINI-SIGNATURE": signature,
                "Cache-Control": "no-cache",
            },
        )

    def _next_nonce(self) -> int:
        self._nonce = max(self._nonce + 1, int(time.time() * 1000))
        return self._nonce

    def _order_from_payload(
        self,
        payload: Mapping[str, Any],
        *,
        request: OrderRequest | None = None,
        client_order_id: str | None = None,
    ) -> Order:
        raw = dict(payload)
        client_id = str(
            raw.get("client_order_id") or client_order_id or request and request.client_order_id
        )
        request = request or self._requests.get(client_id) or _recovered_request(raw, client_id)
        provider_id = str(raw.get("order_id") or client_id)
        self._provider_order_ids[client_id] = provider_id
        self._raw_fills[client_id] = tuple(raw.get("fills") or ())
        return Order(
            order_id=_provider_uuid(provider_id),
            request=request,
            status=_order_status(raw, request.quantity),
            filled_quantity=_decimal(raw.get("executed_amount"), default=Decimal("0")),
            average_fill_price=_optional_decimal(raw.get("avg_execution_price")),
            created_at=_timestamp(raw.get("timestampms")),
            updated_at=utc_now(),
        )


def _require_approval(request: OrderRequest, approval: RiskApproval) -> None:
    if request.signal_id != approval.signal_id:
        raise ValueError("risk approval does not belong to order request")
    if not approval.approved:
        raise PermissionError("risk approval is not approved")


def _gemini_symbol(symbol: str) -> str:
    return symbol.replace("-", "").replace("/", "").lower()


def _gemini_fill(raw: Mapping[str, Any], order: Order) -> Fill:
    return Fill(
        fill_id=str(raw.get("tid") or raw.get("fill_id") or f"{order.order_id}-{raw.get('price')}"),
        order_id=order.order_id,
        symbol=order.request.symbol,
        side=order.request.side,
        quantity=_decimal(raw.get("amount"), default=Decimal("0.00000001")),
        price=_decimal(raw.get("price"), default=Decimal("0.00000001")),
        fee=_decimal(raw.get("fee"), default=Decimal("0")),
        fee_asset=str(raw.get("fee_currency") or "USD"),
        occurred_at=_timestamp(raw.get("timestampms")),
    )


def _order_status(raw: Mapping[str, Any], requested: Decimal) -> OrderStatus:
    if raw.get("is_cancelled") or str(raw.get("reason") or "").lower() in {"cancelled", "canceled"}:
        return OrderStatus.CANCELED
    if str(raw.get("reason") or "").lower() in {"invalidquantity", "rejected", "insufficientfunds"}:
        return OrderStatus.REJECTED
    filled = _decimal(raw.get("executed_amount"), default=Decimal("0"))
    if filled >= requested or str(raw.get("is_live")).lower() == "false" and filled > 0:
        return OrderStatus.FILLED
    if filled > 0:
        return OrderStatus.PARTIALLY_FILLED
    if raw.get("is_live"):
        return OrderStatus.OPEN
    return OrderStatus.PENDING_SUBMIT


def _recovered_request(raw: Mapping[str, Any], client_order_id: str) -> OrderRequest:
    side = OrderSide(str(raw.get("side") or "buy").lower())
    order_type = OrderType.LIMIT if raw.get("price") else OrderType.MARKET
    quantity = _decimal(
        raw.get("original_amount") or raw.get("amount") or raw.get("executed_amount"),
        default=Decimal("0.00000001"),
    )
    symbol = str(raw.get("symbol") or "btcusd").upper()
    if len(symbol) > 3 and "-" not in symbol:
        symbol = f"{symbol[:-3]}-{symbol[-3:]}"
    return OrderRequest(
        signal_id=uuid5(GEMINI_ORDER_NAMESPACE, f"signal|{client_order_id}"),
        strategy_version="provider-recovery",
        symbol=symbol,
        side=side,
        order_type=order_type,
        quantity=max(quantity, Decimal("0.00000001")),
        limit_price=_optional_decimal(raw.get("price")) if order_type is OrderType.LIMIT else None,
        client_order_id=UUID(client_order_id),
        correlation_id=uuid5(GEMINI_ORDER_NAMESPACE, f"correlation|{client_order_id}"),
    )


def _provider_uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        return uuid5(GEMINI_ORDER_NAMESPACE, value)


def _decimal(value: Any, *, default: Decimal | None = None) -> Decimal:
    if value is None:
        if default is not None:
            return default
        raise ValueError("provider response omitted a required decimal")
    return Decimal(str(value))


def _optional_decimal(value: Any) -> Decimal | None:
    return None if value is None else _decimal(value)


def _timestamp(value: Any) -> datetime:
    if value is None:
        return utc_now()
    return datetime.fromtimestamp(float(value) / 1000, tz=UTC)
