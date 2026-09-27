"""Bounded, read-only operator history from the persisted audit tables.

Every list read takes a ``HistoryQuery``: a time window of at most ``MAX_WINDOW``
and a page of at most ``MAX_LIMIT`` rows. ``parse_query`` refuses anything else,
so no request can ask for the whole history. Pages run newest first and continue
from a ``before`` cursor, so rows recorded after the first page never shift later
pages.

Rows carry identifiers, statuses, amounts, reasons, and times. Payloads stay in
the database: a discrepancy says which fields differ, not their values, and an
event shows only the fields its type is known to carry.

Nothing here writes, and nothing reaches a broker.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from core.logging import redact_free_text
from core.models import OrderStatus, utc_now
from db.models import (
    DiscrepancyRecord,
    FillRecord,
    OrderRecord,
    RiskDecisionRecord,
    SignalRecord,
    SystemEventRecord,
)
from execution.audit import OrderLineage
from risk.engine import RISK_GATES
from risk.kill_switch_journal import EVENT_TYPE as KILL_SWITCH_EVENT
from sqlalchemy import ColumnElement, and_, func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, aliased

from api.controls import REARM_CHECKLIST

DEFAULT_LIMIT = 25
MAX_LIMIT = 100
WINDOWS = {
    "1h": timedelta(hours=1),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "31d": timedelta(days=31),
}
DEFAULT_WINDOW = "24h"
MAX_WINDOW = WINDOWS["31d"]
FILLS_PER_ORDER = 50
TEXT_LIMIT = 128

# The trading cycle refuses with this gate when it cannot assemble the inputs the
# ordered gates need, before ``evaluate`` runs.
UNGATED = {"risk_inputs": "Risk inputs could not be assembled"}
GATE_LABELS = dict(RISK_GATES) | UNGATED
_GATE_POSITIONS = {gate: position for position, (gate, _) in enumerate(RISK_GATES, start=1)}
_CHECKLIST_KEYS = frozenset(key for key, _ in REARM_CHECKLIST)
ORDER_STATUSES = tuple(status.value for status in OrderStatus)
UNKNOWN_ORDER_RULE = "Look it up by client order ID. Never resubmit it."
DISCREPANCY_TYPES = ("order", "fill", "position", "balance")
EVENT_TYPES = (KILL_SWITCH_EVENT,)
KINDS = ("orders", "signals", "risk_decisions", "discrepancies", "events", "refusals")
_PAGING = ("since", "until", "window", "limit", "before")


@dataclass(frozen=True, slots=True)
class Filter:
    name: str
    kind: str  # "text", "choice", or "uuid"
    choices: tuple[str, ...] = ()


_SYMBOL = Filter("symbol", "text")
_STRATEGY = Filter("strategy_version", "text")
_CORRELATION = Filter("correlation_id", "uuid")
FILTERS: dict[str, tuple[Filter, ...]] = {
    "orders": (
        _SYMBOL,
        Filter("status", "choice", ORDER_STATUSES),
        _STRATEGY,
        Filter("client_order_id", "uuid"),
        _CORRELATION,
    ),
    "signals": (_SYMBOL, _STRATEGY),
    "risk_decisions": (
        Filter("outcome", "choice", ("approved", "refused")),
        Filter("failed_gate", "choice", tuple(GATE_LABELS)),
        _SYMBOL,
        _STRATEGY,
        _CORRELATION,
    ),
    "discrepancies": (Filter("entity_type", "choice", DISCREPANCY_TYPES),),
    "events": (Filter("event_type", "choice", EVENT_TYPES),),
    "refusals": (),
}


class HistoryQueryRefused(ValueError):
    """The request is unbounded or malformed; nothing was read."""

    def __init__(self, errors: Sequence[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = tuple(errors)


class HistoryUnavailable(RuntimeError):
    """The history could not be read. It is not the same as an empty history."""


@dataclass(frozen=True, slots=True)
class Cursor:
    at: datetime
    key: UUID

    def encode(self) -> str:
        return f"{self.at.astimezone(UTC).strftime('%Y%m%dT%H%M%S%fZ')}-{self.key.hex}"

    @classmethod
    def decode(cls, value: str) -> Cursor:
        match = re.fullmatch(r"(\d{8}T\d{12}Z)-([0-9a-f]{32})", value)
        if match is None:
            raise ValueError(value)
        at = datetime.strptime(match.group(1), "%Y%m%dT%H%M%S%fZ").replace(tzinfo=UTC)
        return cls(at, UUID(match.group(2)))


@dataclass(frozen=True, slots=True)
class HistoryQuery:
    kind: str
    since: datetime
    until: datetime
    limit: int = DEFAULT_LIMIT
    window: str | None = DEFAULT_WINDOW
    before: Cursor | None = None
    filters: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "since": self.since.isoformat(),
            "until": self.until.isoformat(),
            "window": self.window,
            "max_days": MAX_WINDOW.days,
            "limit": self.limit,
            "max_limit": MAX_LIMIT,
            "before": self.before.encode() if self.before else None,
            "filters": {key: str(value) for key, value in self.filters.items()},
        }

    def params(self, **changes: str | None) -> dict[str, str]:
        """Query parameters that repeat this query; ``changes`` override or drop keys."""

        params: dict[str, str] = {"until": self.until.isoformat()}
        if self.window is not None:
            params["window"] = self.window
        else:
            params["since"] = self.since.isoformat()
        if self.limit != DEFAULT_LIMIT:
            params["limit"] = str(self.limit)
        params |= {key: str(value) for key, value in self.filters.items()}
        for key, value in changes.items():
            if value is None:
                params.pop(key, None)
            else:
                params[key] = value
        return params


@dataclass(frozen=True, slots=True)
class Page:
    rows: list[dict[str, Any]]
    total: int
    next_before: str | None = None


def parse_query(
    kind: str, items: Iterable[tuple[str, str]], *, now: datetime | None = None
) -> HistoryQuery:
    """Validate query parameters for ``kind``, refusing unbounded or unknown requests.

    Empty values count as absent, so a submitted filter form works as-is. Without a
    window the last 24 hours are read; a window longer than 31 days, a page larger
    than 100 rows, an unknown parameter, or a repeated one is refused.
    """

    now = now or utc_now()
    allowed = {item.name: item for item in FILTERS[kind]}
    values: dict[str, str] = {}
    seen: set[str] = set()
    errors: list[str] = []
    for key, raw in items:
        if key not in allowed and key not in _PAGING:
            errors.append(f"Unknown parameter {key[:TEXT_LIMIT]!r}.")
            continue
        if key in seen:
            errors.append(f"Give {key!r} once.")
            continue
        seen.add(key)
        if raw.strip():
            values[key] = raw.strip()

    limit = DEFAULT_LIMIT
    if "limit" in values:
        text = values["limit"]
        if not text.isdigit() or not 1 <= int(text) <= MAX_LIMIT:
            errors.append(f"limit must be a whole number from 1 to {MAX_LIMIT}.")
        else:
            limit = int(text)

    until = now
    if "until" in values:
        until = _moment(values["until"], "until", errors) or now
    window: str | None = None
    since: datetime | None = None
    if "since" in values and "window" in values:
        errors.append("Give since or window, not both.")
    elif "since" in values:
        since = _moment(values["since"], "since", errors)
    else:
        window = values.get("window", DEFAULT_WINDOW)
        if window not in WINDOWS:
            errors.append(f"window must be one of {', '.join(WINDOWS)}.")
            window = DEFAULT_WINDOW
        since = until - WINDOWS[window]
    if since is not None and since >= until:
        errors.append("since must be earlier than until.")
    elif since is not None and until - since > MAX_WINDOW:
        errors.append(f"The time window can be at most {MAX_WINDOW.days} days.")

    before = None
    if "before" in values:
        try:
            before = Cursor.decode(values["before"])
        except ValueError:
            errors.append("before is not a cursor from a previous page.")

    filters: dict[str, Any] = {}
    for name, value in values.items():
        spec = allowed.get(name)
        if spec is None:
            continue
        if spec.kind == "choice" and value not in spec.choices:
            errors.append(f"{name} must be one of {', '.join(spec.choices)}.")
        elif spec.kind == "uuid":
            try:
                filters[name] = UUID(value)
            except ValueError:
                errors.append(f"{name} must be a UUID.")
        elif len(value) > TEXT_LIMIT:
            errors.append(f"{name} can be at most {TEXT_LIMIT} characters.")
        else:
            filters[name] = value.upper() if name == "symbol" else value

    if errors:
        raise HistoryQueryRefused(errors)
    assert since is not None
    return HistoryQuery(kind, since, until, limit, window, before, filters)


def _moment(text: str, name: str, errors: list[str]) -> datetime | None:
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        errors.append(f"{name} must be an ISO 8601 time, for example 2026-09-27T12:00:00Z.")
        return None
    # Browser date-time fields send local time without an offset; the page labels them UTC.
    return (moment if moment.tzinfo else moment.replace(tzinfo=UTC)).astimezone(UTC)


class SqlAlchemyHistory:
    """Read-only history queries. Holds a session factory and nothing that can trade."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self.session_factory = session_factory

    def orders(self, query: HistoryQuery) -> Page:
        statement = select(OrderRecord)
        conditions = _where(
            query,
            symbol=OrderRecord.symbol,
            status=OrderRecord.status,
            strategy_version=OrderRecord.strategy_version,
            client_order_id=OrderRecord.client_order_id,
            correlation_id=OrderRecord.correlation_id,
        )

        def rows(session: Session, records: Sequence[Any]) -> list[dict[str, Any]]:
            return _lineage_rows(session, [record for (record,) in records])

        return self._page(
            query, statement, conditions, OrderRecord.created_at, OrderRecord.order_id, rows
        )

    def signals(self, query: HistoryQuery) -> Page:
        statement = select(SignalRecord)
        conditions = _where(
            query, symbol=SignalRecord.symbol, strategy_version=SignalRecord.strategy_version
        )

        def rows(session: Session, records: Sequence[Any]) -> list[dict[str, Any]]:
            signals = [record for (record,) in records]
            ids = [signal.signal_id for signal in signals]
            decisions: dict[UUID, RiskDecisionRecord] = {}
            for decision in session.scalars(
                select(RiskDecisionRecord)
                .where(RiskDecisionRecord.signal_id.in_(ids))
                .order_by(RiskDecisionRecord.decided_at)
            ):
                decisions[decision.signal_id] = decision  # the newest decision wins
            orders = {
                order.signal_id: order
                for order in session.scalars(
                    select(OrderRecord).where(OrderRecord.signal_id.in_(ids))
                )
            }
            return [
                _signal(signal)
                | {
                    "decision": _decision_summary(decisions.get(signal.signal_id)),
                    "order": _order_summary(orders.get(signal.signal_id)),
                }
                for signal in signals
            ]

        return self._page(
            query, statement, conditions, SignalRecord.created_at, SignalRecord.signal_id, rows
        )

    def risk_decisions(self, query: HistoryQuery) -> Page:
        statement = select(RiskDecisionRecord, SignalRecord).outerjoin(
            SignalRecord, SignalRecord.signal_id == RiskDecisionRecord.signal_id
        )
        conditions = _where(
            query,
            failed_gate=RiskDecisionRecord.failed_gate,
            symbol=SignalRecord.symbol,
            strategy_version=SignalRecord.strategy_version,
            correlation_id=RiskDecisionRecord.correlation_id,
        )
        outcome = query.filters.get("outcome")
        if outcome is not None:
            conditions.append(RiskDecisionRecord.approved.is_(outcome == "approved"))

        def rows(session: Session, records: Sequence[Any]) -> list[dict[str, Any]]:
            ids = [row[0].approval_id for row in records]
            orders = {
                order.risk_approval_id: order
                for order in session.scalars(
                    select(OrderRecord).where(OrderRecord.risk_approval_id.in_(ids))
                )
            }
            return [
                _decision(decision)
                | {
                    "signal": _signal(signal) if signal is not None else None,
                    "order": _order_summary(orders.get(decision.approval_id)),
                }
                for decision, signal in records
            ]

        return self._page(
            query,
            statement,
            conditions,
            RiskDecisionRecord.decided_at,
            RiskDecisionRecord.approval_id,
            rows,
        )

    def discrepancies(self, query: HistoryQuery) -> Page:
        statement = select(DiscrepancyRecord)
        conditions = _where(query, entity_type=DiscrepancyRecord.entity_type)
        return self._page(
            query,
            statement,
            conditions,
            DiscrepancyRecord.created_at,
            DiscrepancyRecord.discrepancy_id,
            lambda _session, records: [_discrepancy(record) for (record,) in records],
        )

    def events(self, query: HistoryQuery) -> Page:
        statement = select(SystemEventRecord)
        conditions = _where(query, event_type=SystemEventRecord.event_type)
        return self._page(
            query,
            statement,
            conditions,
            SystemEventRecord.created_at,
            SystemEventRecord.event_id,
            lambda _session, records: [_event(record) for (record,) in records],
        )

    def refusals(self, query: HistoryQuery) -> dict[str, Any]:
        """Refusals in the window grouped by gate, in gate order, and the latest one."""

        window = _window(query, RiskDecisionRecord.decided_at)
        try:
            with self.session_factory() as session:
                outcomes = {
                    bool(approved): count
                    for approved, count in session.execute(
                        select(RiskDecisionRecord.approved, func.count())
                        .where(window)
                        .group_by(RiskDecisionRecord.approved)
                    ).all()
                }
                by_gate = {
                    gate: (count, latest)
                    for gate, count, latest in session.execute(
                        select(
                            RiskDecisionRecord.failed_gate,
                            func.count(),
                            func.max(RiskDecisionRecord.decided_at),
                        )
                        .where(window, RiskDecisionRecord.approved.is_(False))
                        .group_by(RiskDecisionRecord.failed_gate)
                    ).all()
                }
        except SQLAlchemyError as exc:
            raise HistoryUnavailable("risk decisions could not be read") from exc
        latest = self.risk_decisions(
            HistoryQuery(
                "risk_decisions",
                query.since,
                query.until,
                limit=1,
                window=query.window,
                filters={"outcome": "refused"},
            )
        ).rows

        def gate_row(position: int | None, gate: str | None, label: str) -> dict[str, Any]:
            count, at = by_gate.pop(gate, (0, None))
            return {
                "position": position,
                "gate": gate,
                "label": label,
                "refusals": count,
                "latest_at": _iso(at),
            }

        gates = [
            gate_row(position, gate, label)
            for position, (gate, label) in enumerate(RISK_GATES, start=1)
        ]
        other = [gate_row(None, gate, label) for gate, label in UNGATED.items()]
        # Anything left was recorded under a gate this build does not know.
        other += [
            gate_row(None, gate, "Unrecognised gate" if gate else "No gate recorded")
            for gate in sorted(by_gate, key=lambda item: item or "")
        ]
        return {
            "decisions": sum(outcomes.values()),
            "approved": outcomes.get(True, 0),
            "refused": outcomes.get(False, 0),
            "latest_refusal": latest[0] if latest else None,
            "gates": gates,
            "other": other,
        }

    def lineage(self, client_order_id: UUID) -> dict[str, Any] | None:
        """Signal, risk decision, order, and fills for one order, from persisted rows."""

        try:
            with self.session_factory() as session:
                order = session.scalar(
                    select(OrderRecord).where(OrderRecord.client_order_id == client_order_id)
                )
                rows = _lineage_rows(session, [order] if order is not None else [])
        except SQLAlchemyError as exc:
            raise HistoryUnavailable("order lineage could not be read") from exc
        return rows[0] if rows else None

    def _page(
        self,
        query: HistoryQuery,
        statement: Any,
        conditions: list[ColumnElement[bool]],
        at: Any,
        key: Any,
        build: Callable[[Session, Sequence[Any]], list[dict[str, Any]]],
    ) -> Page:
        conditions = [_window(query, at), *conditions]
        filtered = statement.where(*conditions)
        if query.before is not None:
            cursor = query.before
            filtered = filtered.where(or_(at < cursor.at, and_(at == cursor.at, key < cursor.key)))
        try:
            with self.session_factory() as session:
                total = session.scalar(
                    select(func.count()).select_from(statement.where(*conditions).subquery())
                )
                records = session.execute(
                    filtered.order_by(at.desc(), key.desc()).limit(query.limit + 1)
                ).all()
                more = len(records) > query.limit
                records = records[: query.limit]
                rows = build(session, records)
        except SQLAlchemyError as exc:
            raise HistoryUnavailable(f"{query.kind} could not be read") from exc
        next_before = None
        if more:
            last = records[-1][0]
            next_before = Cursor(_utc(getattr(last, at.key)), getattr(last, key.key)).encode()
        return Page(rows, int(total or 0), next_before)


