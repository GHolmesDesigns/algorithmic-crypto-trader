"""Safe application entry point; guards run before service initialization."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from api.alerts import Alert, AlertRouter, build_alert_router
from api.diagnostics import SqlAlchemyDiagnostics
from api.diagnostics_routes import router as diagnostics_router
from api.history import SqlAlchemyHistory
from api.history_routes import router as history_router
from api.markets import SqlAlchemyCandleReads
from api.markets_page_routes import router as markets_page_router
from api.markets_routes import router as markets_router
from api.operator import OperatorState
from api.research import ResearchWorkspace
from api.research_routes import router as research_router
from api.routes import router
from api.soak import SqlAlchemySoak
from api.soak_routes import router as soak_router
from api.system_events import (
    HEARTBEAT_INTERVAL_SECONDS,
    RESTART_EVENT,
    SqlAlchemySystemEventJournal,
)
from api.trends import SqlAlchemyTrends
from api.watchlist_routes import router as watchlist_router
from brokers.http import describe_provider_failure
from core.guards import (
    StartupGuardError,
    StartupSettings,
    load_startup_settings,
    startup_banner,
)
from core.logging import configure_logging
from core.models import Order, TradingMode
from core.reconnect import ReconnectSettings
from core.version import application_version
from data.coinbase import CoinbaseRESTClient
from data.storage import SqlAlchemyCandleStore
from data.watchlist import SqlAlchemyWatchlist
from db.session import create_database_engine, create_session_factory
from execution.engine import ExecutionEngine
from execution.persistence import SqlAlchemyOrderStore
from fastapi import FastAPI
from portfolio.equity import EquitySampler, SqlAlchemyEquityStore
from portfolio.reconciliation import Discrepancy, PortfolioState, Reconciler
from portfolio.scheduler import ScheduledReconciler
from portfolio.store import SqlAlchemyPortfolioStore
from risk.kill_switch import KillSwitch
from risk.kill_switch_journal import SqlAlchemyKillSwitchJournal

from app.heartbeat import HeartbeatScheduler
from app.paper_runtime import parse_paper_symbols, start_paper_runtime
from app.recovery import StartupRecoveryResult, recover_on_startup
from app.startup_broker import assert_live_key_scope, build_startup_broker
from app.watch_feed import WatchFeedConfig, WatchOnlyFeed, build_watch_client

DEFAULT_RECONCILE_INTERVAL_SECONDS = 300.0
DEFAULT_EQUITY_SAMPLE_INTERVAL_SECONDS = 3600.0

logger = logging.getLogger(__name__)


def create_app(
    settings: StartupSettings | None = None,
    *,
    broker=None,
    alert_router: AlertRouter | None = None,
    recover_on_start: bool = False,
) -> FastAPI:
    """Build the service. With recover_on_start, recovery runs in the server's lifespan.

    Running recovery on the server's own event loop keeps provider HTTP clients,
    which bind to the loop that first uses them, usable after startup.
    """

    startup_settings = settings or load_startup_settings()
    application = FastAPI(
        title="Algorithmic Crypto Trader",
        version=application_version(),
        lifespan=_recovery_lifespan if recover_on_start else None,
    )
    application.router.routes.extend(router.routes)
    application.router.routes.extend(diagnostics_router.routes)
    application.router.routes.extend(history_router.routes)
    application.router.routes.extend(markets_router.routes)
    application.router.routes.extend(markets_page_router.routes)
    application.router.routes.extend(research_router.routes)
    application.router.routes.extend(soak_router.routes)
    application.router.routes.extend(watchlist_router.routes)
    switch_path = os.environ.get("KILL_SWITCH_FILE")
    application.state.kill_switch = KillSwitch(Path(switch_path) if switch_path else None)
    application.state.startup_settings = startup_settings
    application.state.operator_state = OperatorState(
        settings=startup_settings,
        kill_switch=application.state.kill_switch,
        broker=broker,
        alert_router=alert_router,
        strategy_version=os.environ.get("STRATEGY_VERSION", "unknown"),
    )
    application.state.research = ResearchWorkspace()
    return application


@asynccontextmanager
async def _recovery_lifespan(application: FastAPI) -> AsyncIterator[None]:
    broker = application.state.operator_state.broker
    stop_reconciliation: Callable[[], Awaitable[None]] | None = None
    stop_heartbeat: Callable[[], Awaitable[None]] | None = None
    close_journal: Callable[[], None] | None = None
    close_history: Callable[[], None] | None = None
    close_system_events: Callable[[], None] | None = None
    paper_runtime = None
    close_watchlist: Callable[[], Awaitable[None]] | None = None
    stop_watch_feed: Callable[[], Awaitable[None]] | None = None
    try:
        # Reject a bad interval before recovery does any work.
        interval = _reconcile_interval() if broker is not None else None
        await assert_live_key_scope(application.state.startup_settings, broker)
        close_journal = attach_kill_switch_journal(application)
        close_history = attach_history(application)
        close_system_events = attach_system_events(application)
        recovery = await run_startup_recovery(application)
        logger.info(
            "startup recovery %s: %s (kill switch %s)",
            recovery.status,
            recovery.detail,
            application.state.kill_switch.state.value,
        )
        stop_reconciliation = start_scheduled_reconciliation(application, interval_seconds=interval)
        stop_heartbeat = start_heartbeat_scheduler(application)
        paper_runtime = await start_paper_runtime(application)
        close_watchlist = attach_watchlist(application)
        stop_watch_feed = start_watch_feed(application)
        yield
    finally:
        try:
            if stop_watch_feed is not None:
                await stop_watch_feed()
        finally:
            if close_watchlist is not None:
                await close_watchlist()
        try:
            await application.state.research.close()
        finally:
            try:
                if paper_runtime is not None:
                    await paper_runtime.stop()
            finally:
                try:
                    if stop_reconciliation is not None:
                        await stop_reconciliation()
                finally:
                    try:
                        if stop_heartbeat is not None:
                            await stop_heartbeat()
                    finally:
                        try:
                            await application.state.operator_state.alert_router.close()
                        finally:
                            try:
                                close = getattr(broker, "close", None)
                                if close is not None:
                                    await close()
                            finally:
                                try:
                                    if close_journal is not None:
                                        close_journal()
                                finally:
                                    try:
                                        if close_history is not None:
                                            close_history()
                                    finally:
                                        if close_system_events is not None:
                                            close_system_events()


def attach_kill_switch_journal(application: FastAPI) -> Callable[[], None]:
    """Record kill-switch transitions in ``system_events``; return a close callback.

    The lifespan attaches it before startup recovery, so a recovery halt is recorded.
    Without it, the kill switch refuses every re-arm.
    """

    settings: StartupSettings = application.state.startup_settings
    engine = create_database_engine(settings.database_url, settings.trading_mode)
    application.state.kill_switch.attach_journal(
        SqlAlchemyKillSwitchJournal(create_session_factory(engine))
    )
    return engine.dispose


def attach_history(application: FastAPI) -> Callable[[], None]:
    """Serve bounded, read-only history, trends, market candles, the soak console, and diagnostics.

    Returns a close callback. Without it, the history, trends, candle, and soak routes
    answer 503: not available, never an empty history or a chart of zeros. The diagnostics
    report instead marks each stored section unavailable.
    """

    settings: StartupSettings = application.state.startup_settings
    engine = create_database_engine(settings.database_url, settings.trading_mode)
    session_factory = create_session_factory(engine)
    application.state.history = SqlAlchemyHistory(session_factory)
    application.state.diagnostics = SqlAlchemyDiagnostics(session_factory)
    application.state.trends = SqlAlchemyTrends(session_factory)
    application.state.market_candles = SqlAlchemyCandleReads(session_factory)
    reconnect = ReconnectSettings.from_env()
    application.state.soak = SqlAlchemySoak(
        session_factory,
        reconnect_storm_threshold=reconnect.storm_threshold,
        reconnect_storm_window=reconnect.storm_window,
    )
    return engine.dispose


def attach_system_events(application: FastAPI) -> Callable[[], None]:
    """Persist restart, disconnect, gap-fill, and heartbeat events; return a close callback.

    Attached before startup recovery, so the restart this process is making gets
    its own row before recovery can halt the kill switch.
    """

    settings: StartupSettings = application.state.startup_settings
    engine = create_database_engine(settings.database_url, settings.trading_mode)
    application.state.system_events = SqlAlchemySystemEventJournal(create_session_factory(engine))
    return engine.dispose


def attach_watchlist(application: FastAPI) -> Callable[[], Awaitable[None]]:
    """Serve the saved watchlist and its public product lookup; return a close callback.

    Public Coinbase market data only. The client is the watch feed's own, so lookups
    and the feed share one request budget that the trading feed never draws on.
    Without it, the watchlist routes answer 503.
    """

    settings: StartupSettings = application.state.startup_settings
    engine = create_database_engine(settings.database_url, settings.trading_mode)
    application.state.watchlist = SqlAlchemyWatchlist(create_session_factory(engine))
    client = build_watch_client()
    application.state.watch_client = client

    async def close() -> None:
        try:
            await client.close()
        finally:
            engine.dispose()

    return close


def start_watch_feed(application: FastAPI, *, environ=None) -> Callable[[], Awaitable[None]] | None:
    """Start the store-only watch feed when WATCH_FEED_ENABLED is set; off by default."""

    values = os.environ if environ is None else environ
    config = WatchFeedConfig.from_env(values)
    if not config.enabled:
        return None
    settings: StartupSettings = application.state.startup_settings
    engine = create_database_engine(settings.database_url, settings.trading_mode)
    client: CoinbaseRESTClient = application.state.watch_client
    feed = WatchOnlyFeed(
        watchlist=application.state.watchlist,
        trading_symbols=parse_paper_symbols(values),
        store=SqlAlchemyCandleStore(create_session_factory(engine)),
        source=client,
        config=config,
    )
    application.state.watch_feed = feed
    application.state.operator_state.watch_feed = feed
    stop = asyncio.Event()
    task = asyncio.create_task(feed.run(stop), name="watch-only-feed")

    async def stop_feed() -> None:
        stop.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            engine.dispose()

    return stop_feed


def start_heartbeat_scheduler(
    application: FastAPI, *, interval_seconds: float = HEARTBEAT_INTERVAL_SECONDS
) -> Callable[[], Awaitable[None]]:
    """Sample the heartbeat onto ``system_events`` every interval; return a stop callback."""

    scheduler = HeartbeatScheduler(
        application.state.operator_state,
        application.state.system_events,
        interval_seconds=interval_seconds,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(scheduler.run(stop))

    async def stop_scheduler() -> None:
        stop.set()
        await task

    return stop_scheduler


def start_scheduled_reconciliation(
    application: FastAPI, *, interval_seconds: float | None = None
) -> Callable[[], Awaitable[None]] | None:
    """Reconcile against the configured broker every interval; return a stop callback.

    Without a broker there is nothing to reconcile against, so nothing starts.
    The baseline is the broker snapshot that startup recovery just saved, plus
    the persisted orders still open and their recorded fills; if it cannot be
    loaded, the first run compares against an empty portfolio and halts.

    Trading must go through ``application.state.execution`` while holding
    ``application.state.trading_lock``: that engine persists every order and
    reports it and its fills to the reconciler, which re-reads open orders
    through it before each comparison.
    """

    operator_state: OperatorState = application.state.operator_state
    if operator_state.broker is None:
        return None
    interval = interval_seconds if interval_seconds is not None else _reconcile_interval()
    equity_interval = _equity_sample_interval()
    settings: StartupSettings = application.state.startup_settings
    engine = create_database_engine(settings.database_url, settings.trading_mode)
    session_factory = create_session_factory(engine)
    portfolio_store = SqlAlchemyPortfolioStore(session_factory)
    order_store = SqlAlchemyOrderStore(session_factory)
    try:
        snapshot = portfolio_store.latest_state(source="broker") or PortfolioState()
        open_orders = order_store.open_orders()
        baseline = PortfolioState(
            orders={str(order.request.client_order_id): order for order in open_orders},
            fills={fill.fill_id: fill for fill in order_store.fills_for(open_orders)},
            positions=snapshot.positions,
            balances=snapshot.balances,
        )
    except Exception:
        logger.exception("reconciliation baseline could not be loaded")
        baseline = PortfolioState()

    async def on_divergence(discrepancies: tuple[Discrepancy, ...]) -> None:
        await operator_state.emit_alert(
            Alert(
                condition="reconciliation_divergence",
                severity="critical",
                message=(
                    f"{len(discrepancies)} difference(s) between local and broker state; "
                    "trading halted and the broker's record adopted"
                ),
            )
        )

    async def on_unavailable(_detail: str) -> None:
        await operator_state.emit_alert(
            Alert(
                condition="reconciliation_unavailable",
                severity="critical",
                message="broker state could not be reconciled; trading halted",
            )
        )

    equity_store = SqlAlchemyEquityStore(session_factory)
    sampler = EquitySampler(operator_state.broker, equity_store, equity_store)
    async def alert_never_sent_order(order: Order, failure: Exception) -> None:
        reference = str(order.request.client_order_id)[:8]
        await operator_state.emit_alert(
            Alert(
                condition="order_closed_never_received",
                severity="warning",
                message=(
                    f"Order {reference} was closed as never sent because its pre-submit status "
                    f"lookup failed ({describe_provider_failure(failure)}). It was not submitted "
                    "or resubmitted; the kill switch is unchanged."
                ),
            )
        )

    execution = ExecutionEngine(
        operator_state.broker, order_store, on_order_closed=alert_never_sent_order
    )
    scheduler = ScheduledReconciler(
        Reconciler(
            operator_state.broker,
            application.state.kill_switch,
            store=portfolio_store,
            log_values=_may_log_divergence_values(settings),
        ),
        baseline=baseline,
        interval_seconds=interval,
        refresh_order=execution.recover,
        on_divergence=on_divergence,
        on_unavailable=on_unavailable,
        on_reconciled=sampler.sample,
        balance_increments=operator_state.broker.capabilities.balance_increments,
    )
    execution.on_recorded = scheduler.observe
    application.state.execution = execution
    application.state.trading_lock = scheduler.lock
    operator_state.scheduled_reconciliation = scheduler
    stop = asyncio.Event()
    task = asyncio.create_task(scheduler.run(stop))
    sampling = asyncio.create_task(sampler.run(stop, equity_interval, lock=scheduler.lock))

    async def stop_scheduler() -> None:
        stop.set()
        await task
        await sampling
        engine.dispose()

    return stop_scheduler


def _may_log_divergence_values(settings: StartupSettings) -> bool:
    """Both sides of a divergence go to the log in full only in ``paper``, a sandbox.

    Every other mode, live above all, logs the field and the delta alone.
    """

    return settings.trading_mode is TradingMode.PAPER


def _equity_sample_interval() -> float:
    raw = os.environ.get("EQUITY_SAMPLE_INTERVAL_SECONDS", "").strip()
    if not raw:
        return DEFAULT_EQUITY_SAMPLE_INTERVAL_SECONDS
    try:
        interval = float(raw)
    except ValueError as exc:
        raise StartupGuardError("EQUITY_SAMPLE_INTERVAL_SECONDS must be a number") from exc
    if not 0 < interval <= 3600:
        raise StartupGuardError("EQUITY_SAMPLE_INTERVAL_SECONDS must be between 0 and 3600")
    return interval


def _reconcile_interval() -> float:
    raw = os.environ.get("RECONCILE_INTERVAL_SECONDS", "").strip()
    if not raw:
        return DEFAULT_RECONCILE_INTERVAL_SECONDS
    try:
        interval = float(raw)
    except ValueError as exc:
        raise StartupGuardError("RECONCILE_INTERVAL_SECONDS must be a number") from exc
    if not 0 < interval <= 3600:
        raise StartupGuardError("RECONCILE_INTERVAL_SECONDS must be between 0 and 3600")
    return interval


async def run_startup_recovery(application: FastAPI, *, broker=None) -> StartupRecoveryResult:
    """Recover persisted state before the HTTP server accepts any request.

    Also records one ``restart`` system event: ``recovered`` when a pending order
    from a previous run had to be resolved, ``clean`` otherwise. Recorded on this
    call's own engine so it lands even when ``attach_system_events`` never ran,
    such as a caller that only wants the recovery result.
    """

    settings: StartupSettings = application.state.startup_settings
    recovery_broker = broker if broker is not None else application.state.operator_state.broker
    engine = create_database_engine(settings.database_url, settings.trading_mode)
    try:
        session_factory = create_session_factory(engine)
        result = await recover_on_startup(
            kill_switch=application.state.kill_switch,
            order_store=SqlAlchemyOrderStore(session_factory),
            portfolio_store=SqlAlchemyPortfolioStore(session_factory),
            broker=recovery_broker,
            log_values=_may_log_divergence_values(settings),
        )
        SqlAlchemySystemEventJournal(session_factory).record(
            RESTART_EVENT,
            {
                "kind": "recovered" if result.pending_orders > 0 else "clean",
                "status": result.status,
            },
        )
    finally:
        engine.dispose()
    application.state.operator_state.startup_recovery = result
    return result


def main() -> None:
    settings = load_startup_settings()
    configure_logging(settings.log_level)
    logger.info(startup_banner(settings))
    broker = build_startup_broker(settings)
    alert_router = build_alert_router(os.environ)
    application = create_app(
        settings, broker=broker, alert_router=alert_router, recover_on_start=True
    )
    import uvicorn

    # uvicorn completes the lifespan startup, including recovery, before it
    # accepts connections, and a failed startup exits the process.
    uvicorn.run(application, host="0.0.0.0", port=8000, lifespan="on")
