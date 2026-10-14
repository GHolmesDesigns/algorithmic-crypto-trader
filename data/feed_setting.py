"""The operator's saved choice to run the watch-only feed, or not.

One row in ``operator_settings`` holds the choice. Every change is one
transaction that also writes a ``watch_feed_change`` system event, so a change
cannot exist without its audit record. Nothing here imports strategy, risk,
execution, broker, or portfolio code.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from core.models import utc_now
from db.models import OperatorSettingRecord, SystemEventRecord
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

FEED_SETTING_KEY = "watch_feed_enabled"
FEED_EVENT = "watch_feed_change"
_ON = "1"
_OFF = "0"


ORIGIN_ENVIRONMENT = "environment"
ORIGIN_SAVED = "saved"
ORIGIN_UNREADABLE = "unreadable"


@dataclass(frozen=True, slots=True)
class FeedOrigin:
    """Why the feed is on or off: the deployment setting, a saved operator choice, or neither."""

    source: str
    changed_by: str | None = None
    changed_at: datetime | None = None
    detail: str | None = None


class FeedSettingError(RuntimeError):
    """The saved choice could not be read or written; the message is safe to show."""


@dataclass(frozen=True, slots=True)
class FeedChoice:
    enabled: bool
    changed_by: str
    changed_at: datetime


class SqlAlchemyFeedSetting:
    """Persisted on/off choice for the watch-only feed."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def read(self) -> FeedChoice | None:
        """The saved choice, or ``None`` when no operator has made one.

        A value that cannot be read or understood raises ``FeedSettingError``;
        the caller must treat that as off, never as on.
        """

        try:
            with self.session_factory() as session:
                row = session.get(OperatorSettingRecord, FEED_SETTING_KEY)
                if row is None:
                    return None
                if row.value not in {_ON, _OFF}:
                    raise FeedSettingError("The saved on/off choice is not a valid value.")
                return FeedChoice(row.value == _ON, row.updated_by, row.updated_at)
        except SQLAlchemyError as exc:
            raise FeedSettingError("The saved on/off choice could not be read.") from exc

    def save(
        self, enabled: bool, *, previous: bool, role: str, now: datetime | None = None
    ) -> FeedChoice:
        """Save the choice and its audit event in one transaction."""

        moment = now or utc_now()
        value = _ON if enabled else _OFF
        try:
            with self.session_factory.begin() as session:
                row = session.get(OperatorSettingRecord, FEED_SETTING_KEY)
                if row is None:
                    session.add(
                        OperatorSettingRecord(
                            key=FEED_SETTING_KEY, value=value, updated_at=moment, updated_by=role
                        )
                    )
                else:
                    row.value, row.updated_at, row.updated_by = value, moment, role
                event_id = uuid4()
                session.add(
                    SystemEventRecord(
                        event_id=event_id,
                        event_type=FEED_EVENT,
                        correlation_id=event_id,
                        payload={
                            "action": "enable" if enabled else "disable",
                            "previous": previous,
                            "enabled": enabled,
                            "role": role,
                        },
                        created_at=moment,
                    )
                )
        except SQLAlchemyError as exc:
            raise FeedSettingError("The on/off choice could not be saved.") from exc
        return FeedChoice(enabled, role, moment)
