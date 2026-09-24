"""Unattended, interval-driven reconciliation against the broker."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime

from core.models import Fill, Order, OrderStatus, utc_now

from portfolio.ledger import apply_fills
from portfolio.reconciliation import (
    Discrepancy,
    PortfolioState,
    Reconciler,
    ReconciliationResult,
    ReconciliationUnavailable,
)

logger = logging.getLogger(__name__)

TERMINAL = frozenset({OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED})

DivergenceHook = Callable[[tuple[Discrepancy, ...]], Awaitable[None]]
UnavailableHook = Callable[[str], Awaitable[None]]
# Re-reads one order by client_order_id; ExecutionEngine.recover also persists it.
OrderRefresher = Callable[[str], Awaitable[object]]


@dataclass(frozen=True, slots=True)
class ReconciliationStatus:
    started_at: datetime
    runs: int = 0
    clean_runs: int = 0
    diverged_runs: int = 0
    unavailable_runs: int = 0
    last_run_at: datetime | None = None
    last_result: str = "not_run"
    last_discrepancies: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "started_at": self.started_at.isoformat(),
            "runs": self.runs,
            "clean_runs": self.clean_runs,
            "diverged_runs": self.diverged_runs,
            "unavailable_runs": self.unavailable_runs,
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "last_result": self.last_result,
            "last_discrepancies": self.last_discrepancies,
        }


class ScheduledReconciler:
    """Compare the expected portfolio with the broker on every interval.

    The expected portfolio is the last broker-authoritative state plus the fills
    this process recorded since then. Each run adopts the broker's state as the
    new baseline, so the broker's record is the one that survives a correction.
    A divergence trips the kill switch (through ``Reconciler``), is persisted as a
    discrepancy, and is reported to ``on_divergence`` for operator alerts.

    Orders still open at the baseline, or seen since, are re-read before each
    comparison, so an order that filled between runs is progress rather than a
    divergence. Wire ``refresh_order`` to ``ExecutionEngine.recover`` so the refresh
    also persists the order's status and fills. The trading loop must hold
    ``lock`` while it trades: an order placed mid-comparison could not be
    attributed to either side of the broker's snapshot.
    """

    def __init__(
        self,
        reconciler: Reconciler,
        *,
        baseline: PortfolioState,
        interval_seconds: float,
        refresh_order: OrderRefresher | None = None,
        on_divergence: DivergenceHook | None = None,
        on_unavailable: UnavailableHook | None = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("reconciliation interval must be positive")
        self.reconciler = reconciler
        self.interval_seconds = interval_seconds
        self.refresh_order = refresh_order or self._read_order
        self.on_divergence = on_divergence
        self.on_unavailable = on_unavailable
        self.lock = asyncio.Lock()
        self._baseline = PortfolioState(positions=baseline.positions, balances=baseline.balances)
        # Orders open at the baseline stay tracked; their fills are already in its balances.
        self._orders: dict[str, Order] = {
            key: order for key, order in baseline.orders.items() if order.status not in TERMINAL
        }
        self._fills: dict[str, Fill] = dict(baseline.fills)
        self._applied_fill_ids: set[str] = set(baseline.fills)
        self.status = ReconciliationStatus(started_at=utc_now())

    def observe(self, order: Order, fills: tuple[Fill, ...]) -> None:
        """Record an order and its fills as the execution engine persists them."""

        self._orders[str(order.request.client_order_id)] = order
        for fill in fills:
            self._fills[fill.fill_id] = fill

    def expected_state(self) -> PortfolioState:
        new_fills = [fill for key, fill in self._fills.items() if key not in self._applied_fill_ids]
        projected = apply_fills(self._baseline, new_fills)
        return PortfolioState(
            orders=dict(self._orders),
            fills=dict(self._fills),
            positions=projected.positions,
            balances=projected.balances,
        )

    async def run_once(self) -> ReconciliationResult | None:
        async with self.lock:
            outcome = await self._compare()
        if isinstance(outcome, str):
            await self._unavailable(outcome)
            return None
        return await self._report(outcome)

    async def _compare(self) -> ReconciliationResult | str:
        """Refresh, project, and reconcile; a string explains why the run was unavailable."""

        try:
            await self._refresh_open_orders()
        except Exception:
            logger.exception("open orders could not be refreshed before reconciliation")
            self.reconciler.kill_switch.trip("open orders could not be refreshed")
            return "open orders could not be refreshed"
        try:
            local = self.expected_state()
        except ValueError:
            self.reconciler.kill_switch.trip("expected portfolio could not be projected")
            return "expected portfolio could not be projected"
        try:
            result = await self.reconciler.reconcile(local)
        except ReconciliationUnavailable as exc:
            return str(exc)
        self._adopt(result, local)
        return result

    async def _refresh_open_orders(self) -> None:
        open_ids = [key for key, order in self._orders.items() if order.status not in TERMINAL]
        for client_order_id in open_ids:
            await self.refresh_order(client_order_id)

    async def _read_order(self, client_order_id: str) -> None:
        broker = self.reconciler.broker
        order = await broker.get_order(client_order_id)
        if order is not None:
            self.observe(order, await broker.get_fills(client_order_id))

    async def _report(self, result: ReconciliationResult) -> ReconciliationResult:
        diverged = bool(result.discrepancies)
        self.status = replace(
            self.status,
            runs=self.status.runs + 1,
            clean_runs=self.status.clean_runs + (0 if diverged else 1),
            diverged_runs=self.status.diverged_runs + (1 if diverged else 0),
            last_run_at=utc_now(),
            last_result="diverged" if diverged else "clean",
            last_discrepancies=len(result.discrepancies),
        )
        if diverged:
            logger.error("scheduled reconciliation diverged: %s", len(result.discrepancies))
            if self.on_divergence is not None:
                await self.on_divergence(result.discrepancies)
        return result

    async def run(self, stop: asyncio.Event) -> None:
        """Reconcile every interval until ``stop`` is set; startup recovery ran first."""

        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.interval_seconds)
            except TimeoutError:
                try:
                    await self.run_once()
                except Exception:
                    # Unattended: an unexpected failure halts trading but keeps the schedule.
                    logger.exception("scheduled reconciliation failed unexpectedly")
                    self.reconciler.kill_switch.trip("scheduled reconciliation failed")

    def _adopt(self, result: ReconciliationResult, local: PortfolioState) -> None:
        authoritative = result.authoritative
        self._baseline = PortfolioState(
            positions=authoritative.positions, balances=authoritative.balances
        )
        # The broker's balances include every fill that was part of the comparison.
        self._applied_fill_ids.update(local.fills)
        self._applied_fill_ids.update(authoritative.fills)
        # Keep following open orders; settled ones and their fills leave the working set.
        open_orders = {
            key: order
            for key, order in authoritative.orders.items()
            if order.status not in TERMINAL
        }
        # Broker fills carry the venue's order ID; locally recorded ones the client ID.
        open_ids = set(open_orders) | {str(order.order_id) for order in open_orders.values()}
        self._orders = open_orders
        self._fills = {
            key: fill for key, fill in authoritative.fills.items() if str(fill.order_id) in open_ids
        }

    async def _unavailable(self, detail: str) -> None:
        self.status = replace(
            self.status,
            runs=self.status.runs + 1,
            unavailable_runs=self.status.unavailable_runs + 1,
            last_run_at=utc_now(),
            last_result="unavailable",
            last_discrepancies=0,
        )
        logger.error("scheduled reconciliation unavailable: %s", detail)
        if self.on_unavailable is not None:
            await self.on_unavailable(detail)
