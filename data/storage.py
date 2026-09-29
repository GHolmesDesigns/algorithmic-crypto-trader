"""Idempotent candle storage used by backfill and replay-safe ingestion."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from core.models import Candle
from db.models import MarketCandleRecord
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker


class CandleStore(Protocol):
    def latest_opened_at(self, symbol: str, interval: str) -> datetime | None: ...

    def upsert_many(self, candles: tuple[Candle, ...]) -> int: ...

    def latest(self, symbol: str, interval: str, limit: int) -> tuple[Candle, ...]: ...


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

    def latest(self, symbol: str, interval: str, limit: int) -> tuple[Candle, ...]:
        if limit <= 0:
            raise ValueError("candle limit must be positive")
        return self.read(symbol, interval)[-limit:]


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

    def latest(self, symbol: str, interval: str, limit: int) -> tuple[Candle, ...]:
        if limit <= 0:
            raise ValueError("candle limit must be positive")
        with self.session_factory() as session:
            statement = (
                select(MarketCandleRecord)
                .where(
                    MarketCandleRecord.symbol == symbol,
                    MarketCandleRecord.interval == interval,
                )
                .order_by(MarketCandleRecord.opened_at.desc())
                .limit(limit)
            )
            rows = tuple(session.scalars(statement))
        return tuple(
            Candle(
                symbol=row.symbol,
                interval=row.interval,
                opened_at=row.opened_at,
                closed_at=row.closed_at,
                open=row.open,
                high=row.high,
                low=row.low,
                close=row.close,
                volume=row.volume,
                source=row.source,
                as_of=row.as_of,
                ingested_at=row.ingested_at,
            )
            for row in reversed(rows)
        )

    def prune_before(self, symbol: str, interval: str, cutoff: datetime, limit: int) -> int:
        """Delete up to ``limit`` of one symbol's oldest candles opened before ``cutoff``."""

        if limit <= 0:
            raise ValueError("prune limit must be positive")
        with self.session_factory.begin() as session:
            ids = list(
                session.scalars(
                    select(MarketCandleRecord.candle_id)
                    .where(
                        MarketCandleRecord.symbol == symbol,
                        MarketCandleRecord.interval == interval,
                        MarketCandleRecord.opened_at < cutoff,
                    )
                    .order_by(MarketCandleRecord.opened_at)
                    .limit(limit)
                )
            )
            if not ids:
                return 0
            session.execute(delete(MarketCandleRecord).where(MarketCandleRecord.candle_id.in_(ids)))
            return len(ids)
