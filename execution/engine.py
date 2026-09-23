"""Execution accepts a RiskApproval, never a raw strategy Signal."""

from brokers.interface import BrokerInterface
from core.models import Order, OrderRequest, RiskApproval


async def submit_approved_order(
    broker: BrokerInterface, request: OrderRequest, approval: RiskApproval
) -> Order:
    if request.signal_id != approval.signal_id:
        raise ValueError("risk approval does not belong to order request")
    if not approval.approved:
        raise PermissionError("order is not approved by risk engine")
    return await broker.submit_order(request, approval)
