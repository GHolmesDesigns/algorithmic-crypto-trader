"""Soak-and-readiness console: a bounded daily digest and a criterion tracker, both
read-only from persisted audit-spine rows, plus the one write path an administrator
uses to record evidence.

The daily digest covers the last 30 UTC days, one row per day, with the counts a
persisted producer already tracks: trades, refusals, reconciliation runs,
divergences, and kill-switch events, plus equity when a snapshot exists that day.
Uptime, restarts, disconnects, backup and restore results, and incidents have no
persisted producer yet, so every day names them as not recorded and its status
stays incomplete: a day is never marked pass on partial data.

The criterion tracker lists #12's soak exit criteria and #13's readiness gates.
Each one is incomplete until an administrator records evidence for it; the latest
record decides pass or fail. A note is redacted before it is stored, so it can
point at a dated result (a PR or issue comment, a CI run) without carrying a
hostname, IP address, bucket name, or token.

Nothing here writes to a broker, and the evidence write path never touches
trading state: it is an audit note, not a risk or execution decision.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from core.logging import redact_free_text
from core.models import utc_now
from db.models import (
    DiscrepancyRecord,
    EquitySnapshotRecord,
    EvidenceRecord,
    FillRecord,
    PortfolioSnapshotRecord,
    RiskDecisionRecord,
    SystemEventRecord,
)
from risk.kill_switch_journal import EVENT_TYPE as KILL_SWITCH_EVENT
from sqlalchemy import Integer, case, func, literal_column, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from api.history import HistoryUnavailable
from api.trends import BROKER_SOURCE, trends_query

DIGEST_WINDOW = "30d"
NOTE_LIMIT = 500
EVIDENCE_KINDS: tuple[tuple[str, str], ...] = (
    ("local", "Local validation"),
    ("ci", "Remote CI"),
    ("infra", "Agent-run infrastructure"),
    ("provider", "Owner-run provider verification"),
)
EVIDENCE_STATUSES = ("pass", "fail")

# #12's exit criteria for the 30-day paper soak.
SOAK_CRITERIA: tuple[tuple[str, str], ...] = (
    (
        "no_unexplained_mismatch",
        "30 consecutive days with no unexplained position or balance mismatch.",
    ),
    ("disconnect_gap_fill", "A WebSocket disconnect survives with correct gap fill."),
    ("restart_recovery", "A process restart survives with correct state recovery."),
    (
        "divergence_handled",
        "A reconciliation divergence is observed and explained, or deliberately injected "
        "and correctly handled.",
    ),
    ("kill_switch_verified", "The kill switch fires and is verified during the period."),
    ("incidents_documented", "No open incident lacks a documented cause."),
)
# #13's preconditions for Phase 2 activation. This console is read-only for trading:
# listing these gates never starts, authorizes, or implies live activation.
READINESS_GATES: tuple[tuple[str, str], ...] = (
    ("soak_complete", "Phase 1.5 soak (#12) is completed in full."),
    (
        "coinbase_key_scoped",
        "The Coinbase production key is limited to View + Trade, with transfers and "
        "withdrawals disabled and IP allowlisting where available.",
    ),
    (
        "safety_controls_tested",
        "The reconciler, alerts, kill switch, and incident runbook are approved and tested.",
    ),
)
CRITERIA: dict[str, str] = dict(SOAK_CRITERIA) | dict(READINESS_GATES)
_KIND_LABELS = dict(EVIDENCE_KINDS)

# Fields the daily digest cannot yet fill: no producer persists them. Recording them
# by hand would be a guess standing in for monitoring, which is worse than an honest gap.
DIGEST_GAPS: tuple[tuple[str, str], ...] = (
    (
        "uptime_restarts_disconnects",
        "Nothing persists uptime, restart, or WebSocket-disconnect history yet, so no day "
        "can show it.",
    ),
    (
        "backup_restore",
        "Backup and restore drill results are recorded in PR and issue comments today, not "
        "in the database, so no day can show them here.",
    ),
    (
        "incidents",
        "Incidents are logged in issue comments today, not in the database, so no day can "
        "show them here.",
    ),
)
_GAP_KEYS = [key for key, _ in DIGEST_GAPS]


@dataclass(frozen=True, slots=True)
class EvidenceSubmission:
    criterion: str
    kind: str
    status: str
    note: str
    errors: tuple[str, ...] = ()


def parse_evidence(form: Mapping[str, str]) -> EvidenceSubmission:
    """Validate a submitted evidence entry; refuse anything unknown, empty, or too long."""

    criterion = form.get("criterion", "").strip()
    kind = form.get("kind", "").strip()
    status = form.get("status", "").strip()
    note = form.get("note", "").strip()
    errors: list[str] = []
    if criterion not in CRITERIA:
        errors.append("Choose one of the listed soak or readiness criteria.")
    if kind not in _KIND_LABELS:
        errors.append(f"kind must be one of {', '.join(_KIND_LABELS)}.")
    if status not in EVIDENCE_STATUSES:
        errors.append(f"status must be one of {', '.join(EVIDENCE_STATUSES)}.")
    if not note:
        errors.append("Enter a reference to the dated result this evidence points at.")
    elif len(note) > NOTE_LIMIT:
        errors.append(f"Keep the note to {NOTE_LIMIT} characters or fewer.")
    return EvidenceSubmission(criterion, kind, status, note, tuple(errors))


class SqlAlchemySoak:
    """Read-only digest and criterion tracker, plus the one evidence write.

    Holds a session factory and nothing that can trade.
    """

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self.session_factory = session_factory

    def read(self, *, now: datetime | None = None) -> dict[str, Any]:
        query = trends_query(DIGEST_WINDOW, now or utc_now())
        try:
            with self.session_factory() as session:
                trades = _daily_counts(session, query.edges, FillRecord.occurred_at)
                refusals = _daily_counts(
                    session,
                    query.edges,
                    RiskDecisionRecord.decided_at,
                    RiskDecisionRecord.approved.is_(False),
                )
                reconciliation_runs = _daily_counts(
                    session,
                    query.edges,
                    PortfolioSnapshotRecord.recorded_at,
                    PortfolioSnapshotRecord.source == BROKER_SOURCE,
                )
                divergences = _daily_counts(session, query.edges, DiscrepancyRecord.created_at)
                kill_switch_events = _daily_counts(
                    session,
                    query.edges,
                    SystemEventRecord.created_at,
                    SystemEventRecord.event_type == KILL_SWITCH_EVENT,
                )
                equity = _daily_last_equity(session, query.edges)
                latest = _latest_evidence(session, CRITERIA)
        except SQLAlchemyError as exc:
            raise HistoryUnavailable("the soak console could not be read") from exc
        days = [
            _day(
                query.edges[index],
                trades[index],
                refusals[index],
                reconciliation_runs[index],
                divergences[index],
                kill_switch_events[index],
                equity[index],
                in_progress=index == query.buckets - 1,
            )
            for index in range(query.buckets)
        ]
        return {
            "kind": "soak",
            "status": "available",
            "window": query.to_dict(),
            "days": days,
            "digest_gaps": [{"key": key, "reason": reason} for key, reason in DIGEST_GAPS],
            "criteria": {
                "soak": [_criterion(key, label, latest.get(key)) for key, label in SOAK_CRITERIA],
                "readiness": [
                    _criterion(key, label, latest.get(key)) for key, label in READINESS_GATES
                ],
            },
            "evidence_kinds": [{"key": key, "label": label} for key, label in EVIDENCE_KINDS],
        }

    def record_evidence(
        self,
        criterion: str,
        kind: str,
        status: str,
        note: str,
        *,
        recorded_by: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Persist one administrator attestation; the note is redacted before it is stored."""

        record = EvidenceRecord(
            evidence_id=uuid4(),
            criterion=criterion,
            kind=kind,
            status=status,
            note=redact_free_text(note),
            recorded_by=recorded_by,
            recorded_at=now or utc_now(),
        )
        try:
            with self.session_factory() as session:
                session.add(record)
                session.commit()
        except SQLAlchemyError as exc:
            raise HistoryUnavailable("the evidence record could not be saved") from exc
        return _evidence_dict(record)


