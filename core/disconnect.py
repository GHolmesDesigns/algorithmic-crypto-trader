"""Why a market-data stream dropped: a fixed set of kinds plus a short redacted note."""

from __future__ import annotations

import re
from dataclasses import dataclass

from core.logging import redact_free_text

DISCONNECT_REASON_KINDS = (
    "connect_failed",
    "subscribe_failed",
    "heartbeat_timeout",
    "stale_data",
    "parse_error",
    "closed_by_peer",
    "unknown",
)
UNKNOWN_KIND = "unknown"
NOTE_LIMIT = 200

_IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_IPV6 = re.compile(r"(?<![\w:])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![\w:])")
_HOSTNAME = re.compile(r"\b(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}\b")


def redact_diagnostic(text: str, *, limit: int = NOTE_LIMIT) -> str:
    """Redact, then bound, text that may carry a hostname, address, or credential.

    Longer text is truncated, never refused, so a verbose exception cannot cost the event.
    """

    value = redact_free_text(text)
    for pattern in (_IPV4, _IPV6, _HOSTNAME):
        value = pattern.sub("[REDACTED]", value)
    return value if len(value) <= limit else value[: limit - 1] + "…"


@dataclass(frozen=True)
class DisconnectReason:
    kind: str
    note: str = ""

    def __post_init__(self) -> None:
        if self.kind not in DISCONNECT_REASON_KINDS:
            object.__setattr__(self, "kind", UNKNOWN_KIND)
        object.__setattr__(self, "note", redact_diagnostic(self.note))
