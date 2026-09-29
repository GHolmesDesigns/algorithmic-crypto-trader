"""Bounded, read-only market candles for charts, aggregated from stored five-minute rows.

A read covers one window of 24 hours, 7 days, 30 days, or 90 days, for one to
nine symbols, as bars of 15 minutes, an hour, six hours, or a day. Bars are
aligned to UTC, so a daily bar is a UTC day and a six-hour bar starts at 00, 06,
12, or 18 UTC. The last bar is the one now running, still in progress.

The server caps every read at ``MAX_POINTS`` bars per series. An omitted
interval is the finest one that fits the cap; a named interval that would exceed
it is refused, with the reason, before anything is read. ``parse_candles_query``
refuses any other window, interval, parameter, or symbol list unread.

The database does the aggregating: one grouped read over the ``market_candles``
unique key returns, per symbol and bar, the candle count, the first and last
candle time, the high, the low, and the volume. A second read fetches only the
first candle's open and the last candle's close, by their times. The candle rows
themselves never leave the database.

A bar with no candles is a gap, reported as one with empty values, never as zero
and never carried forward. A finished bar with fewer candles than its width
implies is partial. The bar now running is in progress. A symbol's freshness is
one of fresh, stale, not collected, or unavailable, and is independent of whether
the window holds any rows.

Prices and volumes are ``Decimal`` in the server and decimal strings in the
payload. Nothing here writes, and nothing reaches a broker or a provider.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from core.models import utc_now
from data.coinbase import GRANULARITY_SECONDS
from data.stream import WEBSOCKET_CANDLE_INTERVAL
from data.watchlist import WATCHLIST_LIMIT, WatchlistRefused, normalize_symbol
from db.models import MarketCandleRecord
from sqlalchemy import Integer, case, func, literal_column, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

SOURCE_INTERVAL = WEBSOCKET_CANDLE_INTERVAL
SOURCE_STEP = timedelta(seconds=GRANULARITY_SECONDS[SOURCE_INTERVAL])
# The trading and watch-only feeds both call a symbol stale after this long.
STALE_AFTER = SOURCE_STEP * 3

MAX_POINTS = 300
MAX_SYMBOLS = WATCHLIST_LIMIT
# Finest first, so the default is the finest interval that fits the cap.
INTERVALS: dict[str, timedelta] = {
    "15m": timedelta(minutes=15),
    "1h": timedelta(hours=1),
    "6h": timedelta(hours=6),
    "1d": timedelta(days=1),
}
WINDOWS: dict[str, timedelta] = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
    "90d": timedelta(days=90),
}
DEFAULT_WINDOW = "24h"
PARAMETERS = ("symbols", "window", "interval")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_TEXT_LIMIT = 40

FRESH = "fresh"
STALE = "stale"
NOT_COLLECTED = "not_collected"
UNAVAILABLE = "unavailable"

COMPLETE = "complete"
PARTIAL = "partial"
IN_PROGRESS = "in_progress"
GAP = "gap"


class CandleQueryRefused(ValueError):
    """A request beyond the caps or outside the vocabulary; ``errors`` are safe to return."""

    def __init__(self, errors: Sequence[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = tuple(errors)


class CandlesUnavailable(RuntimeError):
    """The candle table could not be read; never to be shown as an empty chart."""


@dataclass(frozen=True, slots=True)
class CandleQuery:
    """One read: ``edges`` bound bar ``i`` from ``edges[i]`` up to, not including, ``i + 1``."""

    symbols: tuple[str, ...]
    window: str
    interval: str
    edges: tuple[datetime, ...]
    now: datetime

    @property
    def step(self) -> timedelta:
        return INTERVALS[self.interval]

    @property
    def since(self) -> datetime:
        return self.edges[0]

    @property
    def until(self) -> datetime:
        return self.edges[-1]

    @property
    def points(self) -> int:
        return len(self.edges) - 1

    @property
    def expected(self) -> int:
        """Five-minute candles in one complete bar."""

        return self.step // SOURCE_STEP

    def to_dict(self) -> dict[str, Any]:
        return {
            "window": self.window,
            "interval": self.interval,
            "bucket_seconds": int(self.step.total_seconds()),
            "points": self.points,
            "max_points": MAX_POINTS,
            "max_symbols": MAX_SYMBOLS,
            "since": self.since.isoformat(),
            "until": self.until.isoformat(),
            "as_of": self.now.isoformat(),
            "in_progress_from": self.edges[-2].isoformat(),
            "source_interval": SOURCE_INTERVAL,
        }


def parse_candles_query(
    items: Iterable[tuple[str, str]],
    *,
    default_symbols: Sequence[str] = (),
    now: datetime | None = None,
) -> CandleQuery:
    """Validate a candle read; refuse anything beyond the caps or the vocabulary, unread.

    ``symbols`` is a comma-separated list of ``*-USD`` symbols, given once; when it
    is absent, ``default_symbols`` (the saved watchlist) stand in. ``window`` is
    24h, 7d, 30d, or 90d and defaults to 24h. ``interval`` is 15m, 1h, 6h, or 1d;
    when it is absent the finest one that keeps the series within ``MAX_POINTS``
    is used. Every problem is reported together.
    """

    values: dict[str, str] = {}
    errors: list[str] = []
    for key, raw in items:
        if key not in PARAMETERS:
            errors.append(f"Unknown parameter {key[:_TEXT_LIMIT]!r}.")
        elif key in values:
            errors.append(f"Give '{key}' once.")
        else:
            values[key] = raw.strip()

    window = values.get("window") or DEFAULT_WINDOW
    if window not in WINDOWS:
        errors.append(f"window must be one of {', '.join(WINDOWS)}.")

    interval = values.get("interval") or None
    if interval is not None and interval not in INTERVALS:
        errors.append(f"interval must be one of {', '.join(INTERVALS)}.")
        interval = None
    elif window in WINDOWS:
        fits = _fitting_intervals(window)
        if not fits:
            errors.append(
                f"No interval keeps a {window} window within {MAX_POINTS} points per series."
            )
        elif interval is None:
            interval = fits[0]
        elif interval not in fits:
            points = WINDOWS[window] // INTERVALS[interval]
            errors.append(
                f"A {window} window in {interval} bars is {points} points per series; "
                f"the cap is {MAX_POINTS}. Use {fits[0]} or wider, or a shorter window."
            )

    symbols = _symbols(values.get("symbols"), default_symbols, errors)
    if errors:
        raise CandleQueryRefused(errors)
    assert interval is not None
    return candle_query(symbols, window, interval, now or utc_now())


def candle_query(symbols: Sequence[str], window: str, interval: str, now: datetime) -> CandleQuery:
    """The bar edges for one read, ending with the bar that contains ``now``.

    The caps hold here too, whatever the tables above say, so a caller that skips
    the parser cannot exceed them.
    """

    step = INTERVALS[interval]
    span = WINDOWS[window]
    count = span // step
    if count > MAX_POINTS or count < 1:
        raise CandleQueryRefused([f"A series can have at most {MAX_POINTS} points."])
    if not symbols or len(symbols) > MAX_SYMBOLS or len(set(symbols)) != len(symbols):
        raise CandleQueryRefused([f"A request names one to {MAX_SYMBOLS} different symbols."])
    now = _utc(now)
    current = _EPOCH + ((now - _EPOCH) // step) * step
    end = current + step
    edges = tuple(end - step * (count - index) for index in range(count + 1))
    return CandleQuery(tuple(symbols), window, interval, edges, now)


def _fitting_intervals(window: str) -> list[str]:
    """The intervals that keep ``window`` within the point cap, finest first."""

    span = WINDOWS[window]
    return [name for name, step in INTERVALS.items() if span // step <= MAX_POINTS]


def _symbols(raw: str | None, default: Sequence[str], errors: list[str]) -> tuple[str, ...]:
    if raw is None:
        if not default:
            errors.append(
                "Name at least one symbol with 'symbols', for example symbols=BTC-USD,ETH-USD."
            )
        return tuple(default)
    named = [part for part in (item.strip() for item in raw.split(",")) if part]
    if not named:
        errors.append("Name at least one symbol with 'symbols'.")
        return ()
    if len(named) > MAX_SYMBOLS:
        errors.append(
            f"A request may name at most {MAX_SYMBOLS} symbols; this one names {len(named)}."
        )
        return ()
    cleaned: list[str] = []
    for part in named:
        try:
            symbol = normalize_symbol(part)
        except WatchlistRefused:
            errors.append(f"{part[:_TEXT_LIMIT]!r} is not a USD product such as ETH-USD.")
            continue
        if symbol in cleaned:
            errors.append(f"{symbol} is named twice.")
        else:
            cleaned.append(symbol)
    return tuple(cleaned)


class SqlAlchemyCandleReads:
    """Read-only candle bars. Holds a session factory and nothing that can trade."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self.session_factory = session_factory

    def read(
        self, query: CandleQuery, *, feed_states: Mapping[str, Mapping[str, Any]] | None = None
    ) -> dict[str, Any]:
        """The payload for ``query``: one entry per symbol, in the order requested.

        ``feed_states`` maps a symbol to the watch-only feed's own report; a feed
        that reports a symbol unavailable makes it unavailable here, whatever is
        stored. Symbols the feed does not cover are judged from their stored candles.
        """

        try:
            with self.session_factory() as session:
                groups = _groups(session, query)
                latest = _latest(session, query.symbols)
                edges = _edge_candles(session, query, groups, latest)
        except SQLAlchemyError as exc:
            raise CandlesUnavailable("candles could not be read") from exc
        return {
            "status": "available",
            "query": query.to_dict(),
            "symbols": [
                _symbol_payload(
                    symbol,
                    query,
                    groups.get(symbol, {}),
                    latest.get(symbol),
                    edges,
                    (feed_states or {}).get(symbol),
                )
                for symbol in query.symbols
            ],
        }


