"""Signal and risk-decision audit records, and the lineage check every order must pass.

Each order must link to the signal that produced it, that signal's strategy
version, the risk decision it cites, and its fills. The trading cycle writes the
signal and the decision before it reserves the order, so a failed audit write
stops trading before any broker call.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

from core.models import Order, OrderStatus, RiskApproval, Signal
from db.models import FillRecord, OrderRecord, RiskDecisionRecord, SignalRecord
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from execution.engine import InMemoryOrderStore, PersistenceUnavailable

FILLED_STATUSES = frozenset({OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED})

# The ``system_events`` type written when an administrator closes an order the venue never
# received. The order itself ends ``canceled``, which is also what a broker cancel leaves, so
# this event is what tells the two apart.
ORDER_CLOSED_EVENT = "order_closed_never_received"
NEVER_RECEIVED = "never received by the venue"


@dataclass(frozen=True, slots=True)
class OrderClosureRecord:
    """Who closed a never-received order, when, why, and what the venue said at that moment.

    Carries identifiers and the broker's answer, never a request or a provider response.
    """

    order: Order
    previous_status: OrderStatus
    actor: str
    reason: str
    closed_at: datetime
    broker_lookup: str

    def to_payload(self) -> Mapping[str, Any]:
        """The ``system_events`` payload. The order store adds the risk decision's id."""

        request = self.order.request
        return {
            "client_order_id": str(request.client_order_id),
            "order_id": str(self.order.order_id),
            "signal_id": str(request.signal_id),
            "strategy_version": request.strategy_version,
            "symbol": request.symbol,
            "side": request.side.value,
            # The database column keeps 18 decimals; the event reads as the order was placed.
            "quantity": format(request.quantity.normalize(), "f"),
            "previous_status": self.previous_status.value,
            "new_status": OrderStatus.CANCELED.value,
            "actor": self.actor,
            "reason": self.reason,
            "closed_at": self.closed_at.isoformat(),
            "broker_lookup": self.broker_lookup,
            "broker_fills": 0,
            "outcome": NEVER_RECEIVED,
        }


@dataclass(frozen=True, slots=True)
class OrderLineage:
    client_order_id: str
    status: OrderStatus
    signal_recorded: bool
    strategy_version_matches: bool
    risk_decision_recorded: bool
    risk_decision_approved: bool
    fill_count: int
    # What the venue reported executed. The orders table does not store it, so the
    # database check relies on ExecutionEngine, which never settles an order whose
    # fills fall short of it.
    filled_quantity: Decimal = Decimal("0")

    @property
    def gaps(self) -> tuple[str, ...]:
        gaps: list[str] = []
        if not self.signal_recorded:
            gaps.append("signal")
        elif not self.strategy_version_matches:
            gaps.append("strategy_version")
        if not self.risk_decision_recorded:
            gaps.append("risk_decision")
        elif not self.risk_decision_approved:
            gaps.append("risk_decision_not_approved")
        # A canceled or expired order can still have executed in part.
        executed = self.status in FILLED_STATUSES or self.filled_quantity > 0
        if executed and self.fill_count == 0:
            gaps.append("fills")
        return tuple(gaps)


class AuditStore(Protocol):
    def record_signal(self, signal: Signal) -> None: ...

    def record_risk_decision(self, decision: RiskApproval) -> None: ...

    def lineage(self) -> tuple[OrderLineage, ...]: ...


def unlinked_orders(store: AuditStore) -> tuple[OrderLineage, ...]:
    """Orders missing a signal, strategy version, approved risk decision, or fills."""

    return tuple(item for item in store.lineage() if item.gaps)


