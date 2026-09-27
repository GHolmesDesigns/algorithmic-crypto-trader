from __future__ import annotations

import json

import httpx
import pytest
from api.alerts import (
    Alert,
    AlertConfigurationError,
    NtfyAlertSink,
    SmtpAlertSink,
    build_alert_router,
)


@pytest.mark.asyncio
async def test_ntfy_sink_sends_redacted_phone_push() -> None:
    seen: dict[str, str] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers["authorization"]
        seen["body"] = (await request.aread()).decode()
        return httpx.Response(200, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sink = NtfyAlertSink(
        "https://notify.example.test/private-topic", token="secret-token", client=client
    )
    await sink.send(Alert(condition="trading_cycle_halted", severity="critical", message="halted"))

    assert seen == {
        "url": "https://notify.example.test/",
        "authorization": "Bearer secret-token",
        "body": json.dumps(
            {
                "topic": "private-topic",
                "message": "halted",
                "title": "Crypto trader: trading_cycle_halted",
                "priority": 5,
                "tags": ["warning"],
            },
            separators=(",", ":"),
        ),
    }
    await client.aclose()


class FakeSmtp:
    instances: list[FakeSmtp] = []

    def __init__(self, host: str, port: int, *, timeout: float) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.started_tls = False
        self.login_args: tuple[str, str] | None = None
        self.message = None
        self.instances.append(self)

    def __enter__(self) -> FakeSmtp:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def starttls(self, *, context) -> None:
        assert context is not None
        self.started_tls = True

    def login(self, username: str, password: str) -> None:
        self.login_args = (username, password)

    def send_message(self, message) -> None:
        self.message = message


@pytest.mark.asyncio
async def test_smtp_sink_sends_email_off_the_event_loop() -> None:
    FakeSmtp.instances.clear()
    sink = SmtpAlertSink(
        host="smtp.example.test",
        port=587,
        sender="trader@example.test",
        recipient="operator@example.test",
        username="smtp-user",
        password="smtp-password",
        smtp_factory=FakeSmtp,
    )

    await sink.send(
        Alert(condition="reconciliation_unavailable", severity="critical", message="offline")
    )

    connection = FakeSmtp.instances[0]
    assert connection.started_tls
    assert connection.login_args == ("smtp-user", "smtp-password")
    assert connection.message is not None
    assert connection.message["To"] == "operator@example.test"
    assert "offline" in connection.message.get_content()


def test_alert_router_builder_rejects_partial_or_insecure_configuration() -> None:
    with pytest.raises(AlertConfigurationError, match="required together"):
        build_alert_router({"ALERT_SMTP_HOST": "smtp.example.test"})
    with pytest.raises(AlertConfigurationError, match="HTTPS"):
        build_alert_router({"ALERT_NTFY_TOPIC_URL": "http://notify.example.test/topic"})
    with pytest.raises(AlertConfigurationError, match="required"):
        build_alert_router({"ALERT_NTFY_TOKEN": "secret"})
    with pytest.raises(AlertConfigurationError, match="together"):
        build_alert_router(
            {
                "ALERT_SMTP_HOST": "smtp.example.test",
                "ALERT_EMAIL_FROM": "from@example.test",
                "ALERT_EMAIL_TO": "to@example.test",
                "ALERT_SMTP_USERNAME": "user",
            }
        )


def test_alert_router_builder_selects_both_destinations() -> None:
    router = build_alert_router(
        {
            "ALERT_NTFY_TOPIC_URL": "https://notify.example.test/topic",
            "ALERT_SMTP_HOST": "smtp.example.test",
            "ALERT_EMAIL_FROM": "from@example.test",
            "ALERT_EMAIL_TO": "to@example.test",
        }
    )
    assert router.configured_destinations == ("phone_push", "email")


def test_passive_smtp_defaults_do_not_enable_email() -> None:
    router = build_alert_router({"ALERT_SMTP_PORT": "587", "ALERT_SMTP_STARTTLS": "1"})
    assert router.configured_destinations == ()
