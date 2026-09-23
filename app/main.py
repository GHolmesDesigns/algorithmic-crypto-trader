"""Safe application entry point; guards run before service initialization."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from api.alerts import AlertRouter
from api.operator import OperatorState
from api.routes import router
from core.guards import StartupSettings, load_startup_settings, startup_banner
from core.logging import configure_logging
from fastapi import FastAPI
from risk.kill_switch import KillSwitch


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


def main() -> None:
    settings = load_startup_settings()
    configure_logging(settings.log_level)
    logging.getLogger(__name__).info(startup_banner(settings))
    application = create_app(settings)
    import uvicorn

    uvicorn.run(application, host="0.0.0.0", port=8000)
