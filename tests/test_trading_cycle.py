"""Failure paths of the trading cycle, risk-input assembly, ledger, scheduler, and audit."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from app.trading import BrokerRiskInputs, CycleContext, CycleStatus
from brokers.simulated import FaultPlan, SimulatedBroker, SimulatedFault
from core.models import (
    Balance,
    Fill,
    KillSwitchState,
    Order,
    OrderSide,
    OrderStatus,
    Position,
    Quote,
    RiskApproval,
    utc_now,
)
from execution.audit import InMemoryAuditStore, OrderLineage, SqlAlchemyAuditStore
from execution.engine import InMemoryOrderStore, PersistenceUnavailable
from portfolio.ledger import apply_fills
from portfolio.reconciliation import PortfolioState, Reconciler
from portfolio.scheduler import ScheduledReconciler
from risk.kill_switch import KillSwitch
from sqlalchemy.exc import OperationalError
from strategy.reference import MovingAverageCrossStrategy

from tests.gate_support import CONSTRAINTS, PARITY_WINDOWS, paper_cycle, sqlite_database, state_at

CANDLES = PARITY_WINDOWS["calm_range"]
BAR = next(
    index
    for index in range(len(CANDLES) - 1)
    if MovingAverageCrossStrategy().on_market_state(state_at(CANDLES, index)) is not None
)
STATE = state_at(CANDLES, BAR)
SIGNAL = MovingAverageCrossStrategy().on_market_state(STATE)


def quote() -> Quote:
    price = CANDLES[BAR + 1].open
    return Quote(symbol="BTC-USD", bid=price, ask=price, as_of=utc_now(), source="unit")


class UnreadableStore(InMemoryOrderStore):
    def pending(self):
        raise PersistenceUnavailable("order store is down")


class DecisionOutage(InMemoryAuditStore):
    def record_risk_decision(self, decision) -> None:
        raise PersistenceUnavailable("risk decisions are down")


def cycle(broker=None, store=None, audit=None, **options):
    broker = broker or SimulatedBroker(quote())
    store = store if store is not None else InMemoryOrderStore()
    audit = audit if audit is not None else InMemoryAuditStore(store)
    return paper_cycle(broker, MovingAverageCrossStrategy(), store=store, audit=audit, **options)


@pytest.mark.asyncio
async def test_unreadable_order_store_halts_before_the_strategy_runs() -> None:
    outcome = await cycle(store=UnreadableStore()).on_market_state(STATE)
    assert outcome.status is CycleStatus.HALTED and outcome.signal is None


@pytest.mark.asyncio
async def test_signal_and_decision_audit_failures_halt() -> None:
    store = InMemoryOrderStore()
    halted = await cycle(
        store=store, audit=InMemoryAuditStore(store, fail_writes=True)
    ).on_market_state(STATE)
    assert halted.status is CycleStatus.HALTED and "signal" in halted.detail

    store = InMemoryOrderStore()
    halted = await cycle(store=store, audit=DecisionOutage(store)).on_market_state(STATE)
    assert halted.status is CycleStatus.HALTED and "risk decision" in halted.detail
    assert store.orders == {}


@pytest.mark.asyncio
async def test_broker_failures_are_classified_and_block_new_entries() -> None:
    rejected = await cycle(
        SimulatedBroker(quote(), fault_plan=FaultPlan(submit=(SimulatedFault.REJECT,)))
    ).on_market_state(STATE)
    assert rejected.status is CycleStatus.REJECTED
    assert rejected.order.status is OrderStatus.REJECTED

    store = InMemoryOrderStore()
    broken = cycle(
        SimulatedBroker(quote(), fault_plan=FaultPlan(submit=(SimulatedFault.UNAVAILABLE,))),
        store=store,
    )
    failed = await broken.on_market_state(STATE)
    assert failed.status is CycleStatus.BROKER_ERROR
    assert [order.status for order in store.pending()] == [OrderStatus.PENDING_SUBMIT]
    # The venue never saw it, so the next loop cannot confirm it and takes no new entry.
    assert (await broken.on_market_state(STATE)).status is CycleStatus.UNRESOLVED


@pytest.mark.asyncio
async def test_pending_order_query_failure_takes_no_new_entry() -> None:
    store = InMemoryOrderStore()
    first = cycle(
        SimulatedBroker(quote(), fault_plan=FaultPlan(submit=(SimulatedFault.TIMEOUT,))),
        store=store,
    )
    assert (await first.on_market_state(STATE)).status is CycleStatus.AMBIGUOUS
    unreachable = cycle(
        SimulatedBroker(quote(), fault_plan=FaultPlan(get_order=(SimulatedFault.UNAVAILABLE,))),
        store=store,
    )
    outcome = await unreachable.on_market_state(STATE)
    assert outcome.status is CycleStatus.BROKER_ERROR and outcome.signal is None


class StubBroker:
    healthy = True

    def __init__(self, positions=(), cash="10000") -> None:
        self.positions = positions
        self.cash = Decimal(cash)

    async def get_quote(self, symbol):
        return quote()

    async def get_positions(self):
        return self.positions

    async def get_balances(self):
        return (Balance(asset="USD", available=self.cash, as_of=utc_now()),)


def context(**changes) -> CycleContext:
    values = dict(
        kill_switch=KillSwitchState.RUNNING, ordered_signal_ids=frozenset(), last_order_at=None
    )
    values.update(changes)
    return CycleContext(**values)


@pytest.mark.asyncio
async def test_risk_inputs_leave_unpriced_exposure_unknown_and_honour_window_and_cooldown() -> None:
    dust = Position(
        symbol="DOGE-USD", quantity=Decimal("5"), average_price=Decimal("0"), as_of=utc_now()
    )
    source = BrokerRiskInputs(
        StubBroker((dust,)), constraints=CONSTRAINTS, estimated_slippage=Decimal("0")
    )
    inputs = await source.risk_inputs(SIGNAL, STATE, context())
    assert inputs.open_notional is None and inputs.aggregate_allocation is None
    assert inputs.daily_loss is None and inputs.drawdown is None

    now = datetime(2026, 9, 24, 12, tzinfo=UTC)
    closed = BrokerRiskInputs(
        StubBroker(),
        constraints=CONSTRAINTS,
        estimated_slippage=Decimal("0"),
        cooldown=timedelta(minutes=10),
        trading_window=lambda moment: moment.hour < 12,
        clock=lambda: now,
    )
    inputs = await closed.risk_inputs(
        SIGNAL, STATE, context(last_order_at=now - timedelta(minutes=5))
    )
    assert inputs.trading_window_open is False and inputs.symbol_cooldown_clear is False


@pytest.mark.asyncio
async def test_daily_loss_resets_each_day_and_drawdown_tracks_the_peak() -> None:
    days = iter(
        [
            datetime(2026, 9, 24, 12, tzinfo=UTC),
            datetime(2026, 9, 24, 13, tzinfo=UTC),
            datetime(2026, 9, 25, 9, tzinfo=UTC),
        ]
    )
    broker = StubBroker()
    source = BrokerRiskInputs(
        broker, constraints=CONSTRAINTS, estimated_slippage=Decimal("0"), clock=lambda: next(days)
    )
    await source.risk_inputs(SIGNAL, STATE, context())
    broker.cash = Decimal("9000")
    same_day = await source.risk_inputs(SIGNAL, STATE, context())
    next_day = await source.risk_inputs(SIGNAL, STATE, context())
    assert same_day.daily_loss == Decimal("1000") and same_day.drawdown == Decimal("0.1")
    assert next_day.daily_loss == Decimal("0") and next_day.drawdown == Decimal("0.1")


def fill(side: OrderSide, quantity: str, price: str, fee: str = "0", *, second: int = 0) -> Fill:
    return Fill(
        fill_id=str(uuid4()),
        order_id=uuid4(),
        symbol="BTC-USD",
        side=side,
        quantity=Decimal(quantity),
        price=Decimal(price),
        fee=Decimal(fee),
        fee_asset="USD",
        occurred_at=datetime(2026, 9, 24, tzinfo=UTC) + timedelta(seconds=second),
    )


def test_ledger_projects_buys_and_sells_and_refuses_a_negative_balance() -> None:
    baseline = PortfolioState(
        balances=(Balance(asset="USD", available=Decimal("1000"), as_of=utc_now()),)
    )
    projected = apply_fills(
        baseline,
        [
            fill(OrderSide.SELL, "1", "300", second=3),  # applied in time order, not list order
            fill(OrderSide.BUY, "2", "100", "1", second=1),
            fill(OrderSide.BUY, "2", "200", second=2),
        ],
    )
    assert [(item.quantity, item.average_price) for item in projected.positions] == [
        (Decimal("3"), Decimal("150"))
    ]
    assert {item.asset: item.available for item in projected.balances} == {
        "BTC": Decimal("3"),
        "USD": Decimal("699"),
    }
    with pytest.raises(ValueError, match="negative"):
        apply_fills(baseline, [fill(OrderSide.BUY, "20", "100")])


@pytest.mark.asyncio
async def test_scheduler_trips_when_the_expected_portfolio_cannot_be_projected() -> None:
    switch = KillSwitch()
    reasons = []

    async def unavailable(detail: str) -> None:
        reasons.append(detail)

    scheduler = ScheduledReconciler(
        Reconciler(SimulatedBroker(), switch),
        baseline=PortfolioState(),
        interval_seconds=60,
        on_unavailable=unavailable,
    )
    order = Order(order_id=uuid4(), request=_request(), status=OrderStatus.FILLED)
    scheduler.observe(order, (fill(OrderSide.SELL, "1", "100"),))

    assert await scheduler.run_once() is None
    assert switch.state is KillSwitchState.HALTED
    assert reasons == ["expected portfolio could not be projected"]
    with pytest.raises(ValueError):
        ScheduledReconciler(
            Reconciler(SimulatedBroker(), switch), baseline=PortfolioState(), interval_seconds=0
        )


@pytest.mark.asyncio
async def test_scheduler_loop_survives_an_unexpected_failure() -> None:
    switch = KillSwitch()
    scheduler = ScheduledReconciler(
        Reconciler(SimulatedBroker(), switch), baseline=PortfolioState(), interval_seconds=0.01
    )

    async def explode():
        raise RuntimeError("bug")

    scheduler.run_once = explode  # type: ignore[method-assign]
    stop = asyncio.Event()
    task = asyncio.create_task(scheduler.run(stop))
    await asyncio.sleep(0.05)
    stop.set()
    await task
    assert switch.state is KillSwitchState.HALTED


def _request():
    from core.models import OrderRequest, OrderType

    return OrderRequest(
        signal_id=uuid4(),
        strategy_version="unit",
        symbol="BTC-USD",
        side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        quantity=Decimal("1"),
        correlation_id=uuid4(),
    )


def test_lineage_gaps_name_each_missing_link() -> None:
    def lineage(**changes) -> OrderLineage:
        values = dict(
            client_order_id="x",
            status=OrderStatus.FILLED,
            signal_recorded=True,
            strategy_version_matches=True,
            risk_decision_recorded=True,
            risk_decision_approved=True,
            fill_count=1,
        )
        values.update(changes)
        return OrderLineage(**values)

    assert lineage().gaps == ()
    assert lineage(strategy_version_matches=False).gaps == ("strategy_version",)
    assert lineage(risk_decision_approved=False).gaps == ("risk_decision_not_approved",)
    assert lineage(status=OrderStatus.OPEN, fill_count=0).gaps == ()


def test_audit_store_reports_an_unavailable_database(tmp_path) -> None:
    engine, session_factory = sqlite_database(tmp_path)

    def down():
        raise OperationalError("connect", {}, ConnectionRefusedError("down"))

    audit = SqlAlchemyAuditStore(down)
    approval = RiskApproval(
        signal_id=SIGNAL.signal_id,
        approved=True,
        reason="unit",
        correlation_id=SIGNAL.correlation_id,
    )
    for action in (
        lambda: audit.record_signal(SIGNAL),
        lambda: audit.record_risk_decision(approval),
        audit.lineage,
    ):
        with pytest.raises(PersistenceUnavailable):
            action()
    healthy = SqlAlchemyAuditStore(session_factory)
    healthy.record_signal(SIGNAL)
    healthy.record_signal(SIGNAL)  # idempotent
    healthy.record_risk_decision(approval)
    healthy.record_risk_decision(approval)
    assert healthy.lineage() == ()
    engine.dispose()
