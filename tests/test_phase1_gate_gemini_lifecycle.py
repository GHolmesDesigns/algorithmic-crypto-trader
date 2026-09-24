"""Phase 1 gate: the Gemini Sandbox order lifecycle through the adapter.

Criterion covered (wire level): Gemini Sandbox has exercised submit, partial fill,
cancel, reject, timeout, and recovery paths. The fake below serves the documented
Gemini REST shapes; the same lifecycle against the real Sandbox is owner-run.
"""

from __future__ import annotations

import base64
import json
from decimal import Decimal
from itertools import count
from typing import Any
from uuid import uuid4

import httpx
import pytest
from brokers.gemini import GeminiBroker
from brokers.http import AmbiguousSubmissionError, ProviderOrderRejectedError
from core.models import OrderRequest, OrderSide, OrderStatus, OrderType, RiskApproval
from core.resilience import TokenBucketRateLimiter
from execution.engine import ExecutionEngine, InMemoryOrderStore

MIN_SIZE = Decimal("0.00001")


class GeminiSandboxBook:
    """Resting limit orders, scripted partial fills, cancels, rejects, and lost responses."""

    def __init__(self) -> None:
        self.orders: dict[int, dict[str, Any]] = {}
        self.by_client: dict[str, int] = {}
        self.new_order_calls = 0
        self.fail_next_new: str | None = None  # "timeout_after_accept" | "timeout_before_accept"
        self._ids = count(7_000_000_001)
        self._tids = count(1)

    def fill(self, order_id: int, amount: str, price: str) -> None:
        order = self.orders[order_id]
        size = Decimal(amount)
        order["trades"].append(
            {
                "tid": next(self._tids),
                "order_id": str(order_id),
                "price": price,
                "amount": str(size),
                "type": order["side"].capitalize(),
                "aggressor": False,
                "fee_currency": "USD",
                "fee_amount": str(size * Decimal(price) * Decimal("0.002")),
                "timestampms": 1727179200000,
            }
        )
        order["executed"] += size
        if order["executed"] >= order["original"]:
            order["is_live"] = False

    def payload(self, order: dict[str, Any], *, include_trades: bool) -> dict[str, Any]:
        executed = order["executed"]
        body = {
            "order_id": str(order["id"]),
            "id": str(order["id"]),
            "client_order_id": order["client_order_id"],
            "symbol": "btcusd",
            "side": order["side"],
            "type": order["type"],
            "is_live": order["is_live"],
            "is_cancelled": order["is_cancelled"],
            "executed_amount": str(executed),
            "remaining_amount": str(order["original"] - executed),
            "original_amount": str(order["original"]),
            "price": order["price"],
            "timestampms": 1727179200000,
        }
        if executed:
            total = sum(Decimal(t["amount"]) * Decimal(t["price"]) for t in order["trades"])
            body["avg_execution_price"] = str(total / executed)
        if order["is_cancelled"]:
            body["reason"] = "Requested"
        if include_trades:
            body["trades"] = list(order["trades"])
        return body

    async def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.sandbox.gemini.com"
        if request.method == "GET":
            return httpx.Response(200, json={"bid": "60000", "ask": "60010"})
        payload = json.loads(base64.b64decode(request.headers["x-gemini-payload"]))
        path = payload["request"]
        if path == "/v1/order/new":
            return self.new_order(request, payload)
        if path == "/v1/order/status":
            order_id = payload.get("order_id")
            if order_id is None:
                order_id = self.by_client.get(payload.get("client_order_id"))
            order = self.orders.get(order_id) if order_id is not None else None
            if order is None:
                return httpx.Response(
                    404,
                    json={"result": "error", "reason": "OrderNotFound", "message": "not found"},
                )
            return httpx.Response(
                200, json=self.payload(order, include_trades=bool(payload.get("include_trades")))
            )
        if path == "/v1/order/cancel":
            order = self.orders[int(payload["order_id"])]
            order["is_live"] = False
            order["is_cancelled"] = True
            return httpx.Response(200, json=self.payload(order, include_trades=False))
        raise AssertionError(path)

    def new_order(self, request: httpx.Request, payload: dict[str, Any]) -> httpx.Response:
        self.new_order_calls += 1
        failure, self.fail_next_new = self.fail_next_new, None
        if failure == "timeout_before_accept":
            raise httpx.ReadTimeout("no response", request=request)
        if Decimal(payload["amount"]) < MIN_SIZE:
            return httpx.Response(
                400,
                json={
                    "result": "error",
                    "reason": "InvalidQuantity",
                    "message": f"Invalid quantity for symbol BTCUSD: {payload['amount']}",
                },
            )
        order_id = next(self._ids)
        self.orders[order_id] = {
            "id": order_id,
            "client_order_id": payload["client_order_id"],
            "side": payload["side"],
            "type": payload["type"],
            "original": Decimal(payload["amount"]),
            "price": payload.get("price", "0"),
            "executed": Decimal("0"),
            "is_live": True,
            "is_cancelled": False,
            "trades": [],
        }
        self.by_client[payload["client_order_id"]] = order_id
        if payload["type"] == "exchange market":
            self.fill(order_id, payload["amount"], "60010")
        if failure == "timeout_after_accept":
            raise httpx.ReadTimeout("accepted, but the response was lost", request=request)
        return httpx.Response(200, json=self.payload(self.orders[order_id], include_trades=False))


def gemini(book: GeminiSandboxBook) -> GeminiBroker:
    return GeminiBroker(
        api_key="sandbox-key",
        api_secret="sandbox-secret",
        client=httpx.AsyncClient(transport=httpx.MockTransport(book.handler)),
        rate_limiter=TokenBucketRateLimiter(10_000, 10_000),
    )


