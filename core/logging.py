"""Structured JSON logging with recursive secret scrubbing and correlation context."""

from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
from collections.abc import Mapping
from typing import Any
from uuid import UUID

REDACTED = "[REDACTED]"
SECRET_FIELD_NAMES = frozenset(
    {
        "api_key",
        "apikey",
        "api_secret",
        "apisecret",
        "private_key",
        "privatekey",
        "secret",
        "password",
        "token",
        "access_token",
        "refresh_token",
        "jwt",
        "signature",
        "authorization",
        "authorization_header",
        "auth_header",
        "account_id",
        "portfolio_id",
        "client_order_id",
        "email",
        "ip_address",
    }
)
_SECRET_TEXT = re.compile(
    r"(?i)(bearer\s+|(?:api[_-]?key|secret|token|password|signature)\s*[=:]\s*)[^\s,;]+"
)
_correlation_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "correlation_id", default=None
)


def set_correlation_id(value: UUID | str) -> contextvars.Token[str | None]:
    return _correlation_id.set(str(value))


def reset_correlation_id(token: contextvars.Token[str | None]) -> None:
    _correlation_id.reset(token)


def scrub_secrets(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: REDACTED if _normalise_key(key) in SECRET_FIELD_NAMES else scrub_secrets(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [scrub_secrets(item) for item in value]
    if isinstance(value, str):
        return _SECRET_TEXT.sub(lambda match: f"{match.group(1)}{REDACTED}", value)
    return value


def _normalise_key(key: object) -> str:
    return str(key).replace("-", "_").lower()


class SecretScrubber(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = scrub_secrets(record.msg)
        if record.args:
            record.args = scrub_secrets(record.args)
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": scrub_secrets(record.getMessage()),
        }
        correlation_id = _correlation_id.get()
        if correlation_id:
            payload["correlation_id"] = correlation_id
        extra = getattr(record, "event", None)
        if extra is not None:
            payload["event"] = scrub_secrets(extra)
        return json.dumps(payload, default=str, sort_keys=True)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(SecretScrubber())
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
