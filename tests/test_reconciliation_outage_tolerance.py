"""A brief broker outage must not halt trading; a failure retrying cannot fix still does (#196)."""

import asyncio
from decimal import Decimal
from uuid import uuid4

import pytest
from app.main import DEFAULT_RECONCILE_HALT_AFTER, _reconcile_halt_after, create_app
from brokers.http import (
    ProviderError,
    ProviderHTTPError,
    ProviderTimeoutError,
    ProviderTransportError,
    is_transient_provider_failure,
)
from brokers.simulated import BrokerUnavailableError, RateLimitError, SimulatedBroker
from core.guards import CredentialScope, StartupGuardError, StartupSettings
from core.models import (
    KillSwitchState,
    Order,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    TradingMode,
)
from core.resilience import CircuitOpen
from portfolio.reconciliation import PortfolioState, Reconciler, ReconciliationUnavailable
from portfolio.scheduler import ScheduledReconciler
from risk.kill_switch import KillSwitch

from tests.gate_support import RecordingPortfolioStore, sqlite_database


def maintenance(status: int = 503) -> ProviderHTTPError:
    return ProviderHTTPError(
        status, f"provider returned HTTP {status}", payload={"reason": "Maintenance"}
    )


class RecordingSwitch(KillSwitch):
    """A kill switch that remembers why it was tripped."""

    def __init__(self) -> None:
        super().__init__()
        self.reasons: list[str] = []

    def trip(self, reason: str) -> None:
        self.reasons.append(reason)
        super().trip(reason)


class FlakyBroker(SimulatedBroker):
    """A venue whose reads fail on demand: a queue of errors, or down until told otherwise."""

    def __init__(self) -> None:
        super().__init__()
        self.balance_errors: list[Exception] = []
        self.down: Exception | None = None
        self.order_errors: list[Exception] = []
        self.fill_errors: list[Exception] = []
        self.orders: dict[str, Order] = {}

    async def get_balances(self):
        if self.down is not None:
            raise self.down
        if self.balance_errors:
            raise self.balance_errors.pop(0)
        return await super().get_balances()

    async def get_order(self, client_order_id: str):
        if self.order_errors:
            raise self.order_errors.pop(0)
        return self.orders.get(client_order_id)

    async def get_fills(self, client_order_id: str):
        if self.fill_errors:
            raise self.fill_errors.pop(0)
        return ()


def tracked_order() -> Order:
    request = OrderRequest(
        signal_id=uuid4(),
        strategy_version="outage",
        symbol="BTC-USD",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("0.0001"),
        correlation_id=uuid4(),
    )
    return Order(order_id=uuid4(), request=request, status=OrderStatus.OPEN)


async def scheduler_for(
    broker: SimulatedBroker,
    switch: KillSwitch,
    details: list[str],
    **options,
) -> ScheduledReconciler:
    async def unavailable(detail: str) -> None:
        details.append(detail)

    return ScheduledReconciler(
        Reconciler(broker, switch),
        baseline=PortfolioState(
            balances=await broker.get_balances(), positions=await broker.get_positions()
        ),
        interval_seconds=60,
        on_unavailable=unavailable,
        **options,
    )


@pytest.mark.parametrize(
    "error",
    [
        maintenance(503),
        maintenance(500),
        maintenance(502),
        maintenance(429),
        maintenance(408),
        ProviderTimeoutError("timed out"),
        ProviderTransportError("connection dropped"),
        CircuitOpen("circuit is open"),
        BrokerUnavailableError(),
        RateLimitError(),
    ],
)
def test_an_outage_is_transient(error: Exception) -> None:
    assert is_transient_provider_failure(error) is True


@pytest.mark.parametrize(
    "error",
    [
        ProviderHTTPError(400, "bad request"),
        ProviderHTTPError(401, "unauthorized"),
        ProviderHTTPError(403, "forbidden"),
        ProviderHTTPError(404, "not found"),
        ProviderError("unexpected response shape"),
        ValueError("balance was not a number"),
        KeyError("amount"),
        RuntimeError("something else"),
    ],
)
def test_a_failure_retrying_cannot_fix_is_not_transient(error: Exception) -> None:
    assert is_transient_provider_failure(error) is False


@pytest.mark.asyncio
async def test_the_reconciler_still_halts_on_a_transient_failure_by_default() -> None:
    broker, switch = FlakyBroker(), RecordingSwitch()
    broker.balance_errors = [maintenance()]
    with pytest.raises(ReconciliationUnavailable) as raised:
        await Reconciler(broker, switch).reconcile(PortfolioState())
    assert raised.value.transient is True
    assert switch.state is KillSwitchState.HALTED
    assert switch.reasons == ["broker unavailable during reconciliation"]