def _window(query: HistoryQuery, at: Any) -> ColumnElement[bool]:
    return and_(at >= query.since, at <= query.until)


def _where(query: HistoryQuery, **columns: Any) -> list[ColumnElement[bool]]:
    return [
        column == query.filters[name] for name, column in columns.items() if name in query.filters
    ]


def _fills(session: Session, order_ids: list[UUID]) -> dict[UUID, dict[str, Any]]:
    """Up to ``FILLS_PER_ORDER`` fills per order, oldest first, with totals of all fills."""

    if not order_ids:
        return {}
    totals = {
        order_id: (count, quantity)
        for order_id, count, quantity in session.execute(
            select(FillRecord.order_id, func.count(), func.sum(FillRecord.quantity))
            .where(FillRecord.order_id.in_(order_ids))
            .group_by(FillRecord.order_id)
        ).all()
    }
    numbered = (
        select(
            FillRecord,
            func.row_number()
            .over(
                partition_by=FillRecord.order_id,
                order_by=(FillRecord.occurred_at, FillRecord.fill_id),
            )
            .label("position"),
        )
        .where(FillRecord.order_id.in_(order_ids))
        .subquery()
    )
    fill = aliased(FillRecord, numbered)
    result: dict[UUID, dict[str, Any]] = {
        order_id: {"count": count, "quantity": quantity, "rows": []}
        for order_id, (count, quantity) in totals.items()
    }
    for record in session.scalars(
        select(fill)
        .where(numbered.c.position <= FILLS_PER_ORDER)
        .order_by(numbered.c.order_id, numbered.c.position)
    ):
        result[record.order_id]["rows"].append(
            {
                "fill_id": record.broker_fill_id,
                "quantity": _decimal(record.quantity),
                "price": _decimal(record.price),
                "fee": _decimal(record.fee),
                "occurred_at": _iso(record.occurred_at),
            }
        )
    return result


