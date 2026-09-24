"""Phase 1 gate: kill switch, fail-closed risk inputs, the Gemini host lock, and live mode.

Criteria covered:
- Kill switch behavior is verified through all three actuation paths and across a restart.
- Every missing or stale risk input fails closed.
- GeminiBroker raises at construction against any non-sandbox host, and the host is not
  settable from configuration.
- The application cannot enter live mode without the explicit confirmation flag and a
  trade-capable Coinbase key.
"""

from __future__ import annotations

import ast
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from app.main import create_app
from app.startup_broker import assert_live_key_scope, build_startup_broker
from app.trading import CycleContext, CycleStatus
from brokers.coinbase import CoinbaseBroker
from brokers.gemini import GEMINI_SANDBOX_REST_URL, GeminiBroker
from brokers.simulated import FaultPlan, SimulatedBroker
from core.guards import (
    CredentialScope,
    StartupGuardError,
    StartupSettings,
    load_startup_settings,
)
from core.models import KillSwitchState, Quote, TradingMode, utc_now
from execution.audit import InMemoryAuditStore
from execution.engine import InMemoryOrderStore
from risk.kill_switch import KillSwitch
from strategy.reference import MovingAverageCrossStrategy

from tests.gate_support import PARITY_WINDOWS, paper_cycle, state_at

ROOT = Path(__file__).parents[1]
CANDLES = PARITY_WINDOWS["calm_range"]
SIGNAL_BAR = next(
    index
    for index in range(len(CANDLES) - 1)
    if MovingAverageCrossStrategy().on_market_state(state_at(CANDLES, index)) is not None
)
OPERATOR = "gate-operator-token"
ADMIN = "gate-admin-token"


def fresh_quote(age: timedelta = timedelta(0)) -> Quote:
    price = CANDLES[SIGNAL_BAR + 1].open
    return Quote(symbol="BTC-USD", bid=price, ask=price, as_of=utc_now() - age, source="gate")


def cycle_for(switch: KillSwitch, broker=None, **options):
    broker = broker or SimulatedBroker(fresh_quote())
    store = InMemoryOrderStore()
    return paper_cycle(
        broker,
        MovingAverageCrossStrategy(),
        store=store,
        audit=InMemoryAuditStore(store),
        kill_switch=switch,
        **options,
    )


async def attempt(switch: KillSwitch, **options):
    return await cycle_for(switch, **options).on_market_state(state_at(CANDLES, SIGNAL_BAR))


def operator_app(switch_path: Path, monkeypatch):
    monkeypatch.setenv("OPERATOR_TOKEN", OPERATOR)
    monkeypatch.setenv("OPERATOR_ADMIN_TOKEN", ADMIN)
    monkeypatch.setenv("KILL_SWITCH_FILE", str(switch_path))
    settings = StartupSettings(
        TradingMode.PAPER, CredentialScope.NONE, "", "postgresql://unused", "INFO"
    )
    return create_app(settings)


def client_for(application) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://operator"
    )


@pytest.mark.asyncio
async def test_dashboard_pause_works_without_javascript_and_survives_restart(
    tmp_path, monkeypatch
) -> None:
    switch_path = tmp_path / "kill-switch.json"
    async with client_for(operator_app(switch_path, monkeypatch)) as client:
        await client.post(
            "/operator/login", data={"token": OPERATOR}, headers={"accept": "text/html"}
        )
        dashboard = await client.get("/operator")
        assert 'action="/operator/pause"' in dashboard.text
        assert 'action="/operator/emergency-stop"' in dashboard.text
        # A plain HTML form post: no script, no token in the form.
        response = await client.post("/operator/pause", headers={"accept": "text/html"})
        assert response.status_code == 200 and "paused" in response.text

    refused = await attempt(KillSwitch(switch_path))
    assert refused.status is CycleStatus.REFUSED
    assert refused.decision.failed_gate == "kill_switch"

    # Restart: a new process rebuilds the application and switch from durable state.
    async with client_for(operator_app(switch_path, monkeypatch)) as client:
        state = await client.get("/operator/kill-switch", headers={"x-operator-token": OPERATOR})
        assert state.json() == {"state": "paused"}
    assert (await attempt(KillSwitch(switch_path))).decision.failed_gate == "kill_switch"


@pytest.mark.asyncio
async def test_authenticated_api_emergency_stop_survives_restart_and_needs_admin_rearm(
    tmp_path, monkeypatch
) -> None:
    switch_path = tmp_path / "kill-switch.json"
    async with client_for(operator_app(switch_path, monkeypatch)) as client:
        assert (await client.post("/operator/emergency-stop")).status_code == 401
        assert KillSwitch(switch_path).state is KillSwitchState.RUNNING
        stop = await client.post("/operator/emergency-stop", headers={"x-operator-token": OPERATOR})
        assert stop.json() == {"state": "halted"}

    assert (await attempt(KillSwitch(switch_path))).decision.failed_gate == "kill_switch"

    async with client_for(operator_app(switch_path, monkeypatch)) as client:
        denied = await client.post("/operator/rearm", headers={"x-operator-token": OPERATOR})
        assert denied.status_code == 403
        assert KillSwitch(switch_path).state is KillSwitchState.HALTED
        rearm = await client.post("/operator/rearm", headers={"x-operator-token": ADMIN})
        assert rearm.json() == {"state": "running"}

    assert (await attempt(KillSwitch(switch_path))).status is CycleStatus.SUBMITTED


