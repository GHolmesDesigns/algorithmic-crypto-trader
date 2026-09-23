from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from brokers.simulated import FaultPlan, SimulatedBroker, SimulatedFault, SubmissionTimeoutError
from core.models import (
    KillSwitchState,
    OrderRequest,
    OrderSide,
    OrderType,
    Quote,
    Signal,
)
from db.models import OrderRecord
from execution.engine import ExecutionEngine, InMemoryOrderStore, PersistenceUnavailable
from execution.persistence import SqlAlchemyOrderStore
from risk.engine import ExchangeConstraints, RiskInputs, evaluate
from risk.kill_switch import KillSwitch
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def signal() -> Signal:
    return Signal(
        symbol="BTC-USD",
        side=OrderSide.BUY,
        quantity=Decimal("0.01"),
        strategy_version="strategy-v1",
    )


def safe_inputs(now: datetime) -> RiskInputs:
    return RiskInputs(
        kill_switch=KillSwitchState.RUNNING,
        operator_paused=False,
        trading_window_open=True,
        broker_healthy=True,
        quote=Quote(
            symbol="BTC-USD",
            bid=Decimal("99.99"),
            ask=Decimal("100.01"),
            as_of=now,
            source="fixture",
        ),
        reference_price=Decimal("100"),
        volatility=Decimal("0.01"),
        duplicate_signal_ids=frozenset(),
        symbol_cooldown_clear=True,
        open_positions=0,
        open_notional=Decimal("0"),
        symbol_position=Decimal("0"),
        aggregate_allocation=Decimal("0"),
        available_cash=Decimal("10000"),
        daily_loss=Decimal("0"),
        drawdown=Decimal("0"),
        expected_price=Decimal("100"),
        estimated_slippage=Decimal("0.001"),
        constraints=ExchangeConstraints(
            min_quantity=Decimal("0.001"),
            quantity_increment=Decimal("0.00000001"),
            min_notional=Decimal("1"),
            price_increment=Decimal("0.01"),
        ),
        now=now,
    )


def test_risk_engine_approves_only_when_every_input_is_known() -> None:
    current = datetime.now(UTC)
    approval = evaluate(signal(), safe_inputs(current))
    assert approval.approved is True
    assert approval.failed_gate is None


@pytest.mark.parametrize(
    "field",
    [
        "kill_switch",
        "operator_paused",
        "trading_window_open",
        "broker_healthy",
        "quote",
        "reference_price",
        "volatility",
        "duplicate_signal_ids",
        "symbol_cooldown_clear",
        "open_positions",
        "open_notional",
        "symbol_position",
        "aggregate_allocation",
        "available_cash",
        "daily_loss",
        "drawdown",
        "expected_price",
        "estimated_slippage",
        "constraints",
    ],
)
def test_each_missing_risk_input_fails_closed(field: str) -> None:
    current = datetime.now(UTC)
    approval = evaluate(signal(), safe_inputs(current).model_copy(update={field: None}))
    assert approval.approved is False
    assert approval.failed_gate is not None


def test_order_request_client_id_is_deterministic() -> None:
    signal_id = uuid4()
    values = dict(
        signal_id=signal_id,
        strategy_version="strategy-v1",
        symbol="BTC-USD",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("0.01"),
        correlation_id=uuid4(),
    )
    first = OrderRequest(**values)
    second = OrderRequest(**values)
    assert first.client_order_id == second.client_order_id


@pytest.mark.asyncio
async def test_execution_persists_before_submission_and_recovers_unknown_without_retry() -> None:
    broker = SimulatedBroker(fault_plan=FaultPlan(submit=(SimulatedFault.TIMEOUT,)))
    store = InMemoryOrderStore()
    engine = ExecutionEngine(broker, store)
    request = OrderRequest(
        signal_id=uuid4(),
        strategy_version="strategy-v1",
        symbol="BTC-USD",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("0.01"),
        correlation_id=uuid4(),
    )
    approval = request_approval(request)
    with pytest.raises(SubmissionTimeoutError):
        await engine.submit(request, approval)
    assert store.get(str(request.client_order_id)).status.value == "unknown"
    recovered = await engine.submit(request, approval)
    assert recovered.status.value == "filled"
    assert broker.acknowledgement_count(str(request.client_order_id)) == 1


@pytest.mark.asyncio
async def test_database_unavailability_prevents_unaudited_submission() -> None:
    broker = SimulatedBroker()
    store = InMemoryOrderStore(fail_writes=True)
    engine = ExecutionEngine(broker, store)
    request = OrderRequest(
        signal_id=uuid4(),
        strategy_version="strategy-v1",
        symbol="BTC-USD",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("0.01"),
        correlation_id=uuid4(),
    )
    with pytest.raises(PersistenceUnavailable):
        await engine.submit(request, request_approval(request))
    assert broker.acknowledgement_count(str(request.client_order_id)) == 0


def request_approval(request: OrderRequest):
    from core.models import RiskApproval

    return RiskApproval(
        signal_id=request.signal_id,
        approved=True,
        reason="all gates passed",
        correlation_id=request.correlation_id,
    )


def test_kill_switch_persists_and_requires_manual_rearm(tmp_path) -> None:
    path = tmp_path / "kill-switch.json"
    switch = KillSwitch(path)
    switch.set_state(KillSwitchState.PAUSED, reason="operator pause")
    assert KillSwitch(path).state is KillSwitchState.PAUSED
    switch.trip("automatic risk trip")
    with pytest.raises(PermissionError):
        switch.set_state(KillSwitchState.RUNNING, automatic=True)
    switch.set_state(KillSwitchState.RUNNING, reason="manual re-arm")
    assert switch.state is KillSwitchState.RUNNING


def test_kill_switch_reads_file_and_environment_actuation(tmp_path) -> None:
    switch = KillSwitch(tmp_path / "kill-switch.json")
    assert (
        switch.sync_external_state(env={"TRADING_KILL_SWITCH": "paused"}) is KillSwitchState.PAUSED
    )
    flag = tmp_path / "operator.flag"
    flag.write_text("halted", encoding="utf-8")
    assert switch.sync_external_state(env={}, flag_path=flag) is KillSwitchState.HALTED


def test_sqlalchemy_order_store_persists_and_reloads_pending_order() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    OrderRecord.__table__.create(engine)
    store = SqlAlchemyOrderStore(sessionmaker(bind=engine, expire_on_commit=False))
    request = OrderRequest(
        signal_id=uuid4(),
        strategy_version="strategy-v1",
        symbol="BTC-USD",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("0.01"),
        correlation_id=uuid4(),
    )
    persisted = store.reserve(request, request_approval(request))
    assert persisted.status.value == "pending_submit"
    assert store.get(str(request.client_order_id)).request == request
    assert len(store.pending()) == 1
    engine.dispose()
