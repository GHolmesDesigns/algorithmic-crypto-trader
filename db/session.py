"""Database engine construction with explicit SQLite test/archive boundaries."""

from __future__ import annotations

import os

from core.models import TradingMode
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from db.models import Base


def validate_database_url(url: str, mode: TradingMode, environment: str | None = None) -> None:
    if (
        url.startswith("sqlite")
        and environment not in {"test", "archive"}
        and mode
        not in {
            TradingMode.BACKTEST,
            TradingMode.REPLAY,
        }
    ):
        raise ValueError(
            "SQLite is limited to isolated tests and portable backtest/replay archives"
        )
    if not url.startswith(("postgresql", "sqlite")):
        raise ValueError("DATABASE_URL must use PostgreSQL or SQLite")


def create_database_engine(
    url: str | None = None, mode: TradingMode = TradingMode.BACKTEST
) -> Engine:
    database_url = url or os.environ.get(
        "DATABASE_URL", "postgresql+psycopg://trader:trader@localhost:5432/trader"
    )
    validate_database_url(database_url, mode, os.environ.get("APP_ENV"))
    return create_engine(database_url, future=True, pool_pre_ping=True)


def create_session_factory(engine: Engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def initialize_database(engine: Engine) -> None:
    Base.metadata.create_all(engine)
