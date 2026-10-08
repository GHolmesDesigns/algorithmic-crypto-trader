"""Refusal and persistence-failure paths of the execution package."""

from __future__ import annotations

import logging
from decimal import Decimal
from uuid import uuid4

import pytest
from brokers.http import ProviderHTTPError
from brokers.simulated import SimulatedBroker
from core.logging import short_reference
from core.models import (
    Fill,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    RiskApproval,
    Signal,
    utc_now,
)
from db.models import FillRecord, OrderRecord
from execution.audit import InMemoryAuditStore
from execution.engine import (
    ExecutionEngine,
    InMemoryOrderStore,
    PersistenceUnavailable,
    submit_approved_order,
)
from execution.persistence import SqlAlchemyOrderStore
from sqlalchemy.exc import OperationalError

from tests.gate_support import sqlite_database


def order_request(**changes) -> OrderRequest:
    values = dict(
        signal_id=uuid4(),
        strategy_version="paths",
        symbol="BTC-USD",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("0.5"),
        correlation_id=uuid4(),
    )
    values.update(changes)
    return OrderRequest(**values)


def approve(request: OrderRequest, *, approved: bool = True, signal_id=None) -> RiskApproval:
    return RiskApproval(
        signal_id=signal_id or request.signal_id,
        approved=approved,
        reason="paths",
        correlation_id=request.correlation_id,
    )


def fill(order_id, fill_id: str = "fill-1") -> Fill:
    return Fill(
        fill_id=fill_id,
        order_id=order_id,
        symbol="BTC-USD",
        side=OrderSide.BUY,
        quantity=Decimal("0.5"),
        price=Decimal("30000"),
        fee=Decimal("0"),
        fee_asset="USD",
        occurred_at=utc_now(),
    )


@pytest.mark.asyncio
async def test_execution_refuses_foreign_or_unapproved_decisions() -> None:
    broker = SimulatedBroker()
    request = order_request()
    with pytest.raises(ValueError, match="does not belong"):
        await ExecutionEngine(broker).submit(request, approve(request, signal_id=uuid4()))
    with pytest.raises(PermissionError, match="not approved"):
        await ExecutionEngine(broker).submit(request, approve(request, approved=False))
    order = await submit_approved_order(broker, request, approve(request))
    assert order.filled_quantity == request.quantity


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError("lookup timed out"),
        ProviderHTTPError(503, "unavailable"),
        PermissionError("credentials rejected"),
    ],
)
async def test_new_order_is_audited_closed_when_pre_submit_lookup_fails(failure) -> None:
    broker = SimulatedBroker()
    submit_calls = 0

    async def fail_lookup(_client_order_id):
        raise failure

    async def count_submit(_request, _approval):
        nonlocal submit_calls
        submit_calls += 1
        raise AssertionError("submit_order must not run after the lookup failed")

    broker.get_order = fail_lookup
    broker.submit_order = count_submit
    request = order_request()
    store = InMemoryOrderStore()
    alerts = []

    async def alert(order, error):
        alerts.append((order, error))

    engine = ExecutionEngine(broker, store, on_order_closed=alert)
    with pytest.raises(type(failure)):
        await engine.submit(request, approve(request))

    saved = store.get(str(request.client_order_id))
    assert saved is not None and saved.status is OrderStatus.CANCELED
    assert store.pending() == ()
    assert submit_calls == 0
    assert len(store.closures) == 1
    event_type, payload = store.closures[0]
    assert event_type == "order_closed_never_received"
    assert payload["actor"] == "system"
    assert "pre-submit" in payload["reason"].lower()
    assert payload["outcome"] == "never received by the venue"
    assert payload["signal_id"] == str(request.signal_id)
    assert payload["strategy_version"] == request.strategy_version
    assert len(alerts) == 1 and alerts[0][1] is failure


@pytest.mark.asyncio
async def test_lookup_failure_does_not_close_an_order_that_preexisted_this_call() -> None:
    broker = SimulatedBroker()
    request = order_request()
    approval = approve(request)
    store = InMemoryOrderStore()
    original = store.reserve(request, approval)

    async def fail_lookup(_client_order_id):
        raise TimeoutError("lookup timed out")

    broker.get_order = fail_lookup
    with pytest.raises(TimeoutError):
        await ExecutionEngine(broker, store).submit(request, approval)

    assert store.get(str(request.client_order_id)) == original
    assert store.pending() == (original,)
    assert store.closures == []


@pytest.mark.asyncio
async def test_failed_audit_close_preserves_the_new_pending_order_and_skips_alert() -> None:
    class UnclosableStore(InMemoryOrderStore):
        def close_unreceived(self, client_order_id, *, event_type, payload, at):
            raise PersistenceUnavailable("audit journal unavailable")

    broker = SimulatedBroker()

    async def fail_lookup(_client_order_id):
        raise TimeoutError("lookup timed out")

    broker.get_order = fail_lookup
    request = order_request()
    store = UnclosableStore()
    alerts = []

    async def alert(order, error):
        alerts.append((order, error))

    with pytest.raises(PersistenceUnavailable):
        await ExecutionEngine(broker, store, on_order_closed=alert).submit(
            request, approve(request)
        )

    assert store.pending() and store.pending()[0].status is OrderStatus.PENDING_SUBMIT
    assert store.closures == [] and alerts == []


