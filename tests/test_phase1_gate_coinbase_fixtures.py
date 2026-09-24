"""Phase 1 gate: Coinbase fixtures for authentication, pagination, errors, and scenarios.

Criterion covered: Coinbase sandbox fixtures cover authentication, pagination, error
payloads, and the documented error scenarios. The fixtures follow the documented
response shapes (see ``tests/fixtures/coinbase/README.md``); a redacted owner-run
capture from the Advanced Trade sandbox should replace them without test changes.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
import jwt
import pytest
from brokers.coinbase import (
    COINBASE_SANDBOX_REST_URL,
    MAX_ACCOUNT_PAGES,
    CoinbaseBroker,
    _order_status,
)
from brokers.http import AmbiguousSubmissionError, ProviderHTTPError, ProviderOrderRejectedError
from core.models import OrderRequest, OrderSide, OrderStatus, OrderType, RiskApproval
from core.resilience import TokenBucketRateLimiter
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

FIXTURES = Path(__file__).parent / "fixtures" / "coinbase"
PREFIX = "/api/v3/brokerage"
KEY_NAME = "organizations/gate-org/apiKeys/gate-key"


def fixture(name: str, **values: str):
    text = (FIXTURES / name).read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace("{" + key + "}", value)
    return json.loads(text)


def order_request(side: OrderSide = OrderSide.BUY, **changes) -> OrderRequest:
    values = dict(
        signal_id=uuid4(),
        strategy_version="coinbase-fixture",
        symbol="BTC-USD",
        side=side,
        order_type=OrderType.MARKET,
        quantity=Decimal("0.01"),
        correlation_id=uuid4(),
    )
    values.update(changes)
    return OrderRequest(**values)


def approval(request: OrderRequest) -> RiskApproval:
    return RiskApproval(
        signal_id=request.signal_id,
        approved=True,
        reason="fixture approval",
        correlation_id=request.correlation_id,
    )


def broker_for(handler, **options) -> CoinbaseBroker:
    options.setdefault("auth_token", "gate-token")
    return CoinbaseBroker(
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        rate_limiter=TokenBucketRateLimiter(10_000, 10_000),
        backoff_base_seconds=0,
        **options,
    )


def ec_key_pair() -> tuple[str, ec.EllipticCurvePublicKey]:
    private = ec.generate_private_key(ec.SECP256R1())
    pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return pem, private.public_key()


def accounts_page(assets: list[str], *, cursor: str, has_next: bool) -> dict:
    return {
        "accounts": [
            {
                "uuid": f"redacted-{asset}",
                "currency": asset,
                "available_balance": {"value": "1.5", "currency": asset},
                "hold": {"value": "0", "currency": asset},
            }
            for asset in assets
        ],
        "has_next": has_next,
        "cursor": cursor,
        "size": len(assets),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("base_url", "host"),
    [(None, "api.coinbase.com"), (COINBASE_SANDBOX_REST_URL, "api-sandbox.coinbase.com")],
)
async def test_jwt_matches_the_documented_cdp_format(base_url, host) -> None:
    pem, public_key = ec_key_pair()
    seen: list[tuple[str, dict, dict]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        token = request.headers["authorization"].removeprefix("Bearer ")
        claims = jwt.decode(token, public_key, algorithms=["ES256"], options={"verify_aud": False})
        seen.append((request.url.path, jwt.get_unverified_header(token), claims))
        if request.url.path.endswith("/accounts"):
            return httpx.Response(200, json=accounts_page(["USD"], cursor="", has_next=False))
        return httpx.Response(200, json=fixture("key_permissions_view_only.json"))

    options = {"api_key": KEY_NAME, "private_key": pem, "auth_token": None}
    if base_url:
        options["base_url"] = base_url
    broker = broker_for(handler, **options)
    await broker.get_balances()
    permissions = await broker.key_permissions()
    await broker.close()

    assert permissions == {"can_view": True, "can_trade": False, "can_transfer": False}
    (accounts_path, header, claims), (permissions_path, _, permission_claims) = seen
    assert header["alg"] == "ES256" and header["kid"] == KEY_NAME
    assert len(header["nonce"]) == 32 and int(header["nonce"], 16) >= 0
    assert claims["sub"] == KEY_NAME and claims["iss"] == "cdp"
    assert claims["exp"] - claims["nbf"] == 120
    # The uri claim names the method, host, and full path of this one request.
    assert claims["uri"] == f"GET {host}{PREFIX}/accounts" == f"GET {host}{accounts_path}"
    assert permission_claims["uri"] == f"GET {host}{PREFIX}/key_permissions"
    assert permissions_path == f"{PREFIX}/key_permissions"


@pytest.mark.asyncio
async def test_a_rejected_token_is_refreshed_once_then_fails_closed() -> None:
    pem, _ = ec_key_pair()
    tokens: list[str] = []
    responses = [401, 200]

    async def handler(request: httpx.Request) -> httpx.Response:
        tokens.append(request.headers["authorization"])
        status = responses.pop(0) if responses else 401
        if status == 401:
            return httpx.Response(401, json=fixture("error_unauthenticated.json"))
        return httpx.Response(200, json=accounts_page(["USD"], cursor="", has_next=False))

    broker = broker_for(handler, api_key=KEY_NAME, private_key=pem, auth_token=None)
    balances = await broker.get_balances()
    assert [item.asset for item in balances] == ["USD"]
    assert len(tokens) == 2 and tokens[0] != tokens[1]

    tokens.clear()
    responses.extend([401, 401])
    with pytest.raises(ProviderHTTPError) as error:
        await broker.get_balances()
    assert error.value.status_code == 401 and len(tokens) == 2
    await broker.close()


@pytest.mark.asyncio
async def test_accounts_are_read_across_every_page() -> None:
    pages = {
        "": accounts_page(["USD"] + [f"A{n:03d}" for n in range(59)], cursor="p2", has_next=True),
        "p2": accounts_page([f"B{n:03d}" for n in range(60)], cursor="p3", has_next=True),
        "p3": accounts_page([f"C{n:03d}" for n in range(10)], cursor="", has_next=False),
    }
    requests: list[dict[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"{PREFIX}/accounts"
        requests.append(dict(request.url.params))
        return httpx.Response(200, json=pages[request.url.params.get("cursor", "")])

    broker = broker_for(handler)
    balances = await broker.get_balances()
    positions = await broker.get_positions()
    await broker.close()

    # More than the 49-account default page: nothing beyond page one may be dropped.
    assert len(balances) == 130
    assert len(positions) == 129
    assert requests[0] == {"limit": "250"}
    assert [item.get("cursor") for item in requests] == [None, "p2", "p3"] * 2


@pytest.mark.asyncio
async def test_pagination_fails_closed_when_it_never_ends_or_does_not_advance() -> None:
    requests = []

    async def endless(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, json=accounts_page(["USD"], cursor=f"c{len(requests)}", has_next=True)
        )

    broker = broker_for(endless)
    with pytest.raises(ProviderHTTPError, match="pagination exceeded"):
        await broker.get_balances()
    assert len(requests) == MAX_ACCOUNT_PAGES
    await broker.close()

    async def stuck(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=accounts_page(["USD"], cursor="same", has_next=True))

    broker = broker_for(stuck)
    with pytest.raises(ProviderHTTPError, match="did not advance"):
        await broker.get_balances()
    await broker.close()


@pytest.mark.asyncio
async def test_timed_out_order_is_found_on_a_later_order_page_and_fills_paginate() -> None:
    request = order_request()
    client_id = str(request.client_order_id)
    order_id = "11111111-1111-4111-8111-111111111111"
    calls: list[tuple[str, str, dict[str, str]]] = []

    async def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix(PREFIX)
        calls.append((req.method, path, dict(req.url.params)))
        if req.method == "POST":
            raise httpx.ReadTimeout("no response", request=req)
        if path == "/orders/historical/batch":
            if req.url.params.get("cursor") != "orders-2":
                other = {"order_id": "other", "client_order_id": str(uuid4()), "status": "FILLED"}
                return httpx.Response(
                    200, json={"orders": [other], "has_next": True, "cursor": "orders-2"}
                )
            mine = fixture("get_order_filled.json", client_order_id=client_id)["order"]
            return httpx.Response(200, json={"orders": [mine], "has_next": False, "cursor": ""})
        if path == "/orders/historical/fills":
            assert req.url.params["order_ids"] == order_id
            page = req.url.params.get("cursor", "")
            entries = {"": ("f-1", "fills-2"), "fills-2": ("f-2", "")}[page]
            fill = {
                "entry_id": entries[0],
                "trade_id": "shared-trade-id",
                "order_id": order_id,
                "trade_time": "2026-09-24T12:00:00Z",
                "price": "60000",
                "size": "0.005",
                "commission": "2.7",
                "side": "BUY",
            }
            return httpx.Response(200, json={"fills": [fill], "cursor": entries[1]})
        raise AssertionError(f"unexpected {req.method} {path}")

    broker = broker_for(handler)
    with pytest.raises(AmbiguousSubmissionError):
        await broker.submit_order(request, approval(request))
    recovered = await broker.submit_order(request, approval(request))
    fills = await broker.get_fills(client_id)
    await broker.close()

    assert recovered.status is OrderStatus.FILLED
    assert sum(1 for method, _, _ in calls if method == "POST") == 1
    assert [item.fill_id for item in fills] == ["f-1", "f-2"]
    assert sum(item.quantity for item in fills) == Decimal("0.01")
    assert [item.fee for item in fills] == [Decimal("2.7"), Decimal("2.7")]


@pytest.mark.asyncio
async def test_create_order_reads_back_fill_state_the_create_response_omits() -> None:
    request = order_request()
    client_id = str(request.client_order_id)
    bodies: list[dict] = []

    async def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix(PREFIX)
        if req.method == "POST" and path == "/orders":
            bodies.append(json.loads(req.content))
            return httpx.Response(
                200, json=fixture("create_order_success.json", client_order_id=client_id)
            )
        if path == "/orders/historical/11111111-1111-4111-8111-111111111111":
            return httpx.Response(
                200, json=fixture("get_order_filled.json", client_order_id=client_id)
            )
        if path == "/orders/historical/batch":
            return httpx.Response(200, json={"orders": [], "has_next": False, "cursor": ""})
        raise AssertionError(f"unexpected {req.method} {path}")

    broker = broker_for(handler)
    order = await broker.submit_order(request, approval(request))
    await broker.close()

    assert order.status is OrderStatus.FILLED
    assert order.filled_quantity == Decimal("0.01")
    assert bodies == [
        {
            "client_order_id": client_id,
            "product_id": "BTC-USD",
            "side": "BUY",
            "order_configuration": {"market_market_ioc": {"base_size": "0.01"}},
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "body", "message"),
    [
        (200, "create_order_insufficient_fund.json", "INSUFFICIENT_FUND"),
        (400, "error_invalid_argument.json", "INVALID_ARGUMENT"),
    ],
    ids=["sandbox-insufficient-fund", "invalid-argument"],
)
async def test_documented_order_rejections_are_rejected_not_retried(status, body, message) -> None:
    request = order_request()
    posts = []

    async def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            posts.append(req)
            return httpx.Response(status, json=fixture(body))
        return httpx.Response(200, json={"orders": [], "has_next": False, "cursor": ""})

    broker = broker_for(handler)
    with pytest.raises(ProviderOrderRejectedError, match=message) as error:
        await broker.submit_order(request, approval(request))
    again = await broker.submit_order(request, approval(request))
    await broker.close()

    assert error.value.order.status is OrderStatus.REJECTED
    assert again.status is OrderStatus.REJECTED
    assert len(posts) == 1


async def resting_limit_order(handler_extra, *, status_after_cancel: str | None = None):
    request = order_request(order_type=OrderType.LIMIT, limit_price=Decimal("50000"))
    client_id = str(request.client_order_id)
    order_id = "11111111-1111-4111-8111-111111111111"

    historical_reads = 0

    async def handler(req: httpx.Request) -> httpx.Response:
        nonlocal historical_reads
        path = req.url.path.removeprefix(PREFIX)
        if req.method == "POST" and path == "/orders":
            return httpx.Response(
                200, json=fixture("create_order_success.json", client_order_id=client_id)
            )
        if req.method == "GET" and path == f"/orders/historical/{order_id}":
            historical_reads += 1
            open_order = fixture("get_order_filled.json", client_order_id=client_id)["order"]
            open_order.update(status="OPEN", filled_size="0", order_type="LIMIT")
            if status_after_cancel is not None and historical_reads > 1:
                open_order.update(
                    status=status_after_cancel,
                    filled_size="0.01" if status_after_cancel == "FILLED" else "0",
                )
            return httpx.Response(200, json={"order": open_order})
        if path == "/orders/historical/batch":
            return httpx.Response(200, json={"orders": [], "has_next": False, "cursor": ""})
        return await handler_extra(req, path)

    broker = broker_for(handler)
    order = await broker.submit_order(request, approval(request))
    assert order.status is OrderStatus.OPEN
    return broker, client_id


@pytest.mark.asyncio
async def test_sandbox_cancel_failure_leaves_the_order_open() -> None:
    async def extra(req, path):
        assert path == "/orders/batch_cancel"
        return httpx.Response(200, json=fixture("cancel_orders_failure.json"))

    broker, client_id = await resting_limit_order(extra)
    with pytest.raises(ProviderHTTPError, match="UNKNOWN_CANCEL_FAILURE_REASON") as error:
        await broker.cancel_order(client_id)
    assert error.value.status_code == 409
    assert (await broker.get_order(client_id)).status is OrderStatus.OPEN
    await broker.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_status", "expected"),
    [("OPEN", OrderStatus.OPEN), ("FILLED", OrderStatus.FILLED)],
)
async def test_successful_cancel_uses_authoritative_order_state(provider_status, expected) -> None:
    async def extra(req, path):
        assert path == "/orders/batch_cancel"
        return httpx.Response(200, json={"results": [{"success": True}]})

    broker, client_id = await resting_limit_order(extra, status_after_cancel=provider_status)
    order = await broker.cancel_order(client_id)

    assert order.status is expected
    assert (await broker.get_order(client_id)).status is expected
    await broker.close()


@pytest.mark.asyncio
async def test_sandbox_edit_failure_leaves_the_order_unchanged() -> None:
    async def extra(req, path):
        assert path == "/orders/edit"
        return httpx.Response(200, json=fixture("edit_order_failure.json"))

    broker, client_id = await resting_limit_order(extra)
    with pytest.raises(ProviderHTTPError, match="UNKNOWN_EDIT_ORDER_FAILURE_REASON"):
        await broker.edit_order(client_id, quantity=Decimal("0.02"), limit_price=Decimal("49000"))
    order = await broker.get_order(client_id)
    assert order.status is OrderStatus.OPEN and order.request.quantity == Decimal("0.01")
    await broker.close()


@pytest.mark.asyncio
async def test_successful_edit_refreshes_the_cached_request_terms() -> None:
    async def extra(req, path):
        assert path == "/orders/edit"
        return httpx.Response(200, json={"success": True, "errors": []})

    broker, client_id = await resting_limit_order(extra)
    order = await broker.edit_order(
        client_id, quantity=Decimal("0.02"), limit_price=Decimal("49000")
    )

    assert order.request.quantity == Decimal("0.02")
    assert order.request.limit_price == Decimal("49000")
    assert str(order.request.client_order_id) == client_id
    await broker.close()


@pytest.mark.asyncio
async def test_websocket_jwt_omits_the_rest_uri_claim() -> None:
    pem, public_key = ec_key_pair()

    class WebSocket:
        def __init__(self) -> None:
            self.messages: list[str] = []
            self.closed = False

        async def send(self, message: str) -> None:
            self.messages.append(message)

        async def close(self) -> None:
            self.closed = True

    websocket = WebSocket()

    async def connect(url: str):
        assert url.startswith("wss://")
        return websocket

    async def unused_handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected REST request: {request.method} {request.url}")

    broker = broker_for(unused_handler, api_key=KEY_NAME, private_key=pem, auth_token=None)
    stop = asyncio.Event()
    stop.set()
    events = broker.authenticated_order_events(stop=stop, connect=connect)
    with pytest.raises(StopAsyncIteration):
        await anext(events)

    message = json.loads(websocket.messages[0])
    claims = jwt.decode(
        message["jwt"], public_key, algorithms=["ES256"], options={"verify_aud": False}
    )
    header = jwt.get_unverified_header(message["jwt"])
    assert message["channel"] == "user"
    assert "uri" not in claims
    assert claims["sub"] == KEY_NAME and claims["iss"] == "cdp"
    assert header["kid"] == KEY_NAME and len(header["nonce"]) == 32
    assert websocket.closed is True
    await broker.close()


@pytest.mark.asyncio
async def test_rate_limits_back_off_boundedly_and_server_errors_open_the_circuit() -> None:
    statuses = [429, 200]

    async def handler(req: httpx.Request) -> httpx.Response:
        status = statuses.pop(0) if statuses else 500
        if status == 429:
            return httpx.Response(
                429, headers={"retry-after": "0"}, json=fixture("error_rate_limited.json")
            )
        if status == 500:
            return httpx.Response(500, json=fixture("error_internal.json"))
        return httpx.Response(200, json=accounts_page(["USD"], cursor="", has_next=False))

    broker = broker_for(handler)
    assert [item.asset for item in await broker.get_balances()] == ["USD"]
    statuses.extend([429, 429, 429])
    with pytest.raises(ProviderHTTPError) as limited:
        await broker.get_balances()
    assert limited.value.status_code == 429
    for _ in range(2):
        with pytest.raises(ProviderHTTPError):
            await broker.get_balances()
    # Three consecutive failures open the circuit, which the risk engine reads as unhealthy.
    assert broker.healthy is False
    await broker.close()


@pytest.mark.asyncio
async def test_unknown_venue_order_is_reported_as_missing() -> None:
    async def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/orders/historical/batch"):
            return httpx.Response(200, json={"orders": [], "has_next": False, "cursor": ""})
        return httpx.Response(404, json=fixture("error_not_found.json"))

    broker = broker_for(handler)
    assert await broker.get_order(str(uuid4())) is None
    broker._provider_order_ids["known"] = "22222222-2222-4222-8222-222222222222"
    assert await broker.get_order("known") is None
    await broker.close()


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("OPEN", OrderStatus.OPEN),
        ("QUEUED", OrderStatus.OPEN),
        ("CANCEL_QUEUED", OrderStatus.OPEN),
        ("EDIT_QUEUED", OrderStatus.OPEN),
        ("FILLED", OrderStatus.FILLED),
        ("CANCELLED", OrderStatus.CANCELED),
        ("EXPIRED", OrderStatus.CANCELED),
        ("FAILED", OrderStatus.REJECTED),
        ("PENDING", OrderStatus.PENDING_SUBMIT),
        ("UNKNOWN_ORDER_STATUS", OrderStatus.UNKNOWN),
    ],
)
def test_every_documented_order_status_maps_to_a_local_state(status, expected) -> None:
    assert _order_status({"status": status, "filled_size": "0"}, Decimal("1")) is expected
