from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from app.main import create_app, run_startup_recovery
from app.recovery import recover_on_startup
from brokers.simulated import FaultPlan, SimulatedBroker, SimulatedFault, SubmissionTimeoutError
from core.guards import CredentialScope, StartupSettings
from core.models import (
    KillSwitchState,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    Quote,
    RiskApproval,
    TradingMode,
    utc_now,
)
from db.models import (
    BalanceSnapshotRecord,
    EquitySnapshotRecord,
    FillRecord,
    OrderRecord,
    PortfolioSnapshotRecord,
    PositionSnapshotRecord,
)
from execution.engine import ExecutionEngine
from execution.persistence import SqlAlchemyOrderStore
from portfolio.reconciliation import PortfolioState, Reconciler
from portfolio.store import SqlAlchemyPortfolioStore
from risk.kill_switch import KillSwitch
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

TABLES = (
    OrderRecord.__table__,
    FillRecord.__table__,
    PortfolioSnapshotRecord.__table__,
    PositionSnapshotRecord.__table__,
    BalanceSnapshotRecord.__table__,
    EquitySnapshotRecord.__table__,
)


class RecordingPortfolioStore(SqlAlchemyPortfolioStore):
    """SQLite cannot render the JSONB discrepancy table, so discrepancies stay in memory."""

    def __init__(self, session_factory) -> None:
        super().__init__(session_factory)
        self.discrepancies: list[tuple[object, str]] = []

    def save_discrepancy(self, discrepancy, *, safety_action: str) -> None:
        self.discrepancies.append((discrepancy, safety_action))


class FailingOrderStore:
    def pending(self):
        raise RuntimeError("database is unavailable")


