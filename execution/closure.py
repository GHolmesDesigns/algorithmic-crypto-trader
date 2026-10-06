"""Close a pending order the venue never received, once the venue is proven to have no record.

An order can be saved as ``pending_submit`` and never reach the venue: the process stopped
between persisting and submitting it, or the pre-submit status lookup failed. The trading loop
and startup recovery both halt on such an order for operator review, and nothing else ends it.
This is that review's last step.

The venue is asked again at the moment of closing, by the persisted ``client_order_id``, then for
the order's fills. The order is closed only when the venue answers "no such order" and returns no
fill. A record of the order, any fill, or any failed lookup (a ``5xx``, a timeout, an
authentication error) refuses the close and changes nothing, because only a definite "not found"
is evidence of absence.

This module can only *read* from the venue: ``VenueLookup`` has no way to submit, cancel, or
edit. It never creates a replacement either. A closed order ends ``canceled``, so a later
``ExecutionEngine.submit`` of the same request returns it as settled instead of sending it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from brokers.http import describe_provider_failure, provider_failure_fields
from core.models import Fill, Order, OrderStatus, utc_now

from execution.audit import ORDER_CLOSED_EVENT, OrderClosureRecord
from execution.engine import PersistenceUnavailable

logger = logging.getLogger(__name__)

UNRESOLVED_STATUSES = frozenset({OrderStatus.PENDING_SUBMIT, OrderStatus.UNKNOWN})
LOOKUP_NOT_FOUND = "not found (the venue has no record of this order)"


class VenueLookup(Protocol):
    """The two reads a closure needs. A ``BrokerInterface`` satisfies it; nothing here writes."""

    async def get_order(self, client_order_id: str) -> Order | None: ...

    async def get_fills(self, client_order_id: str) -> tuple[Fill, ...]: ...


@runtime_checkable
class ClosableOrderStore(Protocol):
    def get(self, client_order_id: str) -> Order | None: ...

    def close_unreceived(
        self, client_order_id: str, *, event_type: str, payload: Mapping[str, Any], at: datetime
    ) -> bool: ...


class ClosureCode(StrEnum):
    CLOSED = "closed"
    INVALID_ID = "invalid_id"
    ORDER_NOT_FOUND = "order_not_found"
    NOT_UNRESOLVED = "not_unresolved"
    VENUE_HAS_ORDER = "venue_has_order"
    VENUE_HAS_FILLS = "venue_has_fills"
    LOOKUP_FAILED = "lookup_failed"
    NOT_RECORDED = "not_recorded"


@dataclass(frozen=True, slots=True)
class ClosureOutcome:
    """What a close attempt did. Everything but ``CLOSED`` changed nothing."""

    code: ClosureCode
    client_order_id: str
    detail: str
    # What the venue answered, in words an operator can read; "not asked" before the lookup.
    broker_lookup: str = "not asked"
    record: OrderClosureRecord | None = None

    @property
    def closed(self) -> bool:
        return self.code is ClosureCode.CLOSED


class OrderCloser:
    def __init__(
        self,
        venue: VenueLookup,
        store: ClosableOrderStore,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.venue = venue
        self.store = store
        self.clock = clock

    async def close(self, client_order_id: str, *, actor: str, reason: str) -> ClosureOutcome:
        """Close one order, or say why not. The caller holds the trading lock.

        ``reason`` must already be redacted: it is stored with the audit event as given.
        """

        try:
            UUID(client_order_id)
        except ValueError:
            return _refused(ClosureCode.INVALID_ID, client_order_id, "That is not an order ID.")

        try:
            order = self.store.get(client_order_id)
        except PersistenceUnavailable:
            return _refused(
                ClosureCode.NOT_RECORDED,
                client_order_id,
                "The order store could not be read, so nothing was changed.",
            )
        if order is None:
            return _refused(
                ClosureCode.ORDER_NOT_FOUND, client_order_id, "No such order is saved here."
            )
        if order.status not in UNRESOLVED_STATUSES:
            return _refused(
                ClosureCode.NOT_UNRESOLVED,
                client_order_id,
                f"The order is {order.status.value}. Only a pending_submit or unknown order "
                "can be closed.",
            )

        try:
            found = await self.venue.get_order(client_order_id)
        except Exception as exc:
            return _lookup_failed(client_order_id, "the order lookup", exc)
        if found is not None:
            return _refused(
                ClosureCode.VENUE_HAS_ORDER,
                client_order_id,
                f"The venue has a record of this order ({found.status.value}). It was received, "
                "so it is not closed here. Let startup recovery or reconciliation resolve it.",
                broker_lookup=f"found ({found.status.value})",
            )
        try:
            fills = await self.venue.get_fills(client_order_id)
        except Exception as exc:
            return _lookup_failed(client_order_id, "the fills lookup", exc)
        if fills:
            return _refused(
                ClosureCode.VENUE_HAS_FILLS,
                client_order_id,
                f"The venue reports {len(fills)} fill(s) for this order. It was received, so it "
                "is not closed here. Let startup recovery or reconciliation resolve it.",
                broker_lookup=f"not found, but {len(fills)} fill(s) reported",
            )

        record = OrderClosureRecord(
            order=order,
            previous_status=order.status,
            actor=actor,
            reason=reason,
            closed_at=self.clock(),
            broker_lookup=LOOKUP_NOT_FOUND,
        )
        try:
            closed = self.store.close_unreceived(
                client_order_id,
                event_type=ORDER_CLOSED_EVENT,
                payload=record.to_payload(),
                at=record.closed_at,
            )
        except PersistenceUnavailable:
            return _refused(
                ClosureCode.NOT_RECORDED,
                client_order_id,
                "The closure could not be recorded in the audit history, so it was not applied.",
                broker_lookup=LOOKUP_NOT_FOUND,
            )
        if not closed:
            return _refused(
                ClosureCode.NOT_UNRESOLVED,
                client_order_id,
                "The order was resolved by something else while the venue was being asked, so it "
                "was left as it is.",
                broker_lookup=LOOKUP_NOT_FOUND,
            )
        return ClosureOutcome(
            ClosureCode.CLOSED,
            client_order_id,
            "The venue has no record of this order and no fills. It is closed, never "
            "resubmitted, and no replacement was created.",
            broker_lookup=LOOKUP_NOT_FOUND,
            record=record,
        )


def _refused(
    code: ClosureCode, client_order_id: str, detail: str, *, broker_lookup: str = "not asked"
) -> ClosureOutcome:
    return ClosureOutcome(code, client_order_id, detail, broker_lookup)


def _lookup_failed(client_order_id: str, what: str, exc: Exception) -> ClosureOutcome:
    """A failed lookup is not an answer. Name its kind, never its message: that can carry a URL."""

    status = getattr(exc, "status_code", None)
    failure = f"HTTP {status}" if isinstance(status, int) else type(exc).__name__
    # The page and the audit text above name only the kind; the log adds the endpoint and reason.
    logger.warning(
        "order close refused: %s failed (%s)",
        what,
        describe_provider_failure(exc),
        extra={"event": provider_failure_fields(exc)},
    )
    return ClosureOutcome(
        ClosureCode.LOOKUP_FAILED,
        client_order_id,
        f"{what[0].upper()}{what[1:]} failed ({failure}), so the venue has not said the order is "
        "absent. Only a definite not-found answer allows a close. Try again when the venue "
        "responds.",
        broker_lookup=f"failed ({failure})",
    )