def _lineage_rows(session: Session, orders: Sequence[OrderRecord]) -> list[dict[str, Any]]:
    """Each order with its signal, the risk decision it cites, its fills, and any gaps."""

    if not orders:
        return []
    fills = _fills(session, [order.order_id for order in orders])
    signals = {
        record.signal_id: record
        for record in session.scalars(
            select(SignalRecord).where(
                SignalRecord.signal_id.in_({order.signal_id for order in orders})
            )
        )
    }
    decisions = {
        record.approval_id: record
        for record in session.scalars(
            select(RiskDecisionRecord).where(
                RiskDecisionRecord.approval_id.in_({order.risk_approval_id for order in orders})
            )
        )
    }
    rows = []
    for order in orders:
        signal = signals.get(order.signal_id)
        decision = decisions.get(order.risk_approval_id)
        row = _order(order, fills.get(order.order_id))
        row["signal"] = _signal(signal) if signal is not None else None
        row["risk_decision"] = _decision(decision) if decision is not None else None
        row["gaps"] = list(
            OrderLineage(
                client_order_id=row["client_order_id"],
                # An unrecognised status is treated as unknown: never assume it settled.
                status=OrderStatus(order.status)
                if order.status in ORDER_STATUSES
                else OrderStatus.UNKNOWN,
                signal_recorded=signal is not None,
                strategy_version_matches=(
                    signal is not None and signal.strategy_version == order.strategy_version
                ),
                risk_decision_recorded=decision is not None,
                risk_decision_approved=decision is not None and decision.approved,
                fill_count=row["fill_count"],
            ).gaps
        )
        rows.append(row)
    return rows


