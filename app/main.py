"""Safe application entry point; guards run before service initialization."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from api.alerts import AlertRouter
from api.operator import OperatorState
from api.routes import router
from core.guards import StartupSettings, load_startup_settings, startup_banner
from core.logging import configure_logging
from db.session import create_database_engine, create_session_factory
from execution.persistence import SqlAlchemyOrderStore
from fastapi import FastAPI
from portfolio.store import SqlAlchemyPortfolioStore
from risk.kill_switch import KillSwitch

from app.recovery import StartupRecoveryResult, recover_on_startup


def create_app(
    settings: StartupSettings | None = None,
    *,
    broker=None,
    alert_router: AlertRouter | None = None,
) -> FastAPI:
    startup_settings = settings or load_startup_settings()
    application = FastAPI(title="Algorithmic Crypto Trader", version="0.1.0")
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


async def run_startup_recovery(application: FastAPI, *, broker=None) -> StartupRecoveryResult:
    """Recover persisted state before the HTTP server accepts any request."""

    settings: StartupSettings = application.state.startup_settings
    engine = create_database_engine(settings.database_url, settings.trading_mode)
    try:
        session_factory = create_session_factory(engine)
        result = await recover_on_startup(
            kill_switch=application.state.kill_switch,
            order_store=SqlAlchemyOrderStore(session_factory),
            portfolio_store=SqlAlchemyPortfolioStore(session_factory),
            broker=broker,
        )
    finally:
        engine.dispose()
    application.state.operator_state.startup_recovery = result
    return result


def main() -> None:
    settings = load_startup_settings()
    configure_logging(settings.log_level)
    logger = logging.getLogger(__name__)
    logger.info(startup_banner(settings))
    application = create_app(settings)
    recovery = asyncio.run(run_startup_recovery(application))
    logger.info(
        "startup recovery %s: %s (kill switch %s)",
        recovery.status,
        recovery.detail,
        application.state.kill_switch.state.value,
    )
    import uvicorn

    uvicorn.run(application, host="0.0.0.0", port=8000)