@pytest.mark.asyncio
async def test_one_503_leaves_trading_running_and_is_reported() -> None:
    broker, switch, details = FlakyBroker(), RecordingSwitch(), []
    scheduler = await scheduler_for(broker, switch, details, unavailable_threshold=3)
    broker.balance_errors = [maintenance()]

    assert await scheduler.run_once() is None

    assert switch.state is KillSwitchState.RUNNING and switch.reasons == []
    status = scheduler.status
    assert (status.unavailable_runs, status.consecutive_unavailable_runs) == (1, 1)
    assert status.last_result == "unavailable"
    assert details == ["broker state could not be read; run 1 of 3 before trading halts"]


@pytest.mark.asyncio
async def test_three_unreadable_runs_in_a_row_halt_trading() -> None:
    broker, switch, details = FlakyBroker(), RecordingSwitch(), []
    scheduler = await scheduler_for(broker, switch, details, unavailable_threshold=3)
    broker.down = maintenance()

    await scheduler.run_once()
    await scheduler.run_once()
    assert switch.state is KillSwitchState.RUNNING
    await scheduler.run_once()

    assert switch.state is KillSwitchState.HALTED
    assert switch.reasons == ["broker unavailable for 3 reconciliation runs in a row"]
    assert scheduler.status.consecutive_unavailable_runs == 3
    assert len(details) == 3 and "3 runs in a row" in details[-1]


@pytest.mark.asyncio
async def test_a_readable_run_resets_the_count_so_separate_outages_never_halt() -> None:
    broker, switch, details = FlakyBroker(), RecordingSwitch(), []
    scheduler = await scheduler_for(broker, switch, details, unavailable_threshold=3)

    for failures in (2, 2, 2):
        broker.balance_errors = [maintenance() for _ in range(failures)]
        await scheduler.run_once()
        await scheduler.run_once()
        assert scheduler.status.consecutive_unavailable_runs == 2
        result = await scheduler.run_once()
        assert result is not None and result.discrepancies == ()
        assert scheduler.status.consecutive_unavailable_runs == 0

    assert switch.state is KillSwitchState.RUNNING and switch.reasons == []
    assert scheduler.status.unavailable_runs == 6 and scheduler.status.clean_runs == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [ProviderHTTPError(401, "unauthorized"), ProviderHTTPError(403, "forbidden"), ValueError("x")],
)
async def test_a_failure_retrying_cannot_fix_halts_on_the_first_run(error: Exception) -> None:
    broker, switch, details = FlakyBroker(), RecordingSwitch(), []
    scheduler = await scheduler_for(broker, switch, details, unavailable_threshold=3)
    broker.balance_errors = [error]

    await scheduler.run_once()

    assert switch.state is KillSwitchState.HALTED
    assert switch.reasons == ["broker unavailable during reconciliation"]
    # No promise of further runs when this one already halted trading.
    assert details == ["broker state could not be read"]


@pytest.mark.asyncio
async def test_a_threshold_of_one_halts_on_the_first_unreadable_run_as_before() -> None:
    broker, switch, details = FlakyBroker(), RecordingSwitch(), []
    scheduler = await scheduler_for(broker, switch, details)
    broker.balance_errors = [maintenance()]

    await scheduler.run_once()

    assert switch.state is KillSwitchState.HALTED
    assert switch.reasons == ["broker unavailable during reconciliation"]
    assert details == ["broker state could not be read"]


@pytest.mark.asyncio
async def test_a_divergence_after_an_unreadable_run_still_halts_at_once() -> None:
    broker, switch, details = FlakyBroker(), RecordingSwitch(), []
    scheduler = await scheduler_for(broker, switch, details, unavailable_threshold=3)
    broker.balance_errors = [maintenance()]
    await scheduler.run_once()
    assert switch.state is KillSwitchState.RUNNING

    broker._balances["USD"] -= Decimal("100")
    result = await scheduler.run_once()

    assert result is not None and [item.entity_type for item in result.discrepancies] == ["balance"]
    assert switch.state is KillSwitchState.HALTED
    assert switch.reasons == ["broker-authoritative reconciliation divergence"]
    assert scheduler.status.consecutive_unavailable_runs == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("read", ["order", "fill"])