def _daily_counts(
    session: Session, edges: tuple[datetime, ...], at: Any, *conditions: Any
) -> list[int]:
    """Rows per UTC day in ``[edges[0], edges[-1])``, counted by the database.

    The bucket is a CASE over the edges, computed in a subquery so each bound edge
    appears once, the same technique ``api.trends`` uses for its charts.
    """

    bucket = case(
        *[(at < edge, literal_column(str(index), Integer)) for index, edge in enumerate(edges[1:])]
    ).label("bucket")
    rows = select(bucket).where(at >= edges[0], at < edges[-1], *conditions).subquery()
    counts = [0] * (len(edges) - 1)
    for row in session.execute(select(rows.c.bucket, func.count()).group_by(rows.c.bucket)).all():
        counts[int(row[0])] = int(row[1])
    return counts


def _daily_last_equity(session: Session, edges: tuple[datetime, ...]) -> list[Decimal | None]:
    """The latest recorded equity per UTC day, or ``None`` when no snapshot fell in it."""

    bucket = case(
        *[
            (EquitySnapshotRecord.as_of < edge, literal_column(str(index), Integer))
            for index, edge in enumerate(edges[1:])
        ]
    ).label("bucket")
    numbered = (
        select(
            EquitySnapshotRecord.equity,
            bucket,
            func.row_number()
            .over(partition_by=bucket, order_by=EquitySnapshotRecord.as_of.desc())
            .label("rank"),
        )
        .where(EquitySnapshotRecord.as_of >= edges[0], EquitySnapshotRecord.as_of < edges[-1])
        .subquery()
    )
    values: list[Decimal | None] = [None] * (len(edges) - 1)
    for row in session.execute(
        select(numbered.c.bucket, numbered.c.equity).where(numbered.c.rank == 1)
    ).all():
        values[int(row[0])] = row[1]
    return values


