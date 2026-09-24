"""Phase 1 gate: audit lineage, ambiguous submissions, SIGKILL, and database outages.

Criteria covered:
- Every order is linked to a signal, strategy version, risk decision, and fill record.
- No ambiguous timeout can create a duplicate order.
- SIGKILL between the pre-submit persist and the API call recovers correctly: the
  PENDING_SUBMIT row is resolved by querying the venue, and the position is single.
- With the database unavailable no order is submitted, the system halts rather than
  trading un-audited, and it recovers cleanly when the database returns.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from app.recovery import recover_on_startup
from app.trading import CycleStatus
from brokers.simulated import (
    FaultPlan,
    SimulatedBroker,
    SimulatedFault,
    SubmissionTimeoutError,
)
from core.models import (
    KillSwitchState,
    Order,
    OrderRequest,
    OrderStatus,
    Quote,
    RiskApproval,
    utc_now,
)
from db.models import FillRecord, OrderRecord, RiskDecisionRecord, SignalRecord
from execution.audit import SqlAlchemyAuditStore, unlinked_orders
from execution.engine import ExecutionEngine
from execution.persistence import SqlAlchemyOrderStore
from portfolio.reconciliation import PortfolioState
from risk.kill_switch import KillSwitch
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from strategy.reference import MovingAverageCrossStrategy

from tests.gate_support import (
    PARITY_WINDOWS,
    RecordingPortfolioStore,
    paper_cycle,
    sqlite_database,
    state_at,
)
from tests.sigkill_child import APPROVAL, REQUEST, SIGNAL, JsonFileVenue

ROOT = Path(__file__).parents[1]
CANDLES = PARITY_WINDOWS["calm_range"]


def quote_for(price: Decimal) -> Quote:
    return Quote(symbol="BTC-USD", bid=price, ask=price, as_of=utc_now(), source="gate")


def signal_bars(candles=CANDLES) -> list[int]:
    strategy = MovingAverageCrossStrategy()
    return [
        index
        for index in range(len(candles) - 1)
        if strategy.on_market_state(state_at(candles, index)) is not None
    ]


async def drive(cycle, broker: SimulatedBroker, candles=CANDLES, bars=None):
    outcomes = []
    for index in bars if bars is not None else range(len(candles) - 1):
        broker.set_quote(quote_for(candles[index + 1].open))
        outcomes.append(await cycle.on_market_state(state_at(candles, index)))
    return outcomes


def count_rows(session_factory, model) -> int:
    with session_factory() as session:
        return session.scalar(select(func.count()).select_from(model))


@pytest.mark.asyncio
async def test_every_order_links_to_signal_strategy_version_risk_decision_and_fills(
    tmp_path,
) -> None:
    engine, session_factory = sqlite_database(tmp_path)
    broker = SimulatedBroker(quote_for(CANDLES[0].open))
    store = SqlAlchemyOrderStore(session_factory)
    audit = SqlAlchemyAuditStore(session_factory)
    cycle = paper_cycle(broker, MovingAverageCrossStrategy(), store=store, audit=audit)

    outcomes = await drive(cycle, broker)
    # Replaying an already-traded bar re-emits its signal, which the risk engine refuses.
    outcomes += await drive(cycle, broker, bars=[signal_bars()[0]])

    submitted = [item for item in outcomes if item.status is CycleStatus.SUBMITTED]
    refused = [item for item in outcomes if item.status is CycleStatus.REFUSED]
    assert len(submitted) >= 4
    assert [item.decision.failed_gate for item in refused] == ["duplicate_prevention"]
    lineage = audit.lineage()
    assert len(lineage) == len(submitted) == count_rows(session_factory, OrderRecord)
    assert unlinked_orders(audit) == ()
    assert all(item.fill_count == 1 and item.risk_decision_approved for item in lineage)
    # Refusals are recorded too: one decision per evaluation, approved or not.
    distinct_signals = {item.signal.signal_id for item in submitted + refused}
    assert count_rows(session_factory, SignalRecord) == len(distinct_signals)
    assert count_rows(session_factory, RiskDecisionRecord) == len(submitted) + len(refused)
    with session_factory() as session:
        refused_ids = set(
            session.scalars(select(RiskDecisionRecord.signal_id).filter_by(approved=False))
        )
    assert refused_ids == {item.signal.signal_id for item in refused}
    engine.dispose()


def test_lineage_reports_orders_that_bypassed_the_audit_path(tmp_path) -> None:
    engine, session_factory = sqlite_database(tmp_path)
    store = SqlAlchemyOrderStore(session_factory)
    audit = SqlAlchemyAuditStore(session_factory)
    request = REQUEST.model_copy(update={"signal_id": uuid4(), "client_order_id": uuid4()})
    approval = APPROVAL.model_copy(update={"approval_id": uuid4(), "signal_id": request.signal_id})
    order = store.reserve(request, approval)
    store.update(order.model_copy(update={"status": OrderStatus.FILLED}))

    (gap,) = unlinked_orders(audit)

    assert gap.gaps == ("signal", "risk_decision", "fills")
    audit.record_signal(SIGNAL.model_copy(update={"signal_id": request.signal_id}))
    audit.record_risk_decision(approval)
    assert unlinked_orders(audit)[0].gaps == ("fills",)
    engine.dispose()


@pytest.mark.asyncio
async def test_ambiguous_timeout_is_resolved_by_query_and_never_duplicated(tmp_path) -> None:
    engine, session_factory = sqlite_database(tmp_path)
    broker = SimulatedBroker(
        quote_for(CANDLES[0].open), fault_plan=FaultPlan(submit=(SimulatedFault.TIMEOUT,))
    )
    store = SqlAlchemyOrderStore(session_factory)
    audit = SqlAlchemyAuditStore(session_factory)
    cycle = paper_cycle(broker, MovingAverageCrossStrategy(), store=store, audit=audit)
    first, second, *_ = signal_bars()

    (ambiguous,) = await drive(cycle, broker, bars=[first])
    assert ambiguous.status is CycleStatus.AMBIGUOUS
    client_order_id = str(ambiguous.order.request.client_order_id)
    assert store.get(client_order_id).status is OrderStatus.UNKNOWN

    # The next loop queries the venue by client_order_id before anything else.
    (resolved,) = await drive(cycle, broker, bars=[second])
    assert store.get(client_order_id).status is OrderStatus.FILLED
    assert resolved.status is CycleStatus.SUBMITTED
    assert broker.acknowledgement_count(client_order_id) == 1

    # Replaying the original signal after a restart reuses the deterministic ID.
    restarted = paper_cycle(
        broker,
        MovingAverageCrossStrategy(),
        store=SqlAlchemyOrderStore(session_factory),
        audit=audit,
    )
    await drive(restarted, broker, bars=[first])
    assert broker.acknowledgement_count(client_order_id) == 1
    assert count_rows(session_factory, OrderRecord) == 2
    assert unlinked_orders(audit) == ()
    engine.dispose()


class LostSubmissionBroker(SimulatedBroker):
    """A timeout where the request never reached the venue."""

    async def submit_order(self, request: OrderRequest, approval: RiskApproval) -> Order:
        raise SubmissionTimeoutError(
            Order(order_id=request.client_order_id, request=request, status=OrderStatus.UNKNOWN)
        )


@pytest.mark.asyncio
async def test_unconfirmed_submission_blocks_new_entries_and_halts_on_restart(tmp_path) -> None:
    engine, session_factory = sqlite_database(tmp_path)
    broker = LostSubmissionBroker(quote_for(CANDLES[0].open))
    store = SqlAlchemyOrderStore(session_factory)
    cycle = paper_cycle(
        broker,
        MovingAverageCrossStrategy(),
        store=store,
        audit=SqlAlchemyAuditStore(session_factory),
    )
    first, second, third, *_ = signal_bars()

    outcomes = await drive(cycle, broker, bars=[first, second, third])

    assert [item.status for item in outcomes] == [
        CycleStatus.AMBIGUOUS,
        CycleStatus.UNRESOLVED,
        CycleStatus.UNRESOLVED,
    ]
    assert count_rows(session_factory, OrderRecord) == 1
    assert await broker.get_positions() == ()
    switch = KillSwitch(tmp_path / "kill-switch.json")
    result = await recover_on_startup(
        kill_switch=switch,
        order_store=SqlAlchemyOrderStore(session_factory),
        portfolio_store=RecordingPortfolioStore(session_factory),
        broker=broker,
    )
    assert result.status == "halted" and "no record" in result.detail
    assert switch.state is KillSwitchState.HALTED
    engine.dispose()


def run_child(database: Path, venue: Path, kill_point: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "tests.sigkill_child", str(database), str(venue), kill_point],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
        timeout=120,
    )


def assert_hard_killed(result: subprocess.CompletedProcess[str]) -> None:
    if hasattr(signal, "SIGKILL"):
        assert result.returncode == -signal.SIGKILL, result.stderr
    else:
        assert result.returncode == signal.SIGTERM, result.stderr


async def save_baseline(session_factory, venue: JsonFileVenue) -> None:
    RecordingPortfolioStore(session_factory).save_snapshot(
        PortfolioState(positions=await venue.get_positions(), balances=await venue.get_balances()),
        source="broker",
    )


@pytest.mark.asyncio
async def test_sigkill_after_venue_accepts_resolves_by_query_to_a_single_position(tmp_path) -> None:
    engine, session_factory = sqlite_database(tmp_path)
    venue_path = tmp_path / "venue.json"
    await save_baseline(session_factory, JsonFileVenue(venue_path))

    assert_hard_killed(run_child(tmp_path / "trader.db", venue_path, "after_venue"))

    client_order_id = str(REQUEST.client_order_id)
    assert SqlAlchemyOrderStore(session_factory).get(client_order_id).status is (
        OrderStatus.PENDING_SUBMIT
    )
    venue = JsonFileVenue(venue_path)
    switch_path = tmp_path / "kill-switch.json"
    portfolio = RecordingPortfolioStore(session_factory)
    first = await recover_on_startup(
        kill_switch=KillSwitch(switch_path),
        order_store=SqlAlchemyOrderStore(session_factory),
        portfolio_store=portfolio,
        broker=venue,
    )

    # Resolved by client_order_id, never resubmitted, and recorded with its fill.
    assert first.recovered_orders == 1
    assert SqlAlchemyOrderStore(session_factory).get(client_order_id).status is OrderStatus.FILLED
    assert venue.state()["submissions"] == 1
    assert [(item.symbol, item.quantity) for item in await venue.get_positions()] == [
        ("BTC-USD", Decimal("0.5"))
    ]
    with session_factory() as session:
        assert session.scalar(select(FillRecord.order_id)) == REQUEST.client_order_id
    assert unlinked_orders(SqlAlchemyAuditStore(session_factory)) == ()
    # The fill landed after the last baseline, so recovery halts for review and adopts
    # the venue's record; after an operator re-arm the next restart reconciles cleanly.
    assert first.status == "halted"
    assert portfolio.latest_state().positions[0].quantity == Decimal("0.5")
    KillSwitch(switch_path).set_state(KillSwitchState.RUNNING, reason="manual re-arm")
    second = await recover_on_startup(
        kill_switch=KillSwitch(switch_path),
        order_store=SqlAlchemyOrderStore(session_factory),
        portfolio_store=RecordingPortfolioStore(session_factory),
        broker=JsonFileVenue(venue_path),
    )
    assert second.status == "reconciled"
    assert KillSwitch(switch_path).state is KillSwitchState.RUNNING
    engine.dispose()


@pytest.mark.asyncio
async def test_sigkill_before_the_venue_call_leaves_no_order_and_halts(tmp_path) -> None:
    engine, session_factory = sqlite_database(tmp_path)
    venue_path = tmp_path / "venue.json"
    await save_baseline(session_factory, JsonFileVenue(venue_path))

    assert_hard_killed(run_child(tmp_path / "trader.db", venue_path, "before_venue"))

    venue = JsonFileVenue(venue_path)
    switch = KillSwitch(tmp_path / "kill-switch.json")
    result = await recover_on_startup(
        kill_switch=switch,
        order_store=SqlAlchemyOrderStore(session_factory),
        portfolio_store=RecordingPortfolioStore(session_factory),
        broker=venue,
    )

    assert venue.state()["submissions"] == 0
    assert await venue.get_positions() == ()
    assert result.status == "halted" and "no record" in result.detail
    assert switch.state is KillSwitchState.HALTED
    # A retry of the same request queries first and then submits exactly once.
    await ExecutionEngine(venue, SqlAlchemyOrderStore(session_factory)).submit(REQUEST, APPROVAL)
    await ExecutionEngine(venue, SqlAlchemyOrderStore(session_factory)).submit(REQUEST, APPROVAL)
    assert venue.state()["submissions"] == 1
    assert [item.quantity for item in await venue.get_positions()] == [Decimal("0.5")]
    engine.dispose()


class OutageSessionFactory:
    """Wraps a session factory; while ``down`` every connection attempt fails."""

    def __init__(self, session_factory) -> None:
        self.session_factory = session_factory
        self.down = False

    def __call__(self):
        if self.down:
            raise OperationalError("connect", {}, ConnectionRefusedError("database unavailable"))
        return self.session_factory()


@pytest.mark.asyncio
async def test_database_outage_halts_before_any_submission_and_recovers(tmp_path) -> None:
    engine, session_factory = sqlite_database(tmp_path)
    database = OutageSessionFactory(session_factory)
    broker = SimulatedBroker(quote_for(CANDLES[0].open))
    await save_baseline_for(session_factory, broker)
    switch_path = tmp_path / "kill-switch.json"
    audit = SqlAlchemyAuditStore(database)
    cycle = paper_cycle(
        broker,
        MovingAverageCrossStrategy(),
        store=SqlAlchemyOrderStore(database),
        audit=audit,
        kill_switch=KillSwitch(switch_path),
    )
    first, second, third, *_ = signal_bars()

    database.down = True
    (outage,) = await drive(cycle, broker, bars=[first])

    assert outage.status is CycleStatus.HALTED
    assert KillSwitch(switch_path).state is KillSwitchState.HALTED
    assert await broker.get_positions() == ()

    # The database returns; restart recovery reconciles and the halt waits for an operator.
    database.down = False
    recovery = await recover_on_startup(
        kill_switch=KillSwitch(switch_path),
        order_store=SqlAlchemyOrderStore(session_factory),
        portfolio_store=RecordingPortfolioStore(session_factory),
        broker=broker,
    )
    assert recovery.status == "reconciled"
    assert KillSwitch(switch_path).state is KillSwitchState.HALTED
    refused = await drive(cycle, broker, bars=[second])
    assert refused[0].status is CycleStatus.REFUSED
    assert refused[0].decision.failed_gate == "kill_switch"

    cycle.kill_switch.set_state(KillSwitchState.RUNNING, reason="manual re-arm")
    (resumed,) = await drive(cycle, broker, bars=[third])
    assert resumed.status is CycleStatus.SUBMITTED
    assert unlinked_orders(audit) == ()
    engine.dispose()


async def save_baseline_for(session_factory, broker) -> None:
    RecordingPortfolioStore(session_factory).save_snapshot(
        PortfolioState(
            positions=await broker.get_positions(), balances=await broker.get_balances()
        ),
        source="broker",
    )


@pytest.mark.asyncio
async def test_database_loss_between_audit_and_order_record_never_reaches_the_broker(
    tmp_path,
) -> None:
    engine, session_factory = sqlite_database(tmp_path)
    orders_database = OutageSessionFactory(session_factory)
    audit = SqlAlchemyAuditStore(session_factory)
    record_decision = audit.record_risk_decision

    def decision_then_outage(decision) -> None:
        record_decision(decision)
        orders_database.down = True  # the connection drops right after the decision

    audit.record_risk_decision = decision_then_outage  # type: ignore[method-assign]
    broker = SimulatedBroker(quote_for(CANDLES[0].open))
    cycle = paper_cycle(
        broker,
        MovingAverageCrossStrategy(),
        store=SqlAlchemyOrderStore(orders_database),
        audit=audit,
    )
    first = signal_bars()[0]

    (outcome,) = await drive(cycle, broker, bars=[first])

    assert outcome.status is CycleStatus.HALTED
    assert "un-audited" in outcome.detail
    assert cycle.kill_switch.state is KillSwitchState.HALTED
    assert await broker.get_positions() == ()
    assert count_rows(session_factory, OrderRecord) == 0
    assert count_rows(session_factory, RiskDecisionRecord) == 1
    engine.dispose()


@pytest.mark.asyncio
async def test_database_loss_after_the_broker_call_is_resolved_on_restart(tmp_path) -> None:
    engine, session_factory = sqlite_database(tmp_path)
    database = OutageSessionFactory(session_factory)
    broker = SimulatedBroker(quote_for(CANDLES[0].open))
    await save_baseline_for(session_factory, broker)
    submit = broker.submit_order

    async def submit_then_outage(request, approval):
        order = await submit(request, approval)
        database.down = True  # the venue filled it; recording the result fails
        return order

    broker.submit_order = submit_then_outage  # type: ignore[method-assign]
    store = SqlAlchemyOrderStore(database)
    cycle = paper_cycle(
        broker, MovingAverageCrossStrategy(), store=store, audit=SqlAlchemyAuditStore(database)
    )

    (outcome,) = await drive(cycle, broker, bars=[signal_bars()[0]])

    assert outcome.status is CycleStatus.HALTED
    database.down = False
    pending = SqlAlchemyOrderStore(session_factory).pending()
    assert [item.status for item in pending] == [OrderStatus.PENDING_SUBMIT]
    result = await recover_on_startup(
        kill_switch=KillSwitch(tmp_path / "kill-switch.json"),
        order_store=SqlAlchemyOrderStore(session_factory),
        portfolio_store=RecordingPortfolioStore(session_factory),
        broker=broker,
    )
    client_order_id = str(pending[0].request.client_order_id)
    assert result.recovered_orders == 1
    assert SqlAlchemyOrderStore(session_factory).get(client_order_id).status is OrderStatus.FILLED
    assert broker.acknowledgement_count(client_order_id) == 1
    assert [item.quantity for item in await broker.get_positions()] == [Decimal("1")]
    engine.dispose()
