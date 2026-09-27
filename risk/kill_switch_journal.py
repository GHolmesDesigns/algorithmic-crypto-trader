"""Kill-switch transition history kept in ``system_events``."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from db.models import SystemEventRecord
from sqlalchemy import select

EVENT_TYPE = "kill_switch_transition"


class SqlAlchemyKillSwitchJournal:
    """Save each transition once, keyed by its ``event_id``, so a retry never duplicates."""

    def __init__(self, session_factory) -> None:
        self.session_factory = session_factory

    def record(self, events: Sequence[Mapping[str, Any]]) -> None:
        if not events:
            return
        ids = [UUID(str(event["event_id"])) for event in events]
        with self.session_factory() as session:
            saved = set(
                session.scalars(
                    select(SystemEventRecord.event_id).where(SystemEventRecord.event_id.in_(ids))
                )
            )
            session.add_all(
                [
                    SystemEventRecord(
                        event_id=event_id,
                        event_type=EVENT_TYPE,
                        correlation_id=event_id,
                        payload=dict(event),
                        created_at=datetime.fromisoformat(str(event["created_at"])),
                    )
                    for event_id, event in zip(ids, events, strict=True)
                    if event_id not in saved
                ]
            )
            session.commit()

    def recent(self, limit: int) -> list[dict[str, Any]]:
        with self.session_factory() as session:
            payloads = session.scalars(
                select(SystemEventRecord.payload)
                .where(SystemEventRecord.event_type == EVENT_TYPE)
                .order_by(SystemEventRecord.created_at.desc())
                .limit(limit)
            ).all()
        return [dict(payload) for payload in reversed(payloads)]
