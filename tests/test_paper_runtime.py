from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from api.alerts import AlertRouter
from api.operator import OperatorState
from app.main import create_app
from app.paper_runtime import INTERVAL, PaperRuntime, PaperRuntimeConfig, start_paper_runtime
from app.trading import CycleOutcome, CycleStatus
from brokers.simulated import SimulatedBroker
from core.disconnect import DisconnectReason
from core.guards import CredentialScope, StartupGuardError, StartupSettings
from core.models import Candle, MarketState, Quote, TradingMode
from data.storage import InMemoryCandleStore
from risk.kill_switch import KillSwitch

T0 = datetime(2026, 9, 27, 12, tzinfo=UTC)


def candle(index: int) -> Candle:
    opened = T0 + INTERVAL * index
    price = Decimal("60000") + index
    return Candle(
        symbol="BTC-USD",
        interval="FIVE_MINUTE",
        opened_at=opened,
        closed_at=opened + INTERVAL,
        open=price,
        high=price + 2,
        low=price - 2,
        close=price + 1,
        volume=Decimal("1"),
        source="test",
        as_of=opened + INTERVAL,
        ingested_at=opened + INTERVAL,
    )


class RecordingSink:
    def __init__(self) -> None:
        self.alerts: list[Any] = []

    async def send(self, alert) -> None:
        self.alerts.append(alert)


class RecordingCycle:
    def __init__(self) -> None:
        self.states: list[MarketState] = []
        self.on_halt: Any = None

    async def on_market_state(self, state: MarketState) -> CycleOutcome:
        self.states.append(state)
        return CycleOutcome(CycleStatus.NO_SIGNAL, "strategy returned no signal")


class FailingCycle(RecordingCycle):
    async def on_market_state(self, state: MarketState) -> CycleOutcome:
        raise RuntimeError("unexpected cycle failure")


class FakeIngestor:
    def __init__(self, quote: Quote | None) -> None:
        self.quote = quote
        self.on_candle: Any = None
        self.gap_fill: Any = None
        self.last_candle_at: dict[str, datetime] = {}

    async def run(self, _stop: asyncio.Event, *, max_connections: int | None = None) -> None:
        return None

    def require_fresh_quote(self, _symbol: str, *, now: datetime | None = None) -> Quote:
        if self.quote is None:
            raise RuntimeError("no fresh quote")
        return self.quote


class FakeBackfiller:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def run(self, *args, **kwargs) -> int:
        self.calls.append((args, kwargs))
        return 0


class FakeRest:
    async def close(self) -> None:
        return None


class FakeEngine:
    def dispose(self) -> None:
        return None


class FakeSystemEvents:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def record(self, event_type: str, payload, *, now=None) -> None:
        self.events.append((event_type, dict(payload)))


def operator(tmp_path, sink: RecordingSink) -> OperatorState:
    return OperatorState(
        settings=StartupSettings(
            TradingMode.PAPER,
            CredentialScope.VIEW,
            "",
            "postgresql://unused",
            "INFO",
        ),
        kill_switch=KillSwitch(tmp_path / "switch.json"),
        alert_router=AlertRouter(phone_push=sink),
    )


def runtime(tmp_path, *, quote: Quote | None, system_events=None):
    store = InMemoryCandleStore()
    store.upsert_many(tuple(candle(index) for index in range(8)))
    cycle = RecordingCycle()
    sink = RecordingSink()
    state = operator(tmp_path, sink)
    ingestor = FakeIngestor(quote)
    instance = PaperRuntime(
        config=PaperRuntimeConfig(
            symbols=("BTC-USD",),
            history_bars=9,
            fast_window=3,
            slow_window=8,
            order_quantity=Decimal("0.0001"),
            min_notional=Decimal("1"),
            estimated_slippage=Decimal("0.005"),
            cooldown=timedelta(minutes=5),
            loss_state_path=None,
        ),
        operator=state,
        cycle=cycle,
        store=store,
        backfiller=FakeBackfiller(),
        rest_client=FakeRest(),
        ingestor=ingestor,
        engine=FakeEngine(),
        system_events=system_events,
    )
    return instance, cycle, sink


@pytest.mark.asyncio
async def test_closed_live_candle_drives_the_trading_cycle_and_heartbeat(tmp_path) -> None:
    latest = candle(8)
    quote = Quote(
        symbol="BTC-USD",
        bid=Decimal("60008"),
        ask=Decimal("60009"),
        as_of=latest.closed_at,
        source="coinbase",
        received_at=datetime.now(UTC),
    )
    instance, cycle, sink = runtime(tmp_path, quote=quote)

    await instance.on_candle(latest)

    assert len(cycle.states) == 1
    assert cycle.states[0].candles[-1] == latest
    assert cycle.states[0].quote == quote
    assert instance.operator.snapshot.runtime_last_cycle_status == "no_signal"
    assert instance.operator.snapshot.strategy_heartbeats[0].status == "healthy"
    assert sink.alerts == []


@pytest.mark.asyncio
async def test_missing_live_quote_refuses_processing_and_alerts_once(tmp_path) -> None:
    instance, cycle, sink = runtime(tmp_path, quote=None)

    await instance.on_candle(candle(8))
    await instance.on_candle(candle(8))

    assert cycle.states == []
    assert instance.operator.snapshot.runtime_status == "degraded"
    assert [alert.condition for alert in sink.alerts] == ["market_data_processing_failed"]


