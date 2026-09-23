"""SQLAlchemy-backed order and fill persistence used by restart recovery."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid5

from core.models import Fill, Order, OrderRequest, OrderSide, OrderStatus, OrderType, RiskApproval
from db.models import FillRecord, OrderRecord
from sqlalchemy.exc import SQLAlchemyError

from execution.engine import OrderStore, PersistenceUnavailable


class SqlAlchemyOrderStore(OrderStore):
    def __init__(self, session_factory) -> None:
        self.session_factory = session_factory

    def reserve(self, request: OrderRequest, approval: RiskApproval) -> Order:
        try:
            with self.session_factory() as session:
                record = (
                    session.query(OrderRecord)
                    .filter_by(client_order_id=request.client_order_id)
                    .one_or_none()
                )
                if record is None:
                    record = OrderRecord(
                        order_id=request.client_order_id,
                        signal_id=request.signal_id,
                        client_order_id=request.client_order_id,
                        strategy_version=request.strategy_version,
                        risk_approval_id=approval.approval_id,
                        symbol=request.symbol,
                        side=request.side.value,
                        order_type=request.order_type.value,
                        quantity=request.quantity,
                        limit_price=request.limit_price,
                        correlation_id=request.correlation_id,
                        status=OrderStatus.PENDING_SUBMIT.value,
                        created_at=datetime.now(UTC),
                    )
                    session.add(record)
                    session.commit()
                return self._to_order(record)
        except SQLAlchemyError as exc:
            raise PersistenceUnavailable("database unavailable before order submission") from exc

    def update(self, order: Order) -> None:
        try:
            with self.session_factory() as session:
                record = (
                    session.query(OrderRecord)
                    .filter_by(client_order_id=order.request.client_order_id)
                    .one()
                )
                record.status = order.status.value
                session.commit()
        except SQLAlchemyError as exc:
            raise PersistenceUnavailable("database unavailable while updating order") from exc

    def add_fills(self, fills: tuple[Fill, ...]) -> None:
        try:
            with self.session_factory() as session:
                for fill in fills:
                    if session.query(FillRecord).filter_by(broker_fill_id=fill.fill_id).first():
                        continue
                    session.add(
                        FillRecord(
                            fill_id=uuid5(
                                UUID("6d4d6e25-9d9e-4a8e-a3f4-6c6f3f9c7f81"), fill.fill_id
                            ),
                            order_id=fill.order_id,
                            broker_fill_id=fill.fill_id,
                            quantity=fill.quantity,
                            price=fill.price,
                            fee=fill.fee,
                            occurred_at=fill.occurred_at,
                        )
                    )
                session.commit()
        except SQLAlchemyError as exc:
            raise PersistenceUnavailable("database unavailable while recording fills") from exc

    def get(self, client_order_id: str) -> Order | None:
        try:
            with self.session_factory() as session:
                record = (
                    session.query(OrderRecord)
                    .filter_by(client_order_id=UUID(client_order_id))
                    .one_or_none()
                )
                return self._to_order(record) if record is not None else None
        except (SQLAlchemyError, ValueError) as exc:
            raise PersistenceUnavailable("database unavailable while reading order") from exc

    def pending(self) -> tuple[Order, ...]:
        try:
            with self.session_factory() as session:
                records = (
                    session.query(OrderRecord)
                    .filter(
                        OrderRecord.status.in_(
                            (OrderStatus.PENDING_SUBMIT.value, OrderStatus.UNKNOWN.value)
                        )
                    )
                    .all()
                )
                return tuple(self._to_order(record) for record in records)
        except SQLAlchemyError as exc:
            raise PersistenceUnavailable(
                "database unavailable while reading pending orders"
            ) from exc

    @staticmethod
    def _to_order(record: OrderRecord) -> Order:
        if not all(
            (record.symbol, record.side, record.order_type, record.quantity, record.correlation_id)
        ):
            raise PersistenceUnavailable("persisted order is missing recovery fields")
        assert record.symbol is not None
        assert record.side is not None
        assert record.order_type is not None
        assert record.quantity is not None
        assert record.correlation_id is not None
        request = OrderRequest(
            signal_id=record.signal_id,
            strategy_version=record.strategy_version,
            symbol=record.symbol,
            side=OrderSide(record.side),
            order_type=OrderType(record.order_type),
            quantity=record.quantity,
            limit_price=record.limit_price,
            client_order_id=record.client_order_id,
            correlation_id=record.correlation_id,
        )
        return Order(
            order_id=record.order_id,
            request=request,
            status=OrderStatus(record.status),
            created_at=record.created_at,
        )
