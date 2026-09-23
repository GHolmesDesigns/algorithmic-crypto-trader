"""Durable portfolio snapshots and discrepancy records."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol
from uuid import uuid4

from db.models import (
    BalanceSnapshotRecord,
    DiscrepancyRecord,
    EquitySnapshotRecord,
    PositionSnapshotRecord,
)
from sqlalchemy.exc import SQLAlchemyError

from portfolio.reconciliation import Discrepancy, PortfolioState


class PortfolioStore(Protocol):
    def save_snapshot(
        self, state: PortfolioState, *, equity: Decimal | None = None, source: str
    ) -> None: ...

    def save_discrepancy(self, discrepancy: Discrepancy, *, safety_action: str) -> None: ...


class PortfolioPersistenceUnavailable(RuntimeError):
    pass


class SqlAlchemyPortfolioStore:
    def __init__(self, session_factory) -> None:
        self.session_factory = session_factory

    def save_snapshot(
        self, state: PortfolioState, *, equity: Decimal | None = None, source: str
    ) -> None:
        now = datetime.now(UTC)
        try:
            with self.session_factory() as session:
                session.add_all(
                    [
                        PositionSnapshotRecord(
                            snapshot_id=uuid4(),
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


def _payload(value: object) -> dict[str, object]:
    if value is None:
        return {}
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json")
    return {"value": str(value)}