@dataclass(frozen=True, slots=True)
class _Group:
    count: int
    first_at: datetime
    last_at: datetime
    high: Decimal
    low: Decimal
    volume: Decimal


def _groups(session: Session, query: CandleQuery) -> dict[str, dict[int, _Group]]:
    """Per symbol and bar, the count, first and last time, high, low, and volume.

    The bar is a CASE over the edges, computed in a subquery so each bound edge
    appears once: PostgreSQL cannot match a GROUP BY expression whose parameters
    differ from the select list's.
    """

    edges = query.edges
    bar = case(
        *[
            (MarketCandleRecord.opened_at < edge, literal_column(str(index), Integer))
            for index, edge in enumerate(edges[1:])
        ]
    ).label("bar")
    rows = (
        select(
            MarketCandleRecord.symbol.label("symbol"),
            bar,
            MarketCandleRecord.opened_at.label("opened_at"),
            MarketCandleRecord.high.label("high"),
            MarketCandleRecord.low.label("low"),
            MarketCandleRecord.volume.label("volume"),
        )
        .where(
            MarketCandleRecord.symbol.in_(query.symbols),
            MarketCandleRecord.interval == SOURCE_INTERVAL,
            MarketCandleRecord.opened_at >= edges[0],
            MarketCandleRecord.opened_at < edges[-1],
        )
        .subquery()
    )
    statement = select(
        rows.c.symbol,
        rows.c.bar,
        func.count(),
        func.min(rows.c.opened_at),
        func.max(rows.c.opened_at),
        func.max(rows.c.high),
        func.min(rows.c.low),
        func.sum(rows.c.volume),
    ).group_by(rows.c.symbol, rows.c.bar)
    groups: dict[str, dict[int, _Group]] = {}
    for symbol, index, count, first, last, high, low, volume in session.execute(statement).all():
        groups.setdefault(symbol, {})[int(index)] = _Group(
            int(count), _utc(first), _utc(last), Decimal(high), Decimal(low), Decimal(volume)
        )
    return groups


