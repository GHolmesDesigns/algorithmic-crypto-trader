"""Provider-neutral alert routing and concrete operator notification sinks."""

from __future__ import annotations

import asyncio
import smtplib
import ssl
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage
from typing import Protocol
from urllib.parse import urlparse

import httpx
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


class AlertConfigurationError(ValueError):
    """Raised before startup when an alert destination is only partly configured."""


class NtfyAlertSink:
    """Send phone push notifications through an HTTPS ntfy topic."""

    def __init__(
        self,
        topic_url: str,
        *,
        token: str | None = None,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        parsed = urlparse(topic_url)
        topic = parsed.path.strip("/")
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or not topic
            or "/" in topic
            or parsed.query
            or parsed.fragment
        ):
            raise AlertConfigurationError("ALERT_NTFY_TOPIC_URL must be an HTTPS topic URL")
        if timeout_seconds <= 0:
            raise AlertConfigurationError("alert timeout must be positive")
        # Publish JSON to the host root so HTTP client access logs never contain
        # the private topic path. The topic stays in a request body, which httpx
        # does not log.
        self._publish_url = f"{parsed.scheme}://{parsed.netloc}/"
        self._topic = topic
        self._token = token
        self._client = client
        self._timeout_seconds = timeout_seconds

    async def send(self, alert: Alert) -> None:
        headers = {}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        payload = {
            "topic": self._topic,
            "message": alert.message,
            "title": f"Crypto trader: {alert.condition}",
            "priority": 5 if alert.severity == "critical" else 3,
            "tags": ["warning" if alert.severity == "critical" else "information_source"],
        }
        if self._client is not None:
            response = await self._client.post(
                self._publish_url,
                json=payload,
                headers=headers,
            )
            response.raise_for_status()
            return
        async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
            response = await client.post(
                self._publish_url,
                json=payload,
                headers=headers,
            )
            response.raise_for_status()

    async def close(self) -> None:
        return None


class SmtpAlertSink:
    """Send operator email without blocking the application's event loop."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        sender: str,
        recipient: str,
        username: str | None = None,
        password: str | None = None,
        starttls: bool = True,
        timeout_seconds: float = 10.0,
        smtp_factory=smtplib.SMTP,
    ) -> None:
        if not host or not sender or not recipient:
            raise AlertConfigurationError("SMTP host, sender, and recipient are required")
        if not 0 < port <= 65535 or timeout_seconds <= 0:
            raise AlertConfigurationError("SMTP port and timeout must be positive")
        if (username is None) != (password is None):
            raise AlertConfigurationError("SMTP username and password must be configured together")
        self._host = host
        self._port = port
        self._sender = sender
        self._recipient = recipient
        self._username = username
        self._password = password
        self._starttls = starttls
        self._timeout_seconds = timeout_seconds
        self._smtp_factory = smtp_factory

    async def send(self, alert: Alert) -> None:
        message = EmailMessage()
        message["Subject"] = f"[{alert.severity.upper()}] Crypto trader: {alert.condition}"
        message["From"] = self._sender
        message["To"] = self._recipient
        message.set_content(f"{alert.message}\n\nObserved at {alert.created_at.isoformat()}\n")
        await asyncio.to_thread(self._send_sync, message)

    def _send_sync(self, message: EmailMessage) -> None:
        with self._smtp_factory(
            self._host, self._port, timeout=self._timeout_seconds
        ) as connection:
            if self._starttls:
                connection.starttls(context=ssl.create_default_context())
            if self._username is not None and self._password is not None:
                connection.login(self._username, self._password)
            connection.send_message(message)

    async def close(self) -> None:
        return None


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

    async def close(self) -> None:
        for sink in (self.phone_push, self.email):
            close = getattr(sink, "close", None)
            if close is not None:
                await close()


def build_alert_router(environ: Mapping[str, str]) -> AlertRouter:
    """Build only explicitly configured sinks; reject partial secret-bearing settings."""

    timeout = _positive_float(environ.get("ALERT_TIMEOUT_SECONDS", "10"), "ALERT_TIMEOUT_SECONDS")
    ntfy_url = environ.get("ALERT_NTFY_TOPIC_URL", "").strip()
    ntfy_token = environ.get("ALERT_NTFY_TOKEN", "").strip() or None
    if ntfy_token is not None and not ntfy_url:
        raise AlertConfigurationError(
            "ALERT_NTFY_TOPIC_URL is required when ALERT_NTFY_TOKEN is configured"
        )
    phone_push = (
        NtfyAlertSink(ntfy_url, token=ntfy_token, timeout_seconds=timeout) if ntfy_url else None
    )
    smtp_fields = {
        "host": environ.get("ALERT_SMTP_HOST", "").strip(),
        "sender": environ.get("ALERT_EMAIL_FROM", "").strip(),
        "recipient": environ.get("ALERT_EMAIL_TO", "").strip(),
    }
    smtp_requested = any(smtp_fields.values()) or any(
        environ.get(name, "").strip() for name in ("ALERT_SMTP_USERNAME", "ALERT_SMTP_PASSWORD")
    )
    email = None
    if smtp_requested:
        if not all(smtp_fields.values()):
            raise AlertConfigurationError(
                "ALERT_SMTP_HOST, ALERT_EMAIL_FROM, and ALERT_EMAIL_TO are required together"
            )
        try:
            port = int(environ.get("ALERT_SMTP_PORT", "587"))
        except ValueError as exc:
            raise AlertConfigurationError("ALERT_SMTP_PORT must be an integer") from exc
        username = environ.get("ALERT_SMTP_USERNAME", "").strip() or None
        password = environ.get("ALERT_SMTP_PASSWORD", "").strip() or None
        starttls = _boolean(environ.get("ALERT_SMTP_STARTTLS", "1"), "ALERT_SMTP_STARTTLS")
        email = SmtpAlertSink(
            host=smtp_fields["host"],
            port=port,
            sender=smtp_fields["sender"],
            recipient=smtp_fields["recipient"],
            username=username,
            password=password,
            starttls=starttls,
            timeout_seconds=timeout,
        )
    return AlertRouter(phone_push=phone_push, email=email)


def _positive_float(raw: str, name: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise AlertConfigurationError(f"{name} must be a number") from exc
    if value <= 0:
        raise AlertConfigurationError(f"{name} must be positive")
    return value


def _boolean(raw: str, name: str) -> bool:
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise AlertConfigurationError(f"{name} must be true or false")
