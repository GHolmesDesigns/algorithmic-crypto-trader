"""Safe application entry point; guards run before service initialization."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from api.alerts import Alert, AlertRouter
from api.operator import OperatorState
from api.routes import router
from core.guards import (
    StartupGuardError,
    StartupSettings,
    load_startup_settings,
    startup_banner,
)
from core.logging import configure_logging
from db.session import create_database_engine, create_session_factory
from execution.persistence import SqlAlchemyOrderStore
from fastapi import FastAPI
from portfolio.reconciliation import Discrepancy, PortfolioState, Reconciler
from portfolio.scheduler import ScheduledReconciler
from portfolio.store import SqlAlchemyPortfolioStore
from risk.kill_switch import KillSwitch

from app.recovery import StartupRecoveryResult, recover_on_startup
from app.startup_broker import assert_live_key_scope, build_startup_broker

DEFAULT_RECONCILE_INTERVAL_SECONDS = 300.0

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
        version="0.1.0",
        lifespan=_recovery_lifespan if recover_on_start else None,
    )
    application.router.routes.extend(router.routes)
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
    return application


@asynccontextmanager
async def _recovery_lifespan(application: FastAPI) -> AsyncIterator[None]:
    broker = application.state.operator_state.broker
    await assert_live_key_scope(application.state.startup_settings, broker)
    recovery = await run_startup_recovery(application)
    logger.info(
        "startup recovery %s: %s (kill switch %s)",
        recovery.status,
        recovery.detail,
        application.state.kill_switch.state.value,
    )
    stop_reconciliation = start_scheduled_reconciliation(application)
    try:
        yield
    finally:
        if stop_reconciliation is not None:
            await stop_reconciliation()
        close = getattr(broker, "close", None)
        if close is not None:
            await close()


def start_scheduled_reconciliation(
    application: FastAPI,
) -> Callable[[], Awaitable[None]] | None:
    """Reconcile against the configured broker every interval; return a stop callback.

    Without a broker there is nothing to reconcile against, so nothing starts.
    The baseline is the broker snapshot that startup recovery just saved; if it
    cannot be loaded, the first run compares against an empty portfolio and halts.
    """

    operator_state: OperatorState = application.state.operator_state
    if operator_state.broker is None:
        return None
    interval = _reconcile_interval()
    settings: StartupSettings = application.state.startup_settings
    engine = create_database_engine(settings.database_url, settings.trading_mode)
    portfolio_store = SqlAlchemyPortfolioStore(create_session_factory(engine))
    try:
        baseline = portfolio_store.latest_state(source="broker") or PortfolioState()
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

    scheduler = ScheduledReconciler(
        Reconciler(operator_state.broker, application.state.kill_switch, store=portfolio_store),
        baseline=baseline,
        interval_seconds=interval,
        on_divergence=on_divergence,
        on_unavailable=on_unavailable,
    )
    operator_state.scheduled_reconciliation = scheduler
    stop = asyncio.Event()
    task = asyncio.create_task(scheduler.run(stop))

    async def stop_scheduler() -> None:
        stop.set()
        await task
        engine.dispose()

    return stop_scheduler


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
    """Recover persisted state before the HTTP server accepts any request."""

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
    application = create_app(settings, broker=broker, recover_on_start=True)
    import uvicorn

    # uvicorn completes the lifespan startup, including recovery, before it
    # accepts connections, and a failed startup exits the process.
    uvicorn.run(application, host="0.0.0.0", port=8000, lifespan="on")