@dataclass
class InMemoryAuditStore:
    """Audit records for replay and tests, read against an in-memory order store."""

    orders: InMemoryOrderStore
    fail_writes: bool = False
    signals: dict[UUID, Signal] = field(default_factory=dict)
    decisions: dict[UUID, RiskApproval] = field(default_factory=dict)

    def record_signal(self, signal: Signal) -> None:
        if self.fail_writes:
            raise PersistenceUnavailable("signal audit record is unavailable")
        self.signals.setdefault(signal.signal_id, signal)

    def record_risk_decision(self, decision: RiskApproval) -> None:
        if self.fail_writes:
            raise PersistenceUnavailable("risk decision audit record is unavailable")
        self.decisions.setdefault(decision.approval_id, decision)

    def lineage(self) -> tuple[OrderLineage, ...]:
        result = []
        for key, order in self.orders.orders.items():
            signal = self.signals.get(order.request.signal_id)
            approval = self.orders.approvals.get(key)
            decision = self.decisions.get(approval.approval_id) if approval else None
            result.append(
                OrderLineage(
                    client_order_id=key,
                    status=order.status,
                    signal_recorded=signal is not None,
                    strategy_version_matches=(
                        signal is not None
                        and signal.strategy_version == order.request.strategy_version
                    ),
                    risk_decision_recorded=decision is not None,
                    risk_decision_approved=decision is not None and decision.approved,
                    fill_count=_fill_count(self.orders, order),
                    filled_quantity=order.filled_quantity,
                )
            )
        return tuple(result)


def _fill_count(store: InMemoryOrderStore, order: Order) -> int:
    local_id = order.request.client_order_id
    return sum(1 for fill in store.fills.values() if fill.order_id == local_id)


class SqlAlchemyAuditStore:
    def __init__(self, session_factory) -> None:
        self.session_factory = session_factory

    def record_signal(self, signal: Signal) -> None:
        try:
            with self.session_factory() as session:
                if session.get(SignalRecord, signal.signal_id) is None:
                    session.add(
                        SignalRecord(
                            signal_id=signal.signal_id,
                            symbol=signal.symbol,
                            strategy_version=signal.strategy_version,
                            side=signal.side.value,
                            quantity=signal.quantity,
                            created_at=signal.created_at,
                        )
                    )
                    session.commit()
        except SQLAlchemyError as exc:
            raise PersistenceUnavailable("database unavailable while recording signal") from exc

    def record_risk_decision(self, decision: RiskApproval) -> None:
        try:
            with self.session_factory() as session:
                if session.get(RiskDecisionRecord, decision.approval_id) is None:
                    session.add(
                        RiskDecisionRecord(
                            approval_id=decision.approval_id,
                            signal_id=decision.signal_id,
                            approved=decision.approved,
                            reason=decision.reason,
                            failed_gate=decision.failed_gate,
                            correlation_id=decision.correlation_id,
                            decided_at=decision.approved_at.astimezone(UTC),
                        )
                    )
                    session.commit()
        except SQLAlchemyError as exc:
            raise PersistenceUnavailable(
                "database unavailable while recording risk decision"
            ) from exc

    def lineage(self) -> tuple[OrderLineage, ...]:
        fill_counts = (
            select(FillRecord.order_id, func.count().label("fills"))
            .group_by(FillRecord.order_id)
            .subquery()
        )
        query = (
            select(
                OrderRecord.client_order_id,
                OrderRecord.status,
                OrderRecord.strategy_version,
                SignalRecord.strategy_version,
                RiskDecisionRecord.approval_id,
                RiskDecisionRecord.approved,
                fill_counts.c.fills,
            )
            .outerjoin(SignalRecord, SignalRecord.signal_id == OrderRecord.signal_id)
            .outerjoin(
                RiskDecisionRecord, RiskDecisionRecord.approval_id == OrderRecord.risk_approval_id
            )
            .outerjoin(fill_counts, fill_counts.c.order_id == OrderRecord.order_id)
        )
        try:
            with self.session_factory() as session:
                rows = session.execute(query).all()
        except SQLAlchemyError as exc:
            raise PersistenceUnavailable("database unavailable while reading lineage") from exc
        return tuple(
            OrderLineage(
                client_order_id=str(client_order_id),
                status=OrderStatus(status),
                signal_recorded=signal_version is not None,
                strategy_version_matches=signal_version == order_version,
                risk_decision_recorded=approval_id is not None,
                risk_decision_approved=bool(approved),
                fill_count=int(fills or 0),
            )
            for (
                client_order_id,
                status,
                order_version,
                signal_version,
                approval_id,
                approved,
                fills,
            ) in rows
        )
