"""Bounded, read-only trends: counts per time bucket from the persisted tables.

A trends read covers one window of 24 hours, 7 days, or 30 days, split into at
most 30 buckets aligned to UTC hours or days, so the bars and their labels line
up with the clock. The last bucket is the one now running, still in progress.
``parse_trends_query`` refuses any other window, and any other parameter, before
anything is read.

The database does the counting: each chart reads one indexed time range and
returns one row per bucket and series, never the rows themselves. Only the time
column and the series key leave the database; payloads, reasons, and amounts do not.

A chart is drawn only from a trustworthy persisted producer. Uptime, freshness,
equity, and day P/L have none yet, so they are reported as not started, with the
reason, and never as an empty series that would read as zero.

Nothing here writes, and nothing reaches a broker.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from core.models import utc_now
from db.models import DiscrepancyRecord, PortfolioSnapshotRecord, RiskDecisionRecord
from risk.engine import RISK_GATES
from sqlalchemy import ColumnElement, Integer, case, func, literal_column, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from api.history import (
    DISCREPANCY_TYPES,
    TEXT_LIMIT,
    UNGATED,
    HistoryQueryRefused,
    HistoryUnavailable,
)

# window: (span, bucket width)
WINDOWS: dict[str, tuple[timedelta, timedelta]] = {
    "24h": (timedelta(hours=24), timedelta(hours=1)),
    "7d": (timedelta(days=7), timedelta(hours=6)),
    "30d": (timedelta(days=30), timedelta(days=1)),
}
DEFAULT_WINDOW = "24h"
MAX_WINDOW = timedelta(days=30)
MAX_BUCKETS = 30
# Reconciliation saves the broker's state under this source on every completed run.
BROKER_SOURCE = "broker"
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
# The charts drawn from persisted rows, in page order: key and title.
DATA_CHARTS = (
    ("reconciliation_runs", "Reconciliation runs"),
    ("discrepancies", "Discrepancies by type"),
    ("refusals", "Risk refusals by gate"),
)
_TITLES = dict(DATA_CHARTS)


@dataclass(frozen=True, slots=True)
class TrendsQuery:
    """One window as ``edges``: bucket ``i`` covers ``edges[i]`` up to, not including, ``i + 1``."""

    window: str
    step: timedelta
    edges: tuple[datetime, ...]
    now: datetime

    @property
    def since(self) -> datetime:
        return self.edges[0]

    @property
    def until(self) -> datetime:
        return self.edges[-1]

    @property
    def buckets(self) -> int:
        return len(self.edges) - 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "window": self.window,
            "since": self.since.isoformat(),
            "until": self.until.isoformat(),
            "as_of": self.now.isoformat(),
            "bucket_seconds": int(self.step.total_seconds()),
            "buckets": self.buckets,
            "max_days": MAX_WINDOW.days,
            "in_progress_from": self.edges[-2].isoformat(),
        }


def parse_trends_query(
    items: Iterable[tuple[str, str]], *, now: datetime | None = None
) -> TrendsQuery:
    """Validate the one parameter a trends read takes; refuse everything else unread.

    An empty or absent ``window`` reads the last 24 hours. Any window but 24h, 7d,
    or 30d, a repeated window, or any other parameter is refused.
    """

    window = DEFAULT_WINDOW
    seen = False
    errors: list[str] = []
    for key, raw in items:
        if key != "window":
            errors.append(f"Unknown parameter {key[:TEXT_LIMIT]!r}.")
        elif seen:
            errors.append("Give 'window' once.")
        else:
            seen = True
            window = raw.strip() or DEFAULT_WINDOW
    if window not in WINDOWS:
        errors.append(f"window must be one of {', '.join(WINDOWS)}.")
    if errors:
        raise HistoryQueryRefused(errors)
    return trends_query(window, now or utc_now())


def trends_query(window: str, now: datetime) -> TrendsQuery:
    """The bucket edges for ``window``, ending with the bucket that contains ``now``."""

    span, step = WINDOWS[window]
    now = _utc(now)
    current = _EPOCH + ((now - _EPOCH) // step) * step
    count = span // step
    end = current + step
    edges = tuple(end - step * (count - index) for index in range(count + 1))
    # The server's cap, whatever the table above says.
    if edges[-1] - edges[0] > MAX_WINDOW or count > MAX_BUCKETS:
        raise HistoryQueryRefused([f"A trends window can be at most {MAX_WINDOW.days} days."])
    return TrendsQuery(window, step, edges, now)


@dataclass(frozen=True, slots=True)
class Series:
    key: str | None
    label: str
    position: int | None = None
    recognised: bool = True


# Charts without a trustworthy producer. They are reported, never drawn.
NOT_STARTED: tuple[dict[str, Any], ...] = (
    {
        "key": "uptime_freshness",
        "title": "Uptime and freshness",
        "status": "not_started",
        "source": None,
        "reason": (
            "Nothing persists uptime or data freshness yet. The running process reports its "
            "heartbeat, market-data state, and broker check time on the dashboard, but none of "
            "them is saved, so there is no history to chart. Persisted candles cannot stand "
            "in: backfilled and live candles carry the same source name, and a backfilled "
            "candle's receipt time says when a gap was filled, not how fresh live data was."
        ),
        "blocked_by": [],
    },
    {
        "key": "equity_pnl",
        "title": "Equity and day P/L",
        "status": "not_started",
        "source": None,
        "reason": (
            "No trustworthy producer records equity or day P/L. Reconciliation saves positions "
            "and balances without equity, and P/L stays unavailable until cost basis is known "
            "(#30) and a P/L producer exists. Rows already in equity_curve are not charted "
            "until then."
        ),
        "blocked_by": ["#30"],
    },
)


class SqlAlchemyTrends:
    """Read-only trend counts. Holds a session factory and nothing that can trade."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self.session_factory = session_factory

    def read(self, query: TrendsQuery) -> list[dict[str, Any]]:
        """Every chart for ``query``, in page order; not-started charts carry their reason."""

        try:
            with self.session_factory() as session:
                runs = _counts(
                    session,
                    query.edges,
                    PortfolioSnapshotRecord.recorded_at,
                    None,
                    PortfolioSnapshotRecord.source == BROKER_SOURCE,
                )
                discrepancies = _counts(
                    session,
                    query.edges,
                    DiscrepancyRecord.created_at,
                    DiscrepancyRecord.entity_type,
                )
                refusals = _counts(
                    session,
                    query.edges,
                    RiskDecisionRecord.decided_at,
                    RiskDecisionRecord.failed_gate,
                    RiskDecisionRecord.approved.is_(False),
                )
        except SQLAlchemyError as exc:
            raise HistoryUnavailable("trends could not be read") from exc
        return [
            _chart(
                "reconciliation_runs",
                {
                    "table": "portfolio_snapshots",
                    "where": f"source = '{BROKER_SOURCE}'",
                    "time": "recorded_at",
                },
                "Completed reconciliations, at startup and on schedule. Each one saves the "
                "broker's state once. A run that could not read the broker saves nothing, so "
                "it is not counted here; the dashboard counts those since the process started.",
                [Series("completed", "Completed runs")],
                runs,
                query,
            ),
            _chart(
                "discrepancies",
                {"table": "discrepancies", "where": None, "time": "created_at"},
                "Differences reconciliation found between local and broker state. Each "
                "difference is one row, so one run can record several.",
                [Series(kind, kind) for kind in DISCREPANCY_TYPES]
                + _unlisted(discrepancies, DISCREPANCY_TYPES, "Unrecognised type"),
                discrepancies,
                query,
            ),
            _chart(
                "refusals",
                {"table": "risk_decisions", "where": "approved is false", "time": "decided_at"},
                "Each refusal counts once, under the first gate that stopped it. A missing "
                "input is a refusal, never a pass.",
                [
                    Series(gate, label, position)
                    for position, (gate, label) in enumerate(RISK_GATES, start=1)
                ]
                + [Series(gate, f"Before the gates: {label}") for gate, label in UNGATED.items()]
                + _unlisted(
                    refusals, [*(gate for gate, _ in RISK_GATES), *UNGATED], "Unrecognised gate"
                ),
                refusals,
                query,
            ),
            *not_started(),
        ]