def _latest(session: Session, symbols: Sequence[str]) -> dict[str, datetime]:
    """Each symbol's newest stored candle time, anywhere in time, on the unique key."""

    statement = (
        select(MarketCandleRecord.symbol, func.max(MarketCandleRecord.opened_at))
        .where(
            MarketCandleRecord.symbol.in_(symbols),
            MarketCandleRecord.interval == SOURCE_INTERVAL,
        )
        .group_by(MarketCandleRecord.symbol)
    )
    return {symbol: _utc(opened_at) for symbol, opened_at in session.execute(statement).all()}


@dataclass(frozen=True, slots=True)
class _EdgeCandle:
    open: Decimal
    close: Decimal
    source: str


def _edge_candles(
    session: Session,
    query: CandleQuery,
    groups: dict[str, dict[int, _Group]],
    latest: dict[str, datetime],
) -> dict[tuple[str, datetime], _EdgeCandle]:
    """The open and close of the candles that open and close each bar, and the newest one.

    At most two candles per bar and one per symbol, fetched by symbol and time.
    """

    wanted: set[tuple[str, datetime]] = set(latest.items())
    for symbol, bars in groups.items():
        for group in bars.values():
            wanted.add((symbol, group.first_at))
            wanted.add((symbol, group.last_at))
    if not wanted:
        return {}
    statement = select(
        MarketCandleRecord.symbol,
        MarketCandleRecord.opened_at,
        MarketCandleRecord.open,
        MarketCandleRecord.close,
        MarketCandleRecord.source,
    ).where(
        MarketCandleRecord.symbol.in_(query.symbols),
        MarketCandleRecord.interval == SOURCE_INTERVAL,
        MarketCandleRecord.opened_at.in_({opened_at for _, opened_at in wanted}),
    )
    found: dict[tuple[str, datetime], _EdgeCandle] = {}
    for symbol, opened_at, open_, close, source in session.execute(statement).all():
        key = (symbol, _utc(opened_at))
        if key in wanted:
            found[key] = _EdgeCandle(Decimal(open_), Decimal(close), str(source))
    return found


