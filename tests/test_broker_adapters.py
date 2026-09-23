from __future__ import annotations

import base64
import json
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from brokers.coinbase import CoinbaseBroker
from brokers.gemini import GeminiBroker
from brokers.http import AmbiguousSubmissionError
from core.models import OrderRequest, OrderSide, OrderStatus, OrderType, RiskApproval
from core.resilience import TokenBucketRateLimiter

from tests.contracts.broker_contract import assert_shared_broker_contract


def request() -> OrderRequest:
    return OrderRequest(
        signal_id=uuid4(),
        strategy_version="adapter-test",
        symbol="BTC-USD",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("0.01"),
        correlation_id=uuid4(),
    )


def approval(order_request: OrderRequest) -> RiskApproval:
    return RiskApproval(
        signal_id=order_request.signal_id,
        approved=True,
        reason="adapter test approval",
        correlation_id=order_request.correlation_id,
    )


def limiter() -> TokenBucketRateLimiter:
    return TokenBucketRateLimiter(100, 100)


@pytest.mark.asyncio
async def test_coinbase_adapter_satisfies_unchanged_contract_and_authenticates_private_calls() -> (
    None
):
    order_id = "11111111-1111-4111-8111-111111111111"

    async def handler(request_: httpx.Request) -> httpx.Response:
        if request_.url.path.endswith("/ticker"):
            assert "authorization" not in request_.headers
        else:
            assert request_.headers["authorization"] == "Bearer test-token"
        if request_.method == "GET" and request_.url.path.endswith("/ticker"):
            return httpx.Response(200, json={"best_bid": "99", "best_ask": "101"}, request=request_)
        if request_.method == "POST" and request_.url.path.endswith("/orders"):
            body = json.loads(request_.content)
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "success_response": {
                        "order_id": order_id,
                        "client_order_id": body["client_order_id"],
                        "status": "FILLED",
                        "filled_size": "0.01",
                        "average_filled_price": "100",
                    },
                },
                request=request_,
            )
        if request_.method == "GET" and "/orders/client:" in request_.url.path:
            client_id = request_.url.path.rsplit(":", 1)[1]
            return httpx.Response(
                200,
                json={
                    "order": {
                        "order_id": order_id,
                        "client_order_id": client_id,
                        "status": "FILLED",
                        "filled_size": "0.01",
                    }
                },
                request=request_,
            )
        if request_.method == "GET" and request_.url.path.endswith("/fills"):
            return httpx.Response(
                200,
                json={"fills": [{"trade_id": "cb-fill-1", "size": "0.01", "price": "100"}]},
                request=request_,
            )
        raise AssertionError(f"unexpected request: {request_.method} {request_.url}")

    broker = CoinbaseBroker(
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        auth_token="test-token",
        rate_limiter=limiter(),
    )
    try:
        await assert_shared_broker_contract(lambda: broker)
    finally:
        await broker.close()


@pytest.mark.asyncio
async def test_gemini_adapter_satisfies_contract_using_only_sandbox_paths() -> None:
    provider_order_id = "gemini-123"

    async def handler(request_: httpx.Request) -> httpx.Response:
        if request_.method == "GET" and "/v2/ticker/" in request_.url.path:
            return httpx.Response(200, json={"bid": "99", "ask": "101"}, request=request_)
        assert request_.headers["x-gemini-apikey"] == "sandbox-key"
        payload = json.loads(base64.b64decode(request_.headers["x-gemini-payload"]))
        if payload["request"] == "/v1/order/new":
            return httpx.Response(
                200,
                json={
                    "order_id": provider_order_id,
                    "client_order_id": payload["client_order_id"],
                    "symbol": "btcusd",
                    "side": "buy",
                    "original_amount": "0.01",
                    "executed_amount": "0.01",
                    "avg_execution_price": "100",
                    "is_live": False,
                    "fills": [{"tid": "gm-fill-1", "amount": "0.01", "price": "100"}],
                },
                request=request_,
            )
        if payload["request"] == "/v1/order/status":
            return httpx.Response(
                200,
                json={
                    "order_id": provider_order_id,
                    "client_order_id": payload.get("client_order_id"),
                    "symbol": "btcusd",
                    "side": "buy",
                    "original_amount": "0.01",
                    "executed_amount": "0.01",
                    "is_live": False,
                },
                request=request_,
            )
        raise AssertionError(f"unexpected Gemini request {payload}")

    broker = GeminiBroker(
        api_key="sandbox-key",
        api_secret="sandbox-secret",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        rate_limiter=limiter(),
    )
    try:
        await assert_shared_broker_contract(lambda: broker)
    finally:
        await broker.close()


def test_gemini_rejects_every_non_sandbox_host() -> None:
    with pytest.raises(ValueError, match="sandbox"):
        GeminiBroker(base_url="https://api.gemini.com")


@pytest.mark.asyncio
async def test_coinbase_timeout_is_unknown_and_second_submission_queries_before_retry() -> None:
    order_request = request()
    calls: list[str] = []

    async def handler(request_: httpx.Request) -> httpx.Response:
        calls.append(request_.url.path)
        if request_.method == "POST":
            raise httpx.ReadTimeout("ambiguous", request=request_)
        client_id = request_.url.path.rsplit(":", 1)[1]
        return httpx.Response(
            200,
            json={
                "order": {
                    "order_id": "22222222-2222-4222-8222-222222222222",
                    "client_order_id": client_id,
                    "status": "OPEN",
                    "filled_size": "0",
                }
            },
            request=request_,
        )

    broker = CoinbaseBroker(
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        auth_token="test-token",
        rate_limiter=limiter(),
    )
    try:
        with pytest.raises(AmbiguousSubmissionError) as error:
            await broker.submit_order(order_request, approval(order_request))
        assert error.value.order.status is OrderStatus.UNKNOWN
        recovered = await broker.submit_order(order_request, approval(order_request))
        assert recovered.status is OrderStatus.OPEN
        assert calls[0].endswith("/orders")
        assert "/orders/client:" in calls[1]
        assert sum(path.endswith("/orders") for path in calls) == 1
    finally:
        await broker.close()
