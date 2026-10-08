"""Closing an order the venue never received (#119).

The behaviour protected here: an administrator can end a ``pending_submit`` or ``unknown`` order
only after the app has asked the venue again and the venue has answered "no such order" with no
fills; the close is audited, never resubmits or creates anything, and leaves the kill switch to
the owner's re-arm. A close that is refused, or whose record cannot be saved, changes nothing.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from api.alerts import Alert, AlertRouter
from api.controls import REASON_LIMIT, parse_close_order
from api.dashboard import build_dashboard
from app.trading import CycleStatus
from brokers.gemini import GeminiBroker
from brokers.http import ProviderHTTPError, ProviderTimeoutError
from brokers.simulated import FaultPlan, SimulatedBroker, SimulatedFault
from core.models import (
    Fill,
    KillSwitchState,
    Order,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    Quote,
    RiskApproval,
    Signal,
    utc_now,
)
from core.resilience import TokenBucketRateLimiter
from db.models import OrderRecord, SystemEventRecord
from execution.audit import (
    NEVER_RECEIVED,
    ORDER_CLOSED_EVENT,
    InMemoryAuditStore,
    SqlAlchemyAuditStore,
)
from execution.closure import ClosureCode, OrderCloser
from execution.engine import ExecutionEngine, InMemoryOrderStore, PersistenceUnavailable
from execution.persistence import SqlAlchemyOrderStore
from portfolio.reconciliation import PortfolioState, Reconciler
from portfolio.scheduler import ScheduledReconciler
from risk.kill_switch import KillSwitch
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from strategy.reference import MovingAverageCrossStrategy

from tests.gate_support import PARITY_WINDOWS, paper_cycle, sqlite_database, state_at
from tests.operator_support import history_app

OPERATOR_TOKEN = "operator-secret-close-6d2f"
ADMIN_TOKEN = "admin-secret-close-0a8e"
OPERATOR = {"x-operator-token": OPERATOR_TOKEN}
ADMIN = {"x-operator-token": ADMIN_TOKEN}
BROWSER = {"accept": "text/html,application/xhtml+xml"}
REASON = "INC-119: the 11:20 buy never reached the venue; approved by the owner"


@pytest.fixture(autouse=True)
def tokens(monkeypatch, tmp_path):
    monkeypatch.setenv("OPERATOR_TOKEN", OPERATOR_TOKEN)
    monkeypatch.setenv("OPERATOR_ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("KILL_SWITCH_FILE", str(tmp_path / "kill-switch.json"))


class Venue(SimulatedBroker):
    """A venue whose two lookups are scripted. It records every lookup and every write."""

    def __init__(self, *, order=None, fills=(), order_error=None, fills_error=None) -> None:
        super().__init__()
        self.order = order
        self.fills = tuple(fills)
        self.order_error = order_error
        self.fills_error = fills_error
        self.during_fills = None
        self.lookups: list[str] = []
        self.writes: list[str] = []

    async def get_order(self, client_order_id: str):
        self.lookups.append("get_order")
        if self.order_error is not None:
            raise self.order_error
        return self.order

    async def get_fills(self, client_order_id: str):
        self.lookups.append("get_fills")
        if self.during_fills is not None:
            self.during_fills()
        if self.fills_error is not None:
            raise self.fills_error
        return self.fills

    async def submit_order(self, request, approval):
        self.writes.append("submit_order")
        return await super().submit_order(request, approval)


class RecordingSink:
    def __init__(self) -> None:
        self.alerts: list[Alert] = []

    async def send(self, alert: Alert) -> None:
        self.alerts.append(alert)


def seed_order(factory, *, status=OrderStatus.PENDING_SUBMIT, side=OrderSide.BUY):
    """A saved order with its signal and approved risk decision, as the trading loop leaves it."""

    audit = SqlAlchemyAuditStore(factory)
    store = SqlAlchemyOrderStore(factory)
    signal = Signal(
        symbol="BTC-USD", side=side, quantity=Decimal("0.0001"), strategy_version="ma-v1"
    )
    approval = RiskApproval(
        signal_id=signal.signal_id,
        approved=True,
        reason="all gates passed",
        correlation_id=signal.correlation_id,
    )
    audit.record_signal(signal)
    audit.record_risk_decision(approval)
    request = OrderRequest(
        signal_id=signal.signal_id,
        strategy_version=signal.strategy_version,
        symbol=signal.symbol,
        side=side,
        order_type=OrderType.MARKET,
        quantity=signal.quantity,
        correlation_id=signal.correlation_id,
    )
    order = store.reserve(request, approval)
    if status is not OrderStatus.PENDING_SUBMIT:
        order = order.model_copy(update={"status": status})
        store.update(order)
    return order, approval, request


def stored(factory, order: Order) -> OrderRecord:
    with factory() as session:
        return session.scalars(
            select(OrderRecord).where(OrderRecord.client_order_id == order.order_id)
        ).one()


def closure_events(factory) -> list[SystemEventRecord]:
    with factory() as session:
        return list(
            session.scalars(
                select(SystemEventRecord).where(SystemEventRecord.event_type == ORDER_CLOSED_EVENT)
            )
        )


def venue_order(request: OrderRequest, status=OrderStatus.OPEN) -> Order:
    return Order(order_id=uuid4(), request=request, status=status)


def a_fill(order: Order) -> Fill:
    return Fill(
        fill_id="venue-fill-1",
        order_id=order.order_id,
        symbol="BTC-USD",
        side=OrderSide.BUY,
        quantity=Decimal("0.0001"),
        price=Decimal("64000"),
        fee=Decimal("0.01"),
        fee_asset="USD",
        occurred_at=utc_now(),
    )


# The close service, on a real SQLite order store


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [OrderStatus.PENDING_SUBMIT, OrderStatus.UNKNOWN])
async def test_an_order_the_venue_never_received_is_closed_and_audited(tmp_path, status):
    _, factory = sqlite_database(tmp_path)
    order, approval, request = seed_order(factory, status=status)
    venue = Venue()
    now = datetime(2026, 10, 6, 13, 40, tzinfo=UTC)
    closer = OrderCloser(venue, SqlAlchemyOrderStore(factory), clock=lambda: now)

    outcome = await closer.close(str(order.order_id), actor="admin", reason=REASON)

    assert outcome.closed and outcome.code is ClosureCode.CLOSED
    assert venue.lookups == ["get_order", "get_fills"]  # asked again, order first, then fills
    assert venue.writes == []  # nothing was submitted, cancelled, or edited at the venue
    row = stored(factory, order)
    assert row.status == "canceled"
    # The order keeps every link the audit trail relies on.
    assert (row.signal_id, row.strategy_version, row.risk_approval_id) == (
        request.signal_id,
        "ma-v1",
        approval.approval_id,
    )
    assert row.correlation_id == request.correlation_id
    assert SqlAlchemyOrderStore(factory).pending() == ()
    [event] = closure_events(factory)
    assert event.correlation_id == request.correlation_id
    assert dict(event.payload) == {
        "client_order_id": str(order.order_id),
        "order_id": str(order.order_id),
        "signal_id": str(request.signal_id),
        "strategy_version": "ma-v1",
        "risk_approval_id": str(approval.approval_id),
        "symbol": "BTC-USD",
        "side": "buy",
        "quantity": "0.0001",
        "previous_status": status.value,
        "new_status": "canceled",
        "actor": "admin",
        "reason": REASON,
        "closed_at": now.isoformat(),
        "broker_lookup": "not found (the venue has no record of this order)",
        "broker_fills": 0,
        "outcome": NEVER_RECEIVED,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("venue", "code", "lookup"),
    [
        pytest.param(
            "has_order", ClosureCode.VENUE_HAS_ORDER, "found (open)", id="the venue has the order"
        ),
        pytest.param(
            "has_fill",
            ClosureCode.VENUE_HAS_FILLS,
            "not found, but 1 fill(s) reported",
            id="no order, but a fill",
        ),
        pytest.param(
            ProviderHTTPError(500, "boom"),
            ClosureCode.LOOKUP_FAILED,
            "failed (HTTP 500)",
            id="order lookup 500",
        ),
        pytest.param(
            ProviderHTTPError(503, "down"),
            ClosureCode.LOOKUP_FAILED,
            "failed (HTTP 503)",
            id="order lookup 503",
        ),
        pytest.param(
            ProviderHTTPError(401, "no"),
            ClosureCode.LOOKUP_FAILED,
            "failed (HTTP 401)",
            id="auth error",
        ),
        pytest.param(
            ProviderTimeoutError("slow https://venue.example/?key=SECRET"),
            ClosureCode.LOOKUP_FAILED,
            "failed (ProviderTimeoutError)",
            id="timeout, message never copied",
        ),
        pytest.param(
            "fills_error",
            ClosureCode.LOOKUP_FAILED,
            "failed (HTTP 500)",
            id="fills lookup 500 after not-found",
        ),
    ],
)
async def test_any_sign_the_venue_has_the_order_or_cannot_say_refuses_and_changes_nothing(
    tmp_path, venue, code, lookup
):
    _, factory = sqlite_database(tmp_path)
    order, _, request = seed_order(factory)
    if venue == "has_order":
        scripted = Venue(order=venue_order(request))
    elif venue == "has_fill":
        scripted = Venue(fills=(a_fill(order),))
    elif venue == "fills_error":
        scripted = Venue(fills_error=ProviderHTTPError(500, "boom"))
    else:
        scripted = Venue(order_error=venue)

    outcome = await OrderCloser(scripted, SqlAlchemyOrderStore(factory)).close(
        str(order.order_id), actor="admin", reason=REASON
    )

    assert not outcome.closed and outcome.code is code
    assert outcome.broker_lookup == lookup
    assert "SECRET" not in outcome.detail and "venue.example" not in outcome.detail
    assert stored(factory, order).status == "pending_submit"  # unchanged
    assert closure_events(factory) == []
    assert scripted.writes == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.OPEN])
async def test_an_order_that_is_not_pending_or_unknown_is_not_closed_and_the_venue_is_not_asked(
    tmp_path, status
):
    _, factory = sqlite_database(tmp_path)
    order, _, _ = seed_order(factory, status=status)
    venue = Venue()

    outcome = await OrderCloser(venue, SqlAlchemyOrderStore(factory)).close(
        str(order.order_id), actor="admin", reason=REASON
    )

    assert outcome.code is ClosureCode.NOT_UNRESOLVED and venue.lookups == []
    assert stored(factory, order).status == status.value
    assert closure_events(factory) == []


@pytest.mark.asyncio
async def test_a_missing_or_malformed_order_id_is_refused_without_asking_the_venue(tmp_path):
    _, factory = sqlite_database(tmp_path)
    venue = Venue()
    closer = OrderCloser(venue, SqlAlchemyOrderStore(factory))

    assert (await closer.close("not-a-uuid", actor="admin", reason=REASON)).code is (
        ClosureCode.INVALID_ID
    )
    assert (await closer.close(str(uuid4()), actor="admin", reason=REASON)).code is (
        ClosureCode.ORDER_NOT_FOUND
    )
    assert venue.lookups == []


@pytest.mark.asyncio
async def test_an_order_resolved_while_the_venue_was_being_asked_is_not_overwritten(tmp_path):
    _, factory = sqlite_database(tmp_path)
    order, _, _ = seed_order(factory)
    store = SqlAlchemyOrderStore(factory)
    venue = Venue()
    # Something else settles the order between the venue's answer and the close.
    venue.during_fills = lambda: store.update(
        order.model_copy(update={"status": OrderStatus.FILLED})
    )

    outcome = await OrderCloser(venue, store).close(
        str(order.order_id), actor="admin", reason=REASON
    )

    assert outcome.code is ClosureCode.NOT_UNRESOLVED and not outcome.closed
    assert stored(factory, order).status == "filled"  # not turned into canceled
    assert closure_events(factory) == []


@pytest.mark.asyncio
async def test_a_close_whose_audit_event_cannot_be_saved_does_not_happen(tmp_path):
    engine, factory = sqlite_database(tmp_path)
    order, _, _ = seed_order(factory)
    SystemEventRecord.__table__.drop(engine)  # the audit write will fail

    outcome = await OrderCloser(Venue(), SqlAlchemyOrderStore(factory)).close(
        str(order.order_id), actor="admin", reason=REASON
    )

    assert outcome.code is ClosureCode.NOT_RECORDED and not outcome.closed
    assert stored(factory, order).status == "pending_submit"  # the status change rolled back


@pytest.mark.asyncio
async def test_a_closed_order_is_never_resubmitted_by_the_engine(tmp_path):
    _, factory = sqlite_database(tmp_path)
    order, approval, request = seed_order(factory)
    store = SqlAlchemyOrderStore(factory)
    venue = Venue()
    assert (
        await OrderCloser(venue, store).close(str(order.order_id), actor="admin", reason=REASON)
    ).closed

    # The same signal arrives again, as it would after a restart. The order is settled, so the
    # engine returns it and sends nothing.
    again = await ExecutionEngine(venue, store).submit(request, approval)

    assert again.status is OrderStatus.CANCELED
    assert venue.writes == [] and venue.lookups == ["get_order", "get_fills"]


@pytest.mark.asyncio
async def test_new_order_lookup_failure_is_audited_as_system_close_and_visible_in_history(tmp_path):
    venue = Venue(order_error=TimeoutError("status lookup timed out"))
    application, factory = closing_app(tmp_path, venue)
    signal = Signal(
        symbol="BTC-USD", side=OrderSide.BUY, quantity=Decimal("0.0001"), strategy_version="ma-v1"
    )
    approval = RiskApproval(
        signal_id=signal.signal_id,
        approved=True,
        reason="all gates passed",
        correlation_id=signal.correlation_id,
    )
    audit = SqlAlchemyAuditStore(factory)
    audit.record_signal(signal)
    audit.record_risk_decision(approval)
    request = OrderRequest(
        signal_id=signal.signal_id,
        strategy_version=signal.strategy_version,
        symbol=signal.symbol,
        side=signal.side,
        order_type=OrderType.MARKET,
        quantity=signal.quantity,
        correlation_id=signal.correlation_id,
    )
    store = SqlAlchemyOrderStore(factory)
    engine = ExecutionEngine(venue, store)
    application.state.execution = engine

    with pytest.raises(TimeoutError):
        await engine.submit(request, approval)

    order = store.get(str(request.client_order_id))
    assert order is not None and order.status is OrderStatus.CANCELED
    assert store.pending() == () and venue.writes == []
    order_record = stored(factory, order)
    assert (
        order_record.signal_id,
        order_record.strategy_version,
        order_record.risk_approval_id,
    ) == (
        signal.signal_id,
        signal.strategy_version,
        approval.approval_id,
    )
    [event] = closure_events(factory)
    assert event.payload["actor"] == "system"
    assert "pre-submit" in event.payload["reason"].lower()
    assert event.payload["outcome"] == NEVER_RECEIVED
    assert event.created_at.replace(tzinfo=UTC).isoformat() == event.payload["closed_at"]

    orders = await get_page(application, "/operator/history/orders")
    mine = row_for(orders, order)
    assert "Closed by the system: never received" in mine
    assert "The system" in mine
    assert "pre-submit" in mine.lower()
    assert "TimeoutError" in mine
    events = await get_page(
        application, f"/operator/history/events?event_type={ORDER_CLOSED_EVENT}"
    )
    assert ORDER_CLOSED_EVENT in events and "The system" in events and NEVER_RECEIVED in events


@pytest.mark.asyncio
async def test_the_in_memory_store_closes_only_what_is_still_unresolved():
    store = InMemoryOrderStore()
    signal = Signal(
        symbol="BTC-USD", side=OrderSide.BUY, quantity=Decimal("1"), strategy_version="v"
    )
    approval = RiskApproval(
        signal_id=signal.signal_id, approved=True, reason="ok", correlation_id=signal.correlation_id
    )
    request = OrderRequest(
        signal_id=signal.signal_id,
        strategy_version="v",
        symbol="BTC-USD",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("1"),
        correlation_id=signal.correlation_id,
    )
    order = store.reserve(request, approval)
    key = str(order.order_id)

    assert store.close_unreceived(key, event_type="e", payload={"k": 1}, at=utc_now())
    assert store.orders[key].status is OrderStatus.CANCELED and store.closures == [("e", {"k": 1})]
    assert not store.close_unreceived(key, event_type="e", payload={}, at=utc_now())
    assert not store.close_unreceived(str(uuid4()), event_type="e", payload={}, at=utc_now())
    store.fail_writes = True
    with pytest.raises(PersistenceUnavailable):
        store.close_unreceived(key, event_type="e", payload={}, at=utc_now())


@pytest.mark.asyncio
async def test_an_unreadable_order_store_refuses_before_the_venue_is_asked(tmp_path):
    class Unreadable(InMemoryOrderStore):
        def get(self, client_order_id):
            raise PersistenceUnavailable("the database is down")

    venue = Venue()

    outcome = await OrderCloser(venue, Unreadable()).close(
        str(uuid4()), actor="admin", reason=REASON
    )

    assert outcome.code is ClosureCode.NOT_RECORDED and venue.lookups == []


def test_the_sql_store_closes_nothing_for_an_order_it_does_not_hold(tmp_path):
    _, factory = sqlite_database(tmp_path)

    closed = SqlAlchemyOrderStore(factory).close_unreceived(
        str(uuid4()), event_type=ORDER_CLOSED_EVENT, payload={}, at=utc_now()
    )

    assert closed is False and closure_events(factory) == []


# What "the venue has no record" means for a real adapter: only a 404


def gemini_with(status_code: int):
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(status_code, json={"result": "error", "reason": "x", "message": "x"})

    broker = GeminiBroker(
        api_key="sandbox-key",
        api_secret="sandbox-secret",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        rate_limiter=TokenBucketRateLimiter(10_000, 10_000),
    )
    return broker, paths


@pytest.mark.asyncio
async def test_a_gemini_404_on_the_status_lookup_is_the_answer_that_allows_a_close(tmp_path):
    _, factory = sqlite_database(tmp_path)
    order, _, _ = seed_order(factory)
    broker, paths = gemini_with(404)

    outcome = await OrderCloser(broker, SqlAlchemyOrderStore(factory)).close(
        str(order.order_id), actor="admin", reason=REASON
    )
    await broker.close()

    assert outcome.closed and stored(factory, order).status == "canceled"
    # Only order status was read: no order/new, no order/cancel.
    assert set(paths) == {"/v1/order/status"}


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [500, 502, 503, 401, 403])
async def test_a_gemini_server_or_auth_error_is_not_evidence_the_order_is_absent(
    tmp_path, status_code
):
    # The incident: a 5xx on the pre-submit lookup is exactly why the order was never sent, so
    # a 5xx now must not be read as "the venue has no record".
    _, factory = sqlite_database(tmp_path)
    order, _, _ = seed_order(factory)
    broker, paths = gemini_with(status_code)

    outcome = await OrderCloser(broker, SqlAlchemyOrderStore(factory)).close(
        str(order.order_id), actor="admin", reason=REASON
    )
    await broker.close()

    assert outcome.code is ClosureCode.LOOKUP_FAILED and not outcome.closed
    assert stored(factory, order).status == "pending_submit"
    assert set(paths) == {"/v1/order/status"}


# The route: roles, request validation, audit, alerts, the lock, and the kill switch


def closing_app(tmp_path, venue, **options):
    application = history_app(tmp_path, **options)
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'trader.db'}", future=True)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    application.state.execution = ExecutionEngine(venue, SqlAlchemyOrderStore(factory))
    application.state.trading_lock = asyncio.Lock()
    application.state.kill_switch.trip("test: the loop halted for the unresolved order")
    return application, factory


def client_for(application) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test")


async def close_via_post(application, order, *, headers=ADMIN, reason=REASON, **extra):
    async with client_for(application) as client:
        return await client.post(
            "/operator/orders/close",
            headers=headers,
            json={"client_order_id": str(order.order_id), "reason": reason},
            **extra,
        )


@pytest.mark.asyncio
async def test_an_administrator_closes_the_order_and_the_kill_switch_stays_halted(tmp_path):
    sink = RecordingSink()
    venue = Venue()
    application, factory = closing_app(tmp_path, venue, alert_router=AlertRouter(phone_push=sink))
    order, _, _ = seed_order(factory)

    response = await close_via_post(application, order)

    assert response.status_code == 200
    assert response.json() == {
        "closed": True,
        "client_order_id": str(order.order_id),
        "kill_switch": "halted",
        "code": "closed",
        "detail": response.json()["detail"],
        "broker_lookup": "not found (the venue has no record of this order)",
    }
    assert stored(factory, order).status == "canceled"
    assert SqlAlchemyOrderStore(factory).pending() == ()
    assert application.state.kill_switch.state is KillSwitchState.HALTED  # only re-arm changes it
    assert venue.writes == []
    [event] = closure_events(factory)
    assert event.payload["actor"] == "admin" and event.payload["reason"] == REASON
    [alert] = sink.alerts
    assert alert.condition == "order_closed_never_received"
    assert str(order.order_id)[:8] in alert.message and "not resubmitted" in alert.message


@pytest.mark.asyncio
async def test_a_browser_form_ends_on_a_result_page_that_says_what_changed(tmp_path):
    application, factory = closing_app(tmp_path, Venue())
    order, _, _ = seed_order(factory)

    async with client_for(application) as client:
        response = await client.post(
            "/operator/orders/close",
            headers={**ADMIN, **BROWSER},
            data={"client_order_id": str(order.order_id), "reason": REASON},
        )

    html = response.text
    assert response.status_code == 200
    assert '<h1 id="result-heading">Order closed</h1>' in html
    assert "Yes: pending_submit to canceled" in html and "never received by the venue" in html
    assert "buy 0.0001 BTC-USD" in html and "0.000100000000000000" not in html
    assert "not found (the venue has no record of this order)" in html
    assert "unchanged" in html  # the kill switch line
    assert "Closing does not re-arm" in html
    assert "<script" not in html


@pytest.mark.asyncio
async def test_a_refused_browser_attempt_says_nothing_was_saved_and_keeps_what_was_typed(tmp_path):
    application, factory = closing_app(tmp_path, Venue(order_error=ProviderHTTPError(500, "boom")))
    order, _, _ = seed_order(factory)

    async with client_for(application) as client:
        response = await client.post(
            "/operator/orders/close",
            headers={**ADMIN, **BROWSER},
            data={"client_order_id": str(order.order_id), "reason": REASON},
        )

    html = response.text
    assert response.status_code == 502
    assert '<h1 id="result-heading">Close refused</h1>' in html
    assert "failed (HTTP 500)" in html and "No: nothing was changed" in html
    # The reason was not saved, so the page must not say it was.
    assert "Reason entered (not saved)" in html and "Reason recorded" not in html
    assert stored(factory, order).status == "pending_submit"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("venue", "status_code", "condition"),
    [
        pytest.param("has_order", 409, "order_close_refused", id="venue has the order"),
        pytest.param("has_fill", 409, "order_close_refused", id="venue reports a fill"),
        pytest.param(
            ProviderHTTPError(500, "boom"), 502, "order_close_refused", id="lookup fails with 500"
        ),
    ],
)
async def test_a_refused_attempt_changes_nothing_says_why_and_alerts(
    tmp_path, venue, status_code, condition
):
    sink = RecordingSink()
    order_holder: dict[str, Order] = {}

    class Scripted(Venue):
        async def get_order(self, client_order_id):
            if venue == "has_order":
                return venue_order(order_holder["request"])
            self.lookups.append("get_order")
            if isinstance(venue, Exception):
                raise venue
            return None

        async def get_fills(self, client_order_id):
            self.lookups.append("get_fills")
            return (a_fill(order_holder["order"]),) if venue == "has_fill" else ()

    scripted = Scripted()
    application, factory = closing_app(
        tmp_path, scripted, alert_router=AlertRouter(phone_push=sink)
    )
    order, _, request = seed_order(factory)
    order_holder.update(order=order, request=request)

    response = await close_via_post(application, order)

    assert response.status_code == status_code
    body = response.json()
    assert body["closed"] is False and body["detail"] and body["kill_switch"] == "halted"
    assert stored(factory, order).status == "pending_submit"
    assert closure_events(factory) == []
    assert scripted.writes == []
    [alert] = sink.alerts
    assert alert.condition == condition and "nothing changed" in alert.message


@pytest.mark.asyncio
async def test_only_an_administrator_can_close_and_a_refused_caller_changes_and_asks_nothing(
    tmp_path,
):
    venue = Venue()
    application, factory = closing_app(tmp_path, venue)
    order, _, _ = seed_order(factory)
    body = {"client_order_id": str(order.order_id), "reason": REASON}

    async with client_for(application) as client:
        operator = await client.post("/operator/orders/close", headers=OPERATOR, json=body)
        anonymous = await client.post("/operator/orders/close", json=body)
        wrong = await client.post(
            "/operator/orders/close", headers={"x-operator-token": "guess"}, json=body
        )
        # A token in the URL is refused on every authenticated route.
        in_url = await client.post(
            "/operator/orders/close", params={"token": ADMIN_TOKEN}, json=body
        )

    assert (operator.status_code, anonymous.status_code, wrong.status_code) == (403, 401, 401)
    assert in_url.status_code == 400
    assert venue.lookups == [] and venue.writes == []
    assert stored(factory, order).status == "pending_submit"
    assert closure_events(factory) == []


@pytest.mark.asyncio
async def test_a_browser_post_without_the_session_cookie_is_refused_like_a_cross_site_request(
    tmp_path,
):
    # Sessions use a SameSite=Strict cookie, so a form posted from another site arrives with no
    # cookie. This repository has no separate form token (see the pull request), so that is the
    # protection this control shares with pause, emergency stop, and re-arm.
    application, factory = closing_app(tmp_path, Venue())
    order, _, _ = seed_order(factory)

    async with client_for(application) as client:
        response = await client.post(
            "/operator/orders/close",
            headers=BROWSER,
            data={"client_order_id": str(order.order_id), "reason": REASON},
        )

    assert response.status_code == 401
    assert stored(factory, order).status == "pending_submit"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"reason": REASON}, id="no order"),
        pytest.param({"client_order_id": "x"}, id="no reason"),
        pytest.param({"client_order_id": "x", "reason": "   "}, id="blank reason"),
        pytest.param({"client_order_id": "x", "reason": "r" * (REASON_LIMIT + 1)}, id="too long"),
    ],
)
async def test_an_incomplete_request_is_refused_before_the_venue_is_asked(tmp_path, body):
    venue = Venue()
    application, factory = closing_app(tmp_path, venue)
    order, _, _ = seed_order(factory)
    if body.get("client_order_id") == "x":
        body = {**body, "client_order_id": str(order.order_id)}

    async with client_for(application) as client:
        response = await client.post("/operator/orders/close", headers=ADMIN, json=body)

    assert response.status_code == 422 and response.json()["errors"]
    assert venue.lookups == [] and stored(factory, order).status == "pending_submit"


@pytest.mark.asyncio
async def test_the_reason_is_stored_without_tokens_or_urls(tmp_path):
    application, factory = closing_app(tmp_path, Venue())
    order, _, _ = seed_order(factory)

    response = await close_via_post(
        application,
        order,
        reason=f"approved {ADMIN_TOKEN} see https://ops.example/ticket/9?k=abc for details",
    )

    assert response.status_code == 200
    [event] = closure_events(factory)
    assert ADMIN_TOKEN not in event.payload["reason"]
    assert "ops.example" not in event.payload["reason"] and "approved" in event.payload["reason"]


@pytest.mark.asyncio
async def test_closing_waits_for_the_trading_lock_so_it_cannot_overlap_a_cycle(tmp_path):
    venue = Venue()
    application, factory = closing_app(tmp_path, venue)
    order, _, _ = seed_order(factory)

    async with application.state.trading_lock:  # the trading loop or reconciler is running
        task = asyncio.create_task(close_via_post(application, order))
        await asyncio.sleep(0.05)
        assert not task.done() and venue.lookups == []
        assert stored(factory, order).status == "pending_submit"
    assert (await task).status_code == 200 and stored(factory, order).status == "canceled"


@pytest.mark.asyncio
async def test_closing_is_unavailable_without_a_broker_and_order_store(tmp_path):
    application = history_app(tmp_path)  # no execution engine: no broker is configured
    async with client_for(application) as client:
        response = await client.post(
            "/operator/orders/close",
            headers=ADMIN,
            json={"client_order_id": str(uuid4()), "reason": REASON},
        )
    assert response.status_code == 503 and "unavailable" in response.json()["errors"][0]


@pytest.mark.asyncio
async def test_an_order_store_that_cannot_close_is_reported_unavailable_not_half_used(tmp_path):
    class ReadOnlyStore(InMemoryOrderStore):
        close_unreceived = None  # a store with no way to close

    application, factory = closing_app(tmp_path, Venue())
    application.state.execution = ExecutionEngine(Venue(), ReadOnlyStore())
    order, _, _ = seed_order(factory)

    response = await close_via_post(application, order)

    assert response.status_code == 503 and stored(factory, order).status == "pending_submit"


@pytest.mark.asyncio
async def test_an_alert_that_cannot_be_delivered_never_changes_what_the_close_reports(tmp_path):
    class BrokenRouter(AlertRouter):
        async def route(self, alert):
            raise RuntimeError("the alert service is down")

    application, factory = closing_app(tmp_path, Venue(), alert_router=BrokenRouter())
    order, _, _ = seed_order(factory)

    response = await close_via_post(application, order)

    assert response.status_code == 200 and response.json()["closed"] is True
    assert stored(factory, order).status == "canceled"


def test_the_form_and_json_parsers_agree_and_reject_a_body_that_is_not_an_object():
    form = parse_close_order(
        b"client_order_id=abc&reason=because", "application/x-www-form-urlencoded"
    )
    as_json = parse_close_order(
        b'{"client_order_id": "abc", "reason": "because"}', "application/json"
    )
    assert (form.client_order_id, form.reason, form.errors) == ("abc", "because", ())
    assert as_json == form
    assert parse_close_order(b"[1]", "application/json").errors
    assert parse_close_order(b"{not json", "application/json").errors


# The reconciler and the trading loop after a close


def reconciler_for(venue, kill_switch):
    async def build(order):
        baseline = PortfolioState(
            orders={str(order.order_id): order},
            balances=await venue.get_balances(),
            positions=await venue.get_positions(),
        )
        store = InMemoryOrderStore()
        return ScheduledReconciler(
            Reconciler(venue, kill_switch),
            baseline=baseline,
            interval_seconds=60,
            refresh_order=ExecutionEngine(venue, store).recover,
        )

    return build


@pytest.mark.asyncio
async def test_a_reconciler_that_still_tracks_the_order_reads_it_as_a_divergence(tmp_path):
    # The hazard the close must clear: the broker has no record of a tracked pending order.
    _, factory = sqlite_database(tmp_path)
    order, _, _ = seed_order(factory)
    venue = Venue()
    scheduler = await reconciler_for(venue, KillSwitch())(order)

    result = await scheduler.run_once()

    assert result is not None
    assert [(item.entity_type, item.entity_key) for item in result.discrepancies] == [
        ("order", str(order.order_id))
    ]


@pytest.mark.asyncio
async def test_after_a_close_the_scheduled_reconciler_no_longer_halts_for_the_order(tmp_path):
    venue = Venue()
    application, factory = closing_app(tmp_path, venue)
    order, _, _ = seed_order(factory)
    scheduler = await reconciler_for(venue, KillSwitch())(order)
    application.state.operator_state.scheduled_reconciliation = scheduler
    application.state.trading_lock = scheduler.lock

    assert (await close_via_post(application, order)).status_code == 200
    result = await scheduler.run_once()

    assert result is not None and result.discrepancies == ()
    assert scheduler.status.last_result == "clean"


CANDLES = PARITY_WINDOWS["calm_range"]
BAR = next(
    index
    for index in range(len(CANDLES) - 1)
    if MovingAverageCrossStrategy().on_market_state(state_at(CANDLES, index)) is not None
)
STATE = state_at(CANDLES, BAR)


def trading_cycle(store, clock):
    price = CANDLES[BAR + 1].open
    quote = Quote(symbol="BTC-USD", bid=price, ask=price, as_of=utc_now(), source="unit")
    venue = SimulatedBroker(quote, fault_plan=FaultPlan(submit=(SimulatedFault.UNAVAILABLE,)))
    cycle = paper_cycle(
        venue,
        MovingAverageCrossStrategy(),
        store=store,
        audit=InMemoryAuditStore(store),
        clock=clock,
        pending_timeout=timedelta(minutes=5),
    )
    return venue, cycle


@pytest.mark.asyncio
async def test_after_the_close_and_a_rearm_the_next_loop_does_not_halt_for_the_closed_order():
    now = [datetime(2026, 10, 6, 11, 20, tzinfo=UTC)]
    store = InMemoryOrderStore()
    venue, cycle = trading_cycle(store, lambda: now[0])
    assert (await cycle.on_market_state(STATE)).status is CycleStatus.BROKER_ERROR
    # The unresolved-order timer starts on the next loop, as it did at 11:20 in the incident.
    assert (await cycle.on_market_state(STATE)).status is CycleStatus.UNRESOLVED
    now[0] += timedelta(minutes=5)
    assert (await cycle.on_market_state(STATE)).status is CycleStatus.HALTED
    [pending] = store.pending()

    closed = await OrderCloser(venue, store, clock=lambda: now[0]).close(
        str(pending.order_id), actor="admin", reason=REASON
    )
    assert closed.closed and store.pending() == ()
    # Hours later the owner re-arms. The five-minute limit elapsed long ago for that order.
    now[0] += timedelta(hours=2)
    cycle.kill_switch.set_state(KillSwitchState.RUNNING, reason="re-arm", actor="admin")
    outcome = await cycle.on_market_state(STATE)

    assert outcome.status is not CycleStatus.HALTED
    assert cycle.kill_switch.state is KillSwitchState.RUNNING
    assert cycle._unresolved_since is None  # the loop's unresolved-order timer is cleared
    assert venue.acknowledgement_count(str(pending.order_id)) == 0  # never sent


@pytest.mark.asyncio
async def test_closing_one_of_two_unresolved_orders_does_not_restart_the_loops_clock():
    now = [datetime(2026, 10, 6, 11, 20, tzinfo=UTC)]
    store = InMemoryOrderStore()
    venue, cycle = trading_cycle(store, lambda: now[0])
    assert (await cycle.on_market_state(STATE)).status is CycleStatus.BROKER_ERROR
    [first] = store.pending()
    other = Signal(
        symbol="BTC-USD", side=OrderSide.BUY, quantity=Decimal("1"), strategy_version="v"
    )
    store.reserve(
        OrderRequest(
            signal_id=other.signal_id,
            strategy_version="v",
            symbol="BTC-USD",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=Decimal("1"),
            correlation_id=other.correlation_id,
        ),
        RiskApproval(
            signal_id=other.signal_id,
            approved=True,
            reason="ok",
            correlation_id=other.correlation_id,
        ),
    )
    assert (await cycle.on_market_state(STATE)).status is CycleStatus.UNRESOLVED  # clock starts
    closed = await OrderCloser(venue, store).close(
        str(first.order_id), actor="admin", reason=REASON
    )
    assert closed.closed and len(store.pending()) == 1

    now[0] += timedelta(minutes=5)
    outcome = await cycle.on_market_state(STATE)

    assert outcome.status is CycleStatus.HALTED  # the other order is still unresolved


# Visibility: History, the dashboard, and the re-arm review

CLOSED_LABEL = "Closed by an administrator: never received by the venue"


async def get_page(application, path, *, headers=ADMIN):
    async with client_for(application) as client:
        response = await client.get(path, headers={**headers, **BROWSER})
    assert response.status_code == 200, path
    return response.text


def row_for(html: str, order: Order) -> str:
    """The one ledger row of the Orders page that belongs to ``order``."""

    [row] = [part for part in html.split('<li class="ledger-row">') if str(order.order_id) in part]
    return row


@pytest.mark.asyncio
async def test_history_shows_a_closed_order_and_its_audit_event_apart_from_a_broker_cancel(
    tmp_path,
):
    application, factory = closing_app(tmp_path, Venue())
    closed, _, _ = seed_order(factory)
    broker_canceled, _, _ = seed_order(factory, status=OrderStatus.CANCELED)
    assert (await close_via_post(application, closed)).status_code == 200

    orders = await get_page(application, "/operator/history/orders")

    mine = row_for(orders, closed)
    assert "canceled" in mine and "Closed by an administrator: never received" in mine
    assert CLOSED_LABEL in mine and "Administrator closed it" in mine
    assert "was pending_submit" in mine and REASON in mine
    assert "the venue answered: not found" in mine
    # A cancel the broker made carries no closure, so the two are never confused.
    theirs = row_for(orders, broker_canceled)
    assert "canceled" in theirs and "closed by an administrator" not in theirs.lower()

    lineage = await get_page(application, f"/operator/history/orders/{closed.order_id}")
    assert CLOSED_LABEL in lineage and "Lineage complete" in lineage  # links kept intact

    events = await get_page(
        application, f"/operator/history/events?event_type={ORDER_CLOSED_EVENT}"
    )
    assert ORDER_CLOSED_EVENT in events and CLOSED_LABEL in events
    assert REASON in events and str(closed.order_id) in events and "Administrator" in events


@pytest.mark.asyncio
async def test_the_dashboard_offers_the_close_control_to_an_administrator_only(tmp_path):
    application, factory = closing_app(tmp_path, Venue())
    order, _, _ = seed_order(factory)

    admin = await get_page(application, "/operator")
    assert 'id="unresolved-orders"' in admin
    assert 'action="/operator/orders/close"' in admin
    assert f'name="client_order_id" value="{order.order_id}"' in admin
    # The quantity reads as it was placed, not as the database's 18 decimals.
    assert "buy 0.0001 BTC-USD" in admin and "0.000100000000000000" not in admin
    assert re.search(r'<textarea[^>]*name="reason"[^>]*required', admin)
    assert "Close order: never received by the venue" in admin
    assert "never resubmitted" in admin and "Closing does not re-arm" in admin

    operator = await get_page(application, "/operator", headers=OPERATOR)
    assert "unresolved-orders" not in operator and "/operator/orders/close" not in operator

    assert (await close_via_post(application, order)).status_code == 200
    after = await get_page(application, "/operator")
    assert "No saved order is waiting on the venue" in after
    assert "/operator/orders/close" not in after


def test_a_saved_quantity_that_is_not_a_number_is_shown_as_stored_not_hidden():
    snapshot = {
        "risk": {"kill_switch": "halted", "transitions": []},
        "unresolved_orders": {
            "readable": True,
            "orders": [
                {
                    "status": "pending_submit",
                    "created_at": "2026-10-06T11:20:19+00:00",
                    "request": {"client_order_id": str(uuid4()), "quantity": "n/a"},
                }
            ],
        },
    }

    [row] = build_dashboard(snapshot, role="admin")["unresolved"]["rows"]

    assert row["quantity"] == "n/a"
    # An operator is never handed the list, whatever the snapshot holds.
    assert build_dashboard(snapshot, role="operator")["unresolved"] is None


@pytest.mark.asyncio
async def test_the_rearm_review_counts_the_saved_orders_not_an_empty_in_memory_list(tmp_path):
    # Before this change the row read the snapshot's own order list, which the running service
    # never fills, so it said "none" beside a stuck order.
    application, factory = closing_app(tmp_path, Venue())
    order, _, _ = seed_order(factory)

    stuck = await get_page(application, "/operator/rearm")
    assert (
        "1 pending" in stuck
        and "1 pending submission and 0 unknown among the 1 order(s) saved" in stuck
    )
    assert "is closed from the dashboard" in stuck

    assert (await close_via_post(application, order)).status_code == 200
    clear = await get_page(application, "/operator/rearm")
    assert "0 pending submission and 0 unknown among the 0 order(s) saved" in clear


@pytest.mark.asyncio
async def test_unreadable_saved_orders_offer_no_close_and_do_not_read_as_resolved(tmp_path):
    class Unreadable(InMemoryOrderStore):
        def pending(self):
            raise PersistenceUnavailable("the database is down")

    application, _ = closing_app(tmp_path, Venue())
    application.state.execution = ExecutionEngine(Venue(), Unreadable())

    dashboard = await get_page(application, "/operator")
    assert "The saved orders could not be read" in dashboard
    assert "/operator/orders/close" not in dashboard
    review = await get_page(application, "/operator/rearm")
    assert "unreadable" in review and "Do not confirm the orders item" in review
    assert "0 pending submission" not in review