@pytest.mark.asyncio
async def test_flag_file_is_read_every_loop_and_its_halt_survives_restart(tmp_path) -> None:
    switch_path = tmp_path / "kill-switch.json"
    flag = tmp_path / "trading.flag"
    switch = KillSwitch(switch_path)
    cycle = cycle_for(switch, kill_switch_flag=flag)

    flag.write_text("halted\n", encoding="utf-8")
    outcome = await cycle.on_market_state(state_at(CANDLES, SIGNAL_BAR))

    assert outcome.decision.failed_gate == "kill_switch"
    assert switch.audit_events[-1]["reason"] == "external actuation (file)"
    flag.unlink()
    restarted = KillSwitch(switch_path)
    assert restarted.state is KillSwitchState.HALTED
    # A flag left at "running" never undoes a halt; only the authenticated re-arm does.
    flag.write_text("running", encoding="utf-8")
    after_restart = await cycle_for(restarted, kill_switch_flag=flag).on_market_state(
        state_at(CANDLES, SIGNAL_BAR)
    )
    assert after_restart.decision.failed_gate == "kill_switch"
    assert restarted.state is KillSwitchState.HALTED


@pytest.mark.asyncio
async def test_environment_flag_pauses_and_an_unreadable_flag_halts(tmp_path) -> None:
    paused = KillSwitch(tmp_path / "paused.json")
    outcome = await attempt(paused, environ={"TRADING_KILL_SWITCH": "paused"})
    assert outcome.decision.failed_gate == "kill_switch"
    assert KillSwitch(tmp_path / "paused.json").state is KillSwitchState.PAUSED

    flag = tmp_path / "garbled.flag"
    flag.write_text("stop please", encoding="utf-8")
    halted = KillSwitch(tmp_path / "halted.json")
    assert (await attempt(halted, kill_switch_flag=flag)).decision.failed_gate == "kill_switch"
    assert halted.state is KillSwitchState.HALTED


class RaisingInputs:
    async def risk_inputs(self, signal, state, context: CycleContext):
        raise RuntimeError("market data feed unavailable")


class FixedSignalStrategy:
    """Emits one signal regardless of the market state it is shown."""

    def __init__(self, signal) -> None:
        self.signal = signal
        self.strategy_version = signal.strategy_version

    def on_market_state(self, state):
        return self.signal


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario", "gate"),
    [
        ("broker_unreachable", "broker_health"),
        ("stale_quote", "stale_price"),
        ("future_quote", "stale_price"),
        ("no_market_history", "abnormal_volatility"),
        ("inputs_unavailable", "risk_inputs"),
    ],
)
async def test_missing_or_stale_inputs_refuse_through_the_trading_cycle(
    tmp_path, scenario: str, gate: str
) -> None:
    broker = SimulatedBroker(fresh_quote())
    state = state_at(CANDLES, SIGNAL_BAR)
    if scenario == "broker_unreachable":
        broker = SimulatedBroker(fresh_quote(), fault_plan=FaultPlan(unavailable=True))
    elif scenario == "stale_quote":
        broker = SimulatedBroker(fresh_quote(age=timedelta(minutes=5)))
    elif scenario == "future_quote":
        broker = SimulatedBroker(fresh_quote(age=-timedelta(minutes=5)))
    elif scenario == "no_market_history":
        # The strategy still signals, but the risk inputs see no closed bars at all.
        state = state.model_copy(update={"candles": ()})
    store = InMemoryOrderStore()
    audit = InMemoryAuditStore(store)
    cycle = paper_cycle(
        broker, MovingAverageCrossStrategy(), store=store, audit=audit, kill_switch=KillSwitch()
    )
    if scenario == "inputs_unavailable":
        cycle.risk_inputs = RaisingInputs()
    if scenario == "no_market_history":
        signal = MovingAverageCrossStrategy().on_market_state(state_at(CANDLES, SIGNAL_BAR))
        cycle.strategy = FixedSignalStrategy(signal)

    outcome = await cycle.on_market_state(state)

    assert outcome.status is CycleStatus.REFUSED
    assert outcome.decision.failed_gate == gate
    assert list(audit.decisions.values()) == [outcome.decision]
    assert store.orders == {}


@pytest.mark.parametrize(
    "url",
    [
        "https://api.gemini.com",
        "https://exchange.gemini.com",
        "https://sandbox.gemini.com.attacker.example",
        "https://api.sandbox.gemini.com.evil.example",
        "https://gemini.com",
        "http://localhost:8080",
        "not a url",
    ],
)
def test_gemini_refuses_any_non_sandbox_host_at_construction(url: str) -> None:
    with pytest.raises(ValueError, match="sandbox"):
        GeminiBroker(base_url=url)


