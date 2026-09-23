"""Resumable historical candle backfill orchestration."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol

from core.models import Candle

from data.coinbase import GRANULARITY_SECONDS, MAX_CANDLES_PER_REQUEST
from data.storage import CandleStore
from data.validation import MarketDataValidator


class CandleClient(Protocol):
    async def get_candles(
        self,
        product_id: str,
        start: datetime,
        end: datetime,
        *,
        granularity: str,
    ) -> tuple[Candle, ...]:
        """Fetch one bounded page of normalized candles."""


class HistoricalCandleBackfiller:
    def __init__(
        self,
        client: CandleClient,
        store: CandleStore,
        *,
        validator: MarketDataValidator | None = None,
    ) -> None:
        self.client = client
        self.store = store
        self.validator = validator or MarketDataValidator()

    async def run(
        self,
        product_id: str,
        start: datetime,
        end: datetime,
        *,
        granularity: str = "ONE_MINUTE",
    ) -> int:
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("backfill bounds must be timezone-aware")
        if end <= start:
            return 0
        interval = timedelta(seconds=GRANULARITY_SECONDS[granularity])
        cursor = start.astimezone(UTC)
        latest = self.store.latest_opened_at(product_id, granularity)
        if latest is not None:
            cursor = max(cursor, latest + interval)
        written = 0
        page_span = interval * MAX_CANDLES_PER_REQUEST
        while cursor < end:
            page_end = min(end, cursor + page_span)
            candles = await self.client.get_candles(
                product_id,
                cursor,
                page_end,
                granularity=granularity,
            )
            if not candles:
                raise RuntimeError("Coinbase returned no candles for a non-empty backfill page")
            normalized = self.validator.validate_candles(candles, expected_interval=interval)
            page = tuple(candle for candle in normalized if cursor <= candle.opened_at < page_end)
            if not page:
                raise RuntimeError("Coinbase candle page did not overlap the requested window")
            if page[0].opened_at != cursor or page[-1].opened_at != page_end - interval:
                raise RuntimeError("Coinbase candle page contains an unexplained edge gap")
            written += self.store.upsert_many(page)
            cursor = page_end
        return written
