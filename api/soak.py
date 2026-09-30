"""Soak-and-readiness console: a bounded daily digest and a criterion tracker, both
read-only from persisted audit-spine rows, plus the write paths an administrator
uses to record evidence and to open or close an incident.

The daily digest covers the last 30 UTC days, one row per day, with the counts a
persisted producer already tracks: trades, refusals, reconciliation runs,
divergences, kill-switch events, heartbeats, restarts, disconnects, and gap
fills, plus equity when a snapshot exists that day. A day's uptime field is
incomplete when any expected heartbeat interval has no sample, and its
incidents field is incomplete while any incident is still open at day's end.
Backup and restore drill results are not a per-day fact: a drill runs
periodically, not once a day, so this card tracks it as the
``backup_restore_verified`` soak criterion below (evidence kind ``infra``)
instead of adding a digest field that would be empty most days.

The criterion tracker lists #12's soak exit criteria and #13's readiness gates.
Each one is incomplete until an administrator records evidence for it; the latest
record decides pass or fail. A note is redacted before it is stored, so it can
point at a dated result (a PR or issue comment, a CI run) without carrying a
hostname, IP address, bucket name, or token. An incident's cause and closing
note are redacted the same way; an incident cannot exist without a documented
cause, since ``open_incident`` requires one.

Nothing here writes to a broker, and none of the write paths ever touch
trading state: they are audit notes, not risk or execution decisions.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from core.logging import redact_free_text
from core.models import utc_now
from core.reconnect import DEFAULT_RECONNECT_STORM_THRESHOLD, DEFAULT_RECONNECT_STORM_WINDOW_SECONDS
from db.models import (
    DiscrepancyRecord,
    EquityHoldingRecord,
    EquitySnapshotRecord,
    EvidenceRecord,
    FillRecord,
    IncidentRecord,
    PortfolioSnapshotRecord,
    RiskDecisionRecord,
    SystemEventRecord,
)
from portfolio.valuation import LAST_PRICE_MAX_AGE
from risk.kill_switch_journal import EVENT_TYPE as KILL_SWITCH_EVENT
from sqlalchemy import Integer, case, func, literal_column, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from api.history import HistoryUnavailable
from api.system_events import (
    DISCONNECT_EVENT,
    GAP_FILL_EVENT,
    HEARTBEAT_EVENT,
    HEARTBEAT_INTERVAL_SECONDS,
    RESTART_EVENT,
)
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
    (
        "backup_restore_verified",
        "A backup and restore drill has completed successfully.",
    ),
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

# Digest fields with no producer at all would go here; every field this card names now
# has one, so there is nothing left to list. A day can still read incomplete on a real
# gap (a missed heartbeat interval, or an incident still open at day's end).
DIGEST_GAPS: tuple[tuple[str, str], ...] = ()


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


@dataclass(frozen=True, slots=True)
class IncidentOpenSubmission:
    cause: str
    errors: tuple[str, ...] = ()


def parse_incident_open(form: Mapping[str, str]) -> IncidentOpenSubmission:
    """Validate a submitted incident open; a cause is required, never inferred."""

    cause = form.get("cause", "").strip()
    errors: list[str] = []
    if not cause:
        errors.append("Describe the cause of the incident.")
    elif len(cause) > NOTE_LIMIT:
        errors.append(f"Keep the cause to {NOTE_LIMIT} characters or fewer.")
    return IncidentOpenSubmission(cause, tuple(errors))


@dataclass(frozen=True, slots=True)
class IncidentCloseSubmission:
    incident_id: UUID | None
    note: str
    errors: tuple[str, ...] = ()


def parse_incident_close(form: Mapping[str, str]) -> IncidentCloseSubmission:
    """Validate a submitted incident close; refuses a malformed or missing reference."""

    raw_id = form.get("incident_id", "").strip()
    note = form.get("note", "").strip()
    errors: list[str] = []
    incident_id: UUID | None = None
    if not raw_id:
        errors.append("Choose an open incident to close.")
    else:
        try:
            incident_id = UUID(raw_id)
        except ValueError:
            errors.append("The incident reference is not valid.")
    if not note:
        errors.append("Describe how the incident was resolved.")
    elif len(note) > NOTE_LIMIT:
        errors.append(f"Keep the note to {NOTE_LIMIT} characters or fewer.")
    return IncidentCloseSubmission(incident_id, note, tuple(errors))


class SqlAlchemySoak:
    """Read-only digest and criterion tracker, plus the evidence and incident writes.

    Holds a session factory and nothing that can trade.
    """

    def __init__(
        self,
        session_factory: Callable[[], Session],
        *,
        reconnect_storm_threshold: int = DEFAULT_RECONNECT_STORM_THRESHOLD,
        reconnect_storm_window: timedelta = timedelta(
            seconds=DEFAULT_RECONNECT_STORM_WINDOW_SECONDS
        ),
    ) -> None:
        if reconnect_storm_threshold < 2 or reconnect_storm_window <= timedelta(0):
            raise ValueError("reconnect storm settings must be bounded and positive")
        self.session_factory = session_factory
        self.reconnect_storm_threshold = reconnect_storm_threshold
        self.reconnect_storm_window = reconnect_storm_window

    def read(self, *, now: datetime | None = None) -> dict[str, Any]:
        as_of = now or utc_now()
        query = trends_query(DIGEST_WINDOW, as_of)
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
                heartbeats = _daily_counts(
                    session,
                    query.edges,
                    SystemEventRecord.created_at,
                    SystemEventRecord.event_type == HEARTBEAT_EVENT,
                )
                uptime_ok = _uptime_ok(session, query.edges, as_of)
                restarts, recovered_restarts = _daily_restart_counts(session, query.edges)
                disconnects = _daily_counts(
                    session,
                    query.edges,
                    SystemEventRecord.created_at,
                    SystemEventRecord.event_type == DISCONNECT_EVENT,
                )
                reconnect_storms = _daily_reconnect_storms(
                    session,
                    query.edges,
                    threshold=self.reconnect_storm_threshold,
                    window=self.reconnect_storm_window,
                )
                gap_fills = _daily_counts(
                    session,
                    query.edges,
                    SystemEventRecord.created_at,
                    SystemEventRecord.event_type == GAP_FILL_EVENT,
                )
                open_incidents = _daily_open_incidents(session, query.edges)
                equity = _daily_last_equity(session, query.edges)
                latest = _latest_evidence(session, CRITERIA)
                open_incident_records = _open_incidents(session)
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
                heartbeats=heartbeats[index],
                uptime_ok=uptime_ok[index],
                restarts=restarts[index],
                recovered_restarts=recovered_restarts[index],
                disconnects=disconnects[index],
                gap_fills=gap_fills[index],
                reconnect_storm=reconnect_storms[index],
                open_incidents=open_incidents[index],
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
            "open_incidents": [_incident_dict(record) for record in open_incident_records],
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

    def open_incident(
        self, cause: str, *, opened_by: str, now: datetime | None = None
    ) -> dict[str, Any]:
        """Open one incident; the cause is required and redacted before it is stored."""

        record = IncidentRecord(
            incident_id=uuid4(),
            cause=redact_free_text(cause),
            opened_by=opened_by,
            opened_at=now or utc_now(),
        )
        try:
            with self.session_factory() as session:
                session.add(record)
                session.commit()
        except SQLAlchemyError as exc:
            raise HistoryUnavailable("the incident could not be saved") from exc
        return _incident_dict(record)

    def close_incident(
        self,
        incident_id: UUID,
        note: str,
        *,
        closed_by: str,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Close one open incident; ``None`` when no open incident matches ``incident_id``."""

        try:
            with self.session_factory() as session:
                record = session.get(IncidentRecord, incident_id)
                if record is None or record.closed_at is not None:
                    return None
                record.closed_note = redact_free_text(note)
                record.closed_by = closed_by
                record.closed_at = now or utc_now()
                session.commit()
                result = _incident_dict(record)
        except SQLAlchemyError as exc:
            raise HistoryUnavailable("the incident could not be closed") from exc
        return result


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