async def test_order_and_fill_reads_follow_the_same_rule(read: str) -> None:
    broker, switch, details = FlakyBroker(), RecordingSwitch(), []

    async def already_current(_client_order_id: str) -> None:
        return None

    scheduler = await scheduler_for(
        broker,
        switch,
        details,
        unavailable_threshold=3,
        refresh_order=already_current,
    )
    order = tracked_order()
    broker.orders[str(order.request.client_order_id)] = order
    scheduler.observe(order, ())
    (broker.order_errors if read == "order" else broker.fill_errors).append(maintenance())

    await scheduler.run_once()
    assert switch.state is KillSwitchState.RUNNING and switch.reasons == []
    assert "could not be read; run 1 of 3" in details[0]

    (broker.order_errors if read == "order" else broker.fill_errors).append(
        ProviderHTTPError(403, "forbidden")
    )
    await scheduler.run_once()
    assert switch.state is KillSwitchState.HALTED


@pytest.mark.asyncio
async def test_the_open_order_refresh_follows_the_same_rule() -> None:
    broker, switch, details = FlakyBroker(), RecordingSwitch(), []
    failures = [maintenance(), ProviderHTTPError(401, "unauthorized")]

    async def refresh(_client_order_id: str) -> None:
        raise failures.pop(0)

    scheduler = await scheduler_for(
        broker, switch, details, unavailable_threshold=3, refresh_order=refresh
    )
    scheduler.observe(tracked_order(), ())

    await scheduler.run_once()
    assert switch.state is KillSwitchState.RUNNING and switch.reasons == []
    assert details == ["open orders could not be refreshed; run 1 of 3 before trading halts"]

    await scheduler.run_once()
    assert switch.state is KillSwitchState.HALTED
    assert switch.reasons == ["open orders could not be refreshed"]


@pytest.mark.parametrize("value", ["0", "11", "-1", "soon", "2.5"])
def test_the_halt_threshold_must_be_a_small_whole_number(monkeypatch, value: str) -> None:
    monkeypatch.setenv("RECONCILE_HALT_AFTER_UNAVAILABLE", value)
    with pytest.raises(StartupGuardError, match="RECONCILE_HALT_AFTER_UNAVAILABLE"):
        _reconcile_halt_after()


def test_the_halt_threshold_defaults_to_three_and_accepts_a_setting(monkeypatch) -> None:
    monkeypatch.delenv("RECONCILE_HALT_AFTER_UNAVAILABLE", raising=False)
    assert _reconcile_halt_after() == DEFAULT_RECONCILE_HALT_AFTER == 3
    monkeypatch.setenv("RECONCILE_HALT_AFTER_UNAVAILABLE", "1")
    assert _reconcile_halt_after() == 1
    monkeypatch.setenv("RECONCILE_HALT_AFTER_UNAVAILABLE", "10")
    assert _reconcile_halt_after() == 10


def test_a_threshold_below_one_is_refused() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        ScheduledReconciler(
            Reconciler(SimulatedBroker(), KillSwitch()),
            baseline=PortfolioState(),
            interval_seconds=60,
            unavailable_threshold=0,
        )


@pytest.mark.asyncio
async def test_the_service_warns_while_tolerating_and_goes_critical_when_it_halts(
    tmp_path, monkeypatch
) -> None:
    from app.main import start_scheduled_reconciliation

    monkeypatch.setenv("KILL_SWITCH_FILE", str(tmp_path / "kill-switch.json"))
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("RECONCILE_INTERVAL_SECONDS", "0.02")
    monkeypatch.setenv("RECONCILE_HALT_AFTER_UNAVAILABLE", "3")
    monkeypatch.setenv("OPERATOR_TOKEN", "outage-operator")
    engine, session_factory = sqlite_database(tmp_path)
    broker = FlakyBroker()
    RecordingPortfolioStore(session_factory).save_snapshot(
        PortfolioState(
            balances=await broker.get_balances(), positions=await broker.get_positions()
        ),
        source="broker",
    )
    engine.dispose()
    settings = StartupSettings(
        TradingMode.PAPER,
        CredentialScope.NONE,
        "",
        f"sqlite+pysqlite:///{tmp_path / 'trader.db'}",
        "INFO",
    )
    application = create_app(settings, broker=broker)
    stop = start_scheduled_reconciliation(application)
    assert stop is not None
    state = application.state.operator_state
    try:
        broker.down = maintenance()
        for _ in range(500):
            await asyncio.sleep(0.01)
            if application.state.kill_switch.state is KillSwitchState.HALTED:
                break
    finally:
        await stop()

    assert application.state.kill_switch.state is KillSwitchState.HALTED
    alerts = [
        item
        for item in state.to_dict()["alerts"]
        if item["condition"] == "reconciliation_unavailable"
    ]
    assert [item["severity"] for item in alerts[-3:]] == ["warning", "warning", "critical"]
    assert "run 1 of 3" in alerts[-3]["message"]
    assert alerts[-1]["message"] == "broker state could not be reconciled; trading halted"
    assert state.scheduled_reconciliation.status.consecutive_unavailable_runs >= 3