def test_in_memory_stores_fail_closed_and_refuse_a_rebound_client_order_id() -> None:
    store = InMemoryOrderStore()
    request = order_request()
    order = store.reserve(request, approve(request))
    with pytest.raises(ValueError, match="already bound"):
        store.reserve(request.model_copy(update={"quantity": Decimal("1")}), approve(request))
    store.fail_writes = True
    with pytest.raises(PersistenceUnavailable):
        store.update(order)
    with pytest.raises(PersistenceUnavailable):
        store.add_fills((fill(order.order_id),))
    audit = InMemoryAuditStore(store, fail_writes=True)
    with pytest.raises(PersistenceUnavailable):
        audit.record_risk_decision(approve(request))
    signal = Signal(
        symbol="BTC-USD", side=OrderSide.BUY, quantity=Decimal("1"), strategy_version="x"
    )
    with pytest.raises(PersistenceUnavailable):
        audit.record_signal(signal)


class EmptyStore(InMemoryOrderStore):
    """A pending order that vanishes before it can be read back."""

    def get(self, client_order_id: str):
        return None


class FailFirstFillStore(InMemoryOrderStore):
    def __init__(self) -> None:
        super().__init__()
        self.fail_next_fill_write = True

    def add_fills(self, fills: tuple[Fill, ...]) -> None:
        if self.fail_next_fill_write:
            self.fail_next_fill_write = False
            raise PersistenceUnavailable("fill persistence is unavailable")
        super().add_fills(fills)


@pytest.mark.asyncio
async def test_recover_pending_skips_an_order_that_cannot_be_read_back() -> None:
    store = EmptyStore()
    request = order_request()
    store.reserve(request, approve(request))
    assert await ExecutionEngine(SimulatedBroker(), store).recover_pending() == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["provider-read", "fill-write"])
async def test_fill_failures_leave_terminal_orders_pending_for_recovery(failure) -> None:
    broker = SimulatedBroker()
    request = order_request()
    store = FailFirstFillStore() if failure == "fill-write" else InMemoryOrderStore()
    engine = ExecutionEngine(broker, store)
    get_fills = broker.get_fills

    if failure == "provider-read":
        calls = 0

        async def fail_once(client_order_id: str):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ConnectionError("fill lookup failed")
            return await get_fills(client_order_id)

        broker.get_fills = fail_once  # type: ignore[method-assign]

    with pytest.raises((ConnectionError, PersistenceUnavailable)):
        await engine.submit(request, approve(request))

    client_order_id = str(request.client_order_id)
    pending = store.get(client_order_id)
    assert pending is not None and pending.status is OrderStatus.PENDING_SUBMIT
    assert store.pending() == (pending,)

    assert [item.status for item in await engine.recover_pending()] == [OrderStatus.FILLED]
    recovered = store.get(client_order_id)
    assert recovered is not None and recovered.status is OrderStatus.FILLED
    assert len(store.fills) == 1
    assert {item.order_id for item in store.fills.values()} == {request.client_order_id}


@pytest.mark.asyncio
async def test_missing_accepted_fills_log_pending_quantities_without_secrets(caplog) -> None:
    broker = SimulatedBroker()
    original_get_fills = broker.get_fills
    calls = 0

    async def lagged_fills(client_order_id: str):
        nonlocal calls
        calls += 1
        return () if calls == 1 else await original_get_fills(client_order_id)

    broker.get_fills = lagged_fills  # type: ignore[method-assign]
    request = order_request()
    store = InMemoryOrderStore()
    engine = ExecutionEngine(broker, store)
    with caplog.at_level(logging.WARNING, logger="execution.engine"):
        await engine.submit(request, approve(request))

    warning = [record for record in caplog.records if "result=pending" in record.message]
    assert len(warning) == 1
    assert warning[0].message == (
        f"order step=fill ref={short_reference(request.client_order_id)} "
        "result=pending filled_quantity=0.5 recorded_quantity=0"
    )
    pending = store.get(str(request.client_order_id))
    assert pending is not None and pending.status is OrderStatus.PENDING_SUBMIT
    assert await engine.recover_pending()
    recovered = store.get(str(request.client_order_id))
    assert recovered is not None and recovered.status is OrderStatus.FILLED


def outage():
    raise OperationalError("connect", {}, ConnectionRefusedError("down"))


def test_sql_order_store_reports_every_outage_and_dedupes_fills(tmp_path) -> None:
    engine, session_factory = sqlite_database(tmp_path)
    store = SqlAlchemyOrderStore(session_factory)
    request = order_request()
    order = store.reserve(request, approve(request))
    store.add_fills((fill(request.client_order_id),))
    store.add_fills((fill(request.client_order_id),))  # the same broker fill is stored once
    with session_factory() as session:
        assert session.query(FillRecord).count() == 1

    down = SqlAlchemyOrderStore(outage)
    for action in (
        lambda: down.update(order),
        lambda: down.add_fills((fill(request.client_order_id, "fill-2"),)),
        lambda: down.get(str(request.client_order_id)),
        down.open_orders,
        lambda: down.fills_for((order,)),
    ):
        with pytest.raises(PersistenceUnavailable):
            action()
    with pytest.raises(PersistenceUnavailable):
        store.get("not-a-uuid")
    assert store.fills_for(()) == ()

    # A legacy row without recovery fields cannot be rebuilt and fails closed.
    with session_factory() as session:
        session.query(OrderRecord).update({"symbol": None})
        session.commit()
    with pytest.raises(PersistenceUnavailable, match="missing recovery fields"):
        store.get(str(request.client_order_id))
    engine.dispose()
