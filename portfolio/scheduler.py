"""Unattended, interval-driven reconciliation against the broker."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal

from brokers.http import (
    describe_provider_failure,
    is_transient_provider_failure,
    provider_failure_fields,
)
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
ReconciledHook = Callable[[PortfolioState], Awaitable[object]]
# Re-reads one order by client_order_id; ExecutionEngine.recover also persists it.
OrderRefresher = Callable[[str], Awaitable[object]]


@dataclass(frozen=True, slots=True)
class ReconciliationStatus:
    started_at: datetime
    runs: int = 0
    clean_runs: int = 0
    diverged_runs: int = 0
    unavailable_runs: int = 0
    # Unavailable runs since the broker was last read; any run that compared state resets it.
    consecutive_unavailable_runs: int = 0
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
            "consecutive_unavailable_runs": self.consecutive_unavailable_runs,
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "last_result": self.last_result,
            "last_discrepancies": self.last_discrepancies,
        }


@dataclass(frozen=True, slots=True)
class _Unreadable:
    """A run that could not read the broker; ``transient`` says retrying may fix it."""

    detail: str
    transient: bool = False


class ScheduledReconciler:
    """Compare the expected portfolio with the broker on every interval.

    The expected portfolio is the last broker-authoritative state plus the fills
    this process recorded since then. Each run adopts the broker's state as the
    new baseline, so the broker's record is the one that survives a correction.
    A divergence trips the kill switch (through ``Reconciler``), is persisted as a
    discrepancy, and is reported to ``on_divergence`` for operator alerts.

    Open orders and filled orders with missing recorded quantity are re-read before
    each comparison. Wire ``refresh_order`` to ``ExecutionEngine.recover`` so the
    refresh also persists the order's status and fills. The trading loop must hold
    ``lock`` while it trades: an order placed mid-comparison could not be
    attributed to either side of the broker's snapshot.

    ``balance_increments`` is the broker's declared balance precision
    (``BrokerCapabilities.balance_increments``); without it the projection is compared at
    full precision, which is right for a venue that reports balances exactly.

    ``unavailable_threshold`` is how many runs in a row may fail to read the broker, in a
    way that may clear by itself (a timeout, a dropped connection, a maintenance window),
    before trading halts. Each such run is still logged and alerted, and while the broker is
    unreadable the trading cycle's own risk inputs fail closed, so no new entry is opened.
    A failure that retrying cannot fix, such as a rejected credential or a response of an
    unexpected shape, halts on the first run, and so does a divergence. The default, 1, halts
    on the first unreadable run.
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
        on_reconciled: ReconciledHook | None = None,
        balance_increments: Mapping[str, Decimal] | None = None,
        unavailable_threshold: int = 1,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("reconciliation interval must be positive")
        if unavailable_threshold < 1:
            raise ValueError("unavailable threshold must be at least 1")
        self.reconciler = reconciler
        self.unavailable_threshold = unavailable_threshold
        # The scheduler counts failed runs, so it, not the reconciler, decides when a transient
        # failure halts trading. With a threshold of 1 the reconciler halts at once, as it
        # always has.
        self.reconciler.trip_on_transient = unavailable_threshold <= 1
        self.interval_seconds = interval_seconds
        self.balance_increments: dict[str, Decimal] = dict(balance_increments or {})
        self.refresh_order = refresh_order or self._read_order
        self.on_divergence = on_divergence
        self.on_unavailable = on_unavailable
        self.on_reconciled = on_reconciled
        self.lock = asyncio.Lock()
        self._baseline = PortfolioState(positions=baseline.positions, balances=baseline.balances)
        # Open and incompletely recorded filled orders at the baseline stay tracked.
        self._fills: dict[str, Fill] = dict(baseline.fills)
        self._orders: dict[str, Order] = {
            key: order for key, order in baseline.orders.items() if self._needs_refresh(key, order)
        }
        self._applied_fill_ids: set[str] = set(baseline.fills)
        self.status = ReconciliationStatus(started_at=utc_now())

    def observe(self, order: Order, fills: tuple[Fill, ...]) -> None:
        """Record an order and its fills as the execution engine persists them."""

        self._orders[str(order.request.client_order_id)] = order
        for fill in fills:
            self._fills[fill.fill_id] = fill

    def forget_order(self, client_order_id: str) -> None:
        """Stop following an order an administrator closed because the venue never received it.

        Every tracked order is compared with the broker, which has no record of this one, so
        left here it would read as a divergence and halt trading again after a re-arm. Call it
        while holding ``lock``, which a comparison holds for its whole run.
        """

        self._orders.pop(client_order_id, None)

    def expected_state(self) -> PortfolioState:
        new_fills = [fill for key, fill in self._fills.items() if key not in self._applied_fill_ids]
        projected = apply_fills(
            self._baseline, new_fills, balance_increments=self.balance_increments
        )
        return PortfolioState(
            orders=dict(self._orders),
            fills=dict(self._fills),
            positions=projected.positions,
            balances=projected.balances,
        )

    async def run_once(self) -> ReconciliationResult | None:
        async with self.lock:
            outcome = await self._compare()
        if isinstance(outcome, _Unreadable):
            await self._unavailable(outcome)
            return None
        reported = await self._report(outcome)
        await self._reconciled(outcome.authoritative)
        return reported

    async def _reconciled(self, authoritative: PortfolioState) -> None:
        """Hand the broker's state to a read-only consumer; its failure never affects the run."""

        if self.on_reconciled is None:
            return
        try:
            await self.on_reconciled(authoritative)
        except Exception:
            logger.exception("post-reconciliation hook failed")

    async def _compare(self) -> ReconciliationResult | _Unreadable:
        """Refresh, project, and reconcile; ``_Unreadable`` explains why the run was unavailable."""

        try:
            await self._refresh_tracked_orders()
        except Exception as exc:
            logger.exception(
                "open orders could not be refreshed before reconciliation: %s",
                describe_provider_failure(exc),
                extra={"event": provider_failure_fields(exc)},
            )
            transient = is_transient_provider_failure(exc)
            if self.reconciler.trip_on_transient or not transient:
                self.reconciler.kill_switch.trip("open orders could not be refreshed")
            return _Unreadable("open orders could not be refreshed", transient)
        try:
            local = self.expected_state()
        except ValueError:
            self.reconciler.kill_switch.trip("expected portfolio could not be projected")
            return _Unreadable("expected portfolio could not be projected")
        try:
            result = await self.reconciler.reconcile(local)
        except ReconciliationUnavailable as exc:
            return _Unreadable(str(exc), exc.transient)
        self._adopt(result, local)
        return result

    async def _refresh_tracked_orders(self) -> None:
        for client_order_id, order in tuple(self._orders.items()):
            if self._needs_refresh(client_order_id, order):
                await self.refresh_order(client_order_id)

    def _needs_refresh(self, client_order_id: str, order: Order) -> bool:
        if order.status not in TERMINAL:
            return True
        if order.status is not OrderStatus.FILLED:
            return False
        order_ids = {client_order_id, str(order.order_id)}
        recorded = sum(
            (fill.quantity for fill in self._fills.values() if str(fill.order_id) in order_ids),
            Decimal("0"),
        )
        return recorded < order.filled_quantity

    async def _read_order(self, client_order_id: str) -> None:
        broker = self.reconciler.broker
        order = await broker.get_order(client_order_id)
        if order is not None:
            self.observe(order, await broker.get_fills(client_order_id))

    async def _report(self, result: ReconciliationResult) -> ReconciliationResult:
        diverged = bool(result.discrepancies)
        if self.status.consecutive_unavailable_runs:
            logger.info(
                "broker readable again after %s unavailable reconciliation run(s)",
                self.status.consecutive_unavailable_runs,
            )
        self.status = replace(
            self.status,
            runs=self.status.runs + 1,
            consecutive_unavailable_runs=0,
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

    async def _unavailable(self, outcome: _Unreadable) -> None:
        in_a_row = self.status.consecutive_unavailable_runs + 1
        self.status = replace(
            self.status,
            runs=self.status.runs + 1,
            unavailable_runs=self.status.unavailable_runs + 1,
            consecutive_unavailable_runs=in_a_row,
            last_run_at=utc_now(),
            last_result="unavailable",
            last_discrepancies=0,
        )
        detail = outcome.detail
        # With a threshold of 1 the failing read already halted trading (``trip_on_transient``).
        if outcome.transient and self.unavailable_threshold > 1:
            if in_a_row >= self.unavailable_threshold:
                self.reconciler.kill_switch.trip(
                    f"broker unavailable for {in_a_row} reconciliation runs in a row"
                )
                detail = f"{detail}; unreadable for {in_a_row} runs in a row"
            else:
                detail = (
                    f"{detail}; run {in_a_row} of {self.unavailable_threshold} before trading halts"
                )
        logger.error("scheduled reconciliation unavailable: %s", detail)
        if self.on_unavailable is not None:
            await self.on_unavailable(detail)
