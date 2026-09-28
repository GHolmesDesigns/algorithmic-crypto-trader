"""Generic ``system_events`` journal for the digest's process-lifecycle producers.

Four event types land here, each written by a different part of the running
service: a bounded heartbeat sample (never every tick), a startup classified as
a clean restart or a crash recovery, a WebSocket disconnect, and the gap fill
that follows one. None of them carries a broker or provider payload; each
payload is a small, already-scrubbed summary.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Protocol
from uuid import uuid4

from core.models import utc_now
from db.models import SystemEventRecord

HEARTBEAT_EVENT = "heartbeat"
RESTART_EVENT = "restart"
DISCONNECT_EVENT = "disconnect"
GAP_FILL_EVENT = "gap_fill"

# Bounded heartbeat cadence for the digest's uptime gap check: not every tick.
HEARTBEAT_INTERVAL_SECONDS = 3600.0


class SystemEventJournal(Protocol):
    def record(
        self, event_type: str, payload: Mapping[str, Any], *, now: datetime | None = None
    ) -> None: ...


class SqlAlchemySystemEventJournal:
    """Save one system event per call; every call gets a fresh id, nothing is retried."""

    def __init__(self, session_factory) -> None:
        self.session_factory = session_factory

    def record(
        self, event_type: str, payload: Mapping[str, Any], *, now: datetime | None = None
    ) -> None:
        event_id = uuid4()
        with self.session_factory() as session:
            session.add(
                SystemEventRecord(
                    event_id=event_id,
                    event_type=event_type,
                    correlation_id=event_id,
                    payload=dict(payload),
                    created_at=now or utc_now(),
                )
            )
            session.commit()
