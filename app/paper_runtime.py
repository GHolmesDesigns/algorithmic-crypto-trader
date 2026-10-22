"""Paper-only orchestration for live public market data and the real trading path."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol

from api.alerts import Alert
from api.operator import OperatorState
from api.system_events import DISCONNECT_EVENT, GAP_FILL_EVENT, SystemEventJournal
from brokers.interface import BrokerInterface
from core.disconnect import DisconnectReason
from core.guards import StartupGuardError
from core.models import Candle, MarketState, Quote, TradingMode, utc_now
from core.reconnect import ReconnectSettings
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
    on_disconnect: Callable[[datetime, DisconnectReason], Awaitable[None]] | None
    on_healthy: Callable[[datetime], Awaitable[None]] | None
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
    # Each symbol's order quantity and smallest order value, keyed by symbol (#170).
    order_quantities: Mapping[str, Decimal]
    min_notionals: Mapping[str, Decimal]
    # True when ``PAPER_ORDER_QUANTITIES`` is set: each symbol's limits then come from the
    # venue. False keeps the single-symbol setup built from the broker's capabilities.
    venue_rules: bool
    estimated_slippage: Decimal
    cooldown: timedelta
    loss_state_path: Path | None
    reconnect_storm_threshold: int
    reconnect_storm_window: timedelta
    reconnect_storm_alert_interval: timedelta
    reconnect_healthy_period: timedelta

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> PaperRuntimeConfig:
        symbols = parse_paper_symbols(environ)
        fast = _integer(environ, "PAPER_STRATEGY_FAST_BARS", 3, minimum=1)
        slow = _integer(environ, "PAPER_STRATEGY_SLOW_BARS", 8, minimum=2)
        if slow <= fast:
            raise StartupGuardError("PAPER_STRATEGY_SLOW_BARS must exceed the fast window")
        history = _integer(environ, "PAPER_HISTORY_BARS", 50, minimum=slow + 1, maximum=300)
        quantities = _symbol_decimals(environ, "PAPER_ORDER_QUANTITIES", symbols)
        if quantities:
            missing = [item for item in symbols if item not in quantities]
            if missing:
                raise StartupGuardError(
                    f"PAPER_ORDER_QUANTITIES has no quantity for {', '.join(missing)}"
                )
        elif len(symbols) == 1:
            quantities = {
                symbols[0]: _decimal(environ, "PAPER_ORDER_QUANTITY", "0.0001", positive=True)
            }
        else:
            raise StartupGuardError(
                "PAPER_ORDER_QUANTITIES must give each of the PAPER_SYMBOLS its own quantity"
            )
        min_notional = _decimal(environ, "PAPER_MIN_NOTIONAL", "1", positive=True)
        notional_overrides = _symbol_decimals(environ, "PAPER_MIN_NOTIONALS", symbols)
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
        reconnect = ReconnectSettings.from_env(environ)
        return cls(
            symbols=symbols,
            history_bars=history,
            fast_window=fast,
            slow_window=slow,
            order_quantities=quantities,
            min_notionals={item: notional_overrides.get(item, min_notional) for item in symbols},
            venue_rules=bool(environ.get("PAPER_ORDER_QUANTITIES", "").strip()),
            estimated_slippage=slippage,
            cooldown=timedelta(seconds=cooldown_seconds),
            loss_state_path=loss_path,
            reconnect_storm_threshold=reconnect.storm_threshold,
            reconnect_storm_window=reconnect.storm_window,
            reconnect_storm_alert_interval=reconnect.storm_alert_interval,
            reconnect_healthy_period=reconnect.healthy_connection_period,
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
        self._alerted_at: dict[str, datetime] = {}
        self._disconnects: deque[datetime] = deque()
        self.ingestor.on_candle = self.on_candle
        self.ingestor.gap_fill = self.gap_fill
        self.ingestor.on_disconnect = self.on_disconnect
        self.ingestor.on_healthy = self.on_stream_healthy
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

    async def on_disconnect(self, at: datetime, reason: DisconnectReason) -> None:
        if self.system_events is not None:
            # A DisconnectReason redacts and bounds itself, so the stored note is safe.
            self.system_events.record(
                DISCONNECT_EVENT,
                {"at": at.isoformat(), "reason_kind": reason.kind, "reason_note": reason.note},
                now=at,
            )
        self._expire_disconnects(at)
        self._disconnects.append(at)
        if len(self._disconnects) >= self.config.reconnect_storm_threshold:
            await self._alert_once(
                "market_data_reconnect_storm",
                "Market-data reconnect storm: "
                f"{len(self._disconnects)} disconnects in "
                f"{self.config.reconnect_storm_window.total_seconds():g}s; "
                f"latest reason={reason.kind}",
                severity="warning",
                repeat_after=self.config.reconnect_storm_alert_interval,
                now=at,
            )

    async def on_stream_healthy(self, at: datetime) -> None:
        """Clear reconnect alerts only after the stream's heartbeat health window.

        Disconnect history survives healthy periods and leaves the window by age only, so a feed
        that drops every minute still counts as a storm. The storm alert clears once the window
        holds fewer disconnects than the threshold.
        """

        self._expire_disconnects(at)
        if len(self._disconnects) < self.config.reconnect_storm_threshold:
            self._clear_alert("market_data_reconnect_storm")
        self._clear_alert("market_data_disconnected")

    def _expire_disconnects(self, at: datetime) -> None:
        cutoff = at - self.config.reconnect_storm_window
        while self._disconnects and self._disconnects[0] < cutoff:
            self._disconnects.popleft()

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
        self._clear_alert("market_data_disconnected")
        self._clear_alert("market_data_processing_failed")
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
        self,
        condition: str,
        message: str,
        *,
        severity: str = "critical",
        repeat_after: timedelta | None = None,
        now: datetime | None = None,
    ) -> None:
        current = now or utc_now()
        last = self._alerted_at.get(condition)
        if condition in self._active_alerts and (
            repeat_after is None or last is None or current - last < repeat_after
        ):
            return
        self._active_alerts.add(condition)
        try:
            await self.operator.emit_alert(
                Alert(condition=condition, severity=severity, message=message)
            )
        except Exception:
            # Notification failure must not escape into the stream reconnect loop.
            logger.exception("alert delivery path failed condition=%s", condition)
            self._clear_alert(condition)
            return
        self._alerted_at[condition] = current

    def _clear_alert(self, condition: str) -> None:
        self._active_alerts.discard(condition)
        self._alerted_at.pop(condition, None)


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
    capabilities = operator.broker.capabilities
    # Every symbol's limits are settled before anything is opened, so a symbol without
    # complete rules refuses the start and is never traded with another symbol's numbers.
    constraints = await resolve_constraints(config, operator.broker)
    engine = create_database_engine(settings.database_url, settings.trading_mode)
    session_factory = create_session_factory(engine)
    store = SqlAlchemyCandleStore(session_factory)
    strategy = MovingAverageCrossStrategy(
        fast=config.fast_window,
        slow=config.slow_window,
        quantity=config.order_quantities,
    )
    operator.register_strategy("primary", strategy.strategy_version)
    operator.set_local_data(strategy_version=strategy.strategy_version)
    public_rest = rest_client or CoinbaseRESTClient()
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
    stream = ingestor or CoinbaseWebSocketIngestor(
        config.symbols,
        healthy_connection_seconds=config.reconnect_healthy_period.total_seconds(),
    )
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


async def resolve_constraints(
    config: PaperRuntimeConfig, broker: BrokerInterface
) -> dict[str, ExchangeConstraints]:
    """Each traded symbol's exchange limits, or ``StartupGuardError`` when any is incomplete.

    Without ``PAPER_ORDER_QUANTITIES`` the one symbol keeps the limits it always had: the
    broker's increments and ``PAPER_MIN_NOTIONAL``. With it, every symbol takes the venue's
    published rules (a read-only call), and the larger of the venue's and the configured
    minimum order value applies. A quantity that does not sit on its symbol's own step is
    refused here, before any order can be built.
    """

    capabilities = broker.capabilities
    result: dict[str, ExchangeConstraints] = {}
    for symbol in config.symbols:
        quantity = config.order_quantities[symbol]
        if not config.venue_rules:
            increment = capabilities.quantity_increment
            if increment <= 0 or capabilities.price_increment <= 0:
                raise StartupGuardError("configured broker increments must be positive")
            constraints = ExchangeConstraints(
                min_quantity=increment,
                quantity_increment=increment,
                min_notional=config.min_notionals[symbol],
                price_increment=capabilities.price_increment,
            )
            if not _quantity_fits(constraints, quantity):
                raise StartupGuardError(
                    "PAPER_ORDER_QUANTITY does not match the configured broker quantity increment"
                )
        else:
            try:
                rules = await broker.get_symbol_rules(symbol)
            except Exception as exc:
                logger.warning("exchange rules for %s could not be read", symbol, exc_info=True)
                raise StartupGuardError(
                    f"the exchange rules for {symbol} could not be read; refusing to start"
                ) from exc
            if rules is None:
                raise StartupGuardError(
                    f"the broker publishes no usable order rules for {symbol}; refusing to start"
                )
            constraints = ExchangeConstraints(
                min_quantity=rules.min_quantity,
                quantity_increment=rules.quantity_increment,
                min_notional=max(rules.min_notional or Decimal("0"), config.min_notionals[symbol]),
                price_increment=rules.price_increment,
            )
            if not _quantity_fits(constraints, quantity):
                raise StartupGuardError(
                    f"PAPER_ORDER_QUANTITIES for {symbol} does not match that symbol's "
                    f"minimum ({constraints.min_quantity}) and step "
                    f"({constraints.quantity_increment})"
                )
        result[symbol] = constraints
    return result


def _quantity_fits(constraints: ExchangeConstraints, quantity: Decimal) -> bool:
    """The risk engine's own test: at least the minimum, and a whole number of steps above it."""

    return (
        quantity >= constraints.min_quantity
        and (quantity - constraints.min_quantity) % constraints.quantity_increment == 0
    )


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
    return _parse_decimal(name, environ.get(name, default), positive=positive)


