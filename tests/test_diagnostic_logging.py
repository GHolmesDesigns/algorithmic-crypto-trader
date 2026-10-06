"""What the logs say about a reconciliation divergence, a provider failure, and an order (#117).

The behaviour protected here: on 2026-10-05 and 2026-10-06 the paper app halted three times and
the log said only ``ProviderHTTPError`` or "diverged". These tests pin that a divergence names its
field and delta (values too, but only in ``paper``), that a provider HTTP failure names its status,
endpoint path and reason and never its body, header, or credential, and that every order step is
logged once. None of it may change what is compared, when trading halts, or what is sent.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from itertools import count
from uuid import uuid4

import httpx
import pytest
from app.main import _may_log_divergence_values
from app.recovery import recover_on_startup
from app.trading import CycleStatus, TradingCycle
from brokers.gemini import GeminiBroker
from brokers.http import (
    ProviderHTTPClient,
    ProviderHTTPError,
    describe_provider_failure,
    endpoint_path,
)
from brokers.simulated import SimulatedBroker
from core.guards import CredentialScope, StartupSettings
from core.models import (
    Balance,
    Fill,
    KillSwitchState,
    Order,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    Signal,
    TradingMode,
    utc_now,
)
from core.resilience import TokenBucketRateLimiter
from execution.engine import ExecutionEngine, InMemoryOrderStore
from execution.persistence import SqlAlchemyOrderStore
from portfolio.divergence import field_changes, log_discrepancy
from portfolio.reconciliation import Discrepancy, PortfolioState, Reconciler
from portfolio.store import SqlAlchemyPortfolioStore
from risk.kill_switch import KillSwitch

from tests.test_phase1_gate_gemini_lifecycle import GeminiSandboxBook, gemini, request
from tests.test_startup_recovery import RecordingPortfolioStore, database

DIVERGENCE = "portfolio.reconciliation"
ENGINE = "execution.engine"
NOW = utc_now()


class FixedBroker(SimulatedBroker):
    """A venue that reports exactly the balances and positions it is given. It never writes."""

    def __init__(self, balances=(), positions=()) -> None:
        super().__init__()
        self.reported_balances = tuple(balances)
        self.reported_positions = tuple(positions)

    async def get_balances(self):
        return self.reported_balances

    async def get_positions(self):
        return self.reported_positions

    async def get_order(self, client_order_id: str):
        return None


def usd(available: str, hold: str = "0") -> Balance:
    return Balance(asset="USD", available=Decimal(available), hold=Decimal(hold), as_of=NOW)


def divergence_records(caplog) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name == DIVERGENCE]


# A divergence names its field and delta; paper adds both values, anything else adds none


@pytest.mark.asyncio
async def test_a_paper_divergence_logs_the_field_both_values_and_the_delta(caplog) -> None:
    caplog.set_level(logging.INFO)
    broker = FixedBroker(balances=(usd("31415.926535"),))
    local = PortfolioState(balances=(usd("31415.926533"),))
    switch = KillSwitch()

    result = await Reconciler(broker, switch, log_values=True).reconcile(local)

    [record] = divergence_records(caplog)
    assert record.levelno == logging.ERROR
    assert record.getMessage() == (
        "reconciliation divergence: balance USD field=available delta(broker-local)=+0.000002"
        " local=31415.926533 broker=31415.926535"
    )
    assert record.event == {
        "kind": "balance",
        "key": "USD",
        "field": "available",
        "delta": "+0.000002",
        "local": "31415.926533",
        "broker": "31415.926535",
    }
    # Logging explains the divergence and decides nothing: it still halts exactly as before.
    assert result.safety_tripped is True
    assert switch.state is KillSwitchState.HALTED


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", [{}, {"log_values": False}], ids=["by default", "switched off"])
async def test_a_divergence_outside_paper_logs_the_field_and_delta_but_never_a_balance(
    caplog, flag
) -> None:
    caplog.set_level(logging.DEBUG)
    broker = FixedBroker(balances=(usd("31415.926535"),))
    local = PortfolioState(balances=(usd("31415.926533"),))
    switch = KillSwitch()

    result = await Reconciler(broker, switch, **flag).reconcile(local)

    [record] = divergence_records(caplog)
    assert record.getMessage() == (
        "reconciliation divergence: balance USD field=available delta(broker-local)=+0.000002"
    )
    assert record.event == {
        "kind": "balance",
        "key": "USD",
        "field": "available",
        "delta": "+0.000002",
    }
    # Neither value, in any record at any level, from any logger.
    assert "31415" not in caplog.text
    assert result.safety_tripped is True and switch.state is KillSwitchState.HALTED


@pytest.mark.asyncio
async def test_every_differing_field_is_one_line_and_an_order_key_is_shortened(caplog) -> None:
    caplog.set_level(logging.INFO)
    order_request = OrderRequest(
        signal_id=uuid4(),
        strategy_version="v1",
        symbol="BTC-USD",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("1"),
        correlation_id=uuid4(),
    )
    order = Order(request=order_request, status=OrderStatus.PENDING_SUBMIT)
    client_order_id = str(order_request.client_order_id)
    # The venue holds more USD and less on hold, and has never heard of the saved order.
    broker = FixedBroker(balances=(usd("10.5", hold="1"),))
    local = PortfolioState(orders={client_order_id: order}, balances=(usd("10", hold="2"),))

    await Reconciler(broker, KillSwitch(), log_values=True).reconcile(local)

    lines = [record.getMessage() for record in divergence_records(caplog)]
    assert sorted(lines) == sorted(
        [
            "reconciliation divergence: balance USD field=available delta(broker-local)=+0.5"
            " local=10 broker=10.5",
            "reconciliation divergence: balance USD field=hold delta(broker-local)=-1"
            " local=2 broker=1",
            f"reconciliation divergence: order {client_order_id[:8]} field=presence"
            " delta(broker-local)=n/a local=pending_submit broker=absent",
        ]
    )
    assert client_order_id not in caplog.text


def test_two_fill_keys_that_share_a_prefix_are_logged_apart(caplog) -> None:
    # Gemini trade IDs count up from a shared prefix; their first eight characters are the same.
    caplog.set_level(logging.INFO)
    logger = logging.getLogger(DIVERGENCE)

    for tid in ("2840141812001", "2840141812002"):
        # The venue lists a fill the app never recorded.
        unrecorded = Fill(
            fill_id=tid,
            order_id=uuid4(),
            symbol="BTC-USD",
            side=OrderSide.SELL,
            quantity=Decimal("0.00005"),
            price=Decimal("84390.47"),
            fee=Decimal("0.01"),
            fee_asset="USD",
            occurred_at=NOW,
        )
        log_discrepancy(logger, Discrepancy("fill", tid, None, unrecorded), include_values=False)

    assert [record.event["key"] for record in divergence_records(caplog)] == [
        "41812001",
        "41812002",
    ]
    assert "2840141812001" not in caplog.text and "2840141812002" not in caplog.text


def test_each_kind_of_difference_is_named_by_its_own_field() -> None:
    held = Position(
        symbol="BTC-USD", quantity=Decimal("1"), average_price=Decimal("60000"), as_of=NOW
    )
    more = held.model_copy(update={"quantity": Decimal("1.25"), "average_price": Decimal("59000")})
    fill = Fill(
        fill_id="tid-1",
        order_id=uuid4(),
        symbol="BTC-USD",
        side=OrderSide.BUY,
        quantity=Decimal("0.5"),
        price=Decimal("100"),
        fee=Decimal("0.1"),
        fee_asset="USD",
        occurred_at=NOW,
    )

    def changes(discrepancy: Discrepancy) -> list[tuple]:
        return [
            (item.field, item.local, item.broker, item.delta) for item in field_changes(discrepancy)
        ]

    assert changes(Discrepancy("position", "BTC-USD", held, more)) == [
        ("quantity", "1", "1.25", Decimal("0.25")),
        ("average_price", "60000", "59000", Decimal("-1000")),
    ]
    assert changes(Discrepancy("order", "id", ("open", "0"), ("filled", "0.5"))) == [
        ("status", "open", "filled", None),
        ("filled_quantity", "0", "0.5", Decimal("0.5")),
    ]
    assert changes(Discrepancy("fill", "tid-1", fill, None)) == [
        ("presence", "quantity=0.5 price=100 fee=0.1", "absent", None)
    ]
    assert changes(Discrepancy("position", "ETH-USD", None, held)) == [
        ("presence", "absent", "quantity=1 average_price=60000", None)
    ]


@pytest.mark.parametrize("mode", list(TradingMode))
def test_only_paper_may_log_both_sides_of_a_divergence(mode) -> None:
    settings = StartupSettings(mode, CredentialScope.NONE, "", "sqlite://", "INFO")

    assert _may_log_divergence_values(settings) is (mode is TradingMode.PAPER)


@pytest.mark.asyncio
async def test_startup_recovery_passes_the_mode_decision_to_its_reconciler(
    tmp_path, caplog
) -> None:
    caplog.set_level(logging.INFO)
    engine, session_factory = database(tmp_path)
    broker = FixedBroker()
    stale = Position(symbol="BTC-USD", quantity=Decimal("7.5"), average_price=None, as_of=NOW)

    async def recover(*, log_values: bool) -> None:
        # A run adopts the broker's empty book, so each one starts from the stale snapshot again.
        SqlAlchemyPortfolioStore(session_factory).save_snapshot(
            PortfolioState(positions=(stale,)), source="broker"
        )
        await recover_on_startup(
            kill_switch=KillSwitch(),
            order_store=SqlAlchemyOrderStore(session_factory),
            portfolio_store=RecordingPortfolioStore(session_factory),
            broker=broker,
            log_values=log_values,
        )

    await recover(log_values=False)
    assert divergence_records(caplog)
    assert "7.5" not in caplog.text
    caplog.clear()
    await recover(log_values=True)
    assert "local=quantity=7.5" in caplog.text and "broker=absent" in caplog.text
    engine.dispose()


@pytest.mark.asyncio
async def test_a_failing_log_line_never_changes_whether_trading_halts(monkeypatch) -> None:
    def broken(*_args, **_kwargs) -> None:
        raise RuntimeError("the logger is broken")

    monkeypatch.setattr("portfolio.reconciliation.log_discrepancy", broken)
    switch = KillSwitch()

    result = await Reconciler(FixedBroker(balances=(usd("2"),)), switch).reconcile(
        PortfolioState(balances=(usd("1"),))
    )

    assert result.safety_tripped is True
    assert switch.state is KillSwitchState.HALTED


# A provider HTTP failure names its status, path, and reason, never its body or credentials

FAILURE_BODY = {"result": "error", "reason": "SystemError", "message": "internal token=LEAKED-123"}


def gemini_failing_with(status: int, *, on: str = "/v1/order/status", body=FAILURE_BODY):
    """A Gemini adapter whose ``on`` endpoint answers ``status``; every other call is a no-op."""

    async def handler(http_request: httpx.Request) -> httpx.Response:
        if http_request.url.path == on:
            return httpx.Response(status, json=body, request=http_request)
        return httpx.Response(404, json={"reason": "OrderNotFound"}, request=http_request)

    return GeminiBroker(
        api_key="sandbox-key",
        api_secret="sandbox-secret",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        rate_limiter=TokenBucketRateLimiter(10_000, 10_000),
    )


def test_an_endpoint_path_keeps_the_route_and_drops_host_query_and_identifiers() -> None:
    long_id = "3f2b1c4e-9a8d-4f6e-b1c2-0a9e8d7c6b5a"

    assert endpoint_path("https://api.sandbox.gemini.com/v1/order/status?token=abc") == (
        "/v1/order/status"
    )
    assert endpoint_path(f"https://api.coinbase.example/api/v3/brokerage/orders/{long_id}") == (
        "/api/v3/brokerage/orders/:id"
    )


def test_a_reason_is_bounded_and_scrubbed_and_the_rest_of_the_body_is_never_kept() -> None:
    error = ProviderHTTPError(
        500, "boom", payload={"reason": "Oops token=LEAKED-123 " + "x" * 300, "message": "body"}
    )

    assert error.reason is not None and len(error.reason) <= 80
    assert "LEAKED" not in error.reason
    assert ProviderHTTPError(500, "boom", payload="plain text").reason is None
    assert ProviderHTTPError(500, "boom", payload={"reason": 5}).reason is None
    assert describe_provider_failure(RuntimeError("secret")) == "RuntimeError"


@pytest.mark.asyncio
async def test_the_http_client_attaches_the_path_and_reason_but_not_the_body() -> None:
    async def handler(http_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json=FAILURE_BODY, request=http_request)

    client = ProviderHTTPClient(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    with pytest.raises(ProviderHTTPError) as caught:
        await client.request_json(
            "POST",
            "https://api.sandbox.gemini.com/v1/order/status?apikey=LEAKED",
            headers={"X-GEMINI-APIKEY": "LEAKED-KEY"},
        )

    assert describe_provider_failure(caught.value) == (
        "ProviderHTTPError HTTP 500 path=/v1/order/status reason=SystemError"
    )
    assert "LEAKED" not in describe_provider_failure(caught.value)


@pytest.mark.asyncio
async def test_the_presubmit_lookup_that_halted_the_app_now_logs_its_status_path_and_reason(
    caplog,
) -> None:
    caplog.set_level(logging.INFO)
    broker = gemini_failing_with(500)
    store = InMemoryOrderStore()
    order_request, approval = request()

    with pytest.raises(ProviderHTTPError) as caught:
        await ExecutionEngine(broker, store).submit(order_request, approval)

    detail = "ProviderHTTPError HTTP 500 path=/v1/order/status reason=SystemError"
    adapter = [r for r in caplog.records if r.name == "brokers.gemini"]
    assert [record.getMessage() for record in adapter] == [
        f"Gemini order status lookup failed: {detail}"
    ]
    [lookup] = [r for r in caplog.records if r.name == ENGINE and r.event["step"] == "lookup"]
    assert lookup.levelno == logging.WARNING
    assert lookup.event == {
        "step": "lookup",
        "ref": str(order_request.client_order_id)[:8],
        "result": "failed",
        "error": "ProviderHTTPError",
        "status_code": 500,
        "path": "/v1/order/status",
        "reason": "SystemError",
    }
    # The trading loop's own failure line carries the same detail it used to drop.
    signal = Signal(
        symbol="BTC-USD", side=OrderSide.BUY, quantity=Decimal("0.01"), strategy_version="v"
    )
    outcome = TradingCycle._broker_failure(caught.value, signal, approval)
    assert f"broker failure during submission: {detail}" in caplog.text
    assert (outcome.status, outcome.detail) == (
        CycleStatus.BROKER_ERROR,
        "broker failure: ProviderHTTPError",
    )
    # No body, header, or credential, and the order is still saved but never sent.
    for secret in ("LEAKED", "sandbox-key", "sandbox-secret", "X-GEMINI", "internal"):
        assert secret not in caplog.text
    assert store.get(str(order_request.client_order_id)).status is OrderStatus.PENDING_SUBMIT


@pytest.mark.asyncio
async def test_a_not_found_lookup_is_the_normal_answer_and_logs_no_failure(caplog) -> None:
    caplog.set_level(logging.DEBUG)

    found = await gemini_failing_with(404).get_order(str(uuid4()))

    assert found is None
    assert [r for r in caplog.records if r.name == "brokers.gemini"] == []


@pytest.mark.asyncio
async def test_a_rejected_order_logs_the_venues_reason_once_at_the_adapter_and_the_engine(
    caplog,
) -> None:
    caplog.set_level(logging.INFO)
    broker = gemini_failing_with(
        400,
        on="/v1/order/new",
        body={"result": "error", "reason": "InvalidQuantity", "message": "x"},
    )
    # A limit order needs no quote: the lookup answers 404, then the order is sent and refused.
    order_request, approval = request(order_type=OrderType.LIMIT, limit_price="60000")

    with pytest.raises(Exception, match="InvalidQuantity"):
        await ExecutionEngine(broker, InMemoryOrderStore()).submit(order_request, approval)

    detail = "ProviderHTTPError HTTP 400 path=/v1/order/new reason=InvalidQuantity"
    assert f"Gemini order/new failed: {detail}" in caplog.text
    [step] = [r for r in caplog.records if r.name == ENGINE and r.event["step"] == "order_new"]
    assert step.event["status"] == "rejected" and step.event["result"] == "failed"


# Each order step is logged once, with each fill's quantity, price, fee, and notional


@pytest.mark.asyncio
async def test_an_order_logs_saved_lookup_order_new_and_each_fill_once(caplog) -> None:
    caplog.set_level(logging.INFO)
    book = GeminiSandboxBook()
    book._tids = count(2840141812001)  # a trade ID as long as the Sandbox's
    broker = gemini(book)
    store = InMemoryOrderStore()
    engine = ExecutionEngine(broker, store)
    order_request, approval = request()
    client_order_id = str(order_request.client_order_id)

    order = await engine.submit(order_request, approval)
    # Reconciliation re-reads an open order every interval; its fills are not logged again.
    await engine.recover(client_order_id)
    await engine.recover(client_order_id)

    steps = [r.event for r in caplog.records if r.name == ENGINE]
    assert [step["step"] for step in steps] == ["saved", "lookup", "order_new", "fill"]
    assert {step["ref"] for step in steps} == {client_order_id[:8]}
    saved, lookup, order_new, fill = steps
    assert saved == {
        "step": "saved",
        "ref": client_order_id[:8],
        "symbol": "BTC-USD",
        "side": "buy",
        "quantity": "0.01",
        "status": "pending_submit",
    }
    assert (lookup["result"], order_new["result"], order_new["status"]) == (
        "not_found",
        "accepted",
        order.status.value,
    )
    [stored] = store.fills.values()
    assert fill["quantity"] == "0.01" and fill["price"] == "60010"
    assert fill["fee"] == format(stored.fee, "f")
    assert Decimal(fill["notional"]) == Decimal("0.01") * Decimal("60010")
    # A fill is shown by the end of its ID, where one trade ID differs from the next one's.
    assert stored.fill_id == "2840141812001"
    assert fill["fee_asset"] == stored.fee_asset and fill["fill"] == "41812001"
    line = next(
        r.getMessage() for r in caplog.records if r.name == ENGINE and "step=fill" in r.getMessage()
    )
    assert f"order step=fill ref={client_order_id[:8]} " in line and "notional=600.10" in line
    # The identifier itself never reaches the log, only its eight-character reference.
    assert client_order_id not in caplog.text
