"""Bounded, periodic persistence of the process heartbeat the dashboard already reports.

``OperatorState`` already tracks runtime and strategy heartbeat status in memory
for the dashboard; nothing before this saved a sample of it, so the soak digest
could never show uptime history. This samples it on a fixed interval, never on
every tick, and saves one small ``system_events`` row per sample.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from api.operator import OperatorState
from api.system_events import HEARTBEAT_EVENT, SystemEventJournal

logger = logging.getLogger(__name__)


class HeartbeatScheduler:
    """Persist one heartbeat sample every ``interval_seconds`` until ``stop`` is set."""

    def __init__(
        self,
        operator: OperatorState,
        journal: SystemEventJournal,
        *,
        interval_seconds: float,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("heartbeat interval must be positive")
        self.operator = operator
        self.journal = journal
        self.interval_seconds = interval_seconds

    def sample(self) -> None:
        snapshot = self.operator.snapshot
        self.journal.record(
            HEARTBEAT_EVENT,
            {
                "runtime_status": snapshot.runtime_status,
                "connectivity_status": snapshot.connectivity_status,
                "strategies": [
                    {
                        "name": item.name,
                        "status": item.status,
                        "last_seen": _iso(item.last_seen),
                    }
                    for item in snapshot.strategy_heartbeats
                ],
            },
        )

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.interval_seconds)
            except TimeoutError:
                try:
                    self.sample()
                except Exception:
                    # Unattended: a failed sample is a gap in the digest, not a halt.
                    logger.exception("heartbeat sample could not be saved")


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
