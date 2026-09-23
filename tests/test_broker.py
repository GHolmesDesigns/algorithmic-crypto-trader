from decimal import Decimal
from uuid import uuid4

import pytest
from brokers.simulated import SimulatedBroker
from core.models import OrderRequest, OrderSide, OrderType, RiskApproval


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