def _daily_reconnect_storms(
    session: Session,
    edges: tuple[datetime, ...],
    *,
    threshold: int,
    window: timedelta,
) -> list[bool]:
    """Flag each UTC day containing ``threshold`` disconnects in the rolling window."""

    moments = session.scalars(
        select(SystemEventRecord.created_at)
        .where(
            SystemEventRecord.event_type == DISCONNECT_EVENT,
            SystemEventRecord.created_at >= edges[0],
            SystemEventRecord.created_at < edges[-1],
        )
        .order_by(SystemEventRecord.created_at)
    ).all()
    storms = [False] * (len(edges) - 1)
    recent: list[datetime] = []
    for raw_moment in moments:
        moment = _utc(raw_moment)
        cutoff = moment - window
        recent = [item for item in recent if item >= cutoff]
        recent.append(moment)
        if len(recent) < threshold:
            continue
        for index, edge in enumerate(edges[1:]):
            if moment < edge:
                storms[index] = True
                break
    return storms


def _utc(moment: datetime) -> datetime:
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class DayEquity:
    """A day's final equity snapshot and the holdings it could not value at a live quote."""

    equity: Decimal | None
    partial: bool
    holdings: tuple[tuple[str, str, int | None], ...]  # symbol, basis, price age in seconds


def _daily_last_equity(session: Session, edges: tuple[datetime, ...]) -> list[DayEquity | None]:
    """The latest snapshot per UTC day, or ``None`` when no snapshot fell in it."""

    bucket = case(
        *[
            (EquitySnapshotRecord.as_of < edge, literal_column(str(index), Integer))
            for index, edge in enumerate(edges[1:])
        ]
    ).label("bucket")
    numbered = (
        select(
            EquitySnapshotRecord.snapshot_id,
            EquitySnapshotRecord.equity,
            EquitySnapshotRecord.partial,
            bucket,
            func.row_number()
            .over(partition_by=bucket, order_by=EquitySnapshotRecord.as_of.desc())
            .label("rank"),
        )
        .where(EquitySnapshotRecord.as_of >= edges[0], EquitySnapshotRecord.as_of < edges[-1])
        .subquery()
    )
    finals = session.execute(
        select(
            numbered.c.bucket, numbered.c.snapshot_id, numbered.c.equity, numbered.c.partial
        ).where(numbered.c.rank == 1)
    ).all()
    holdings: dict[Any, list[tuple[str, str, int | None]]] = {}
    for row in session.execute(
        select(
            EquityHoldingRecord.snapshot_id,
            EquityHoldingRecord.symbol,
            EquityHoldingRecord.basis,
            EquityHoldingRecord.price_age_seconds,
        )
        .where(EquityHoldingRecord.snapshot_id.in_([row[1] for row in finals]))
        .order_by(EquityHoldingRecord.symbol)
    ):
        holdings.setdefault(row[0], []).append((row[1], row[2], row[3]))
    values: list[DayEquity | None] = [None] * (len(edges) - 1)
    for row in finals:  # bucket, snapshot_id, equity, partial
        values[int(row[0])] = DayEquity(row[2], bool(row[3]), tuple(holdings.get(row[1], ())))
    return values


