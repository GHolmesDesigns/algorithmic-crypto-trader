from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from brokers.coinbase import CoinbaseBroker
from brokers.gemini import GeminiBroker
from brokers.http import AmbiguousSubmissionError, ProviderHTTPError
from core.models import Order, OrderRequest, OrderSide, OrderStatus, OrderType, RiskApproval
from core.resilience import TokenBucketRateLimiter
from execution.engine import ExecutionEngine, InMemoryOrderStore

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
        if request_.method == "GET" and request_.url.path.endswith("/orders/historical/fills"):
            assert request_.url.params["order_ids"] == order_id
            return httpx.Response(
                200,
                json={
                    "fills": [{"entry_id": "cb-fill-1", "size": "0.01", "price": "100"}],
                    "cursor": "",
                },
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
    provider_order_id = "123"

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
                },
                request=request_,
            )
        if payload["request"] == "/v1/order/status":
            # Documented shape: trades appear only when include_trades is requested.
            assert payload["include_trades"] is True
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
                    "trades": [
                        {"tid": 1001, "amount": "0.01", "price": "100", "fee_amount": "0.006"}
                    ],
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


def gemini_balance(currency: str, amount: str, available: str) -> dict[str, str]:
    """One row in Gemini's documented Get Available Balances shape (checked 2026-09-27)."""

    return {
        "type": "exchange",
        "currency": currency,
        "amount": amount,
        "available": available,
        "availableForWithdrawal": available,
        "_timestamp": "2024-03-16T00:00:00.000000Z",
    }


def gemini_with_balances(rows: list[dict[str, str]]) -> GeminiBroker:
    async def handler(request_: httpx.Request) -> httpx.Response:
        payload = json.loads(base64.b64decode(request_.headers["x-gemini-payload"]))
        assert payload["request"] == "/v1/balances"
        return httpx.Response(200, json=rows, request=request_)

    return GeminiBroker(
        api_key="sandbox-key",
        api_secret="sandbox-secret",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        rate_limiter=limiter(),
    )


# Gemini's documented example, plus a coin whose whole balance a resting sell reserves.
GEMINI_BALANCES = [
    gemini_balance("BTC", "5.0", "4.5"),
    gemini_balance("USD", "15000.00", "5000.00"),
    gemini_balance("ETH", "10.0", "10.0"),
    gemini_balance("SOL", "2", "0"),
]


@pytest.mark.asyncio
async def test_gemini_hold_is_the_total_less_what_is_available_to_trade() -> None:
    broker = gemini_with_balances(GEMINI_BALANCES)
    try:
        balances = await broker.get_balances()
    finally:
        await broker.close()

    assert [(item.asset, item.available, item.hold) for item in balances] == [
        ("BTC", Decimal("4.5"), Decimal("0.5")),
        ("ETH", Decimal("10.0"), Decimal("0")),
        ("SOL", Decimal("0"), Decimal("2")),
        ("USD", Decimal("5000.00"), Decimal("10000.00")),
    ]


@pytest.mark.asyncio
async def test_a_coin_held_by_a_resting_sell_still_counts_as_a_gemini_position() -> None:
    broker = gemini_with_balances(GEMINI_BALANCES)
    try:
        positions = await broker.get_positions()
    finally:
        await broker.close()

    assert {item.symbol: item.quantity for item in positions} == {
        "BTC-USD": Decimal("5.0"),
        "ETH-USD": Decimal("10.0"),
        "SOL-USD": Decimal("2"),
    }


@pytest.mark.asyncio
async def test_a_gemini_balance_row_without_its_total_is_refused() -> None:
    row = gemini_balance("BTC", "1", "1")
    del row["amount"]
    broker = gemini_with_balances([row])
    try:
        with pytest.raises(ProviderHTTPError) as error:
            await broker.get_balances()
        with pytest.raises(ProviderHTTPError):
            await broker.get_positions()
    finally:
        await broker.close()

    assert error.value.status_code == 502


@pytest.mark.asyncio
async def test_a_coin_held_by_a_resting_sell_still_counts_as_a_coinbase_position() -> None:
    def account(currency: str, available: str, hold: str) -> dict[str, object]:
        return {
            "currency": currency,
            "available_balance": {"value": available, "currency": currency},
            "hold": {"value": hold, "currency": currency},
        }

    async def handler(request_: httpx.Request) -> httpx.Response:
        assert request_.url.path.endswith("/accounts")
        accounts = [
            account("BTC", "0.3", "0.2"),
            account("ETH", "0", "1"),
            account("USD", "100", "50"),
        ]
        return httpx.Response(
            200,
            json={"accounts": accounts, "has_next": False, "cursor": "", "size": len(accounts)},
            request=request_,
        )

    broker = CoinbaseBroker(
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        auth_token="test-token",
        rate_limiter=limiter(),
    )
    try:
        balances = await broker.get_balances()
        positions = await broker.get_positions()
    finally:
        await broker.close()

    assert [(item.asset, item.available, item.hold) for item in balances] == [
        ("BTC", Decimal("0.3"), Decimal("0.2")),
        ("ETH", Decimal("0"), Decimal("1")),
        ("USD", Decimal("100"), Decimal("50")),
    ]
    assert {item.symbol: item.quantity for item in positions} == {
        "BTC-USD": Decimal("0.5"),
        "ETH-USD": Decimal("1"),
    }


