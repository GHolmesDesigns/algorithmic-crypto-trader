"""Idempotent candle storage used by backfill and replay-safe ingestion."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from core.models import Candle
from db.models import MarketCandleRecord
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker


class CandleStore(Protocol):
    def latest_opened_at(self, symbol: str, interval: str) -> datetime | None: ...

    def upsert_many(self, candles: tuple[Candle, ...]) -> int: ...


class InMemoryCandleStore:
    """Deterministic store for tests and portable replay archives."""

    def __init__(self) -> None:
        self._candles: dict[tuple[str, str, datetime], Candle] = {}

    def latest_opened_at(self, symbol: str, interval: str) -> datetime | None:
        timestamps = [
            opened_at
            for candle_symbol, candle_interval, opened_at in self._candles
            if candle_symbol == symbol and candle_interval == interval
        ]
        return max(timestamps) if timestamps else None

    def upsert_many(self, candles: tuple[Candle, ...]) -> int:
        before = len(self._candles)
        for candle in candles:
            key = (candle.symbol, candle.interval, candle.opened_at)
            self._candles[key] = candle
        return len(self._candles) - before

    def read(self, symbol: str, interval: str) -> tuple[Candle, ...]:
        return tuple(
            sorted(
                (
                    candle
                    for (candle_symbol, candle_interval, _), candle in self._candles.items()
                    if candle_symbol == symbol and candle_interval == interval
                ),
                key=lambda candle: candle.opened_at,
            )
        )


class SqlAlchemyCandleStore:
    """PostgreSQL-backed store with a unique natural key for idempotent writes."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def latest_opened_at(self, symbol: str, interval: str) -> datetime | None:
        with self.session_factory() as session:
            statement = (
                select(MarketCandleRecord.opened_at)
                .where(
                    MarketCandleRecord.symbol == symbol,
                    MarketCandleRecord.interval == interval,
                )
                .order_by(MarketCandleRecord.opened_at.desc())
                .limit(1)
            )
            return session.scalar(statement)

    def upsert_many(self, candles: tuple[Candle, ...]) -> int:
        inserted = 0
        with self.session_factory.begin() as session:
            for candle in candles:
                statement = select(MarketCandleRecord).where(
                    MarketCandleRecord.symbol == candle.symbol,
                    MarketCandleRecord.interval == candle.interval,
                    MarketCandleRecord.opened_at == candle.opened_at,
                )
                record = session.scalar(statement)
                values = {
                    "closed_at": candle.closed_at,
                    "open": candle.open,
                    "high": candle.high,
                    "low": candle.low,
                    "close": candle.close,
                    "volume": candle.volume,
                    "source": candle.source,
                    "as_of": candle.as_of,
                    "ingested_at": candle.ingested_at,
                }
                if record is None:
                    session.add(
                        MarketCandleRecord(
                            symbol=candle.symbol,
                            interval=candle.interval,
                            opened_at=candle.opened_at,
                            **values,
                        )
                    )
                    inserted += 1
                else:
                    for name, value in values.items():
                        setattr(record, name, value)
        return inserted
