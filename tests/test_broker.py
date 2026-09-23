from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from brokers.simulated import (
    BrokerUnavailableError,
    DuplicateAcknowledgementError,
    FaultPlan,
    OrderRejectedError,
    RateLimitError,
    SimulatedBroker,
    SimulatedFault,
    SimulationConfig,
    SubmissionTimeoutError,
)
from core.models import OrderRequest, OrderSide, OrderStatus, OrderType, Quote, RiskApproval


@pytest.mark.asyncio
async def test_simulated_broker_requires_risk_approval_and_is_idempotent() -> None:
    broker = SimulatedBroker()
    request = OrderRequest(
        signal_id=uuid4(),
        strategy_version="test-v1",
        symbol="BTC-USD",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("0.01"),
        correlation_id=uuid4(),
    )
    rejected = RiskApproval(
        signal_id=request.signal_id,
        approved=False,
        reason="stale price",
        correlation_id=request.correlation_id,
    )
    with pytest.raises(PermissionError):
        await broker.submit_order(request, rejected)
    approval = rejected.model_copy(update={"approved": True, "reason": "all gates passed"})
    first = await broker.submit_order(request, approval)
    second = await broker.submit_order(request, approval)
    assert first.order_id == second.order_id
    assert len(await broker.get_fills(str(request.client_order_id))) == 1


def approval_for(request: OrderRequest) -> RiskApproval:
    return RiskApproval(
        signal_id=request.signal_id,
        approved=True,
        reason="test approval",
        correlation_id=request.correlation_id,
    )


def request(*, order_type: OrderType = OrderType.MARKET, limit_price: Decimal | None = None):
    return OrderRequest(
        signal_id=uuid4(),
        strategy_version="test-v1",
        symbol="BTC-USD",
        side=OrderSide.BUY,
        order_type=order_type,
        quantity=Decimal("1"),
        limit_price=limit_price,
        correlation_id=uuid4(),
    )


@pytest.mark.asyncio
async def test_market_orders_use_far_side_slippage_and_fee() -> None:
    quote = Quote(
        symbol="BTC-USD",
        bid=Decimal("99"),
        ask=Decimal("101"),
        as_of=datetime.now(UTC),
        source="fixture",
    )
    broker = SimulatedBroker(
        quote,
        config=SimulationConfig(slippage=Decimal("0.01"), fee_rate=Decimal("0.001")),
    )
    order_request = request()
    order = await broker.submit_order(order_request, approval_for(order_request))
    assert order.status is OrderStatus.FILLED
    assert order.average_fill_price == Decimal("102.01")
    fill = (await broker.get_fills(str(order_request.client_order_id)))[0]
    assert fill.fee == Decimal("0.10201")


@pytest.mark.asyncio
async def test_marketable_limit_fills_on_a_deterministic_partial_schedule() -> None:
    broker = SimulatedBroker()
    order_request = request(order_type=OrderType.LIMIT, limit_price=Decimal("60001"))
    first = await broker.submit_order(order_request, approval_for(order_request))
    assert first.status is OrderStatus.PARTIALLY_FILLED
    assert first.filled_quantity == Decimal("0.5")
    changed = await broker.advance()
    assert changed[0].status is OrderStatus.FILLED
    assert [
        fill.quantity for fill in await broker.get_fills(str(order_request.client_order_id))
    ] == [
        Decimal("0.5"),
        Decimal("0.5"),
    ]


@pytest.mark.asyncio
async def test_limit_waits_until_quote_conditions_permit_matching() -> None:
    broker = SimulatedBroker()
    order_request = request(order_type=OrderType.LIMIT, limit_price=Decimal("59900"))
    order = await broker.submit_order(order_request, approval_for(order_request))
    assert order.status is OrderStatus.OPEN
    await broker.advance()
    assert (await broker.get_fills(str(order_request.client_order_id))) == ()
    broker.set_quote(
        Quote(
            symbol="BTC-USD",
            bid=Decimal("59899"),
            ask=Decimal("59900"),
            as_of=order.created_at,
            source="fixture",
        )
    )
    changed = await broker.advance()
    assert changed[0].status is OrderStatus.PARTIALLY_FILLED


@pytest.mark.asyncio
async def test_timeout_is_unknown_until_status_query_and_retry_is_idempotent() -> None:
    broker = SimulatedBroker(fault_plan=FaultPlan(submit=(SimulatedFault.TIMEOUT,)))
    order_request = request()
    with pytest.raises(SubmissionTimeoutError) as raised:
        await broker.submit_order(order_request, approval_for(order_request))
    assert raised.value.order is not None
    assert raised.value.order.status is OrderStatus.UNKNOWN
    resolved = await broker.get_order(str(order_request.client_order_id))
    assert resolved is not None
    assert resolved.status is OrderStatus.FILLED
    retry = await broker.submit_order(order_request, approval_for(order_request))
    assert retry == resolved
    assert len(await broker.get_fills(str(order_request.client_order_id))) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fault", "error"),
    [
        (SimulatedFault.REJECT, OrderRejectedError),
        (SimulatedFault.RATE_LIMITED, RateLimitError),
        (SimulatedFault.UNAVAILABLE, BrokerUnavailableError),
    ],
)
async def test_injected_submit_faults_are_explicit_and_persist_safe_state(fault, error) -> None:
    broker = SimulatedBroker(faults=(fault,))
    order_request = request()
    with pytest.raises(error):
        await broker.submit_order(order_request, approval_for(order_request))
    if fault is SimulatedFault.REJECT:
        persisted = await broker.get_order(str(order_request.client_order_id))
        assert persisted is not None
        assert persisted.status is OrderStatus.REJECTED


@pytest.mark.asyncio
async def test_duplicate_ack_and_out_of_order_fills_are_injected_without_duplicate_orders() -> None:
    broker = SimulatedBroker(faults=(SimulatedFault.DUPLICATE_ACK,))
    order_request = request(order_type=OrderType.LIMIT, limit_price=Decimal("60001"))
    with pytest.raises(DuplicateAcknowledgementError):
        await broker.submit_order(order_request, approval_for(order_request))
    await broker.advance()
    retry = await broker.submit_order(order_request, approval_for(order_request))
    fills = await broker.get_fills(str(order_request.client_order_id))
    assert retry.status is OrderStatus.FILLED
    assert len(fills) == 2
    assert broker.acknowledgement_count(str(order_request.client_order_id)) == 2

    out_of_order = SimulatedBroker(
        faults=(SimulatedFault.OUT_OF_ORDER_FILLS,),
        config=SimulationConfig(partial_fill_schedule=(Decimal("0.25"), Decimal("1"))),
    )
    other_request = request(order_type=OrderType.LIMIT, limit_price=Decimal("60001"))
    await out_of_order.submit_order(other_request, approval_for(other_request))
    await out_of_order.advance()
    reversed_fills = await out_of_order.get_fills(str(other_request.client_order_id))
    assert [fill.quantity for fill in reversed_fills] == [Decimal("0.75"), Decimal("0.25")]