@pytest.mark.asyncio
async def test_coinbase_timeout_is_unknown_and_second_submission_queries_before_retry() -> None:
    order_request = request()
    calls: list[str] = []

    async def handler(request_: httpx.Request) -> httpx.Response:
        calls.append(request_.url.path)
        if request_.method == "POST":
            raise httpx.ReadTimeout("ambiguous", request=request_)
        # Coinbase documents no lookup by client_order_id; the adapter searches List Orders.
        assert request_.url.path.endswith("/orders/historical/batch")
        assert request_.url.params["product_ids"] == "BTC-USD"
        return httpx.Response(
            200,
            json={
                "orders": [
                    {
                        "order_id": "22222222-2222-4222-8222-222222222222",
                        "client_order_id": str(order_request.client_order_id),
                        "product_id": "BTC-USD",
                        "status": "OPEN",
                        "filled_size": "0",
                    }
                ],
                "has_next": False,
                "cursor": "",
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
        assert calls[1].endswith("/orders/historical/batch")
        assert sum(path.endswith("/orders") for path in calls) == 1
    finally:
        await broker.close()


# --- #152: the Coinbase order search is bounded by time and fails closed --------------------------

SAVED_AT = datetime(2026, 10, 9, 14, 0, 0, tzinfo=UTC)


class SearchVenue:
    """A List Orders endpoint that serves scripted pages and records every request."""

    def __init__(self, pages=None, *, forever: bool = False, orders_for=None) -> None:
        self.pages = list(pages or [])
        self.forever = forever
        self.searches: list[dict[str, str]] = []
        self.posts = 0
        self.orders_for = orders_for

    async def __call__(self, request_: httpx.Request) -> httpx.Response:
        if request_.method == "POST":
            self.posts += 1
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "success_response": {
                        "order_id": "44444444-4444-4444-8444-444444444444",
                        "client_order_id": self.orders_for,
                        "product_id": "BTC-USD",
                    },
                },
                request=request_,
            )
        if request_.url.path.endswith("/orders/historical/batch"):
            self.searches.append(dict(request_.url.params))
            if self.forever:
                page = {"orders": [], "has_next": True, "cursor": f"c{len(self.searches)}"}
            else:
                page = self.pages.pop(0) if self.pages else {"orders": [], "has_next": False}
            return httpx.Response(200, json=page, request=request_)
        return httpx.Response(
            200,
            json={"order": {"order_id": "44444444-4444-4444-8444-444444444444", "status": "OPEN"}},
            request=request_,
        )


def search_broker(venue: SearchVenue) -> CoinbaseBroker:
    return CoinbaseBroker(
        client=httpx.AsyncClient(transport=httpx.MockTransport(venue)),
        auth_token="test-token",
        rate_limiter=limiter(),
    )


def saved(order_request: OrderRequest, created_at: datetime = SAVED_AT):
    stored = Order(
        order_id=order_request.client_order_id, request=order_request, created_at=created_at
    )
    return lambda client_order_id: (
        stored if client_order_id == str(order_request.client_order_id) else None
    )


def listed(order_request: OrderRequest, *, has_next: bool = False, cursor: str = "") -> dict:
    return {
        "orders": [
            {
                "order_id": "22222222-2222-4222-8222-222222222222",
                "client_order_id": str(order_request.client_order_id),
                "product_id": "BTC-USD",
                "status": "OPEN",
                "filled_size": "0",
            }
        ],
        "has_next": has_next,
        "cursor": cursor,
    }


