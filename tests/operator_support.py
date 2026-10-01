"""Operator-surface helpers: a SQLite database that the real journal and history use."""

from __future__ import annotations

from pathlib import Path

from api.controls import REARM_CHECKLIST
from app.main import attach_history, attach_kill_switch_journal, create_app
from core.guards import CredentialScope, StartupSettings
from core.models import TradingMode
from db.models import Base, SystemEventRecord
from fastapi import FastAPI
from sqlalchemy import create_engine

# A complete re-arm review: every checklist item and a cause-and-approval reference.
REARM = {
    "checklist": [key for key, _ in REARM_CHECKLIST],
    "reason": "INC-42 drill pause ended; approved by the incident owner",
}
# The one <link> an operator page may carry: the same-origin tab icon, never a stylesheet.
ICON_LINK = '<link rel="icon" href="/favicon.ico" sizes="any">'


def sqlite_settings(
    tmp_path: Path,
    mode: TradingMode = TradingMode.BACKTEST,
    scope: CredentialScope = CredentialScope.NONE,
) -> StartupSettings:
    """Settings for a file database with the ``system_events`` table the journal uses."""

    url = f"sqlite+pysqlite:///{tmp_path / 'trader.db'}"
    engine = create_engine(url, future=True)
    SystemEventRecord.__table__.create(engine, checkfirst=True)
    engine.dispose()
    return StartupSettings(mode, scope, "", url, "INFO")


def journaled_app(tmp_path: Path, mode: TradingMode = TradingMode.BACKTEST, **options) -> FastAPI:
    """``create_app`` plus the journal that the service lifespan attaches at startup.

    Paper or live mode with SQLite needs ``APP_ENV=test``.
    """

    application = create_app(sqlite_settings(tmp_path, mode), **options)
    attach_kill_switch_journal(application)
    return application


def history_app(tmp_path: Path, mode: TradingMode = TradingMode.BACKTEST, **options) -> FastAPI:
    """``journaled_app`` with every table and the history reads the lifespan attaches."""

    settings = sqlite_settings(tmp_path, mode)
    engine = create_engine(settings.database_url, future=True)
    Base.metadata.create_all(engine)
    engine.dispose()
    application = create_app(settings, **options)
    attach_kill_switch_journal(application)
    attach_history(application)
    return application
