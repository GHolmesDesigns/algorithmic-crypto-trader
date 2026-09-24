"""Refusal and persistence-failure paths of the execution package."""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest
from brokers.simulated import SimulatedBroker
from core.models import (
    Fill,
    OrderRequest,
    OrderSide,
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


@pytest.mark.asyncio
async def test_recover_pending_skips_an_order_that_cannot_be_read_back() -> None:
    store = EmptyStore()
    request = order_request()
    store.reserve(request, approve(request))
    assert await ExecutionEngine(SimulatedBroker(), store).recover_pending() == ()


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