def _latest_evidence(session: Session, criteria: Mapping[str, str]) -> dict[str, EvidenceRecord]:
    latest: dict[str, EvidenceRecord] = {}
    for record in session.scalars(
        select(EvidenceRecord)
        .where(EvidenceRecord.criterion.in_(criteria))
        .order_by(EvidenceRecord.criterion, EvidenceRecord.recorded_at)
    ):
        latest[record.criterion] = record  # the newest record per criterion wins
    return latest


def _day(
    start: datetime,
    trades: int,
    refusals: int,
    reconciliation_runs: int,
    divergences: int,
    kill_switch_events: int,
    equity: Decimal | None,
    *,
    in_progress: bool,
) -> dict[str, Any]:
    missing = list(_GAP_KEYS)
    if equity is None:
        missing = ["equity", *missing]
    return {
        "date": start.date().isoformat(),
        "in_progress": in_progress,
        "trades": trades,
        "refusals": refusals,
        "reconciliation_runs": reconciliation_runs,
        "divergences": divergences,
        "kill_switch_events": kill_switch_events,
        "equity": _decimal(equity),
        "missing": missing,
        # Missing data always includes fields with no producer yet, so a day is never
        # marked pass on partial evidence.
        "status": "incomplete" if missing else "pass",
    }


def _criterion(key: str, label: str, record: EvidenceRecord | None) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "status": record.status if record is not None else "incomplete",
        "evidence": _evidence_dict(record) if record is not None else None,
    }


def _evidence_dict(record: EvidenceRecord) -> dict[str, Any]:
    return {
        "evidence_id": str(record.evidence_id),
        "criterion": record.criterion,
        "kind": record.kind,
        "kind_label": _KIND_LABELS.get(record.kind, record.kind),
        "status": record.status,
        "note": record.note,
        "recorded_by": record.recorded_by,
        "recorded_at": _iso(record.recorded_at),
    }


def _decimal(value: Decimal | None) -> str | None:
    return format(Decimal(value).normalize(), "f") if value is not None else None


def _iso(value: datetime) -> str:
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).astimezone(UTC).isoformat()