def _uptime_ok(session: Session, edges: tuple[datetime, ...], now: datetime) -> list[bool]:
    """True per day when every heartbeat interval expected by now has a sample.

    Heartbeats land in the same table as every other system event, so the rows
    are fetched once and bucketed in Python: at the bounded hourly cadence this
    is at most a few hundred rows for the whole 30-day window, and it is the
    only way to tell "one per interval" from "several, with a gap" apart from a
    total count that could hide the gap.
    """

    since, until = edges[0], edges[-1]
    timestamps = sorted(
        _aware(value)
        for value in session.scalars(
            select(SystemEventRecord.created_at).where(
                SystemEventRecord.event_type == HEARTBEAT_EVENT,
                SystemEventRecord.created_at >= since,
                SystemEventRecord.created_at < until,
            )
        )
    )
    step = timedelta(seconds=HEARTBEAT_INTERVAL_SECONDS)
    horizon = _aware(now)
    results: list[bool] = []
    for start, end in zip(edges, edges[1:], strict=False):
        day_end = min(end, horizon)
        expected = int((day_end - start) / step) if day_end > start else 0
        if expected <= 0:
            results.append(True)  # nothing expected for this day yet
            continue
        present = [False] * expected
        for ts in timestamps:
            if start <= ts < day_end:
                index = int((ts - start) / step)
                if index < expected:
                    present[index] = True
        results.append(all(present))
    return results


def _daily_restart_counts(
    session: Session, edges: tuple[datetime, ...]
) -> tuple[list[int], list[int]]:
    """Restarts per day, and how many of them were a crash recovery, not a clean start."""

    since, until = edges[0], edges[-1]
    rows = session.execute(
        select(SystemEventRecord.created_at, SystemEventRecord.payload).where(
            SystemEventRecord.event_type == RESTART_EVENT,
            SystemEventRecord.created_at >= since,
            SystemEventRecord.created_at < until,
        )
    ).all()
    total = [0] * (len(edges) - 1)
    recovered = [0] * (len(edges) - 1)
    for row in rows:
        ts = _aware(row[0])
        payload: dict[str, Any] = row[1]
        for index, (start, end) in enumerate(zip(edges, edges[1:], strict=False)):
            if start <= ts < end:
                total[index] += 1
                if payload.get("kind") == "recovered":
                    recovered[index] += 1
                break
    return total, recovered


