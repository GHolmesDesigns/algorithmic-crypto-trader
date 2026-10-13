"""Today's P/L (#154): the change in recorded equity since the UTC day's first snapshot.

The figure comes only from the ``equity_curve`` rows the equity sampler saves (#100), which
use the one equity definition the loss-limit gate uses. Reading is side-effect free: no
broker call and no write.

A figure is reported only when it is real. No snapshot, a single snapshot, a partial one
(a holding that never had a price), or a latest snapshot older than the stale limit gives
``unavailable`` with the reason, never zero and never an old figure shown as current.

The result is a change in account equity. A deposit or withdrawal during the day would move
it, because nothing here detects transfers.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from core.models import utc_now
from db.models import EquityHoldingRecord, EquitySnapshotRecord
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

CURRENT = "current"
UNAVAILABLE = "unavailable"
LAST_KNOWN = "last_known"
STAMP = "%Y-%m-%d %H:%M:%S UTC"


@dataclass(frozen=True, slots=True)
class DayPnl:
    status: str
    detail: str
    amount: Decimal | None = None
    as_of: datetime | None = None

    @classmethod
    def unavailable(cls, reason: str) -> DayPnl:
        return cls(UNAVAILABLE, reason)


class DayPnlReader:
    """Reads today's first and latest equity snapshots and reports their difference."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        *,
        stale_after: timedelta,
        quote_asset: str = "USD",
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if stale_after <= timedelta(0):
            raise ValueError("the stale limit must be positive")
        self.session_factory = session_factory
        self.stale_after = stale_after
        self.quote_asset = quote_asset
        self.clock = clock

    def read(self) -> DayPnl:
        now = _aware(self.clock())
        start = datetime(now.year, now.month, now.day, tzinfo=UTC)
        end = start + timedelta(days=1)
        today = (EquitySnapshotRecord.as_of >= start, EquitySnapshotRecord.as_of < end)
        try:
            with self.session_factory() as session:
                first = session.scalars(
                    select(EquitySnapshotRecord)
                    .where(*today)
                    .order_by(EquitySnapshotRecord.as_of.asc())
                    .limit(1)
                ).first()
                latest = session.scalars(
                    select(EquitySnapshotRecord)
                    .where(*today)
                    .order_by(EquitySnapshotRecord.as_of.desc())
                    .limit(1)
                ).first()
                if first is None or latest is None:
                    return DayPnl.unavailable(
                        "no equity snapshot has been recorded yet today (UTC)"
                    )
                if first.snapshot_id == latest.snapshot_id:
                    return DayPnl.unavailable(
                        "only one equity snapshot has been recorded today (UTC); a change needs two"
                    )
                latest_at = _aware(latest.as_of)
                if now - latest_at > self.stale_after:
                    return DayPnl.unavailable(
                        f"the latest equity snapshot ({latest_at.strftime(STAMP)}) is older than "
                        f"{_duration(self.stale_after)}, so it is not shown as current"
                    )
                if first.partial or first.equity is None:
                    return DayPnl.unavailable(
                        f"the first equity snapshot today ({_aware(first.as_of).strftime(STAMP)}) "
                        "is partial: a holding had no price, and it is not counted as zero"
                    )
                if latest.partial or latest.equity is None:
                    return DayPnl.unavailable(
                        f"the latest equity snapshot ({latest_at.strftime(STAMP)}) is partial: "
                        "a holding had no price, and it is not counted as zero"
                    )
                held_at_last_price = sorted(
                    set(
                        session.scalars(
                            select(EquityHoldingRecord.symbol).where(
                                EquityHoldingRecord.snapshot_id.in_(
                                    [first.snapshot_id, latest.snapshot_id]
                                ),
                                EquityHoldingRecord.basis == LAST_KNOWN,
                            )
                        )
                    )
                )
                amount = latest.equity - first.equity
                first_at = _aware(first.as_of)
        except SQLAlchemyError:
            return DayPnl.unavailable("the saved equity history could not be read")
        detail = (
            f"{format_amount(amount)} {self.quote_asset} since the first equity snapshot today "
            f"({first_at.strftime(STAMP)}), as of {latest_at.strftime(STAMP)}"
        )
        if held_at_last_price:
            detail += f"; includes last-known prices for {', '.join(held_at_last_price)}"
        detail += "; a change in account equity, so a deposit or withdrawal would move it"
        return DayPnl(CURRENT, detail, amount, latest_at)


def format_amount(value: Decimal) -> str:
    """The signed amount with at least cents and no trailing zeros beyond them."""

    whole, _, fraction = format(value.normalize(), "f").partition(".")
    text = f"{whole}.{fraction.ljust(2, '0')}"
    return f"+{text}" if value > 0 else text


def _duration(value: timedelta) -> str:
    minutes = int(value.total_seconds() // 60)
    if minutes % 60 == 0 and minutes >= 60:
        hours = minutes // 60
        return f"{hours} hour" + ("" if hours == 1 else "s")
    return f"{minutes} minute" + ("" if minutes == 1 else "s")


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)
