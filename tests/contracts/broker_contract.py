"""The shared broker contract that every adapter must satisfy."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from uuid import uuid4

from brokers.interface import BrokerInterface
from core.models import OrderRequest, OrderSide, OrderType, RiskApproval


def make_request(*, order_type: OrderType = OrderType.MARKET) -> OrderRequest:
    return OrderRequest(
        signal_id=uuid4(),
        strategy_version="contract-v1",
        symbol="BTC-USD",
        side=OrderSide.BUY,
        order_type=order_type,
        quantity=Decimal("0.01"),
        limit_price=Decimal("60001") if order_type is OrderType.LIMIT else None,
        correlation_id=uuid4(),
    )


def approve(request: OrderRequest) -> RiskApproval:
    return RiskApproval(
        signal_id=request.signal_id,
        approved=True,
        reason="contract test approval",
        correlation_id=request.correlation_id,
    )


async def assert_shared_broker_contract(
    factory: Callable[[], BrokerInterface],
) -> None:
    """Run provider-neutral assertions against a fresh broker instance.

    Adapter-specific tests should call this function unchanged and add only
    capability or provider-behavior assertions that are not part of the
    common interface.
    """
    broker = factory()
    capabilities = broker.capabilities
    assert capabilities.order_types
    assert capabilities.price_increment > 0
    assert capabilities.quantity_increment > 0
    assert capabilities.max_quote_age_seconds > 0

    quote = await broker.get_quote("BTC-USD")
    assert quote.ask >= quote.bid

    request = make_request()
    denied = request.model_copy()
    unapproved = RiskApproval(
        signal_id=denied.signal_id,
        approved=False,
        reason="contract rejection",
        correlation_id=denied.correlation_id,
    )
    try:
        await broker.submit_order(denied, unapproved)
    except PermissionError:
        pass
    else:
        raise AssertionError("broker accepted an order without risk approval")

    order = await broker.submit_order(request, approve(request))
    assert order.request.client_order_id == request.client_order_id
    assert order.filled_quantity == request.quantity
    assert await broker.get_order(str(request.client_order_id)) == order
    fills = await broker.get_fills(str(request.client_order_id))
    assert len(fills) == 1
    assert fills[0].order_id == order.order_id

    duplicate = await broker.submit_order(request, approve(request))
    assert duplicate == order
    assert len(await broker.get_fills(str(request.client_order_id))) == 1