def _parse_decimal(name: str, raw: str, *, positive: bool) -> Decimal:
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise StartupGuardError(f"{name} must be a decimal") from exc
    if not value.is_finite() or (value <= 0 if positive else value < 0):
        qualifier = "positive" if positive else "non-negative"
        raise StartupGuardError(f"{name} must be a finite {qualifier} decimal")
    return value


def _symbol_decimals(
    environ: Mapping[str, str], name: str, symbols: tuple[str, ...]
) -> dict[str, Decimal]:
    """Parse ``BTC-USD:0.0001,ETH-USD:0.01``; empty when the setting is unset or blank.

    Every entry must name a symbol in ``PAPER_SYMBOLS`` once, so a typo or a leftover entry
    stops the start instead of silently sizing nothing.
    """

    result: dict[str, Decimal] = {}
    for item in environ.get(name, "").split(","):
        if not item.strip():
            continue
        symbol, separator, raw = item.partition(":")
        symbol = symbol.strip().upper()
        if not separator or not _SYMBOL.fullmatch(symbol):
            raise StartupGuardError(f"{name} entries must look like BTC-USD:0.0001")
        if symbol not in symbols:
            raise StartupGuardError(f"{name} names {symbol}, which is not in PAPER_SYMBOLS")
        if symbol in result:
            raise StartupGuardError(f"{name} names {symbol} more than once")
        result[symbol] = _parse_decimal(f"{name} for {symbol}", raw.strip(), positive=True)
    return result