def test_gemini_host_is_fixed_and_not_read_from_configuration(monkeypatch) -> None:
    for name in ("GEMINI_BASE_URL", "GEMINI_API_URL", "GEMINI_HOST", "GEMINI_REST_URL"):
        monkeypatch.setenv(name, "https://api.gemini.com")
    monkeypatch.setenv("BROKER_PROVIDER", "gemini-sandbox")
    monkeypatch.setenv("GEMINI_API_KEY", "sandbox-key")
    monkeypatch.setenv("GEMINI_API_SECRET", "sandbox-secret")

    broker = build_startup_broker(
        StartupSettings(TradingMode.PAPER, CredentialScope.VIEW, "", "postgresql://unused", "INFO")
    )

    assert isinstance(broker, GeminiBroker)
    assert broker._base_url == GEMINI_SANDBOX_REST_URL
    # Even another sandbox host passed in code is replaced by the fixed constant.
    assert GeminiBroker(base_url="https://other.sandbox.gemini.com")._base_url == (
        GEMINI_SANDBOX_REST_URL
    )
    # The adapter module never reads the environment.
    tree = ast.parse((ROOT / "brokers" / "gemini.py").read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in node.names
    }
    assert "os" not in imported and "environ" not in imported


@pytest.mark.parametrize(
    ("environment", "message"),
    [
        ({"TRADING_MODE": "live", "CREDENTIAL_SCOPE": "trade"}, "LIVE_CONFIRMATION"),
        (
            {
                "TRADING_MODE": "live",
                "CREDENTIAL_SCOPE": "view",
                "LIVE_CONFIRMATION": "I_UNDERSTAND_LIVE_TRADING",
            },
            "trade-capable",
        ),
        (
            {
                "TRADING_MODE": "live",
                "CREDENTIAL_SCOPE": "trade",
                "LIVE_CONFIRMATION": "yes",
            },
            "LIVE_CONFIRMATION",
        ),
    ],
)
def test_live_mode_requires_confirmation_and_declared_trade_scope(environment, message) -> None:
    with pytest.raises(StartupGuardError, match=message):
        load_startup_settings(environment)


def test_live_mode_requires_the_coinbase_broker(monkeypatch) -> None:
    monkeypatch.delenv("BROKER_PROVIDER", raising=False)
    live = StartupSettings(
        TradingMode.LIVE,
        CredentialScope.TRADE,
        "I_UNDERSTAND_LIVE_TRADING",
        "postgresql://x",
        "INFO",
    )
    with pytest.raises(StartupGuardError, match="BROKER_PROVIDER=coinbase"):
        build_startup_broker(live)


def coinbase_with_permissions(**permissions) -> CoinbaseBroker:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v3/brokerage/key_permissions"
        return httpx.Response(
            200,
            json={"portfolio_uuid": "redacted", "portfolio_type": "DEFAULT", **permissions},
            request=request,
        )

    return CoinbaseBroker(
        auth_token="gate-token", client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )


LIVE = StartupSettings(
    TradingMode.LIVE, CredentialScope.TRADE, "I_UNDERSTAND_LIVE_TRADING", "postgresql://x", "INFO"
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("permissions", "message"),
    [
        ({"can_view": True, "can_trade": False, "can_transfer": False}, "trade-capable"),
        ({"can_view": True, "can_trade": True, "can_transfer": True}, "transfer"),
        ({"can_view": True}, "trade-capable"),
    ],
)
async def test_live_mode_refuses_a_coinbase_key_that_cannot_trade_or_can_transfer(
    permissions, message
) -> None:
    broker = coinbase_with_permissions(**permissions)
    with pytest.raises(StartupGuardError, match=message):
        await assert_live_key_scope(LIVE, broker)
    await broker.close()


@pytest.mark.asyncio
async def test_live_mode_accepts_a_trade_only_coinbase_key() -> None:
    broker = coinbase_with_permissions(can_view=True, can_trade=True, can_transfer=False)
    await assert_live_key_scope(LIVE, broker)
    await broker.close()
    paper = StartupSettings(TradingMode.PAPER, CredentialScope.NONE, "", "postgresql://x", "INFO")
    await assert_live_key_scope(paper, None)
    with pytest.raises(StartupGuardError, match="Coinbase broker"):
        await assert_live_key_scope(LIVE, SimulatedBroker())


@pytest.mark.asyncio
async def test_live_service_startup_stops_before_recovery_with_a_view_only_key(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("KILL_SWITCH_FILE", str(tmp_path / "kill-switch.json"))
    broker = coinbase_with_permissions(can_view=True, can_trade=False, can_transfer=False)
    application = create_app(LIVE, broker=broker, recover_on_start=True)
    with pytest.raises(StartupGuardError, match="trade-capable"):
        async with application.router.lifespan_context(application):
            pass
    assert application.state.operator_state.startup_recovery is None
    await broker.close()
