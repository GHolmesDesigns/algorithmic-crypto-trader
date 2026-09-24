"""Failure paths of the trading cycle, risk-input assembly, ledger, scheduler, and audit."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from app.trading import BrokerRiskInputs, CycleContext, CycleStatus, TradingCycle
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
from execution.engine import ExecutionEngine, InMemoryOrderStore, PersistenceUnavailable
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


def _request(side: OrderSide = OrderSide.SELL):
    from core.models import OrderRequest, OrderType

    return OrderRequest(
        signal_id=uuid4(),
        strategy_version="unit",
        symbol="BTC-USD",
        side=side,
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
    # An immediate-or-cancel order that partly filled, then canceled, still owes its fills.
    partly = lineage(status=OrderStatus.CANCELED, fill_count=0, filled_quantity=Decimal("0.4"))
    assert partly.gaps == ("fills",)


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


class QuotingBroker(StubBroker):
    """Quotes the traded symbol and whatever other holdings it is given prices for."""

    def __init__(self, positions=(), cash="10000", prices=None) -> None:
        super().__init__(positions, cash)
        self.prices = prices or {}

    async def get_quote(self, symbol):
        if symbol == "BTC-USD":
            return quote()
        price = self.prices[symbol]  # an unknown market raises, as a venue would
        return Quote(symbol=symbol, bid=price, ask=price + 1, as_of=utc_now(), source="unit")


@pytest.mark.asyncio
async def test_other_holdings_are_valued_at_the_venue_bid_not_a_missing_cost_basis() -> None:
    # Coinbase and Gemini report no cost basis: average_price is always 0.
    eth = Position(
        symbol="ETH-USD", quantity=Decimal("2"), average_price=Decimal("0"), as_of=utc_now()
    )
    priced = BrokerRiskInputs(
        QuotingBroker((eth,), prices={"ETH-USD": Decimal("2500")}),
        constraints=CONSTRAINTS,
        estimated_slippage=Decimal("0"),
    )
    inputs = await priced.risk_inputs(SIGNAL, STATE, context())
    assert inputs.open_notional == inputs.aggregate_allocation == Decimal("5000")
    assert inputs.daily_loss == Decimal("0") and inputs.drawdown == Decimal("0")

    unpriced = BrokerRiskInputs(
        QuotingBroker((eth,)), constraints=CONSTRAINTS, estimated_slippage=Decimal("0")
    )
    inputs = await unpriced.risk_inputs(SIGNAL, STATE, context())
    assert inputs.aggregate_allocation is None and inputs.drawdown is None


@pytest.mark.asyncio
async def test_loss_limits_survive_a_restart_and_a_bad_state_file_fails_closed(tmp_path) -> None:
    path = tmp_path / "loss-state.json"
    broker = StubBroker()
    noon = datetime(2026, 9, 24, 12, tzinfo=UTC)

    def source(state_path=path) -> BrokerRiskInputs:
        return BrokerRiskInputs(
            broker,
            constraints=CONSTRAINTS,
            estimated_slippage=Decimal("0"),
            clock=lambda: noon,
            loss_state_path=state_path,
        )

    await source().risk_inputs(SIGNAL, STATE, context())
    broker.cash = Decimal("9000")
    restarted = await source().risk_inputs(SIGNAL, STATE, context())
    assert restarted.daily_loss == Decimal("1000") and restarted.drawdown == Decimal("0.1")

    path.write_text("{not json", encoding="utf-8")
    unreadable = await source().risk_inputs(SIGNAL, STATE, context())
    assert unreadable.daily_loss is None and unreadable.drawdown is None

    blocker = tmp_path / "not-a-directory"
    blocker.write_text("", encoding="utf-8")
    unwritable = await source(blocker / "loss-state.json").risk_inputs(SIGNAL, STATE, context())
    assert unwritable.daily_loss is None and unwritable.drawdown is None


class Silent:
    strategy_version = "silent-v1"

    def on_market_state(self, state):
        return None


@pytest.mark.asyncio
async def test_loops_without_a_signal_still_sample_equity_for_the_loss_limits() -> None:
    broker = StubBroker()
    source = BrokerRiskInputs(broker, constraints=CONSTRAINTS, estimated_slippage=Decimal("0"))
    store = InMemoryOrderStore()
    quiet = TradingCycle(
        strategy=Silent(),
        execution=ExecutionEngine(broker, store),
        audit=InMemoryAuditStore(store),
        kill_switch=KillSwitch(),
        risk_inputs=source,
        environ={},
    )
    assert (await quiet.on_market_state(STATE)).status is CycleStatus.NO_SIGNAL

    broker.cash = Decimal("9000")
    inputs = await source.risk_inputs(SIGNAL, STATE, context())
    # Measured from the equity of the quiet bar, not from this first signal.
    assert inputs.daily_loss == Decimal("1000") and inputs.drawdown == Decimal("0.1")


@pytest.mark.asyncio
async def test_an_order_the_venue_never_received_halts_after_the_pending_timeout() -> None:
    now = [datetime(2026, 9, 24, 12, tzinfo=UTC)]
    halts: list[str] = []

    async def on_halt(reason: str) -> None:
        halts.append(reason)

    broken = cycle(
        SimulatedBroker(quote(), fault_plan=FaultPlan(submit=(SimulatedFault.UNAVAILABLE,))),
        clock=lambda: now[0],
        pending_timeout=timedelta(minutes=5),
        on_halt=on_halt,
    )
    assert (await broken.on_market_state(STATE)).status is CycleStatus.BROKER_ERROR
    assert (await broken.on_market_state(STATE)).status is CycleStatus.UNRESOLVED
    now[0] += timedelta(minutes=4)
    assert (await broken.on_market_state(STATE)).status is CycleStatus.UNRESOLVED

    now[0] += timedelta(minutes=1)
    halted = await broken.on_market_state(STATE)
    assert halted.status is CycleStatus.HALTED and "operator review" in halted.detail
    assert broken.kill_switch.state is KillSwitchState.HALTED
    assert (await broken.on_market_state(STATE)).status is CycleStatus.HALTED
    assert len(halts) == 1  # one alert, not one per loop


@pytest.mark.asyncio
async def test_the_trading_loop_waits_while_the_reconciler_holds_the_lock() -> None:
    lock = asyncio.Lock()
    store = InMemoryOrderStore()
    serialized = cycle(store=store, lock=lock)
    async with lock:
        task = asyncio.create_task(serialized.on_market_state(STATE))
        await asyncio.sleep(0.01)
        assert not task.done() and store.orders == {}
    assert (await task).status is CycleStatus.SUBMITTED


class LaggingFills:
    """The venue reports the order filled before its fills can be listed."""

    def __init__(self, order: Order) -> None:
        self.order = order
        self.fills: tuple[Fill, ...] = ()

    async def get_order(self, client_order_id):
        return self.order

    async def get_fills(self, client_order_id):
        return self.fills


@pytest.mark.asyncio
async def test_an_order_is_not_settled_until_its_fills_are_recorded() -> None:
    request = _request()
    key = str(request.client_order_id)
    store = InMemoryOrderStore()
    store.reserve(request, _approval(request))
    broker = LaggingFills(
        Order(
            order_id=uuid4(),
            request=request,
            status=OrderStatus.FILLED,
            filled_quantity=request.quantity,
        )
    )
    engine = ExecutionEngine(broker, store)

    await engine.recover(key)
    assert store.get(key).status is OrderStatus.PENDING_SUBMIT  # still recoverable

    broker.fills = (fill(OrderSide.SELL, "1", "100"),)
    await engine.recover(key)
    assert store.get(key).status is OrderStatus.FILLED and len(store.fills) == 1


class SettlingVenue:
    """A venue whose accepted order is still OPEN when recorded and fills before the next run."""

    def __init__(self) -> None:
        self.balances = {"USD": Decimal("10000")}
        self.order: Order | None = None
        self.fills: tuple[Fill, ...] = ()

    async def get_balances(self):
        return tuple(
            Balance(asset=asset, available=amount, as_of=utc_now())
            for asset, amount in sorted(self.balances.items())
            if amount
        )

    async def get_positions(self):
        return tuple(
            Position(
                symbol=f"{asset}-USD", quantity=amount, average_price=Decimal("0"), as_of=utc_now()
            )
            for asset, amount in self.balances.items()
            if asset != "USD" and amount
        )

    async def get_order(self, client_order_id):
        return self.order

    async def get_fills(self, client_order_id):
        return self.fills

    def fill(self) -> None:
        assert self.order is not None
        quantity, price = self.order.request.quantity, Decimal("100")
        self.balances["BTC"] = quantity
        self.balances["USD"] -= quantity * price
        self.order = self.order.model_copy(
            update={"status": OrderStatus.FILLED, "filled_quantity": quantity}
        )
        self.fills = (
            Fill(
                fill_id="venue-fill-1",
                order_id=self.order.order_id,
                symbol="BTC-USD",
                side=OrderSide.BUY,
                quantity=quantity,
                price=price,
                fee=Decimal("0"),
                fee_asset="USD",
                occurred_at=utc_now(),
            ),
        )


@pytest.mark.asyncio
async def test_an_order_filling_between_runs_is_progress_and_its_fills_are_persisted() -> None:
    venue = SettlingVenue()
    switch = KillSwitch()
    store = InMemoryOrderStore()
    engine = ExecutionEngine(venue, store)
    scheduler = ScheduledReconciler(
        Reconciler(venue, switch),
        baseline=PortfolioState(balances=await venue.get_balances()),
        interval_seconds=60,
        refresh_order=engine.recover,
    )
    engine.on_recorded = scheduler.observe

    request = _request(OrderSide.BUY)
    key = str(request.client_order_id)
    store.reserve(request, _approval(request))
    # Recorded OPEN, as when the Coinbase read-back after Create Order fails.
    venue.order = Order(order_id=uuid4(), request=request, status=OrderStatus.OPEN)
    await engine.recover(key)
    assert store.get(key).status is OrderStatus.OPEN

    venue.fill()
    result = await scheduler.run_once()

    assert result is not None and result.discrepancies == ()
    assert switch.state is KillSwitchState.RUNNING
    assert store.get(key).status is OrderStatus.FILLED
    assert [item.fill_id for item in store.fills.values()] == ["venue-fill-1"]
    # Settled: the next run neither re-reads it nor counts its fill twice.
    assert (await scheduler.run_once()).discrepancies == ()


@pytest.mark.asyncio
async def test_sampling_and_alert_failures_never_break_the_loop_or_the_halt() -> None:
    class NoSampling:
        async def risk_inputs(self, signal, state, context):
            raise AssertionError("a quiet bar assembles no risk inputs")

    class BrokenSampling(NoSampling):
        async def observe(self, state):
            raise RuntimeError("venue down")

    for source in (NoSampling(), BrokenSampling()):
        store = InMemoryOrderStore()
        quiet = TradingCycle(
            strategy=Silent(),
            execution=ExecutionEngine(StubBroker(), store),
            audit=InMemoryAuditStore(store),
            kill_switch=KillSwitch(),
            risk_inputs=source,
            environ={},
        )
        assert (await quiet.on_market_state(STATE)).status is CycleStatus.NO_SIGNAL

    async def pager_down(reason: str) -> None:
        raise RuntimeError("pager down")

    halted = cycle(store=UnreadableStore(), on_halt=pager_down)
    assert (await halted.on_market_state(STATE)).status is CycleStatus.HALTED
    assert halted.kill_switch.state is KillSwitchState.HALTED


@pytest.mark.asyncio
async def test_scheduler_reads_open_orders_itself_and_halts_when_it_cannot() -> None:
    venue = SettlingVenue()
    request = _request(OrderSide.BUY)
    venue.order = Order(order_id=uuid4(), request=request, status=OrderStatus.OPEN)
    switch = KillSwitch()
    scheduler = ScheduledReconciler(
        Reconciler(venue, switch),
        baseline=PortfolioState(balances=await venue.get_balances()),
        interval_seconds=60,
    )
    scheduler.observe(venue.order, ())
    venue.fill()
    # Without an execution engine the scheduler re-reads the order from the broker.
    assert (await scheduler.run_once()).discrepancies == ()
    assert switch.state is KillSwitchState.RUNNING

    reasons: list[str] = []

    async def unavailable(detail: str) -> None:
        reasons.append(detail)

    async def unreachable(client_order_id: str) -> None:
        raise RuntimeError("venue down")

    blind = ScheduledReconciler(
        Reconciler(venue, switch),
        baseline=PortfolioState(),
        interval_seconds=60,
        refresh_order=unreachable,
        on_unavailable=unavailable,
    )
    blind.observe(Order(order_id=uuid4(), request=_request(), status=OrderStatus.OPEN), ())
    assert await blind.run_once() is None
    assert reasons == ["open orders could not be refreshed"]
    assert switch.state is KillSwitchState.HALTED


def _approval(request) -> RiskApproval:
    return RiskApproval(
        signal_id=request.signal_id,
        approved=True,
        reason="unit",
        correlation_id=request.correlation_id,
    )
