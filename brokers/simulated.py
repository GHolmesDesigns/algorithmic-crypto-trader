"""Deterministic local broker used by tests, paper mode, and replay."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from core.models import (
    Balance,
    Fill,
    Order,
    OrderRequest,
    OrderStatus,
    Position,
    Quote,
    RiskApproval,
    utc_now,
)

from brokers.interface import BrokerCapabilities, BrokerInterface


class SimulatedBroker(BrokerInterface):
    def __init__(self, quote: Quote | None = None) -> None:
        self._quote = quote or Quote(
            symbol="BTC-USD",
            bid=Decimal("59999"),
            ask=Decimal("60001"),
            as_of=utc_now(),
            source="simulated",
        )
        self._orders: dict[str, Order] = {}
        self._fills: dict[str, tuple[Fill, ...]] = {}

    @property
    def capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities(
            provider="simulated",
            environment="local",
            streaming=True,
            historical_candles=True,
            native_order_edit=False,
            preview_orders=True,
            order_types=("market", "limit"),
            price_increment=Decimal("0.01"),
            quantity_increment=Decimal("0.00000001"),
            max_quote_age_seconds=60,
        )

    async def get_quote(self, symbol: str) -> Quote:
        if symbol != self._quote.symbol:
            raise KeyError(symbol)
        return self._quote

    async def get_balances(self) -> tuple[Balance, ...]:
        return (Balance(asset="USD", available=Decimal("100000"), as_of=utc_now()),)

    async def get_positions(self) -> tuple[Position, ...]:
        return ()

    async def submit_order(self, request: OrderRequest, approval: RiskApproval) -> Order:
        if not approval.approved:
            raise PermissionError("risk approval is not approved")
        key = str(request.client_order_id)
        existing = self._orders.get(key)
        if existing is not None:
            return existing
        price = self._quote.ask if request.side.value == "buy" else self._quote.bid
        order = Order(
            order_id=UUID(key),
            request=request,
            status=OrderStatus.FILLED,
            filled_quantity=request.quantity,
            average_fill_price=price,
        )
        fill = Fill(
            fill_id=f"sim-{key}",
            order_id=order.order_id,
            symbol=request.symbol,
            side=request.side,
            quantity=request.quantity,
            price=price,
            fee=Decimal("0"),
            fee_asset="USD",
            occurred_at=utc_now(),
        )
        self._orders[key] = order
        self._fills[key] = (fill,)
        return order

    async def get_order(self, client_order_id: str) -> Order | None:
        return self._orders.get(client_order_id)

    async def get_fills(self, client_order_id: str) -> tuple[Fill, ...]:
        return self._fills.get(client_order_id, ())
