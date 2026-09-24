"""Phase 1 gate: restart recovery and the scheduled, broker-authoritative reconciler.

Criteria covered:
- A process restart recovers order and portfolio state correctly.
- The scheduled reconciler runs unattended (the seven-day duration is owner-run). An
  injected divergence in positions, and separately in balances, each produces a
  discrepancy event, an operator alert, and the configured safety trip, and the
  broker's record is the one that survives correction.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from api.alerts import Alert, AlertRouter
from api.operator import OperatorState
from app.main import create_app
from app.recovery import recover_on_startup
from app.trading import CycleStatus
from brokers.simulated import SimulatedBroker, SimulatedFault
from core.guards import CredentialScope, StartupGuardError, StartupSettings
from core.models import KillSwitchState, OrderStatus, Quote, TradingMode, utc_now
from execution.audit import SqlAlchemyAuditStore, unlinked_orders
from execution.engine import ExecutionEngine
from execution.persistence import SqlAlchemyOrderStore
from portfolio.reconciliation import PortfolioState, Reconciler
from portfolio.scheduler import ScheduledReconciler
from risk.kill_switch import KillSwitch
from strategy.reference import MovingAverageCrossStrategy

from tests.gate_support import (
    WHOLE_DOLLAR_RANGE,
    RecordingPortfolioStore,
    paper_cycle,
    sqlite_database,
    state_at,
)

CANDLES = WHOLE_DOLLAR_RANGE


class RecordingSink:
    def __init__(self) -> None:
        self.alerts: list[Alert] = []

    async def send(self, alert: Alert) -> None:
        self.alerts.append(alert)


def quote_for(price: Decimal) -> Quote:
    return Quote(symbol="BTC-USD", bid=price, ask=price, as_of=utc_now(), source="gate")


class TradingHarness:
    """A paper runtime: trading cycle, scheduled reconciler, and operator alerts."""

    def __init__(self, tmp_path, broker: SimulatedBroker | None = None) -> None:
        self.engine, self.session_factory = sqlite_database(tmp_path)
        self.switch_path = tmp_path / "kill-switch.json"
        self.broker = broker or SimulatedBroker(quote_for(CANDLES[0].open))
        self.kill_switch = KillSwitch(self.switch_path)
        self.portfolio = RecordingPortfolioStore(self.session_factory)
        self.phone = RecordingSink()
        self.operator = OperatorState(
            settings=StartupSettings(
                TradingMode.PAPER, CredentialScope.NONE, "", "postgresql://unused", "INFO"
            ),
            kill_switch=self.kill_switch,
            broker=self.broker,
            alert_router=AlertRouter(phone_push=self.phone),
        )
        self.scheduler: ScheduledReconciler | None = None

    async def start(self, *, interval: float = 3600) -> None:
        baseline = PortfolioState(
            positions=await self.broker.get_positions(), balances=await self.broker.get_balances()
        )
        self.portfolio.save_snapshot(baseline, source="broker")

        async def alert(discrepancies) -> None:
            await self.operator.emit_alert(
                Alert(
                    condition="reconciliation_divergence",
                    severity="critical",
                    message=f"{len(discrepancies)} difference(s); trading halted",
                )
            )

        async def unavailable(detail: str) -> None:
            await self.operator.emit_alert(
                Alert(condition="reconciliation_unavailable", severity="critical", message=detail)
            )

        self.scheduler = ScheduledReconciler(
            Reconciler(self.broker, self.kill_switch, store=self.portfolio),
            baseline=baseline,
            interval_seconds=interval,
            on_divergence=alert,
            on_unavailable=unavailable,
        )
        self.store = SqlAlchemyOrderStore(self.session_factory)
        self.audit = SqlAlchemyAuditStore(self.session_factory)
        self.cycle = paper_cycle(
            self.broker,
            MovingAverageCrossStrategy(),
            store=self.store,
            audit=self.audit,
            kill_switch=self.kill_switch,
            engine=ExecutionEngine(self.broker, self.store, on_recorded=self.scheduler.observe),
        )

    async def trade(self, bars) -> list:
        outcomes = []
        for index in bars:
            self.broker.set_quote(quote_for(CANDLES[index + 1].open))
            outcomes.append(await self.cycle.on_market_state(state_at(CANDLES, index)))
        return outcomes


@pytest.mark.asyncio
async def test_scheduled_reconciliation_stays_clean_while_the_cycle_trades(tmp_path) -> None:
    harness = TradingHarness(tmp_path)
    await harness.start()
    assert harness.scheduler is not None

    results = []
    for chunk in (range(0, 16), range(16, 30), range(30, len(CANDLES) - 1)):
        outcomes = await harness.trade(chunk)
        results.append(await harness.scheduler.run_once())
        assert all(item.status is not CycleStatus.HALTED for item in outcomes)

    assert [len(item.discrepancies) for item in results] == [0, 0, 0]
    assert harness.kill_switch.state is KillSwitchState.RUNNING
    assert harness.scheduler.status.clean_runs == 3
    assert harness.phone.alerts == []
    assert harness.portfolio.discrepancies == []
    harness.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("entity", ["position", "balance"])
async def test_injected_divergence_alerts_trips_and_adopts_the_broker_record(
    tmp_path, entity: str
) -> None:
    harness = TradingHarness(tmp_path)
    await harness.start()
    assert harness.scheduler is not None
    await harness.trade(range(0, 16))  # leaves one unit of BTC held
    assert (await harness.scheduler.run_once()).discrepancies == ()

    broker = harness.broker
    if entity == "position":
        quantity, average = broker._positions["BTC-USD"]
        broker._positions["BTC-USD"] = (quantity, average + Decimal("125"))
    else:
        broker._balances["USD"] -= Decimal("2500")

    result = await harness.scheduler.run_once()

    assert result is not None
    assert [item.entity_type for item in result.discrepancies] == [entity]
    assert [item.entity_type for item, _ in harness.portfolio.discrepancies] == [entity]
    assert [action for _, action in harness.portfolio.discrepancies] == ["halted"]
    assert [alert.condition for alert in harness.phone.alerts] == ["reconciliation_divergence"]
    assert harness.operator.to_dict()["alerts"][0]["deliveries"] == [
        {"destination": "phone_push", "status": "sent"}
    ]
    assert harness.kill_switch.state is KillSwitchState.HALTED
    assert KillSwitch(harness.switch_path).state is KillSwitchState.HALTED
    # The broker's record survives: it is the persisted baseline and the next run is clean.
    saved = harness.portfolio.latest_state()
    assert saved is not None
    assert {item.symbol: item.average_price for item in saved.positions} == {
        item.symbol: item.average_price for item in await broker.get_positions()
    }
    assert {item.asset: item.available for item in saved.balances} == {
        item.asset: item.available for item in await broker.get_balances()
    }
    assert (await harness.scheduler.run_once()).discrepancies == ()
    assert harness.kill_switch.state is KillSwitchState.HALTED  # only an operator re-arms
    # New entries stay blocked until then.
    blocked = await harness.trade(range(16, len(CANDLES) - 1))
    assert {item.decision.failed_gate for item in blocked if item.decision} == {"kill_switch"}
    harness.engine.dispose()


@pytest.mark.asyncio
async def test_unattended_loop_keeps_running_through_broker_outages(tmp_path) -> None:
    harness = TradingHarness(tmp_path)
    await harness.start(interval=0.01)
    assert harness.scheduler is not None
    # The venue fails exactly one position read once the loop is running.
    harness.broker._faults["get_positions"].append(SimulatedFault.UNAVAILABLE)
    stop = asyncio.Event()
    task = asyncio.create_task(harness.scheduler.run(stop))
    for _ in range(500):
        await asyncio.sleep(0.01)
        if harness.scheduler.status.clean_runs >= 3:
            break
    stop.set()
    await task

    status = harness.scheduler.status
    assert status.unavailable_runs == 1 and status.clean_runs >= 3
    assert status.runs == status.unavailable_runs + status.clean_runs
    assert [alert.condition for alert in harness.phone.alerts] == ["reconciliation_unavailable"]
    assert harness.kill_switch.state is KillSwitchState.HALTED
    harness.engine.dispose()


@pytest.mark.asyncio
async def test_process_restart_recovers_orders_fills_and_portfolio(tmp_path) -> None:
    harness = TradingHarness(tmp_path)
    await harness.start()
    assert harness.scheduler is not None
    outcomes = await harness.trade(range(0, len(CANDLES) - 1))
    await harness.scheduler.run_once()
    submitted = [item for item in outcomes if item.status is CycleStatus.SUBMITTED]
    before = {str(item.order.request.client_order_id): item.order.status for item in submitted}

    # Restart: only the database, the kill-switch file, and the venue survive.
    restarted_switch = KillSwitch(harness.switch_path)
    result = await recover_on_startup(
        kill_switch=restarted_switch,
        order_store=SqlAlchemyOrderStore(harness.session_factory),
        portfolio_store=RecordingPortfolioStore(harness.session_factory),
        broker=harness.broker,
    )

    assert result.status == "reconciled" and result.discrepancies == 0
    assert restarted_switch.state is KillSwitchState.RUNNING
    reloaded = SqlAlchemyOrderStore(harness.session_factory)
    assert {key: reloaded.get(key).status for key in before} == before
    audit = SqlAlchemyAuditStore(harness.session_factory)
    assert len(audit.lineage()) == len(before) and unlinked_orders(audit) == ()
    for key in before:
        assert harness.broker.acknowledgement_count(key) == 1
    harness.engine.dispose()


@pytest.mark.asyncio
async def test_service_starts_the_scheduled_reconciler_with_a_broker(tmp_path, monkeypatch) -> None:
    engine, session_factory = sqlite_database(tmp_path)
    broker = SimulatedBroker(quote_for(CANDLES[0].open))
    RecordingPortfolioStore(session_factory).save_snapshot(
        PortfolioState(balances=await broker.get_balances()), source="broker"
    )
    engine.dispose()
    monkeypatch.setenv("KILL_SWITCH_FILE", str(tmp_path / "kill-switch.json"))
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("RECONCILE_INTERVAL_SECONDS", "0.02")
    monkeypatch.setenv("OPERATOR_TOKEN", "gate-operator")
    settings = StartupSettings(
        TradingMode.PAPER,
        CredentialScope.NONE,
        "",
        f"sqlite+pysqlite:///{tmp_path / 'trader.db'}",
        "INFO",
    )
    application = create_app(settings, broker=broker, recover_on_start=True)

    async with application.router.lifespan_context(application):
        state = application.state.operator_state
        for _ in range(500):
            await asyncio.sleep(0.01)
            if state.scheduled_reconciliation.status.clean_runs >= 2:
                break
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://operator"
        ) as client:
            snapshot = (
                await client.get("/operator/state", headers={"x-operator-token": "gate-operator"})
            ).json()

    assert snapshot["recovery"]["status"] == "reconciled"
    assert snapshot["reconciliation"]["last_result"] == "clean"
    assert snapshot["reconciliation"]["clean_runs"] >= 2
    assert snapshot["risk"]["kill_switch"] == "running"


@pytest.mark.parametrize("value", ["0", "-5", "soon", "7200"])
def test_reconcile_interval_must_be_a_sane_number(tmp_path, monkeypatch, value) -> None:
    from app.main import start_scheduled_reconciliation

    monkeypatch.setenv("RECONCILE_INTERVAL_SECONDS", value)
    settings = StartupSettings(
        TradingMode.PAPER, CredentialScope.NONE, "", "postgresql://unused", "INFO"
    )
    application = create_app(settings, broker=SimulatedBroker())
    with pytest.raises(StartupGuardError, match="RECONCILE_INTERVAL_SECONDS"):
        start_scheduled_reconciliation(application)


@pytest.mark.asyncio
async def test_the_service_reconciler_follows_persisted_open_orders_and_the_engine(
    tmp_path, monkeypatch
) -> None:
    from app.main import start_scheduled_reconciliation
    from core.models import Fill, OrderRequest, OrderSide, OrderType, RiskApproval

    monkeypatch.setenv("APP_ENV", "test")
    engine, session_factory = sqlite_database(tmp_path)
    orders = SqlAlchemyOrderStore(session_factory)
    request = OrderRequest(
        signal_id=uuid4(),
        strategy_version="gate",
        symbol="BTC-USD",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("1"),
        limit_price=Decimal("100"),
        correlation_id=uuid4(),
    )
    reserved = orders.reserve(
        request,
        RiskApproval(
            signal_id=request.signal_id,
            approved=True,
            reason="gate",
            correlation_id=request.correlation_id,
        ),
    )
    orders.update(reserved.model_copy(update={"status": OrderStatus.PARTIALLY_FILLED}))
    earlier = Fill(
        fill_id="venue-fill-1",
        order_id=request.client_order_id,
        symbol="BTC-USD",
        side=OrderSide.BUY,
        quantity=Decimal("0.4"),
        price=Decimal("100"),
        fee=Decimal("0"),
        fee_asset="USD",
        occurred_at=utc_now(),
    )
    orders.add_fills((earlier,))
    engine.dispose()
    settings = StartupSettings(
        TradingMode.PAPER,
        CredentialScope.NONE,
        "",
        f"sqlite+pysqlite:///{tmp_path / 'trader.db'}",
        "INFO",
    )
    application = create_app(settings, broker=SimulatedBroker())

    stop = start_scheduled_reconciliation(application, interval_seconds=3600)
    assert stop is not None
    scheduler = application.state.operator_state.scheduled_reconciliation
    execution = application.state.execution
    try:
        # Every order the engine records reaches the reconciler, which re-reads open
        # orders through the engine, and trading shares the reconciler's lock.
        assert execution.on_recorded == scheduler.observe
        assert scheduler.refresh_order == execution.recover
        assert application.state.trading_lock is scheduler.lock
        # The persisted open order is followed; its recorded fill is already in the baseline.
        key = str(request.client_order_id)
        assert list(scheduler.expected_state().orders) == [key]
        assert list(scheduler.expected_state().fills) == [earlier.fill_id]
        assert scheduler.expected_state().balances == ()
    finally:
        await stop()


@pytest.mark.asyncio
async def test_a_bad_interval_stops_startup_before_recovery_and_closes_the_broker(
    monkeypatch,
) -> None:
    class ClosingBroker(SimulatedBroker):
        closed = False

        async def close(self) -> None:
            self.closed = True

    monkeypatch.setenv("RECONCILE_INTERVAL_SECONDS", "soon")
    settings = StartupSettings(
        TradingMode.PAPER, CredentialScope.NONE, "", "postgresql://unused", "INFO"
    )
    broker = ClosingBroker()
    application = create_app(settings, broker=broker, recover_on_start=True)

    with pytest.raises(StartupGuardError, match="RECONCILE_INTERVAL_SECONDS"):
        async with application.router.lifespan_context(application):
            pass

    assert application.state.operator_state.startup_recovery is None  # recovery never ran
    assert broker.closed


def test_no_broker_means_no_scheduled_reconciliation() -> None:
    from app.main import start_scheduled_reconciliation

    settings = StartupSettings(
        TradingMode.PAPER, CredentialScope.NONE, "", "postgresql://unused", "INFO"
    )
    assert start_scheduled_reconciliation(create_app(settings)) is None
