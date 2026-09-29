"""Paper-only orchestration for live public market data and the real trading path."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol

from api.alerts import Alert
from api.operator import OperatorState
from api.system_events import DISCONNECT_EVENT, GAP_FILL_EVENT, SystemEventJournal
from core.guards import StartupGuardError
from core.models import Candle, MarketState, Quote, TradingMode, utc_now
from data.backfill import HistoricalCandleBackfiller
from data.coinbase import GRANULARITY_SECONDS, CoinbaseRESTClient
from data.storage import CandleStore, SqlAlchemyCandleStore
from data.stream import WEBSOCKET_CANDLE_INTERVAL, CoinbaseWebSocketIngestor
from data.validation import MarketDataValidator
from db.session import create_database_engine, create_session_factory
from execution.audit import SqlAlchemyAuditStore
from risk.engine import ExchangeConstraints, RiskLimits
from strategy.reference import MovingAverageCrossStrategy

from app.trading import BrokerRiskInputs, CycleOutcome, CycleStatus, TradingCycle

logger = logging.getLogger(__name__)

INTERVAL = timedelta(seconds=GRANULARITY_SECONDS[WEBSOCKET_CANDLE_INTERVAL])
_SYMBOL = re.compile(r"^[A-Z0-9]+-USD$")


def parse_paper_symbols(environ: Mapping[str, str]) -> tuple[str, ...]:
    """The trading symbols: ``PAPER_SYMBOLS``, the only list the trading path uses."""

    symbols = tuple(
        dict.fromkeys(
            item.strip().upper()
            for item in environ.get("PAPER_SYMBOLS", "BTC-USD").split(",")
            if item.strip()
        )
    )
    if not symbols or len(symbols) > 10 or any(not _SYMBOL.fullmatch(item) for item in symbols):
        raise StartupGuardError("PAPER_SYMBOLS must contain 1-10 comma-separated *-USD symbols")
    return symbols


class CycleRunner(Protocol):
    on_halt: Callable[[str], Awaitable[None]] | None

    async def on_market_state(self, state: MarketState) -> CycleOutcome: ...


class BackfillRunner(Protocol):
    async def run(
        self,
        product_id: str,
        start: datetime,
        end: datetime,
        *,
        granularity: str = "ONE_MINUTE",
        refresh_existing: bool = False,
    ) -> int: ...


class RestCloser(Protocol):
    async def close(self) -> None: ...


class StreamRunner(Protocol):
    on_candle: Callable[[Candle], Awaitable[None]] | None
    gap_fill: Callable[[str, datetime | None, datetime], Awaitable[None]] | None
    on_disconnect: Callable[[datetime], Awaitable[None]] | None
    last_candle_at: dict[str, datetime]

    async def run(self, stop: asyncio.Event, *, max_connections: int | None = None) -> None: ...

    def require_fresh_quote(self, product_id: str, *, now: datetime | None = None) -> Quote: ...


class DisposableEngine(Protocol):
    def dispose(self) -> None: ...


@dataclass(frozen=True, slots=True)
class PaperRuntimeConfig:
    symbols: tuple[str, ...]
    history_bars: int
    fast_window: int
    slow_window: int
    order_quantity: Decimal
    min_notional: Decimal
    estimated_slippage: Decimal
    cooldown: timedelta
    loss_state_path: Path | None

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> PaperRuntimeConfig:
        symbols = parse_paper_symbols(environ)
        fast = _integer(environ, "PAPER_STRATEGY_FAST_BARS", 3, minimum=1)
        slow = _integer(environ, "PAPER_STRATEGY_SLOW_BARS", 8, minimum=2)
        if slow <= fast:
            raise StartupGuardError("PAPER_STRATEGY_SLOW_BARS must exceed the fast window")
        history = _integer(environ, "PAPER_HISTORY_BARS", 50, minimum=slow + 1, maximum=300)
        quantity = _decimal(environ, "PAPER_ORDER_QUANTITY", "0.0001", positive=True)
        min_notional = _decimal(environ, "PAPER_MIN_NOTIONAL", "1", positive=True)
        slippage = _decimal(environ, "PAPER_ESTIMATED_SLIPPAGE", "0.005", positive=False)
        if slippage > Decimal("0.01"):
            raise StartupGuardError("PAPER_ESTIMATED_SLIPPAGE must not exceed 0.01")
        cooldown_seconds = _integer(environ, "PAPER_COOLDOWN_SECONDS", 300, minimum=0)
        configured_loss_path = environ.get("LOSS_STATE_FILE", "").strip()
        loss_path: Path | None
        if configured_loss_path:
            loss_path = Path(configured_loss_path)
        else:
            kill_switch = environ.get("KILL_SWITCH_FILE", "").strip()
            loss_path = Path(kill_switch).with_name("loss-limits.json") if kill_switch else None
        return cls(
            symbols=symbols,
            history_bars=history,
            fast_window=fast,
            slow_window=slow,
            order_quantity=quantity,
            min_notional=min_notional,
            estimated_slippage=slippage,
            cooldown=timedelta(seconds=cooldown_seconds),
            loss_state_path=loss_path,
        )


class PaperRuntime:
    """Own the public feed, closed-candle history, and one paper trading cycle."""

    def __init__(
        self,
        *,
        config: PaperRuntimeConfig,
        operator: OperatorState,
        cycle: CycleRunner,
        store: CandleStore,
        backfiller: BackfillRunner,
        rest_client: RestCloser,
        ingestor: StreamRunner,
        engine: DisposableEngine,
        system_events: SystemEventJournal | None = None,
    ) -> None:
        self.config = config
        self.operator = operator
        self.cycle = cycle
        self.store = store
        self.backfiller = backfiller
        self.rest_client = rest_client
        self.ingestor = ingestor
        self.engine = engine
        self.system_events = system_events
        self.stop_event = asyncio.Event()
        self.task: asyncio.Task[None] | None = None
        self._active_alerts: set[str] = set()
        self.ingestor.on_candle = self.on_candle
        self.ingestor.gap_fill = self.gap_fill
        self.ingestor.on_disconnect = self.on_disconnect
        self.cycle.on_halt = self.on_halt

    async def start(self) -> None:
        end = _bucket_start(utc_now())
        start = end - INTERVAL * self.config.history_bars
        for symbol in self.config.symbols:
            await self.backfiller.run(
                symbol,
                start,
                end,
                granularity=WEBSOCKET_CANDLE_INTERVAL,
                refresh_existing=True,
            )
            latest = self.store.latest_opened_at(symbol, WEBSOCKET_CANDLE_INTERVAL)
            if latest is None:
                raise StartupGuardError(f"no closed market history is available for {symbol}")
            self.ingestor.last_candle_at[symbol] = latest
        self.operator.set_runtime("running", "waiting for the next closed live candle")
        self.operator.heartbeat(
            "primary", status="starting", detail="live feed connected task started"
        )
        self.task = asyncio.create_task(self._run(), name="paper-market-data")

    async def _run(self) -> None:
        try:
            await self.ingestor.run(self.stop_event)
            if not self.stop_event.is_set():
                raise RuntimeError("market-data stream stopped unexpectedly")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("paper market-data runtime stopped")
            self.operator.kill_switch.trip("paper market-data runtime stopped")
            self.operator.heartbeat(
                "primary", status="unhealthy", detail="market-data task stopped"
            )
            self.operator.set_runtime("failed", "market-data task stopped; trading halted")
            await self._alert_once(
                "market_data_stopped",
                "Live market data stopped unexpectedly; trading was halted",
            )

    async def stop(self) -> None:
        self.stop_event.set()
        try:
            if self.task is not None:
                self.task.cancel()
                try:
                    await self.task
                except asyncio.CancelledError:
                    pass
        finally:
            try:
                await self.rest_client.close()
            finally:
                self.engine.dispose()
                self.operator.set_runtime("stopped", "paper runtime stopped")

    async def on_disconnect(self, at: datetime) -> None:
        if self.system_events is not None:
            self.system_events.record(DISCONNECT_EVENT, {"at": at.isoformat()}, now=at)

    async def gap_fill(self, symbol: str, last_closed: datetime | None, end: datetime) -> None:
        self.operator.heartbeat("primary", status="degraded", detail="market-data reconnecting")
        await self._alert_once(
            "market_data_disconnected",
            "Live market data disconnected; closed bars are being gap-filled before reconnect",
            severity="warning",
        )
        start = (
            last_closed + INTERVAL
            if last_closed is not None
            else end - INTERVAL * self.config.history_bars
        )
        if self.system_events is not None:
            self.system_events.record(
                GAP_FILL_EVENT,
                {
                    "symbol": symbol,
                    "from": start.isoformat() if last_closed is not None else None,
                    "to": end.isoformat(),
                },
                now=end,
            )
        if start < end:
            await self.backfiller.run(symbol, start, end, granularity=WEBSOCKET_CANDLE_INTERVAL)

    async def on_candle(self, candle: Candle) -> None:
        try:
            prior = self.store.latest(
                candle.symbol,
                WEBSOCKET_CANDLE_INTERVAL,
                self.config.history_bars - 1,
            )
            candles = (prior + (candle,))[-self.config.history_bars :]
            if len(candles) < self.config.slow_window + 1:
                raise ValueError("closed-candle history is incomplete")
            MarketDataValidator().validate_candles(candles, expected_interval=INTERVAL)
            quote = self.ingestor.require_fresh_quote(candle.symbol)
            self.store.upsert_many((candle,))
        except Exception:
            logger.exception("closed live candle could not be validated")
            self.operator.heartbeat(
                "primary", status="unhealthy", detail="closed candle could not be validated"
            )
            self.operator.set_runtime("degraded", "closed candle could not be validated")
            await self._alert_once(
                "market_data_processing_failed",
                "A closed live candle could not be persisted or validated; no order was submitted",
            )
            return
        try:
            outcome = await self.cycle.on_market_state(
                MarketState(
                    symbol=candle.symbol,
                    quote=quote,
                    candles=candles,
                    observed_at=utc_now(),
                )
            )
        except Exception:
            logger.exception("paper trading cycle failed unexpectedly")
            self.operator.kill_switch.trip("paper trading cycle failed unexpectedly")
            self.operator.heartbeat(
                "primary", status="unhealthy", detail="trading cycle failed unexpectedly"
            )
            self.operator.set_runtime("halted", "trading cycle failed; provider review required")
            await self._alert_once(
                "trading_cycle_failed",
                "The trading cycle failed unexpectedly; trading was halted and broker state must "
                "be reviewed",
            )
            return
        self._active_alerts.discard("market_data_disconnected")
        self._active_alerts.discard("market_data_processing_failed")
        self._record_outcome(outcome)

    async def on_halt(self, reason: str) -> None:
        await self._alert_once("trading_cycle_halted", f"Trading halted: {reason}")

    def _record_outcome(self, outcome: CycleOutcome) -> None:
        status = "unhealthy" if outcome.status is CycleStatus.HALTED else "healthy"
        self.operator.heartbeat(
            "primary", status=status, detail=f"{outcome.status.value}: {outcome.detail}"
        )
        self.operator.set_runtime(
            "running" if status == "healthy" else "halted",
            outcome.detail,
            cycle_status=outcome.status.value,
            cycle_at=utc_now(),
        )

    async def _alert_once(
        self, condition: str, message: str, *, severity: str = "critical"
    ) -> None:
        if condition in self._active_alerts:
            return
        self._active_alerts.add(condition)
        await self.operator.emit_alert(
            Alert(condition=condition, severity=severity, message=message)
        )


async def start_paper_runtime(
    application,
    *,
    environ: Mapping[str, str] | None = None,
    rest_client: CoinbaseRESTClient | None = None,
    ingestor: CoinbaseWebSocketIngestor | None = None,
) -> PaperRuntime | None:
    """Start issue #51's runtime only for paper mode with a configured broker."""

    settings = application.state.startup_settings
    operator: OperatorState = application.state.operator_state
    if settings.trading_mode is not TradingMode.PAPER:
        operator.set_runtime("disabled", "live market runtime is paper-only")
        return None
    if operator.broker is None:
        operator.set_runtime("disabled", "paper runtime requires a configured broker")
        operator.heartbeat("primary", status="unhealthy", detail="paper broker is not configured")
        return None
    if not hasattr(application.state, "execution") or not hasattr(
        application.state, "trading_lock"
    ):
        raise StartupGuardError("paper runtime requires scheduled reconciliation")

    values = os.environ if environ is None else environ
    enabled = values.get("PAPER_RUNTIME_ENABLED", "0").strip().lower()
    if enabled not in {"0", "1", "false", "true"}:
        raise StartupGuardError("PAPER_RUNTIME_ENABLED must be true or false")
    if enabled in {"0", "false"}:
        operator.set_runtime("disabled", "PAPER_RUNTIME_ENABLED is false")
        return None
    config = PaperRuntimeConfig.from_env(values)
    engine = create_database_engine(settings.database_url, settings.trading_mode)
    session_factory = create_session_factory(engine)
    store = SqlAlchemyCandleStore(session_factory)
    strategy = MovingAverageCrossStrategy(
        fast=config.fast_window,
        slow=config.slow_window,
        quantity=config.order_quantity,
    )
    operator.register_strategy("primary", strategy.strategy_version)
    operator.set_local_data(strategy_version=strategy.strategy_version)
    capabilities = operator.broker.capabilities
    increment = capabilities.quantity_increment
    if increment <= 0 or capabilities.price_increment <= 0:
        engine.dispose()
        raise StartupGuardError("configured broker increments must be positive")
    if config.order_quantity < increment or (config.order_quantity - increment) % increment != 0:
        engine.dispose()
        raise StartupGuardError(
            "PAPER_ORDER_QUANTITY does not match the configured broker quantity increment"
        )
    public_rest = rest_client or CoinbaseRESTClient()
    constraints = ExchangeConstraints(
        min_quantity=increment,
        quantity_increment=increment,
        min_notional=config.min_notional,
        price_increment=capabilities.price_increment,
    )
    risk_inputs = BrokerRiskInputs(
        operator.broker,
        constraints=constraints,
        estimated_slippage=config.estimated_slippage,
        cooldown=config.cooldown,
        loss_state_path=config.loss_state_path,
    )
    limits = RiskLimits(max_quote_age_seconds=capabilities.max_quote_age_seconds)
    cycle = TradingCycle(
        strategy=strategy,
        execution=application.state.execution,
        audit=SqlAlchemyAuditStore(session_factory),
        kill_switch=application.state.kill_switch,
        risk_inputs=risk_inputs,
        limits=limits,
        kill_switch_flag=Path(values["TRADING_KILL_SWITCH_FILE"])
        if values.get("TRADING_KILL_SWITCH_FILE", "").strip()
        else None,
        lock=application.state.trading_lock,
    )
    stream = ingestor or CoinbaseWebSocketIngestor(config.symbols)
    runtime = PaperRuntime(
        config=config,
        operator=operator,
        cycle=cycle,
        store=store,
        backfiller=HistoricalCandleBackfiller(public_rest, store),
        rest_client=public_rest,
        ingestor=stream,
        engine=engine,
        system_events=getattr(application.state, "system_events", None),
    )
    try:
        await runtime.start()
    except Exception:
        await public_rest.close()
        engine.dispose()
        raise
    application.state.paper_runtime = runtime
    return runtime


def _bucket_start(moment: datetime) -> datetime:
    seconds = int(INTERVAL.total_seconds())
    return datetime.fromtimestamp(int(moment.timestamp()) // seconds * seconds, tz=UTC)


def _integer(
    environ: Mapping[str, str],
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int | None = None,
) -> int:
    try:
        value = int(environ.get(name, str(default)))
    except ValueError as exc:
        raise StartupGuardError(f"{name} must be an integer") from exc
    if value < minimum or (maximum is not None and value > maximum):
        suffix = f" and at most {maximum}" if maximum is not None else ""
        raise StartupGuardError(f"{name} must be at least {minimum}{suffix}")
    return value


def _decimal(environ: Mapping[str, str], name: str, default: str, *, positive: bool) -> Decimal:
    try:
        value = Decimal(environ.get(name, default))
    except InvalidOperation as exc:
        raise StartupGuardError(f"{name} must be a decimal") from exc
    if not value.is_finite() or (value <= 0 if positive else value < 0):
        qualifier = "positive" if positive else "non-negative"
        raise StartupGuardError(f"{name} must be a finite {qualifier} decimal")
    return value
