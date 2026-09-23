"""Minimal independent risk boundary used by the execution layer."""

from core.models import RiskApproval, Signal


def decide(signal: Signal, approved: bool, reason: str) -> RiskApproval:
    return RiskApproval(
        signal_id=signal.signal_id,
        approved=approved,
        reason=reason,
        correlation_id=signal.correlation_id,
    )
