"""Persistence-first execution and ambiguity recovery."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

from brokers.http import provider_failure_fields
from brokers.interface import BrokerInterface
from core.logging import fill_reference, short_reference
from core.models import Fill, Order, OrderRequest, OrderStatus, RiskApproval, utc_now

logger = logging.getLogger(__name__)

# Fills already logged, remembered so a re-read of an open order does not log them again.
LOGGED_FILL_MEMORY = 10_000


class PersistenceUnavailable(RuntimeError):
    """Raised when the pre-submit audit record cannot be written."""


class OrderStore(Protocol):
    def reserve(self, request: OrderRequest, approval: RiskApproval) -> Order: ...

    def reserve_with_created(
        self, request: OrderRequest, approval: RiskApproval
    ) -> tuple[Order, bool]: ...

    def update(self, order: Order) -> None: ...

    def add_fills(self, fills: tuple[Fill, ...]) -> None: ...

    def get(self, client_order_id: str) -> Order | None: ...

    def pending(self) -> tuple[Order, ...]: ...

    def close_unreceived(
        self, client_order_id: str, *, event_type: str, payload: Mapping[str, Any], at: datetime
    ) -> bool: ...


@dataclass
class InMemoryOrderStore:
    """Deterministic store used by tests and local paper/replay runs."""

    fail_writes: bool = False
    orders: dict[str, Order] = field(default_factory=dict)
    fills: dict[str, Fill] = field(default_factory=dict)
    approvals: dict[str, RiskApproval] = field(default_factory=dict)
    # The audit events written by ``close_unreceived``, as (event type, payload).
    closures: list[tuple[str, Mapping[str, Any]]] = field(default_factory=list)

    def reserve(self, request: OrderRequest, approval: RiskApproval) -> Order:
        return self.reserve_with_created(request, approval)[0]

    def reserve_with_created(
        self, request: OrderRequest, approval: RiskApproval
    ) -> tuple[Order, bool]:
        if self.fail_writes:
            raise PersistenceUnavailable("order pre-submit persistence is unavailable")
        key = str(request.client_order_id)
        existing = self.orders.get(key)
        if existing is not None:
            if existing.request != request:
                raise ValueError("client_order_id is already bound to a different order request")
            return existing, False
        assert request.client_order_id is not None
        order = Order(order_id=request.client_order_id, request=request)
        self.orders[key] = order
        self.approvals[key] = approval
        return order, True

    def update(self, order: Order) -> None:
        if self.fail_writes:
            raise PersistenceUnavailable("order update persistence is unavailable")
        self.orders[str(order.request.client_order_id)] = order

    def add_fills(self, fills: tuple[Fill, ...]) -> None:
        if self.fail_writes:
            raise PersistenceUnavailable("fill persistence is unavailable")
        for fill in fills:
            self.fills[fill.fill_id] = fill

    def get(self, client_order_id: str) -> Order | None:
        return self.orders.get(client_order_id)

    def pending(self) -> tuple[Order, ...]:
        return tuple(
            order
            for order in self.orders.values()
            if order.status in {OrderStatus.PENDING_SUBMIT, OrderStatus.UNKNOWN}
        )

    def close_unreceived(
        self, client_order_id: str, *, event_type: str, payload: Mapping[str, Any], at: datetime
    ) -> bool:
        """End a still-unresolved order as ``canceled`` and journal why, or change nothing.

        Returns ``False`` when the order is missing or already resolved, so a caller that
        looked the order up earlier cannot overwrite a status something else has since set.
        """

        if self.fail_writes:
            raise PersistenceUnavailable("order close persistence is unavailable")
        order = self.orders.get(client_order_id)
        if order is None or order.status not in {OrderStatus.PENDING_SUBMIT, OrderStatus.UNKNOWN}:
            return False
        self.orders[client_order_id] = order.model_copy(
            update={"status": OrderStatus.CANCELED, "updated_at": at}
        )
        self.closures.append((event_type, dict(payload)))
        return True


class ExecutionEngine:
    def __init__(
        self,
        broker: BrokerInterface,
        store: OrderStore | None = None,
        *,
        on_recorded: Callable[[Order, tuple[Fill, ...]], None] | None = None,
        on_order_closed: Callable[[Order, Exception], Awaitable[None]] | None = None,
    ) -> None:
        self.broker = broker
        self.store = store or InMemoryOrderStore()
        # Observers such as the scheduled reconciler see each persisted order and its fills.
        self.on_recorded = on_recorded
        self.on_order_closed = on_order_closed
        self._logged_fills: dict[str, None] = {}

    async def submit(self, request: OrderRequest, approval: RiskApproval) -> Order:
        if request.signal_id != approval.signal_id:
            raise ValueError("risk approval does not belong to order request")
        if not approval.approved:
            raise PermissionError("order is not approved by risk engine")

        persisted, created = self.store.reserve_with_created(request, approval)
        reference = short_reference(request.client_order_id)
        _log_step(
            "saved",
            reference,
            symbol=request.symbol,
            side=request.side.value,
            quantity=request.quantity,
            status=persisted.status.value,
        )
        if persisted.status not in {OrderStatus.UNKNOWN, OrderStatus.PENDING_SUBMIT}:
            return persisted
        # Pending or unknown: ask the venue by client_order_id before any (re)submission.
        try:
            existing = await self.broker.get_order(str(request.client_order_id))
        except Exception as exc:
            _log_step("lookup", reference, level=logging.WARNING, **_failure(exc))
            if created:
                await self._close_never_sent(persisted, exc)
            raise
        _log_step(
            "lookup",
            reference,
            result="found" if existing is not None else "not_found",
            status=existing.status.value if existing is not None else None,
        )
        if existing is not None:
            return await self._record(existing)
        try:
            result = await self.broker.submit_order(request, approval)
        except Exception as exc:
            provider_order = getattr(exc, "order", None)
            _log_step(
                "order_new",
                reference,
                level=logging.WARNING,
                status=provider_order.status.value if isinstance(provider_order, Order) else None,
                **_failure(exc),
            )
            if isinstance(provider_order, Order):
                self.store.update(provider_order)
            raise
        _log_step(
            "order_new",
            reference,
            result="accepted",
            status=result.status.value,
            filled_quantity=result.filled_quantity,
        )
        return await self._record(result)

    async def _close_never_sent(self, order: Order, failure: Exception) -> None:
        """Audit-close a row created by this call when its pre-submit lookup fails.

        ``submit_order`` has not run, so this new row cannot represent an ambiguous venue write.
        Existing rows are deliberately left unresolved for normal recovery and operator review.
        """

        from brokers.http import describe_provider_failure

        from execution.audit import ORDER_CLOSED_EVENT, OrderClosureRecord

        reason = f"Pre-submit order-status lookup failed: {describe_provider_failure(failure)}"
        closed_at = utc_now()
        record = OrderClosureRecord(
            order=order,
            previous_status=order.status,
            actor="system",
            reason=reason,
            closed_at=closed_at,
            broker_lookup=f"failed ({type(failure).__name__})",
        )
        close = getattr(self.store, "close_unreceived", None)
        if close is None:
            raise PersistenceUnavailable("order store cannot audit-close a never-submitted order")
        if not close(
            str(order.request.client_order_id),
            event_type=ORDER_CLOSED_EVENT,
            payload=record.to_payload(),
            at=closed_at,
        ):
            raise PersistenceUnavailable("new pending order could not be audit-closed")
        logger.warning(
            "order closed as never sent ref=%s lookup_failure=%s",
            short_reference(order.request.client_order_id),
            type(failure).__name__,
            extra={
                "event": {
                    "step": "close",
                    "ref": short_reference(order.request.client_order_id),
                    "result": "never_sent",
                    "lookup_failure": type(failure).__name__,
                }
            },
        )
        if self.on_order_closed is not None:
            try:
                await self.on_order_closed(order, failure)
            except Exception:
                logger.exception("never-sent order alert could not be delivered")

    async def recover(self, client_order_id: str) -> Order | None:
        existing = await self.broker.get_order(client_order_id)
        if existing is None:
            return self.store.get(client_order_id)
        return await self._record(existing)

    async def recover_pending(self) -> tuple[Order, ...]:
        recovered: list[Order] = []
        for order in self.store.pending():
            result = await self.recover(str(order.request.client_order_id))
            if result is not None:
                recovered.append(result)
        return tuple(recovered)

    async def _record(self, order: Order) -> Order:
        fills = await self.broker.get_fills(str(order.request.client_order_id))
        linked = _linked_to_local_order(order, fills)
        if linked:
            self.store.add_fills(linked)
            self._log_new_fills(order, linked)
        # Keep the durable order recoverable until every authoritative fill has
        # been read and persisted. If either step fails, or the venue reports more
        # executed than its fills cover, the existing row keeps its status, so a
        # PENDING_SUBMIT/UNKNOWN order stays eligible for recovery and no order
        # settles without its fills.
        recorded = sum((fill.quantity for fill in linked), Decimal("0"))
        if recorded >= order.filled_quantity:
            self.store.update(order)
        else:
            _log_step(
                "fill",
                short_reference(order.request.client_order_id),
                level=logging.WARNING,
                result="pending",
                filled_quantity=order.filled_quantity,
                recorded_quantity=recorded,
            )
        if self.on_recorded is not None:
            self.on_recorded(order, linked)
        return order

    def _log_new_fills(self, order: Order, fills: tuple[Fill, ...]) -> None:
        """Log each fill's quantity, price, fee, and notional the first time it is recorded."""

        reference = short_reference(order.request.client_order_id)
        for fill in fills:
            if fill.fill_id in self._logged_fills:
                continue
            self._logged_fills[fill.fill_id] = None
            if len(self._logged_fills) > LOGGED_FILL_MEMORY:
                self._logged_fills.pop(next(iter(self._logged_fills)))
            _log_step(
                "fill",
                reference,
                fill=fill_reference(fill.fill_id),
                symbol=fill.symbol,
                side=fill.side.value,
                quantity=fill.quantity,
                price=fill.price,
                fee=fill.fee,
                fee_asset=fill.fee_asset,
                notional=fill.quantity * fill.price,
            )