def _order(record: OrderRecord, fills: Mapping[str, Any] | None) -> dict[str, Any]:
    fills = fills or {"count": 0, "quantity": Decimal("0"), "rows": []}
    row = {
        "client_order_id": str(record.client_order_id),
        "correlation_id": _text(record.correlation_id),
        "signal_id": str(record.signal_id),
        "risk_approval_id": str(record.risk_approval_id),
        "strategy_version": record.strategy_version,
        "symbol": record.symbol,
        "side": record.side,
        "order_type": record.order_type,
        "quantity": _decimal(record.quantity),
        "limit_price": _decimal(record.limit_price),
        "status": record.status,
        "created_at": _iso(record.created_at),
        "filled_quantity": _decimal(fills["quantity"]),
        "fill_count": fills["count"],
        "fills": fills["rows"],
    }
    if record.status == OrderStatus.UNKNOWN.value:
        row["rule"] = UNKNOWN_ORDER_RULE
    return row


def _order_summary(record: OrderRecord | None) -> dict[str, Any] | None:
    if record is None:
        return None
    return {"client_order_id": str(record.client_order_id), "status": record.status}


def _signal(record: SignalRecord) -> dict[str, Any]:
    return {
        "signal_id": str(record.signal_id),
        "symbol": record.symbol,
        "side": record.side,
        "quantity": _decimal(record.quantity),
        "strategy_version": record.strategy_version,
        "created_at": _iso(record.created_at),
    }


