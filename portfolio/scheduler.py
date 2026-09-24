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
    """

    def __init__(
        self,
        reconciler: Reconciler,
        *,
        baseline: PortfolioState,
        interval_seconds: float,
        on_divergence: DivergenceHook | None = None,
        on_unavailable: UnavailableHook | None = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("reconciliation interval must be positive")
        self.reconciler = reconciler
        self.interval_seconds = interval_seconds
        self.on_divergence = on_divergence
        self.on_unavailable = on_unavailable
        self._baseline = PortfolioState(positions=baseline.positions, balances=baseline.balances)
        self._orders: dict[str, Order] = {}
        self._fills: dict[str, Fill] = {}
        self._applied_fill_ids: set[str] = set()
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
        try:
            local = self.expected_state()
        except ValueError:
            self.reconciler.kill_switch.trip("expected portfolio could not be projected")
            await self._unavailable("expected portfolio could not be projected")
            return None
        try:
            result = await self.reconciler.reconcile(local)
        except ReconciliationUnavailable as exc:
            await self._unavailable(str(exc))
            return None
        self._adopt(result)
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

    def _adopt(self, result: ReconciliationResult) -> None:
        authoritative = result.authoritative
        self._baseline = PortfolioState(
            positions=authoritative.positions, balances=authoritative.balances
        )
        # The broker's balances already include every fill seen so far.
        self._applied_fill_ids.update(self._fills)
        self._applied_fill_ids.update(authoritative.fills)
        # Keep following open orders; settled ones and their fills leave the working set.
        open_orders = {
            key: order
            for key, order in authoritative.orders.items()
            if order.status not in TERMINAL
        }
        self._orders = open_orders
        self._fills = {
            key: fill
            for key, fill in authoritative.fills.items()
            if str(fill.order_id) in open_orders
            or any(order.order_id == fill.order_id for order in open_orders.values())
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