def _symbol_payload(
    symbol: str,
    query: CandleQuery,
    groups: dict[int, _Group],
    last_candle_at: datetime | None,
    edges: dict[tuple[str, datetime], _EdgeCandle],
    feed: Mapping[str, Any] | None,
) -> dict[str, Any]:
    bars: list[dict[str, Any]] = []
    candles = 0
    for index in range(query.points):
        opened_at, closed_at = query.edges[index], query.edges[index + 1]
        in_progress = index == query.points - 1
        group = groups.get(index)
        bar: dict[str, Any] = {
            "opened_at": opened_at.isoformat(),
            "closed_at": closed_at.isoformat(),
            "state": IN_PROGRESS if in_progress else GAP,
            "candles": 0,
            "expected": query.expected,
            "open": None,
            "high": None,
            "low": None,
            "close": None,
            "volume": None,
        }
        first = edges.get((symbol, group.first_at)) if group else None
        last = edges.get((symbol, group.last_at)) if group else None
        if group is not None and first is not None and last is not None:
            candles += group.count
            bar.update(
                candles=group.count,
                open=_text(first.open),
                high=_text(group.high),
                low=_text(group.low),
                close=_text(last.close),
                volume=_text(group.volume),
            )
            if not in_progress:
                bar["state"] = COMPLETE if group.count >= query.expected else PARTIAL
        bars.append(bar)
    newest = edges.get((symbol, last_candle_at)) if last_candle_at is not None else None
    return {
        "symbol": symbol,
        "source": newest.source if newest is not None else None,
        "freshness": _freshness(last_candle_at, query.now, feed),
        "in_window": {
            "candles": candles,
            "expected": query.points * query.expected,
            "empty": candles == 0,
        },
        "bars": bars,
        "gaps": _gaps(bars, query),
    }


def _freshness(
    last_candle_at: datetime | None, now: datetime, feed: Mapping[str, Any] | None
) -> dict[str, Any]:
    """One state per symbol, independent of whether the window holds any rows.

    ``unavailable`` comes only from the watch-only feed's own report of a failing
    symbol; the stored candles alone say fresh, stale, or not collected.
    """

    detail: str | None = None
    if feed is not None and feed.get("state") == UNAVAILABLE:
        state = UNAVAILABLE
        detail = feed.get("detail") or "the feed for this symbol is failing"
    elif last_candle_at is None:
        state = NOT_COLLECTED
        detail = "no candle has been stored for this symbol yet"
    elif now - (last_candle_at + SOURCE_STEP) > STALE_AFTER:
        state = STALE
        detail = "the newest stored candle is older than three candle intervals"
    else:
        state = FRESH
    return {
        "state": state,
        "last_candle_at": last_candle_at.isoformat() if last_candle_at is not None else None,
        "detail": detail,
    }


def _gaps(bars: Sequence[Mapping[str, Any]], query: CandleQuery) -> list[dict[str, Any]]:
    """Runs of finished bars with no candles. The bar now running is never a gap."""

    gaps: list[dict[str, Any]] = []
    start: int | None = None
    for index, bar in enumerate([*bars, {"state": COMPLETE}]):
        if bar["state"] == GAP:
            start = index if start is None else start
        elif start is not None:
            gaps.append(
                {
                    "from": query.edges[start].isoformat(),
                    "to": query.edges[index].isoformat(),
                    "bars": index - start,
                }
            )
            start = None
    return gaps


def _text(value: Decimal) -> str:
    """A plain decimal string: no exponent, no trailing zeros, no context rounding."""

    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _utc(moment: datetime) -> datetime:
    """The stored time as an aware UTC time; SQLite hands back naive ones."""

    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)