def not_started() -> list[dict[str, Any]]:
    """The charts that have no producer; they need no read, so they show even when reads fail."""

    return [{**chart, "blocked_by": list(chart["blocked_by"])} for chart in NOT_STARTED]


def _counts(
    session: Session,
    edges: Sequence[datetime],
    at: Any,
    key: Any,
    *conditions: ColumnElement[bool],
) -> dict[str | None, list[int]]:
    """Rows per bucket and ``key`` in ``[edges[0], edges[-1])``, counted by the database.

    The bucket is a CASE over the edges, computed in a subquery so each bound edge
    appears once: PostgreSQL cannot match a GROUP BY expression whose parameters
    differ from the select list's.
    """

    bucket = case(
        *[(at < edge, literal_column(str(index), Integer)) for index, edge in enumerate(edges[1:])]
    ).label("bucket")
    columns = [bucket] + ([key.label("series")] if key is not None else [])
    rows = select(*columns).where(at >= edges[0], at < edges[-1], *conditions).subquery()
    grouping = [rows.c.bucket] + ([rows.c.series] if key is not None else [])
    counts: dict[str | None, list[int]] = {}
    for row in session.execute(select(*grouping, func.count()).group_by(*grouping)).all():
        name = row[1] if key is not None else "completed"
        series = counts.setdefault(name, [0] * (len(edges) - 1))
        series[int(row[0])] += int(row[-1])
    return counts


def _unlisted(
    counts: dict[str | None, list[int]], known: Iterable[str], label: str
) -> list[Series]:
    """Series recorded under a key this build does not know, so no count goes missing."""

    listed = set(known)
    extra = sorted((name for name in counts if name not in listed), key=lambda name: name or "")
    return [Series(name, label if name else "No gate recorded", recognised=False) for name in extra]


def _chart(
    key: str,
    source: dict[str, Any],
    note: str,
    series: list[Series],
    counts: dict[str | None, list[int]],
    query: TrendsQuery,
) -> dict[str, Any]:
    empty = [0] * query.buckets
    rows: list[dict[str, Any]] = []
    for item in series:
        values = counts.get(item.key, empty)
        rows.append(
            {
                "key": _text(item.key),
                "label": item.label,
                "position": item.position,
                "recognised": item.recognised,
                "counts": list(values),
                "total": sum(values),
            }
        )
    totals = [sum(row["counts"][index] for row in rows) for index in range(query.buckets)]
    return {
        "key": key,
        "title": _TITLES[key],
        "status": "available",
        "source": source,
        "note": note,
        "buckets": [
            {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "in_progress": index == query.buckets - 1,
            }
            for index, (start, end) in enumerate(zip(query.edges, query.edges[1:], strict=False))
        ],
        "series": rows,
        "totals": totals,
        "total": sum(totals),
    }


def _text(value: str | None) -> str | None:
    return value[:TEXT_LIMIT] if value else None


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