def database(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'trader.db'}", future=True)
    for table in TABLES:
        table.create(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def market_order() -> tuple[OrderRequest, RiskApproval]:
    request = OrderRequest(
        signal_id=uuid4(),
        strategy_version="strategy-v1",
        symbol="BTC-USD",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("0.01"),
        correlation_id=uuid4(),
    )
    approval = RiskApproval(
        signal_id=request.signal_id,
        approved=True,
        reason="all gates passed",
        correlation_id=request.correlation_id,
    )
    return request, approval


def reserve_pending(session_factory) -> OrderRequest:
    request, approval = market_order()
    SqlAlchemyOrderStore(session_factory).reserve(request, approval)
    return request


@pytest.mark.asyncio
async def test_restart_reloads_persisted_state_and_stays_running(tmp_path) -> None:
    engine, session_factory = database(tmp_path)
    # SQLite stores Numeric as float; these prices keep balances exact (PostgreSQL is exact).
    broker = SimulatedBroker(
        Quote(
            symbol="BTC-USD",
            bid=Decimal("59999"),
            ask=Decimal("60000"),
            as_of=utc_now(),
            source="fixture",
        )
    )
    switch_path = tmp_path / "kill-switch.json"
    request, approval = market_order()
    await ExecutionEngine(broker, SqlAlchemyOrderStore(session_factory)).submit(request, approval)
    before_restart = PortfolioState(
        positions=await broker.get_positions(), balances=await broker.get_balances()
    )
    await Reconciler(
        broker, KillSwitch(switch_path), store=SqlAlchemyPortfolioStore(session_factory)
    ).reconcile(before_restart)

    # Simulated restart: every process-local object is rebuilt from durable state.
    restarted_switch = KillSwitch(switch_path)
    portfolio_store = RecordingPortfolioStore(session_factory)
    result = await recover_on_startup(
        kill_switch=restarted_switch,
        order_store=SqlAlchemyOrderStore(session_factory),
        portfolio_store=portfolio_store,
        broker=broker,
    )

    assert result.status == "reconciled"
    assert result.discrepancies == 0
    assert restarted_switch.state is KillSwitchState.RUNNING
    assert portfolio_store.discrepancies == []
    baseline = portfolio_store.latest_state()
    assert baseline is not None
    assert [(item.symbol, item.quantity) for item in baseline.positions] == [
        ("BTC-USD", Decimal("0.01"))
    ]
    assert broker.acknowledgement_count(str(request.client_order_id)) == 1
    engine.dispose()


@pytest.mark.asyncio
async def test_restart_resolves_unknown_order_by_client_order_id_without_resubmitting(
    tmp_path,
) -> None:
    engine, session_factory = database(tmp_path)
    broker = SimulatedBroker(fault_plan=FaultPlan(submit=(SimulatedFault.TIMEOUT,)))
    SqlAlchemyPortfolioStore(session_factory).save_snapshot(
        PortfolioState(balances=await broker.get_balances()), source="broker"
    )
    request, approval = market_order()
    with pytest.raises(SubmissionTimeoutError):
        await ExecutionEngine(broker, SqlAlchemyOrderStore(session_factory)).submit(
            request, approval
        )

    switch_path = tmp_path / "kill-switch.json"
    result = await recover_on_startup(
        kill_switch=KillSwitch(switch_path),
        order_store=SqlAlchemyOrderStore(session_factory),
        portfolio_store=RecordingPortfolioStore(session_factory),
        broker=broker,
    )

    assert result.recovered_orders == 1
    assert broker.acknowledgement_count(str(request.client_order_id)) == 1
    stored = SqlAlchemyOrderStore(session_factory).get(str(request.client_order_id))
    assert stored is not None and stored.status is OrderStatus.FILLED
    with session_factory() as session:
        assert session.scalar(select(FillRecord.order_id)) == request.client_order_id
    # The fill moved the portfolio after the last baseline, so new entries wait for review.
    assert result.status == "halted"
    assert result.discrepancies > 0
    assert KillSwitch(switch_path).state is KillSwitchState.HALTED
    engine.dispose()


@pytest.mark.asyncio
async def test_pending_order_unknown_to_broker_halts_persistently(tmp_path) -> None:
    engine, session_factory = database(tmp_path)
    reserve_pending(session_factory)
    switch_path = tmp_path / "kill-switch.json"

    result = await recover_on_startup(
        kill_switch=KillSwitch(switch_path),
        order_store=SqlAlchemyOrderStore(session_factory),
        portfolio_store=RecordingPortfolioStore(session_factory),
        broker=SimulatedBroker(),
    )

    assert result.status == "halted"
    assert "no record" in result.detail
    assert KillSwitch(switch_path).state is KillSwitchState.HALTED
    engine.dispose()


@pytest.mark.asyncio
async def test_portfolio_divergence_from_persisted_baseline_halts(tmp_path) -> None:
    engine, session_factory = database(tmp_path)
    broker = SimulatedBroker()
    stale = Position(
        symbol="BTC-USD", quantity=Decimal("1"), average_price=Decimal("60000"), as_of=utc_now()
    )
    SqlAlchemyPortfolioStore(session_factory).save_snapshot(
        PortfolioState(positions=(stale,), balances=await broker.get_balances()), source="broker"
    )
    switch = KillSwitch()
    portfolio_store = RecordingPortfolioStore(session_factory)

    result = await recover_on_startup(
        kill_switch=switch,
        order_store=SqlAlchemyOrderStore(session_factory),
        portfolio_store=portfolio_store,
        broker=broker,
    )

    assert result.status == "halted"
    assert result.discrepancies == 1
    assert [item.entity_type for item, _ in portfolio_store.discrepancies] == ["position"]
    assert switch.state is KillSwitchState.HALTED
    engine.dispose()


@pytest.mark.asyncio
async def test_missing_portfolio_baseline_fails_closed(tmp_path) -> None:
    engine, session_factory = database(tmp_path)
    switch = KillSwitch()

    result = await recover_on_startup(
        kill_switch=switch,
        order_store=SqlAlchemyOrderStore(session_factory),
        portfolio_store=RecordingPortfolioStore(session_factory),
        broker=SimulatedBroker(),
    )

    assert result.status == "halted"
    assert "no persisted portfolio baseline" in result.detail
    assert switch.state is KillSwitchState.HALTED
    engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault_plan",
    [FaultPlan(get_order=(SimulatedFault.UNAVAILABLE,)), FaultPlan(unavailable=True)],
)
async def test_broker_unavailable_during_recovery_halts(tmp_path, fault_plan) -> None:
    engine, session_factory = database(tmp_path)
    if fault_plan.get_order:
        reserve_pending(session_factory)
    switch = KillSwitch()

    result = await recover_on_startup(
        kill_switch=switch,
        order_store=SqlAlchemyOrderStore(session_factory),
        portfolio_store=RecordingPortfolioStore(session_factory),
        broker=SimulatedBroker(fault_plan=fault_plan),
    )

    assert result.status == "halted"
    assert switch.state is KillSwitchState.HALTED
    engine.dispose()


