"""Restart recovery that runs before the service accepts operator or trading work."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from brokers.interface import BrokerInterface
from core.models import Fill, Order, utc_now
from execution.engine import ExecutionEngine, OrderStore
from portfolio.reconciliation import (
    Discrepancy,
    PortfolioState,
    Reconciler,
    ReconciliationUnavailable,
)
from portfolio.store import PortfolioStore
from risk.kill_switch import KillSwitch

logger = logging.getLogger(__name__)


class RecoverableOrderStore(OrderStore, Protocol):
    def open_orders(self) -> tuple[Order, ...]: ...

    def fills_for(self, orders: tuple[Order, ...]) -> tuple[Fill, ...]: ...


@dataclass(frozen=True, slots=True)
class StartupRecoveryResult:
    status: str
    detail: str
    pending_orders: int = 0
    recovered_orders: int = 0
    discrepancies: int = 0
    completed_at: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "detail": self.detail,
            "pending_orders": self.pending_orders,
            "recovered_orders": self.recovered_orders,
            "discrepancies": self.discrepancies,
            "completed_at": self.completed_at,
        }


async def recover_on_startup(
    *,
    kill_switch: KillSwitch,
    order_store: RecoverableOrderStore,
    portfolio_store: PortfolioStore,
    broker: BrokerInterface | None,
    alert: Callable[[Discrepancy], None] | None = None,
) -> StartupRecoveryResult:
    """Resolve persisted orders and reconcile against the broker before new entries.

    Every failure path leaves the kill switch HALTED; only an operator can re-arm it.
    """

    try:
        pending = order_store.pending()
    except Exception:
        return _halt(kill_switch, "persisted orders could not be read during startup recovery")

    if broker is None:
        if pending:
            return _halt(
                kill_switch,
                "persisted pending orders cannot be resolved without a configured broker",
                pending_orders=len(pending),
            )
        return _result(
            "no_broker",
            "no broker is configured and no orders are pending; reconciliation not performed",
        )

    try:
        recovered = await ExecutionEngine(broker, order_store).recover_pending()
        unresolved = order_store.pending()
    except Exception:
        return _halt(
            kill_switch,
            "pending orders could not be queried by client_order_id during startup recovery",
            pending_orders=len(pending),
        )
    if unresolved:
        return _halt(
            kill_switch,
            "broker has no record of persisted pending orders; operator review required",
            pending_orders=len(pending),
            recovered_orders=len(recovered),
        )

    try:
        baseline = portfolio_store.latest_state(source="broker")
        orders = order_store.open_orders()
        fills = order_store.fills_for(orders)
    except Exception:
        return _halt(
            kill_switch,
            "persisted portfolio state could not be loaded during startup recovery",
            pending_orders=len(pending),
            recovered_orders=len(recovered),
        )
    local = PortfolioState(
        orders={str(order.request.client_order_id): order for order in orders},
        fills={fill.fill_id: fill for fill in fills},
        positions=baseline.positions if baseline is not None else (),
        balances=baseline.balances if baseline is not None else (),
    )
    reconciler = Reconciler(broker, kill_switch, alert, store=portfolio_store)
    try:
        reconciliation = await reconciler.reconcile(local)
    except ReconciliationUnavailable:
        return _halt(
            kill_switch,
            "broker reconciliation was unavailable during startup recovery",
            pending_orders=len(pending),
            recovered_orders=len(recovered),
        )
    if reconciliation.safety_tripped:
        detail = "broker-authoritative reconciliation diverged from persisted state"
        if baseline is None:
            detail += "; no persisted portfolio baseline existed"
        return _halt(
            kill_switch,
            detail,
            pending_orders=len(pending),
            recovered_orders=len(recovered),
            discrepancies=len(reconciliation.discrepancies),
        )
    return _result(
        "reconciled",
        "persisted orders and portfolio state match the broker",
        pending_orders=len(pending),
        recovered_orders=len(recovered),
    )


def _halt(kill_switch: KillSwitch, reason: str, **counts: int) -> StartupRecoveryResult:
    kill_switch.trip(f"startup recovery: {reason}")
    logger.error("startup recovery halted trading: %s", reason)
    return _result("halted", reason, **counts)


def _result(status: str, detail: str, **counts: int) -> StartupRecoveryResult:
    return StartupRecoveryResult(
        status=status, detail=detail, completed_at=utc_now().isoformat(), **counts
    )
