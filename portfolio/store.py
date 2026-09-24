"""Durable portfolio snapshots and discrepancy records."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol
from uuid import uuid4

from core.models import Balance, Position
from db.models import (
    BalanceSnapshotRecord,
    DiscrepancyRecord,
    EquitySnapshotRecord,
    PortfolioSnapshotRecord,
    PositionSnapshotRecord,
)
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from portfolio.reconciliation import Discrepancy, PortfolioState


class PortfolioStore(Protocol):
    def save_snapshot(
        self, state: PortfolioState, *, equity: Decimal | None = None, source: str
    ) -> None: ...

    def save_discrepancy(self, discrepancy: Discrepancy, *, safety_action: str) -> None: ...

    def latest_state(self, *, source: str = "broker") -> PortfolioState | None: ...


class PortfolioPersistenceUnavailable(RuntimeError):
    pass


class SqlAlchemyPortfolioStore:
    def __init__(self, session_factory) -> None:
        self.session_factory = session_factory

    def save_snapshot(
        self, state: PortfolioState, *, equity: Decimal | None = None, source: str
    ) -> None:
        now = datetime.now(UTC)
        batch_id = uuid4()
        try:
            with self.session_factory() as session:
                # The batch row marks a complete snapshot, including one with no positions.
                session.add(
                    PortfolioSnapshotRecord(batch_id=batch_id, source=source, recorded_at=now)
                )
                session.add_all(
                    [
                        PositionSnapshotRecord(
                            snapshot_id=uuid4(),
                            batch_id=batch_id,
                            symbol=position.symbol,
                            quantity=position.quantity,
                            average_price=position.average_price,
                            as_of=position.as_of,
                            source=source,
                        )
                        for position in state.positions
                    ]
                )
                session.add_all(
                    [
                        BalanceSnapshotRecord(
                            snapshot_id=uuid4(),
                            batch_id=batch_id,
                            asset=balance.asset,
                            available=balance.available,
                            hold=balance.hold,
                            as_of=balance.as_of,
                            source=source,
                        )
                        for balance in state.balances
                    ]
                )
                if equity is not None:
                    session.add(
                        EquitySnapshotRecord(
                            snapshot_id=uuid4(), equity=equity, as_of=now, source=source
                        )
                    )
                session.commit()
        except SQLAlchemyError as exc:
            raise PortfolioPersistenceUnavailable(
                "database unavailable for portfolio snapshot"
            ) from exc

    def save_discrepancy(self, discrepancy: Discrepancy, *, safety_action: str) -> None:
        try:
            with self.session_factory() as session:
                session.add(
                    DiscrepancyRecord(
                        discrepancy_id=uuid4(),
                        entity_type=discrepancy.entity_type,
                        entity_key=discrepancy.entity_key,
                        local_payload=_payload(discrepancy.local),
                        broker_payload=_payload(discrepancy.broker),
                        safety_action=safety_action,
                        created_at=datetime.now(UTC),
                    )
                )
                session.commit()
        except SQLAlchemyError as exc:
            raise PortfolioPersistenceUnavailable(
                "database unavailable for discrepancy record"
            ) from exc

    def latest_state(self, *, source: str = "broker") -> PortfolioState | None:
        """Return the most recent complete snapshot, or None when no baseline exists."""

        try:
            with self.session_factory() as session:
                batch_id = session.scalar(
                    select(PortfolioSnapshotRecord.batch_id)
                    .filter_by(source=source)
                    .order_by(PortfolioSnapshotRecord.recorded_at.desc())
                    .limit(1)
                )
                if batch_id is None:
                    return None
                positions = session.scalars(
                    select(PositionSnapshotRecord).filter_by(batch_id=batch_id)
                ).all()
                balances = session.scalars(
                    select(BalanceSnapshotRecord).filter_by(batch_id=batch_id)
                ).all()
                return PortfolioState(
                    positions=tuple(
                        Position(
                            symbol=item.symbol,
                            quantity=item.quantity,
                            average_price=item.average_price,
                            as_of=item.as_of,
                        )
                        for item in positions
                    ),
                    balances=tuple(
                        Balance(
                            asset=item.asset,
                            available=item.available,
                            hold=item.hold,
                            as_of=item.as_of,
                        )
                        for item in balances
                    ),
                )
        except SQLAlchemyError as exc:
            raise PortfolioPersistenceUnavailable(
                "database unavailable while loading portfolio snapshot"
            ) from exc


def _payload(value: object) -> dict[str, object]:
    if value is None:
        return {}
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json")
    return {"value": str(value)}