def _daily_open_incidents(session: Session, edges: tuple[datetime, ...]) -> list[int]:
    """Incidents still open at the end of each day: opened before it, not yet closed."""

    until = edges[-1]
    rows = session.execute(
        select(IncidentRecord.opened_at, IncidentRecord.closed_at).where(
            IncidentRecord.opened_at < until
        )
    ).all()
    counts = [0] * (len(edges) - 1)
    for index, end in enumerate(edges[1:]):
        counts[index] = sum(
            1 for row in rows if _aware(row[0]) < end and (row[1] is None or _aware(row[1]) >= end)
        )
    return counts


def _open_incidents(session: Session) -> list[IncidentRecord]:
    return list(
        session.scalars(
            select(IncidentRecord)
            .where(IncidentRecord.closed_at.is_(None))
            .order_by(IncidentRecord.opened_at)
        )
    )


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
    equity: DayEquity | None,
    *,
    in_progress: bool,
    heartbeats: int,
    uptime_ok: bool,
    restarts: int,
    recovered_restarts: int,
    disconnects: int,
    gap_fills: int,
    reconnect_storm: bool,
    open_incidents: int,
) -> dict[str, Any]:
    missing: list[str] = []
    value, notes, approximate = _equity_field(equity, missing)
    if not uptime_ok:
        missing.append("uptime")
    if open_incidents > 0:
        missing.append("incidents")
    if reconnect_storm:
        missing.append("reconnect_storm")
    return {
        "date": start.date().isoformat(),
        "in_progress": in_progress,
        "trades": trades,
        "refusals": refusals,
        "reconciliation_runs": reconciliation_runs,
        "divergences": divergences,
        "kill_switch_events": kill_switch_events,
        "heartbeats": heartbeats,
        "restarts": restarts,
        "recovered_restarts": recovered_restarts,
        "disconnects": disconnects,
        "gap_fills": gap_fills,
        "reconnect_storm": reconnect_storm,
        "review": "reconnect storm" if reconnect_storm else None,
        "open_incidents": open_incidents,
        "equity": _decimal(value),
        # The asterisk and footnotes come from the stored holdings, not from display logic.
        "equity_approximate": approximate,
        "equity_notes": notes,
        "missing": missing,
        # Missing data always includes a real gap, so a day is never marked pass on
        # partial evidence: a missed heartbeat interval, or an incident still open.
        "status": "incomplete" if missing else "pass",
    }


def _equity_field(
    equity: DayEquity | None, missing: list[str]
) -> tuple[Decimal | None, list[str], bool]:
    """The digest's equity value, its footnotes, and whether it rests on a last-known price.

    A day with no snapshot is ``not recorded``. A partial snapshot has no value at all, and
    a last-known price that has reached the age limit keeps the day incomplete: both name
    the holding.
    """

    if equity is None:
        missing.append("equity")
        return None, [], False
    notes: list[str] = []
    for symbol, basis, age in equity.holdings:
        if basis == "unpriced":
            notes.append(f"{symbol} has never had a price, so equity could not be valued")
        elif basis == "last_known" and age is not None:
            if timedelta(seconds=age) >= LAST_PRICE_MAX_AGE:
                notes.append(
                    f"{symbol} is valued at its last price, {_age_text(age)} old; "
                    f"the limit is {_age_text(int(LAST_PRICE_MAX_AGE.total_seconds()))}"
                )
                if "equity_price_age" not in missing:
                    missing.append("equity_price_age")
            else:
                notes.append(f"includes {symbol} at its last price, {_age_text(age)} old")
    if equity.equity is None:
        missing.append("equity")
    return (
        equity.equity,
        notes,
        equity.equity is not None and any(basis == "last_known" for _, basis, _ in equity.holdings),
    )


def _age_text(seconds: int) -> str:
    if seconds < 3600:
        return f"{seconds // 60} min"
    return f"{seconds // 3600} h"


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


def _incident_dict(record: IncidentRecord) -> dict[str, Any]:
    return {
        "incident_id": str(record.incident_id),
        "cause": record.cause,
        "opened_by": record.opened_by,
        "opened_at": _iso(record.opened_at),
        "closed_note": record.closed_note,
        "closed_by": record.closed_by,
        "closed_at": _iso(record.closed_at) if record.closed_at is not None else None,
    }


def _decimal(value: Decimal | None) -> str | None:
    return format(Decimal(value).normalize(), "f") if value is not None else None


def _iso(value: datetime) -> str:
    return _aware(value).isoformat()


def _aware(value: datetime) -> datetime:
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).astimezone(UTC)