@pytest.mark.asyncio
async def test_a_new_orders_lookup_is_one_bounded_request() -> None:
    order_request = request()
    venue = SearchVenue()  # the venue knows no such order
    broker = search_broker(venue)
    broker.use_saved_orders(saved(order_request, datetime.now(UTC)))
    try:
        assert await broker.get_order(str(order_request.client_order_id)) is None
    finally:
        await broker.close()

    [search] = venue.searches
    assert search["product_ids"] == "BTC-USD"
    # Starts five minutes before the order was saved, as an RFC 3339 time with no fraction.
    assert search["start_date"].endswith("Z") and "." not in search["start_date"]
    start = datetime.strptime(search["start_date"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    assert (
        timedelta(minutes=4, seconds=50)
        < datetime.now(UTC) - start
        < timedelta(minutes=5, seconds=10)
    )


@pytest.mark.asyncio
async def test_the_search_finds_an_order_on_a_later_page_with_the_same_bound() -> None:
    order_request = request()
    venue = SearchVenue(
        [
            {"orders": [], "has_next": True, "cursor": "p2"},
            {"orders": [], "has_next": True, "cursor": "p3"},
            listed(order_request),
        ]
    )
    broker = search_broker(venue)
    broker.use_saved_orders(saved(order_request))
    try:
        found = await broker.get_order(str(order_request.client_order_id))
    finally:
        await broker.close()

    assert found is not None and found.status is OrderStatus.OPEN
    assert [item.get("cursor") for item in venue.searches] == [None, "p2", "p3"]
    assert {item["start_date"] for item in venue.searches} == {"2026-10-09T13:55:00Z"}


@pytest.mark.asyncio
async def test_after_a_restart_the_search_starts_at_the_orders_saved_time_and_product() -> None:
    order_request = request()
    three_days_ago = SAVED_AT - timedelta(days=3)
    venue = SearchVenue([listed(order_request)])
    broker = search_broker(venue)  # a new process: the adapter remembers no request
    broker.use_saved_orders(saved(order_request, three_days_ago))
    try:
        found = await broker.get_order(str(order_request.client_order_id))
    finally:
        await broker.close()

    assert found is not None
    [search] = venue.searches
    assert search["start_date"] == "2026-10-06T13:55:00Z"
    assert search["product_ids"] == "BTC-USD"


@pytest.mark.asyncio
async def test_a_saved_time_without_a_zone_is_read_as_utc() -> None:
    order_request = request()
    venue = SearchVenue()
    broker = search_broker(venue)
    broker.use_saved_orders(
        saved(order_request, SAVED_AT.replace(tzinfo=None))
    )  # as SQLite returns
    try:
        await broker.get_order(str(order_request.client_order_id))
    finally:
        await broker.close()

    assert venue.searches[0]["start_date"] == "2026-10-09T13:55:00Z"


@pytest.mark.asyncio
async def test_without_a_saved_order_the_search_is_unbounded_but_cannot_end_open() -> None:
    order_request = request()
    venue = SearchVenue(forever=True)
    broker = search_broker(venue)
    try:
        with pytest.raises(ProviderHTTPError) as error:
            await broker.get_order(str(order_request.client_order_id))
    finally:
        await broker.close()

    assert error.value.status_code == 502
    assert len(venue.searches) == 10
    assert all("start_date" not in item and "product_ids" not in item for item in venue.searches)


@pytest.mark.asyncio
async def test_a_search_the_page_budget_cut_short_raises_even_with_a_time_bound() -> None:
    order_request = request()
    venue = SearchVenue(forever=True)
    broker = search_broker(venue)
    broker.use_saved_orders(saved(order_request))
    try:
        with pytest.raises(ProviderHTTPError, match="did not reach the end"):
            await broker.get_order(str(order_request.client_order_id))
    finally:
        await broker.close()

    assert len(venue.searches) == 10


@pytest.mark.asyncio
async def test_more_pages_reported_without_a_cursor_is_not_an_empty_result() -> None:
    order_request = request()
    venue = SearchVenue([{"orders": [], "has_next": True, "cursor": ""}])
    broker = search_broker(venue)
    broker.use_saved_orders(saved(order_request))
    try:
        with pytest.raises(ProviderHTTPError, match="no cursor"):
            await broker.get_order(str(order_request.client_order_id))
    finally:
        await broker.close()


@pytest.mark.asyncio
async def test_an_unreadable_saved_order_store_never_narrows_the_search() -> None:
    order_request = request()
    venue = SearchVenue([listed(order_request)])
    broker = search_broker(venue)

    def broken(_client_order_id):
        raise RuntimeError("store is down")

    broker.use_saved_orders(broken)
    try:
        found = await broker.get_order(str(order_request.client_order_id))
    finally:
        await broker.close()

    assert found is not None
    assert "start_date" not in venue.searches[0] and "product_ids" not in venue.searches[0]


@pytest.mark.asyncio
async def test_the_engine_bounds_a_new_orders_pre_submit_search_and_then_submits() -> None:
    order_request = request()
    venue = SearchVenue(orders_for=str(order_request.client_order_id))
    broker = search_broker(venue)
    store = InMemoryOrderStore()
    engine = ExecutionEngine(broker, store)  # registers the store with the adapter
    try:
        order = await engine.submit(order_request, approval(order_request))
    finally:
        await broker.close()

    assert order.status in {OrderStatus.OPEN, OrderStatus.UNKNOWN, OrderStatus.FILLED}
    assert venue.posts == 1
    [search] = venue.searches
    assert search["product_ids"] == "BTC-USD" and "start_date" in search


@pytest.mark.asyncio
async def test_the_engine_submits_nothing_when_the_search_cannot_rule_the_order_out() -> None:
    order_request = request()
    venue = SearchVenue(forever=True)
    broker = search_broker(venue)
    store = InMemoryOrderStore()
    engine = ExecutionEngine(broker, store)
    try:
        with pytest.raises(ProviderHTTPError):
            await engine.submit(order_request, approval(order_request))
    finally:
        await broker.close()

    assert venue.posts == 0
    # The new order is closed as never sent (#143), so nothing is left pending.
    assert store.pending() == () and len(store.closures) == 1