@pytest.mark.asyncio
async def test_start_refreshes_the_bounded_history_window(tmp_path) -> None:
    instance, _cycle, _sink = runtime(tmp_path, quote=None)

    await instance.start()
    try:
        assert len(instance.backfiller.calls) == 1
        _args, kwargs = instance.backfiller.calls[0]
        assert kwargs["granularity"] == "FIVE_MINUTE"
        assert kwargs["refresh_existing"] is True
    finally:
        await instance.stop()


@pytest.mark.asyncio
async def test_unexpected_cycle_error_halts_for_broker_review(tmp_path) -> None:
    latest = candle(8)
    quote = Quote(
        symbol="BTC-USD",
        bid=Decimal("60008"),
        ask=Decimal("60009"),
        as_of=latest.closed_at,
        source="coinbase",
        received_at=datetime.now(UTC),
    )
    instance, _cycle, sink = runtime(tmp_path, quote=quote)
    instance.cycle = FailingCycle()

    await instance.on_candle(latest)

    assert instance.operator.kill_switch.state.value == "halted"
    assert instance.operator.snapshot.runtime_status == "halted"
    assert [alert.condition for alert in sink.alerts] == ["trading_cycle_failed"]


@pytest.mark.asyncio
async def test_disconnect_gap_fill_alert_is_deduplicated(tmp_path) -> None:
    instance, _cycle, sink = runtime(tmp_path, quote=None)
    end = T0 + INTERVAL * 12

    await instance.gap_fill("BTC-USD", T0 + INTERVAL * 8, end)
    await instance.gap_fill("BTC-USD", T0 + INTERVAL * 8, end)

    assert len(instance.backfiller.calls) == 2
    assert [alert.condition for alert in sink.alerts] == ["market_data_disconnected"]


@pytest.mark.asyncio
async def test_disconnect_and_gap_fill_each_persist_one_system_event(tmp_path) -> None:
    events = FakeSystemEvents()
    instance, _cycle, _sink = runtime(tmp_path, quote=None, system_events=events)
    end = T0 + INTERVAL * 12

    await instance.on_disconnect(
        T0, DisconnectReason("connect_failed", "OSError: no route to 203.0.113.9")
    )
    await instance.gap_fill("BTC-USD", T0 + INTERVAL * 8, end)

    assert [event_type for event_type, _ in events.events] == ["disconnect", "gap_fill"]
    disconnect_payload = events.events[0][1]
    gap_fill_payload = events.events[1][1]
    assert disconnect_payload["at"] == T0.isoformat()
    assert disconnect_payload["reason_kind"] == "connect_failed"
    assert disconnect_payload["reason_note"] == "OSError: no route to [REDACTED]"
    assert gap_fill_payload["symbol"] == "BTC-USD"
    assert gap_fill_payload["to"] == end.isoformat()


@pytest.mark.asyncio
async def test_ingestor_disconnect_hook_is_wired_to_the_runtime(tmp_path) -> None:
    events = FakeSystemEvents()
    instance, _cycle, _sink = runtime(tmp_path, quote=None, system_events=events)

    await instance.ingestor.on_disconnect(T0, DisconnectReason("unknown"))

    assert [event_type for event_type, _ in events.events] == ["disconnect"]


@pytest.mark.asyncio
async def test_unexpected_stream_exit_halts_and_alerts(tmp_path) -> None:
    instance, _cycle, sink = runtime(tmp_path, quote=None)

    await instance._run()

    assert instance.operator.kill_switch.state.value == "halted"
    assert instance.operator.snapshot.runtime_status == "failed"
    assert [alert.condition for alert in sink.alerts] == ["market_data_stopped"]


def test_paper_runtime_configuration_is_bounded_and_fail_closed() -> None:
    config = PaperRuntimeConfig.from_env({})
    assert config.symbols == ("BTC-USD",)
    assert config.history_bars == 50
    with pytest.raises(StartupGuardError, match="exceed"):
        PaperRuntimeConfig.from_env(
            {"PAPER_STRATEGY_FAST_BARS": "8", "PAPER_STRATEGY_SLOW_BARS": "8"}
        )
    with pytest.raises(StartupGuardError, match="at most 300"):
        PaperRuntimeConfig.from_env({"PAPER_HISTORY_BARS": "301"})
    with pytest.raises(StartupGuardError, match="finite"):
        PaperRuntimeConfig.from_env({"PAPER_ORDER_QUANTITY": "NaN"})


@pytest.mark.asyncio
async def test_runtime_enablement_is_explicit_and_missing_broker_is_visible(tmp_path) -> None:
    settings = StartupSettings(
        TradingMode.PAPER,
        CredentialScope.NONE,
        "",
        "postgresql://unused",
        "INFO",
    )
    without_broker = create_app(settings)
    assert await start_paper_runtime(without_broker, environ={"PAPER_RUNTIME_ENABLED": "1"}) is None
    assert without_broker.state.operator_state.snapshot.runtime_status == "disabled"
    assert without_broker.state.operator_state.snapshot.strategy_heartbeats[0].status == "unhealthy"

    with_broker = create_app(
        settings,
        broker=SimulatedBroker(
            Quote(
                symbol="BTC-USD",
                bid=Decimal("60000"),
                ask=Decimal("60001"),
                as_of=datetime.now(UTC),
                source="test",
            )
        ),
    )
    with_broker.state.execution = object()
    with_broker.state.trading_lock = asyncio.Lock()
    assert await start_paper_runtime(with_broker, environ={"PAPER_RUNTIME_ENABLED": "0"}) is None
    with pytest.raises(StartupGuardError, match="true or false"):
        await start_paper_runtime(with_broker, environ={"PAPER_RUNTIME_ENABLED": "sometimes"})