def request(
    *, order_type=OrderType.MARKET, quantity="0.01", limit_price=None
) -> tuple[OrderRequest, RiskApproval]:
    order = OrderRequest(
        signal_id=uuid4(),
        strategy_version="gemini-lifecycle",
        symbol="BTC-USD",
        side=OrderSide.BUY,
        order_type=order_type,
        quantity=Decimal(quantity),
        limit_price=Decimal(limit_price) if limit_price else None,
        correlation_id=uuid4(),
    )
    return order, RiskApproval(
        signal_id=order.signal_id,
        approved=True,
        reason="lifecycle",
        correlation_id=order.correlation_id,
    )


@pytest.mark.asyncio
async def test_submit_acknowledge_and_immediate_fill() -> None:
    book = GeminiSandboxBook()
    broker = gemini(book)
    store = InMemoryOrderStore()
    order_request, approval = request()

    order = await ExecutionEngine(broker, store).submit(order_request, approval)

    assert order.status is OrderStatus.FILLED
    fills = tuple(store.fills.values())
    assert [(fill.quantity, fill.price) for fill in fills] == [(Decimal("0.01"), Decimal("60010"))]
    assert fills[0].fee == Decimal("0.01") * Decimal("60010") * Decimal("0.002")
    assert fills[0].order_id == order_request.client_order_id
    await broker.close()


@pytest.mark.asyncio
async def test_working_order_partially_fills_then_completes() -> None:
    book = GeminiSandboxBook()
    broker = gemini(book)
    store = InMemoryOrderStore()
    order_request, approval = request(
        order_type=OrderType.LIMIT, quantity="0.02", limit_price="59000"
    )
    engine = ExecutionEngine(broker, store)

    working = await engine.submit(order_request, approval)
    assert working.status is OrderStatus.OPEN
    venue_id = book.by_client[str(order_request.client_order_id)]

    book.fill(venue_id, "0.008", "59000")
    partial = await engine.recover(str(order_request.client_order_id))
    assert partial.status is OrderStatus.PARTIALLY_FILLED
    assert partial.filled_quantity == Decimal("0.008")
    assert len(store.fills) == 1

    book.fill(venue_id, "0.012", "59000")
    filled = await engine.recover(str(order_request.client_order_id))
    assert filled.status is OrderStatus.FILLED
    assert sorted(fill.quantity for fill in store.fills.values()) == [
        Decimal("0.008"),
        Decimal("0.012"),
    ]
    await broker.close()


@pytest.mark.asyncio
async def test_working_order_is_canceled() -> None:
    book = GeminiSandboxBook()
    broker = gemini(book)
    order_request, approval = request(order_type=OrderType.LIMIT, limit_price="50000")
    await broker.submit_order(order_request, approval)

    canceled = await broker.cancel_order(str(order_request.client_order_id))

    assert canceled is not None and canceled.status is OrderStatus.CANCELED
    restarted = gemini(book)  # a fresh adapter reads the venue, not a cache
    after = await restarted.get_order(str(order_request.client_order_id))
    assert after is not None and after.status is OrderStatus.CANCELED
    await broker.close()
    await restarted.close()


@pytest.mark.asyncio
async def test_undersized_order_is_rejected_and_never_retried() -> None:
    book = GeminiSandboxBook()
    broker = gemini(book)
    store = InMemoryOrderStore()
    order_request, approval = request(quantity="0.000001")
    engine = ExecutionEngine(broker, store)

    with pytest.raises(ProviderOrderRejectedError, match="InvalidQuantity"):
        await engine.submit(order_request, approval)
    again = await engine.submit(order_request, approval)

    assert store.get(str(order_request.client_order_id)).status is OrderStatus.REJECTED
    assert again.status is OrderStatus.REJECTED
    assert book.new_order_calls == 1
    await broker.close()


@pytest.mark.asyncio
async def test_timeout_after_the_venue_accepted_is_recovered_without_a_second_order() -> None:
    book = GeminiSandboxBook()
    book.fail_next_new = "timeout_after_accept"
    broker = gemini(book)
    store = InMemoryOrderStore()
    order_request, approval = request()
    engine = ExecutionEngine(broker, store)

    with pytest.raises(AmbiguousSubmissionError):
        await engine.submit(order_request, approval)
    assert store.get(str(order_request.client_order_id)).status is OrderStatus.UNKNOWN

    # After a restart, recovery queries by client_order_id and never resubmits.
    restarted = gemini(book)
    recovered = await ExecutionEngine(restarted, store).recover_pending()

    assert [order.status for order in recovered] == [OrderStatus.FILLED]
    assert book.new_order_calls == 1 and len(book.orders) == 1
    assert len(store.fills) == 1
    await broker.close()
    await restarted.close()


@pytest.mark.asyncio
async def test_timeout_before_the_venue_accepted_is_submitted_exactly_once() -> None:
    book = GeminiSandboxBook()
    book.fail_next_new = "timeout_before_accept"
    broker = gemini(book)
    store = InMemoryOrderStore()
    order_request, approval = request()
    engine = ExecutionEngine(broker, store)

    with pytest.raises(AmbiguousSubmissionError):
        await engine.submit(order_request, approval)
    # The venue has no record, so the pending order stays unresolved on recovery...
    recovered = await ExecutionEngine(gemini(book), store).recover_pending()
    assert [order.status for order in recovered] == [OrderStatus.UNKNOWN]
    assert [order.status for order in store.pending()] == [OrderStatus.UNKNOWN]
    # ...and an explicit retry queries first, then submits once.
    retried = await engine.submit(order_request, approval)

    assert retried.status is OrderStatus.FILLED
    assert book.new_order_calls == 2 and len(book.orders) == 1
    await broker.close()
