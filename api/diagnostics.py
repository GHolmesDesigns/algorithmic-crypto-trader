"""The read-only report behind ``deploy/drill.sh diagnose``.

When trading halts, the questions are always the same: which order is pending and does the venue
know it, which value differed and by how much, and does the projected balance add up from the
fills. This answers them from what is already stored, so nobody queries the database by hand.

Nothing here writes. ``SqlAlchemyDiagnostics`` issues only ``SELECT``; the one provider call is
``get_order`` for a pending order, the same read startup recovery makes. The report never prints
a secret, token, host name, or address: orders and fills are shown by an eight-character
reference (an order's first eight characters, a fill's last eight, where trade IDs differ), and a
provider failure by its type, status code, endpoint path, and reason.

The service refuses to build it in ``live`` mode or with a trade-capable credential scope.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from brokers.http import describe_provider_failure
from core.guards import CredentialScope
from core.logging import fill_reference, short_reference
from core.models import Balance, Fill, OrderSide, Position, TradingMode, utc_now
from db.models import (
    BalanceSnapshotRecord,
    DiscrepancyRecord,
    FillRecord,
    OrderRecord,
    PortfolioSnapshotRecord,
)
from portfolio.divergence import FieldChange, field_changes
from portfolio.explain import AssetExplanation, explain_window
from portfolio.reconciliation import Discrepancy, PortfolioState
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from api.operator import OperatorState

DEFAULT_LIMIT = 5
MAX_LIMIT = 20
# Pending orders are few; each one asked about costs a provider read, so only some are asked.
PENDING_LISTED = 20
PENDING_LOOKUPS = 5
LOOKUP_SECONDS = 6.0
# Broker snapshots read to place fills in their reconciliation windows (header rows only).
SNAPSHOT_LIMIT = 5000
PENDING_STATUSES = ("pending_submit", "unknown")
SAFE_MODES = frozenset({TradingMode.BACKTEST, TradingMode.REPLAY, TradingMode.PAPER})

REFUSAL = "diagnostics are refused in live mode or with a trade-capable credential scope"


class DiagnosticsUnavailable(RuntimeError):
    """A stored section could not be read."""


def permitted(mode: TradingMode, scope: CredentialScope) -> bool:
    """Only a sandbox or offline mode with a scope that cannot trade may be diagnosed."""

    return mode in SAFE_MODES and scope is not CredentialScope.TRADE


@dataclass(frozen=True, slots=True)
class PendingOrder:
    client_order_id: str
    symbol: str | None
    side: str | None
    quantity: Decimal | None
    status: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class StoredDiscrepancy:
    created_at: datetime
    entity_type: str
    entity_key: str
    local: Mapping[str, Any]
    broker: Mapping[str, Any]
    safety_action: str


@dataclass(frozen=True, slots=True)
class SnapshotRef:
    batch_id: UUID
    recorded_at: datetime


class SqlAlchemyDiagnostics:
    """SELECT-only reads. There is no insert, update, or delete in this class."""

    def __init__(self, session_factory) -> None:
        self.session_factory = session_factory

    def pending_orders(self, limit: int = PENDING_LISTED) -> tuple[PendingOrder, ...]:
        statement = (
            select(OrderRecord)
            .where(OrderRecord.status.in_(PENDING_STATUSES))
            .order_by(OrderRecord.created_at, OrderRecord.order_id)
            .limit(limit)
        )
        return tuple(
            PendingOrder(
                client_order_id=str(record.client_order_id),
                symbol=record.symbol,
                side=record.side,
                quantity=record.quantity,
                status=record.status,
                created_at=record.created_at,
            )
            for record in self._scalars(statement)
        )

    def discrepancies(self, limit: int) -> tuple[StoredDiscrepancy, ...]:
        statement = (
            select(DiscrepancyRecord)
            .order_by(DiscrepancyRecord.created_at.desc(), DiscrepancyRecord.discrepancy_id)
            .limit(limit)
        )
        return tuple(
            StoredDiscrepancy(
                created_at=record.created_at,
                entity_type=record.entity_type,
                entity_key=record.entity_key,
                local=record.local_payload or {},
                broker=record.broker_payload or {},
                safety_action=record.safety_action,
            )
            for record in self._scalars(statement)
        )

    def fills(self, limit: int) -> tuple[Fill, ...]:
        """The newest fills with their order's symbol and side; a fill without its order is skipped.

        The fee's asset is not stored, so it is taken to be the quote asset, as persistence does.
        """

        statement = (
            select(FillRecord, OrderRecord.symbol, OrderRecord.side)
            .join(OrderRecord, OrderRecord.order_id == FillRecord.order_id)
            .order_by(FillRecord.occurred_at.desc(), FillRecord.broker_fill_id)
            .limit(limit)
        )
        try:
            with self.session_factory() as session:
                rows = session.execute(statement).all()
        except SQLAlchemyError as exc:
            raise DiagnosticsUnavailable("fills could not be read") from exc
        try:
            return tuple(
                Fill(
                    fill_id=record.broker_fill_id,
                    order_id=record.order_id,
                    symbol=symbol,
                    side=OrderSide(side),
                    quantity=record.quantity,
                    price=record.price,
                    fee=record.fee,
                    fee_asset=symbol.rsplit("-", 1)[-1],
                    occurred_at=record.occurred_at,
                )
                for record, symbol, side in rows
                if symbol and side
            )
        except ValueError as exc:
            raise DiagnosticsUnavailable("stored fills could not be interpreted") from exc

    def snapshot_timeline(
        self, earliest: datetime
    ) -> tuple[SnapshotRef | None, tuple[SnapshotRef, ...]]:
        """The newest broker snapshot before ``earliest``, and every one from ``earliest`` on."""

        before = (
            select(PortfolioSnapshotRecord)
            .where(
                PortfolioSnapshotRecord.source == "broker",
                PortfolioSnapshotRecord.recorded_at < earliest,
            )
            .order_by(PortfolioSnapshotRecord.recorded_at.desc())
            .limit(1)
        )
        after = (
            select(PortfolioSnapshotRecord)
            .where(
                PortfolioSnapshotRecord.source == "broker",
                PortfolioSnapshotRecord.recorded_at >= earliest,
            )
            .order_by(PortfolioSnapshotRecord.recorded_at)
            .limit(SNAPSHOT_LIMIT)
        )
        previous = [SnapshotRef(item.batch_id, item.recorded_at) for item in self._scalars(before)]
        later = tuple(SnapshotRef(item.batch_id, item.recorded_at) for item in self._scalars(after))
        return (previous[0] if previous else None), later

    def balances(self, batch_id: UUID) -> tuple[Balance, ...]:
        statement = select(BalanceSnapshotRecord).where(BalanceSnapshotRecord.batch_id == batch_id)
        return tuple(
            Balance(
                asset=record.asset,
                available=record.available,
                hold=record.hold,
                as_of=record.as_of,
            )
            for record in self._scalars(statement)
        )

    def _scalars(self, statement):
        try:
            with self.session_factory() as session:
                return session.scalars(statement).all()
        except SQLAlchemyError as exc:
            raise DiagnosticsUnavailable("stored state could not be read") from exc


async def build_diagnostics(
    state: OperatorState,
    reader: SqlAlchemyDiagnostics | None,
    *,
    limit: int = DEFAULT_LIMIT,
    increments: Mapping[str, Decimal] | None = None,
) -> dict[str, Any]:
    """Assemble every section; one that cannot be read says so and the rest still print."""

    now = utc_now()
    report: dict[str, Any] = {
        "mode": state.settings.trading_mode.value,
        "credential_scope": state.settings.credential_scope.value,
        "generated_at": now.isoformat(),
        "limit": limit,
        "kill_switch": state.kill_switch.state.value,
        "recovery": (
            state.startup_recovery.to_dict()
            if state.startup_recovery is not None
            else {"status": "not_run", "detail": "startup recovery has not run"}
        ),
        "reconciliation": (
            state.scheduled_reconciliation.status.to_dict()
            if state.scheduled_reconciliation is not None
            else {"last_result": "not_scheduled"}
        ),
    }
    if reader is None:
        unreadable = {"readable": False, "reason": "no database is configured in this process"}
        report.update(pending_orders=unreadable, discrepancies=unreadable, fills=unreadable)
    else:
        report["pending_orders"] = await _pending_section(state, reader, now)
        report["discrepancies"] = _section(lambda: _discrepancy_rows(reader, limit))
        report["fills"] = _section(lambda: _fill_rows(reader, limit, increments or {}))
    report["report"] = render_report(report)
    return report


def _section(build) -> dict[str, Any]:
    try:
        return {"readable": True, **build()}
    except DiagnosticsUnavailable as exc:
        return {"readable": False, "reason": str(exc)}


async def _pending_section(
    state: OperatorState, reader: SqlAlchemyDiagnostics, now: datetime
) -> dict[str, Any]:
    try:
        orders = reader.pending_orders()
    except DiagnosticsUnavailable as exc:
        return {"readable": False, "reason": str(exc)}
    answers = await asyncio.gather(
        *(
            _lookup(state, order.client_order_id) if index < PENDING_LOOKUPS else _skipped()
            for index, order in enumerate(orders)
        )
    )
    rows = [
        {
            "ref": short_reference(order.client_order_id),
            "status": order.status,
            "symbol": order.symbol,
            "side": order.side,
            "quantity": _plain(order.quantity) if order.quantity is not None else None,
            "age_seconds": max(0, int((now - _utc(order.created_at)).total_seconds())),
            "broker": answer,
        }
        for order, answer in zip(orders, answers, strict=True)
    ]
    return {"readable": True, "orders": rows}


async def _skipped() -> str:
    return "not asked (lookup limit reached)"


async def _lookup(state: OperatorState, client_order_id: str) -> str:
    """Whether the venue knows an order: one ``get_order`` read, bounded in time."""

    if state.broker is None:
        return "not asked (no broker is configured)"
    try:
        found = await asyncio.wait_for(state.broker.get_order(client_order_id), LOOKUP_SECONDS)
    except TimeoutError:
        return "lookup timed out"
    except Exception as exc:
        return f"lookup failed: {describe_provider_failure(exc)}"
    return "not found" if found is None else f"found ({found.status.value})"


def _discrepancy_rows(reader: SqlAlchemyDiagnostics, limit: int) -> dict[str, Any]:
    rows = []
    for stored in reader.discrepancies(limit):
        local = _rebuild(stored.entity_type, stored.local)
        broker = _rebuild(stored.entity_type, stored.broker)
        try:
            changes = field_changes(
                Discrepancy(stored.entity_type, stored.entity_key, local, broker)
            )
        except Exception:
            # An odd stored row is shown as stored; it must not hide the rows after it.
            changes = (FieldChange("value", str(local), str(broker), None),)
        rows.append(
            {
                "at": _utc(stored.created_at).isoformat(),
                "kind": stored.entity_type,
                "key": _key(stored.entity_type, stored.entity_key),
                "safety_action": stored.safety_action,
                "fields": [
                    {
                        "field": change.field,
                        "local": change.local,
                        "broker": change.broker,
                        "delta": None if change.delta is None else format(change.delta, "+f"),
                    }
                    for change in changes
                ],
            }
        )
    return {"rows": rows}


def _fill_rows(
    reader: SqlAlchemyDiagnostics, limit: int, increments: Mapping[str, Decimal]
) -> dict[str, Any]:
    fills = reader.fills(limit)
    try:
        checks = _checks(reader, fills, increments)
    except (TypeError, ValueError):
        checks = [
            {"fills": [], "status": "the balance check could not be computed from stored rows"}
        ]
    return {
        "rows": [
            {
                "at": _utc(fill.occurred_at).isoformat(),
                "ref": fill_reference(fill.fill_id),
                "order": short_reference(fill.order_id),
                "symbol": fill.symbol,
                "side": fill.side.value,
                "quantity": _plain(fill.quantity),
                "price": _plain(fill.price),
                "notional": _plain(fill.quantity * fill.price),
                "fee": _plain(fill.fee),
            }
            for fill in fills
        ],
        "checks": checks,
    }


def _checks(
    reader: SqlAlchemyDiagnostics, fills: Sequence[Fill], increments: Mapping[str, Decimal]
) -> list[dict[str, Any]]:
    """Place each fill in the reconciliation window that followed it, and itemize that window.

    A window runs from one stored broker snapshot to the next. Its fills are placed by their
    recorded time, so this is a reconstruction from stored data, not a replay of the run.
    """

    if not fills:
        return []
    earliest = min(fill.occurred_at for fill in fills)
    previous, later = reader.snapshot_timeline(earliest)
    timeline = ([previous] if previous else []) + list(later)
    windows: dict[int | None, list[Fill]] = {}
    for fill in fills:
        end = next(
            (index for index, ref in enumerate(timeline) if ref.recorded_at >= fill.occurred_at),
            None,
        )
        windows.setdefault(end, []).append(fill)

    checks: list[dict[str, Any]] = []
    for end in sorted(windows, key=lambda index: -1 if index is None else index):
        members = windows[end]
        refs = [fill_reference(fill.fill_id) for fill in members]
        if end is None:
            checks.append({"fills": refs, "status": "not yet reconciled"})
        elif end == 0:
            checks.append({"fills": refs, "status": "no earlier broker snapshot is stored"})
        else:
            checks.append(
                _checked_window(reader, timeline[end - 1], timeline[end], members, refs, increments)
            )
    return checks


def _checked_window(
    reader: SqlAlchemyDiagnostics,
    start: SnapshotRef,
    end: SnapshotRef,
    members: Sequence[Fill],
    refs: list[str],
    increments: Mapping[str, Decimal],
) -> dict[str, Any]:
    window = {
        "fills": refs,
        "from": _utc(start.recorded_at).isoformat(),
        "to": _utc(end.recorded_at).isoformat(),
    }
    try:
        explained = explain_window(
            PortfolioState(balances=reader.balances(start.batch_id)),
            PortfolioState(balances=reader.balances(end.batch_id)),
            members,
            balance_increments=increments,
        )
    except ValueError:
        return {**window, "status": "the projection went negative; it cannot be itemized"}
    except DiagnosticsUnavailable as exc:
        return {**window, "status": str(exc)}
    return {**window, "status": "itemized", "assets": [_asset(item) for item in explained]}


def _asset(item: AssetExplanation) -> dict[str, Any]:
    return {
        "asset": item.asset,
        "before": _plain(item.before),
        "items": [{"label": entry.label, "amount": _signed(entry.amount)} for entry in item.items],
        "exact": _plain(item.exact),
        "rounding": _signed(item.rounding),
        "projected": _plain(item.projected),
        "broker": _plain(item.broker),
        "difference": _signed(item.difference),
    }


def _rebuild(kind: str, payload: Mapping[str, Any]) -> object:
    """A stored side as the object reconciliation compared, or its text when it cannot be."""

    if not payload:
        return None
    try:
        if kind == "balance":
            return Balance.model_validate(dict(payload))
        if kind == "position":
            return Position.model_validate(dict(payload))
        if kind == "fill":
            return Fill.model_validate(dict(payload))
    except ValueError:
        pass
    # An order side is stored as the text of its (status, filled quantity) pair.
    return str(payload.get("value", dict(payload)))


def _key(kind: str, key: str) -> str:
    if kind == "order":
        return short_reference(key)
    return fill_reference(key) if kind == "fill" else key


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _plain(value: Decimal) -> str:
    """A decimal without an exponent or trailing zeros: ``0.000002``, ``100``."""

    return format(value.normalize(), "f") if value else "0"


def _signed(value: Decimal) -> str:
    return "0" if not value else format(value.normalize(), "+f")


def render_report(report: Mapping[str, Any]) -> list[str]:
    """The report as plain lines, from the same structure the JSON carries."""

    recovery, reconciliation = report["recovery"], report["reconciliation"]
    lines = [
        f"diagnose mode={report['mode']} credential_scope={report['credential_scope']}"
        f" at={report['generated_at']}",
        f"kill_switch={report['kill_switch']}",
        f"recovery={recovery.get('status')}",
        f"reconciliation last_result={reconciliation.get('last_result')}"
        f" last_discrepancies={reconciliation.get('last_discrepancies', 'n/a')}",
    ]
    lines += _pending_lines(report["pending_orders"])
    lines += _discrepancy_lines(report["discrepancies"], report["limit"])
    lines += _fill_lines(report["fills"], report["limit"])
    return lines


def _unreadable(name: str, section: Mapping[str, Any]) -> list[str]:
    return [f"{name}: unavailable ({section.get('reason', 'not readable')})"]


def _pending_lines(section: Mapping[str, Any]) -> list[str]:
    if not section["readable"]:
        return _unreadable("pending_orders", section)
    lines = [f"pending_orders={len(section['orders'])}"]
    for order in section["orders"]:
        lines.append(
            f"  order {order['ref']} {order['status']} {order['side']} {order['quantity']}"
            f" {order['symbol']} age={order['age_seconds']}s broker={order['broker']}"
        )
    return lines


def _discrepancy_lines(section: Mapping[str, Any], limit: int) -> list[str]:
    if not section["readable"]:
        return _unreadable("discrepancies", section)
    lines = [f"discrepancies shown={len(section['rows'])} (newest first, up to {limit})"]
    for row in section["rows"]:
        lines.append(f"  {row['at']} {row['kind']} {row['key']} safety={row['safety_action']}")
        for change in row["fields"]:
            delta = f" delta(broker-local)={change['delta']}" if change["delta"] else ""
            lines.append(
                f"    {change['field']} local={change['local']} broker={change['broker']}{delta}"
            )
    return lines


def _fill_lines(section: Mapping[str, Any], limit: int) -> list[str]:
    if not section["readable"]:
        return _unreadable("fills", section)
    lines = [f"fills shown={len(section['rows'])} (newest first, up to {limit})"]
    for row in section["rows"]:
        lines.append(
            f"  {row['at']} fill {row['ref']} order {row['order']} {row['side']} {row['symbol']}"
            f" quantity={row['quantity']} price={row['price']} notional={row['notional']}"
            f" fee={row['fee']}"
        )
    if section["checks"]:
        lines.append("balance check: projected against broker, from stored snapshots")
    for check in section["checks"]:
        span = f" {check['from']} to {check['to']}" if "from" in check else ""
        lines.append(f"  window{span} fills={','.join(check['fills'])}: {check['status']}")
        for asset in check.get("assets", ()):
            lines += _asset_lines(asset)
    return lines


def _asset_lines(asset: Mapping[str, Any]) -> list[str]:
    lines = [f"    {asset['asset']} available", f"      before {asset['before']}"]
    lines += [f"      {item['label']} {item['amount']}" for item in asset["items"]]
    lines += [
        f"      exact {asset['exact']}",
        f"      rounding to the venue's unit {asset['rounding']}",
        f"      projected {asset['projected']}",
        f"      broker {asset['broker']}",
        f"      difference (broker - projected) {asset['difference']}",
    ]
    return lines