def _decision(record: RiskDecisionRecord) -> dict[str, Any]:
    return {
        "approval_id": str(record.approval_id),
        "signal_id": str(record.signal_id),
        "correlation_id": str(record.correlation_id),
    } | _verdict(record)


def _decision_summary(record: RiskDecisionRecord | None) -> dict[str, Any] | None:
    return _verdict(record) if record is not None else None


def _verdict(record: RiskDecisionRecord) -> dict[str, Any]:
    gate = record.failed_gate
    return {
        "outcome": "approved" if record.approved else "refused",
        "failed_gate": gate,
        "gate_position": _GATE_POSITIONS.get(gate or ""),
        "gate_label": GATE_LABELS.get(gate, "Unrecognised gate") if gate else None,
        "reason": redact_free_text(record.reason),
        "decided_at": _iso(record.decided_at),
    }


# Fields a discrepancy may compare. Only their names leave the database.
_COMPARED = (
    "status",
    "filled_quantity",
    "quantity",
    "average_price",
    "available",
    "hold",
    "price",
    "fee",
)


def _discrepancy(record: DiscrepancyRecord) -> dict[str, Any]:
    local = record.local_payload or {}
    broker = record.broker_payload or {}
    differs: list[str] = []
    if local and broker:
        differs = [key for key in _COMPARED if local.get(key) != broker.get(key)]
        if local.get("value") != broker.get("value"):
            # Order differences are recorded as (status, filled quantity) text.
            differs.append("status or filled quantity")
    return {
        "discrepancy_id": str(record.discrepancy_id),
        "entity_type": record.entity_type,
        "entity_key": record.entity_key,
        "local_recorded": bool(local),
        "broker_recorded": bool(broker),
        "differs": differs,
        "safety_action": record.safety_action,
        "created_at": _iso(record.created_at),
    }


def _event(record: SystemEventRecord) -> dict[str, Any]:
    detail: dict[str, Any] | None = None
    if record.event_type == KILL_SWITCH_EVENT:
        payload = record.payload or {}
        detail = {
            "from": _text(payload.get("from")),
            "to": _text(payload.get("to")),
            "actor": _text(payload.get("actor")),
            "automatic": bool(payload.get("automatic")),
            "reason": redact_free_text(str(payload.get("reason") or "")),
            "checklist": [
                str(item) for item in payload.get("checklist") or () if item in _CHECKLIST_KEYS
            ],
        }
    return {
        "event_id": str(record.event_id),
        "event_type": record.event_type,
        "correlation_id": str(record.correlation_id),
        "created_at": _iso(record.created_at),
        "detail": detail,
    }


def _utc(value: datetime) -> datetime:
    # SQLite returns naive values; every writer records UTC.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _iso(value: datetime | None) -> str | None:
    return _utc(value).isoformat() if value is not None else None


def _decimal(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(Decimal(value).normalize(), "f")


def _text(value: object) -> str | None:
    return str(value) if value not in (None, "") else None
