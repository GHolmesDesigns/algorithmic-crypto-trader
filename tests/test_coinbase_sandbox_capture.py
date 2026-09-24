"""The Coinbase adapter against responses captured from the Advanced Trade sandbox.

The files in ``tests/fixtures/coinbase/sandbox/`` are the sandbox's own static
responses, captured on 2026-09-24 (see ``manifest.json``). The sandbox echoes fixed
identifiers rather than the request's, so tests seed the adapter with the order it
would already know and check that each real response parses as the adapter expects.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from brokers.coinbase import (
    COINBASE_SANDBOX_REST_URL,
    CoinbaseBroker,
    _configured_order,
)
from brokers.http import ProviderHTTPError, ProviderOrderRejectedError
from core.models import Order, OrderRequest, OrderSide, OrderStatus, OrderType, RiskApproval
from core.resilience import TokenBucketRateLimiter

SANDBOX = Path(__file__).parent / "fixtures" / "coinbase" / "sandbox"
PREFIX = "/api/v3/brokerage"


def captured(name: str) -> dict:
    return json.loads((SANDBOX / f"{name}.json").read_text(encoding="utf-8"))


def response_of(name: str) -> dict:
    return captured(name)["response"]


def replaying(routes: dict[tuple[str, str], str]) -> CoinbaseBroker:
    """An adapter whose requests are answered with the named captured responses."""

    async def handler(request: httpx.Request) -> httpx.Response:
        key = (request.method, request.url.path.removeprefix(PREFIX))
        if key not in routes:
            raise AssertionError(f"unexpected {key}")
        fixture = captured(routes[key])
        return httpx.Response(fixture["status"], json=fixture["response"])

    return CoinbaseBroker(
        auth_token="sandbox-needs-no-token",
        base_url=COINBASE_SANDBOX_REST_URL,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        rate_limiter=TokenBucketRateLimiter(10_000, 10_000),
        backoff_base_seconds=0,
    )


def limit_request() -> tuple[OrderRequest, RiskApproval]:
    request = OrderRequest(
        signal_id=uuid4(),
        strategy_version="sandbox-capture",
        symbol="BTC-USD",
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        quantity=Decimal("1"),
        limit_price=Decimal("70000.44"),
        correlation_id=uuid4(),
    )
    approval = RiskApproval(
        signal_id=request.signal_id,
        approved=True,
        reason="sandbox capture",
        correlation_id=request.correlation_id,
    )
    return request, approval


def known_order(broker: CoinbaseBroker, provider_id: str) -> tuple[str, OrderRequest]:
    """Seed the adapter with an open order it placed earlier under ``provider_id``."""

    request, _ = limit_request()
    key = str(request.client_order_id)
    broker._requests[key] = request
    broker._provider_order_ids[key] = provider_id
    broker._orders[key] = Order(order_id=uuid4(), request=request, status=OrderStatus.OPEN)
    return key, request


def test_the_capture_manifest_names_its_source_and_every_response() -> None:
    manifest = json.loads((SANDBOX / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["source"] == "https://api-sandbox.coinbase.com/api/v3/brokerage"
    assert manifest["captured_at"].startswith("2026-09-24")
    assert len(manifest["requests"]) == 11
    for name in manifest["requests"]:
        assert (SANDBOX / f"{name}.json").is_file()


@pytest.mark.asyncio
async def test_captured_accounts_parse_into_balances_and_positions() -> None:
    broker = replaying({("GET", "/accounts"): "list_accounts"})
    balances = await broker.get_balances()
    positions = await broker.get_positions()
    await broker.close()

    accounts = response_of("list_accounts")["accounts"]
    expected = {
        row["currency"]: Decimal(row["available_balance"]["value"])
        for row in accounts
        if Decimal(row["available_balance"]["value"]) or Decimal(row["hold"]["value"])
    }
    assert {item.asset: item.available for item in balances} == expected
    assert {item.symbol for item in positions} == {
        f"{asset}-USD" for asset, amount in expected.items() if asset != "USD" and amount
    }


@pytest.mark.asyncio
async def test_captured_order_parses_status_and_nested_configuration() -> None:
    raw = response_of("get_order")["order"]
    broker = replaying({("GET", f"/orders/historical/{raw['order_id']}"): "get_order"})
    key, _ = known_order(broker, raw["order_id"])
    broker._orders.pop(key)  # force a read from the venue
    order = await broker.get_order(key)
    await broker.close()

    assert order is not None and order.status is OrderStatus.CANCELED
    assert order.filled_quantity == Decimal(raw["filled_size"])
    assert order.created_at.isoformat().startswith(raw["created_time"][:19])
    # Size and limit price live under order_configuration, as restart recovery reads them.
    assert _configured_order(raw) == (Decimal("1"), Decimal("70000.44"))


@pytest.mark.asyncio
async def test_captured_create_success_is_accepted_even_with_the_sandbox_fixed_ids() -> None:
    broker = replaying(
        {
            ("POST", "/orders"): "create_order",
            ("GET", "/orders/historical/batch"): "list_orders",
        }
    )
    request, approval = limit_request()
    order = await broker.submit_order(request, approval)
    await broker.close()

    # The sandbox echoes a fixed client_order_id and a static order list, so the
    # read-back finds nothing; the accepted order stays OPEN for reconciliation.
    assert response_of("create_order")["success"] is True
    assert order.status is OrderStatus.OPEN
    assert order.request.client_order_id == request.client_order_id


@pytest.mark.asyncio
async def test_captured_insufficient_funds_is_a_readable_rejection() -> None:
    broker = replaying({("POST", "/orders"): "create_order_insufficient_fund"})
    request, approval = limit_request()
    with pytest.raises(ProviderOrderRejectedError) as rejected:
        await broker.submit_order(request, approval)
    await broker.close()

    assert str(rejected.value) == "INSUFFICIENT_FUND: Insufficient balance in source account"
    assert rejected.value.order.status is OrderStatus.REJECTED


@pytest.mark.asyncio
async def test_captured_cancel_results_are_read_for_success_and_failure() -> None:
    provider_id = response_of("cancel_orders")["results"][0]["order_id"]
    ok = replaying(
        {
            ("POST", "/orders/batch_cancel"): "cancel_orders",
            ("GET", f"/orders/historical/{provider_id}"): "get_order",
        }
    )
    key, _ = known_order(ok, provider_id)
    canceled = await ok.cancel_order(key)
    await ok.close()
    assert canceled is not None and canceled.status is OrderStatus.CANCELED

    failing = replaying({("POST", "/orders/batch_cancel"): "cancel_orders_failure"})
    key, _ = known_order(failing, provider_id)
    with pytest.raises(ProviderHTTPError, match="UNKNOWN_CANCEL_ORDER") as error:
        await failing.cancel_order(key)
    await failing.close()
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_captured_edit_results_are_read_for_success_and_failure() -> None:
    provider_id = response_of("cancel_orders")["results"][0]["order_id"]
    failing = replaying({("POST", "/orders/edit"): "edit_order_failure"})
    key, _ = known_order(failing, provider_id)
    with pytest.raises(ProviderHTTPError, match="ORDER_NOT_FOUND"):
        await failing.edit_order(key, quantity=Decimal("2"), limit_price=Decimal("69000"))
    await failing.close()

    ok = replaying(
        {
            ("POST", "/orders/edit"): "edit_order",
            ("GET", f"/orders/historical/{provider_id}"): "get_order",
        }
    )
    key, _ = known_order(ok, provider_id)
    edited = await ok.edit_order(key, quantity=Decimal("2"), limit_price=Decimal("69000"))
    await ok.close()
    assert edited.request.quantity == Decimal("2")
    assert edited.request.limit_price == Decimal("69000")


@pytest.mark.asyncio
async def test_captured_fills_parse_with_unique_entry_ids() -> None:
    broker = replaying({("GET", "/orders/historical/fills"): "list_fills"})
    rows = response_of("list_fills")["fills"]
    key, _ = known_order(broker, rows[0]["order_id"])
    fills = await broker.get_fills(key)
    await broker.close()

    assert [item.fill_id for item in fills] == [row["entry_id"] for row in rows]
    assert [item.quantity for item in fills] == [Decimal(row["size"]) for row in rows]
    assert [item.price for item in fills] == [Decimal(row["price"]) for row in rows]
    assert [item.fee for item in fills] == [Decimal(row["commission"]) for row in rows]
