"""Store-only feed for the coins on the watchlist that are not traded.

This module collects closed five-minute Coinbase candles for watched symbols
that are not in ``PAPER_SYMBOLS`` and writes them to the candle store. Nothing
else reads them: it does not import the trading cycle, strategy, risk, or
execution code, and it takes neither the operator state nor the kill switch. A
failure here changes one symbol's feed status and nothing else.

Request budget (public Coinbase market data, no credentials), all through one
``TokenBucketRateLimiter`` and one ``CircuitBreaker`` that the trading feed does
not share:

- steady state: one request per watched symbol every five minutes, at most 9;
- adding a coin: a 30-day backfill, 29 requests of 300 candles;
- at most one request per second averaged, with a burst of 2.

A symbol that fails is retried after 1, 2, 4, ... minutes, never faster, and
never more slowly than every 30 minutes.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import httpx
from core.guards import StartupGuardError
from core.models import Candle, utc_now
from core.resilience import CircuitBreaker, CircuitOpen, RateLimitExceeded, TokenBucketRateLimiter
from data.coinbase import GRANULARITY_SECONDS, MAX_CANDLES_PER_REQUEST, CoinbaseRESTClient
from data.stream import WEBSOCKET_CANDLE_INTERVAL
from data.validation import MarketDataValidationError, MarketDataValidator
from data.watchlist import SqlAlchemyWatchlist

logger = logging.getLogger(__name__)

INTERVAL_NAME = WEBSOCKET_CANDLE_INTERVAL
INTERVAL = timedelta(seconds=GRANULARITY_SECONDS[INTERVAL_NAME])
BACKFILL_DAYS = 30
STALE_AFTER = INTERVAL * 3
COLLECT_DELAY = timedelta(seconds=15)
FIRST_BACKOFF = timedelta(minutes=1)
MAX_BACKOFF = timedelta(minutes=30)
PRUNE_EVERY = timedelta(hours=1)
PRUNE_BATCH = 5000
DEFAULT_RETENTION_DAYS = 30
MIN_RETENTION_DAYS = 30
MAX_RETENTION_DAYS = 365

FRESH = "fresh"
STALE = "stale"
NOT_COLLECTED = "not_collected"
UNAVAILABLE = "unavailable"


class WatchCandleStore(Protocol):
    def latest_opened_at(self, symbol: str, interval: str) -> datetime | None: ...

    def upsert_many(self, candles: tuple[Candle, ...]) -> int: ...

    def prune_before(self, symbol: str, interval: str, cutoff: datetime, limit: int) -> int: ...


class CandleSource(Protocol):
    async def get_candles(
        self, product_id: str, start: datetime, end: datetime, *, granularity: str
    ) -> tuple[Candle, ...]: ...


@dataclass(frozen=True, slots=True)
class WatchFeedConfig:
    enabled: bool
    retention: timedelta

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> WatchFeedConfig:
        raw = environ.get("WATCH_FEED_ENABLED", "0").strip().lower()
        if raw not in {"0", "1", "false", "true"}:
            raise StartupGuardError("WATCH_FEED_ENABLED must be true or false")
        try:
            days = int(environ.get("WATCH_FEED_RETENTION_DAYS", str(DEFAULT_RETENTION_DAYS)))
        except ValueError as exc:
            raise StartupGuardError("WATCH_FEED_RETENTION_DAYS must be an integer") from exc
        if not MIN_RETENTION_DAYS <= days <= MAX_RETENTION_DAYS:
            raise StartupGuardError(
                f"WATCH_FEED_RETENTION_DAYS must be between {MIN_RETENTION_DAYS} "
                f"and {MAX_RETENTION_DAYS}"
            )
        return cls(enabled=raw in {"1", "true"}, retention=timedelta(days=days))


@dataclass(slots=True)
class SymbolFeedStatus:
    symbol: str
    last_candle_at: datetime | None = None
    failures: int = 0
    last_error: str | None = None
    next_attempt_at: datetime | None = None

    def state(self, now: datetime) -> str:
        if self.failures > 0:
            return UNAVAILABLE
        if self.last_candle_at is None:
            return NOT_COLLECTED
        if now - (self.last_candle_at + INTERVAL) > STALE_AFTER:
            return STALE
        return FRESH


def build_watch_client() -> CoinbaseRESTClient:
    """The watch feed's own public client, so its failures never trip the trading feed's."""

    return CoinbaseRESTClient(
        rate_limiter=TokenBucketRateLimiter(capacity=2, refill_per_second=1.0),
        circuit_breaker=CircuitBreaker(failure_threshold=5, recovery_timeout=60.0),
    )


class WatchOnlyFeed:
    """Collect and store closed candles for watch-only symbols; never trade on them."""

    def __init__(
        self,
        *,
        watchlist: SqlAlchemyWatchlist,
        trading_symbols: tuple[str, ...],
        store: WatchCandleStore,
        source: CandleSource,
        config: WatchFeedConfig,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.watchlist = watchlist
        self.trading_symbols = frozenset(trading_symbols)
        self.store = store
        self.source = source
        self.config = config
        self.clock = clock
        self.statuses: dict[str, SymbolFeedStatus] = {}
        self.requests = 0
        self._validator = MarketDataValidator()
        self._wake = asyncio.Event()
        self._next_prune: datetime | None = None

    def watch_only(self) -> tuple[str, ...]:
        """Watched symbols the trading feed does not already collect."""

        return tuple(s for s in self.watchlist.symbols() if s not in self.trading_symbols)

    def wake(self) -> None:
        """Ask the run loop to collect now, for example after a coin was added."""

        self._wake.set()

    def to_dict(self, *, now: datetime | None = None) -> dict[str, Any]:
        moment = now or self.clock()
        return {
            "enabled": True,
            "symbols": [
                {
                    "symbol": status.symbol,
                    "state": status.state(moment),
                    "detail": status.last_error,
                    "last_candle_at": (
                        status.last_candle_at.isoformat() if status.last_candle_at else None
                    ),
                }
                for status in self.statuses.values()
            ],
        }

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.run_cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("watch-only feed cycle failed")
            self._wake.clear()
            now = self.clock()
            timeout = max(
                1.0, (_bucket_start(now) + INTERVAL + COLLECT_DELAY - now).total_seconds()
            )
            waiters = {
                asyncio.ensure_future(stop.wait()),
                asyncio.ensure_future(self._wake.wait()),
            }
            _, pending = await asyncio.wait(
                waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()

    async def run_cycle(self) -> None:
        now = self.clock()
        symbols = self.watch_only()
        for gone in [s for s in self.statuses if s not in symbols]:
            del self.statuses[gone]
        for symbol in symbols:
            status = self.statuses.get(symbol)
            if status is None:
                status = SymbolFeedStatus(symbol, last_candle_at=self._latest(symbol))
                self.statuses[symbol] = status
            if status.next_attempt_at is not None and status.next_attempt_at > now:
                continue
            try:
                await self._collect(status, now)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._fail(status, exc, now)
            else:
                status.failures = 0
                status.last_error = None
                status.next_attempt_at = None
        self._prune(symbols, now)

    async def _collect(self, status: SymbolFeedStatus, now: datetime) -> None:
        symbol = status.symbol
        end = _bucket_start(now)
        earliest = end - timedelta(days=BACKFILL_DAYS)
        latest = self._latest(symbol)
        cursor = earliest if latest is None else max(earliest, latest + INTERVAL)
        span = INTERVAL * MAX_CANDLES_PER_REQUEST
        while cursor < end:
            page_end = min(end, cursor + span)
            self.requests += 1
            fetched = await self.source.get_candles(
                symbol, cursor, page_end, granularity=INTERVAL_NAME
            )
            page = tuple(
                c for c in fetched if cursor <= c.opened_at < page_end and c.symbol == symbol
            )
            # A coin with no trades in a window, or one listed inside the backfill
            # span, legitimately returns fewer candles; check each one on its own.
            for candle in page:
                self._validator.validate_candles((candle,), expected_interval=INTERVAL)
            if page:
                self.store.upsert_many(page)
            cursor = page_end
        status.last_candle_at = self._latest(symbol)

    def _latest(self, symbol: str) -> datetime | None:
        latest = self.store.latest_opened_at(symbol, INTERVAL_NAME)
        # SQLite hands back naive datetimes; the rest of this module compares aware ones.
        return latest if latest is None or latest.tzinfo else latest.replace(tzinfo=UTC)

    def _fail(self, status: SymbolFeedStatus, exc: Exception, now: datetime) -> None:
        status.failures += 1
        status.last_error = _describe(exc)
        delay = min(MAX_BACKOFF, FIRST_BACKOFF * 2 ** (status.failures - 1))
        status.next_attempt_at = now + delay
        logger.warning("watch-only feed for %s failed: %s", status.symbol, status.last_error)

    def _prune(self, symbols: tuple[str, ...], now: datetime) -> None:
        """Remove old candles of watch-only symbols only; trading symbols are never touched."""

        if self._next_prune is not None and now < self._next_prune:
            return
        self._next_prune = now + PRUNE_EVERY
        # Bucket-aligned, so a fresh backfill's oldest candle is never pruned at once.
        cutoff = _bucket_start(now) - self.config.retention
        for symbol in symbols:
            if symbol in self.trading_symbols:
                continue
            try:
                self.store.prune_before(symbol, INTERVAL_NAME, cutoff, PRUNE_BATCH)
            except Exception:
                logger.exception("watch-only candle pruning failed for %s", symbol)


def _bucket_start(moment: datetime) -> datetime:
    seconds = int(INTERVAL.total_seconds())
    return datetime.fromtimestamp(int(moment.timestamp()) // seconds * seconds, tz=UTC)


def _describe(exc: Exception) -> str:
    """A short, secret-free reason for the operator; never the raw provider body."""

    if isinstance(exc, CircuitOpen):
        return "Coinbase requests paused after repeated failures"
    if isinstance(exc, RateLimitExceeded):
        return "request budget was not available; waiting"
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code == 429:
            return "Coinbase is rate limiting this feed (429)"
        return f"Coinbase answered HTTP {code}"
    if isinstance(exc, httpx.TimeoutException):
        return "Coinbase did not answer in time"
    if isinstance(exc, httpx.HTTPError):
        return "Coinbase could not be reached"
    if isinstance(exc, MarketDataValidationError):
        return "Coinbase returned a candle that failed validation"
    return "the candle request failed"
