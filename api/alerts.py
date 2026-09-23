"""Provider-neutral alert routing for the authenticated operator surface."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from core.models import utc_now


@dataclass(frozen=True, slots=True)
class Alert:
    """An alert payload with no credentials or provider-specific fields."""

    condition: str
    severity: str
    message: str
    created_at: datetime = field(default_factory=utc_now)


class AlertSink(Protocol):
    async def send(self, alert: Alert) -> None: ...


@dataclass(frozen=True, slots=True)
class AlertDelivery:
    destination: str
    status: str
    error: str | None = None


class AlertRouter:
    """Fan out alerts to explicitly selected sinks."""

    def __init__(
        self,
        *,
        phone_push: AlertSink | None = None,
        email: AlertSink | None = None,
    ) -> None:
        self.phone_push = phone_push
        self.email = email

    @property
    def configured_destinations(self) -> tuple[str, ...]:
        return tuple(
            destination
            for destination, sink in (("phone_push", self.phone_push), ("email", self.email))
            if sink is not None
        )

    async def route(self, alert: Alert) -> tuple[AlertDelivery, ...]:
        deliveries: list[AlertDelivery] = []
        for destination, sink in (("phone_push", self.phone_push), ("email", self.email)):
            if sink is None:
                continue
            try:
                await sink.send(alert)
            except Exception:
                deliveries.append(
                    AlertDelivery(destination=destination, status="failed", error="delivery failed")
                )
            else:
                deliveries.append(AlertDelivery(destination=destination, status="sent"))
        return tuple(deliveries)