@pytest.mark.asyncio
async def test_unreadable_order_store_halts() -> None:
    switch = KillSwitch()
    result = await recover_on_startup(
        kill_switch=switch,
        order_store=FailingOrderStore(),  # type: ignore[arg-type]
        portfolio_store=SqlAlchemyPortfolioStore(None),
        broker=SimulatedBroker(),
    )
    assert result.status == "halted"
    assert switch.state is KillSwitchState.HALTED


@pytest.mark.asyncio
async def test_without_broker_pending_orders_halt_and_clean_state_does_not(tmp_path) -> None:
    engine, session_factory = database(tmp_path)
    clean_switch = KillSwitch()
    clean = await recover_on_startup(
        kill_switch=clean_switch,
        order_store=SqlAlchemyOrderStore(session_factory),
        portfolio_store=RecordingPortfolioStore(session_factory),
        broker=None,
    )
    assert clean.status == "no_broker"
    assert clean_switch.state is KillSwitchState.RUNNING

    reserve_pending(session_factory)
    switch = KillSwitch()
    result = await recover_on_startup(
        kill_switch=switch,
        order_store=SqlAlchemyOrderStore(session_factory),
        portfolio_store=RecordingPortfolioStore(session_factory),
        broker=None,
    )
    assert result.status == "halted"
    assert result.pending_orders == 1
    assert switch.state is KillSwitchState.HALTED
    engine.dispose()


@pytest.mark.asyncio
async def test_service_startup_runs_recovery_and_reports_it(tmp_path, monkeypatch) -> None:
    engine, session_factory = database(tmp_path)
    reserve_pending(session_factory)
    engine.dispose()
    switch_path = tmp_path / "kill-switch.json"
    monkeypatch.setenv("KILL_SWITCH_FILE", str(switch_path))
    settings = StartupSettings(
        TradingMode.BACKTEST,
        CredentialScope.NONE,
        "",
        f"sqlite+pysqlite:///{tmp_path / 'trader.db'}",
        "INFO",
    )
    application = create_app(settings)

    result = await run_startup_recovery(application)

    assert result.status == "halted"
    snapshot = application.state.operator_state.to_dict()
    assert snapshot["recovery"]["status"] == "halted"
    assert snapshot["risk"]["kill_switch"] == "halted"
    assert KillSwitch(switch_path).state is KillSwitchState.HALTED


def test_latest_snapshot_batch_includes_an_empty_position_set(tmp_path) -> None:
    engine, session_factory = database(tmp_path)
    store = SqlAlchemyPortfolioStore(session_factory)
    held = Position(
        symbol="BTC-USD", quantity=Decimal("1"), average_price=Decimal("60000"), as_of=utc_now()
    )
    assert store.latest_state() is None
    store.save_snapshot(PortfolioState(positions=(held,)), source="broker")
    store.save_snapshot(PortfolioState(), source="broker")
    latest = store.latest_state()
    assert latest is not None and latest.positions == ()
    with session_factory() as session:
        assert isinstance(session.scalar(select(PositionSnapshotRecord.batch_id)), UUID)
    engine.dispose()