def _failure(exc: Exception) -> dict[str, object]:
    """A failed step's fields: the exception's kind and, for HTTP, its status, path, and reason."""

    return {"result": "failed", **provider_failure_fields(exc)}


def _log_step(step: str, reference: str, *, level: int = logging.INFO, **fields: object) -> None:
    """One line per order step: ``order step=<step> ref=<8 characters> key=value ...``.

    A ``None`` field is left out and a decimal is written in full, never in scientific form.
    """

    shown = {
        key: format(value, "f") if isinstance(value, Decimal) else value
        for key, value in fields.items()
        if value is not None
    }
    detail = " ".join(f"{key}={value}" for key, value in shown.items())
    logger.log(
        level,
        "order step=%s ref=%s %s",
        step,
        reference,
        detail,
        extra={"event": {"step": step, "ref": reference, **shown}},
    )


def _linked_to_local_order(order: Order, fills: tuple[Fill, ...]) -> tuple[Fill, ...]:
    """Link fills to the persisted order, which is keyed by its client_order_id.

    Provider adapters label fills with the venue's own order ID; persisting that
    would orphan the fills from the local order and break restart reconciliation.
    """

    local_id = order.request.client_order_id
    assert local_id is not None
    return tuple(
        fill if fill.order_id == local_id else fill.model_copy(update={"order_id": local_id})
        for fill in fills
    )


async def submit_approved_order(
    broker: BrokerInterface, request: OrderRequest, approval: RiskApproval
) -> Order:
    """Compatibility entry point that still enforces RiskApproval-only execution."""

    return await ExecutionEngine(broker).submit(request, approval)
