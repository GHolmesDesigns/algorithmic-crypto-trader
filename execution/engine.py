"""Persistence-first execution and ambiguity recovery."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from brokers.interface import BrokerInterface
from core.models import Fill, Order, OrderRequest, OrderStatus, RiskApproval


class PersistenceUnavailable(RuntimeError):
    """Raised when the pre-submit audit record cannot be written."""


class OrderStore(Protocol):
    def reserve(self, request: OrderRequest, approval: RiskApproval) -> Order: ...

    def update(self, order: Order) -> None: ...

    def add_fills(self, fills: tuple[Fill, ...]) -> None: ...

    def get(self, client_order_id: str) -> Order | None: ...

    def pending(self) -> tuple[Order, ...]: ...


@dataclass
class InMemoryOrderStore:
    """Deterministic store used by tests and local paper/replay runs."""

    fail_writes: bool = False
    orders: dict[str, Order] = field(default_factory=dict)
    fills: dict[str, Fill] = field(default_factory=dict)
    approvals: dict[str, RiskApproval] = field(default_factory=dict)

    def reserve(self, request: OrderRequest, approval: RiskApproval) -> Order:
        if self.fail_writes:
            raise PersistenceUnavailable("order pre-submit persistence is unavailable")
        key = str(request.client_order_id)
        existing = self.orders.get(key)
        if existing is not None:
            if existing.request != request:
                raise ValueError("client_order_id is already bound to a different order request")
            return existing
        assert request.client_order_id is not None
        order = Order(order_id=request.client_order_id, request=request)
        self.orders[key] = order
        self.approvals[key] = approval
        return order

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


class ExecutionEngine:
    def __init__(
        self,
        broker: BrokerInterface,
        store: OrderStore | None = None,
        *,
        on_recorded: Callable[[Order, tuple[Fill, ...]], None] | None = None,
    ) -> None:
        self.broker = broker
        self.store = store or InMemoryOrderStore()
        # Observers such as the scheduled reconciler see each persisted order and its fills.
        self.on_recorded = on_recorded

    async def submit(self, request: OrderRequest, approval: RiskApproval) -> Order:
        if request.signal_id != approval.signal_id:
            raise ValueError("risk approval does not belong to order request")
        if not approval.approved:
            raise PermissionError("order is not approved by risk engine")

        persisted = self.store.reserve(request, approval)
        if persisted.status not in {OrderStatus.UNKNOWN, OrderStatus.PENDING_SUBMIT}:
            return persisted
        # Pending or unknown: ask the venue by client_order_id before any (re)submission.
        existing = await self.broker.get_order(str(request.client_order_id))
        if existing is not None:
            return await self._record(existing)
        try:
            result = await self.broker.submit_order(request, approval)
        except Exception as exc:
            provider_order = getattr(exc, "order", None)
            if isinstance(provider_order, Order):
                self.store.update(provider_order)
            raise
        return await self._record(result)

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
        self.store.update(order)
        fills = await self.broker.get_fills(str(order.request.client_order_id))
        linked = _linked_to_local_order(order, fills)
        if linked:
            self.store.add_fills(linked)
        if self.on_recorded is not None:
            self.on_recorded(order, linked)
        return order


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
