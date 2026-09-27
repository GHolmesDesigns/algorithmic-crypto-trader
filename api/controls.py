"""Operator control contract: the re-arm review and the result every control ends on."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs

from core.models import KillSwitchState

# The user manual's re-arm checklist. The server refuses a re-arm missing any item.
REARM_CHECKLIST: tuple[tuple[str, str], ...] = (
    ("cause_documented", "The original cause is identified and documented."),
    ("mode_and_scope_confirmed", "The approved mode and credential scope are confirmed."),
    (
        "orders_resolved",
        "Every pending or unknown order is resolved by looking it up with its client order "
        "ID, never by resubmitting it.",
    ),
    (
        "broker_state_confirmed",
        "Broker-authoritative balances, positions, orders, and fills are confirmed.",
    ),
    ("recovery_and_reconciliation_clean", "Startup recovery and reconciliation are clean."),
    ("inputs_current", "Required market data and risk inputs are current."),
    ("approval_obtained", "The incident owner has approved, when the procedure requires it."),
)
REASON_LIMIT = 500

_ACTIONS = {
    "pause": ("PAUSE", "Pause applied"),
    "emergency_stop": ("EMERGENCY STOP", "Emergency stop applied"),
    "rearm": ("RE-ARM", "Re-arm applied"),
    "sign_out": ("Sign out", "Signed out"),
}


@dataclass(frozen=True, slots=True)
class RearmRequest:
    checklist: tuple[str, ...]
    reason: str
    errors: tuple[str, ...] = field(default=())

    @property
    def missing(self) -> tuple[str, ...]:
        return tuple(key for key, _ in REARM_CHECKLIST if key not in self.checklist)


def parse_rearm(body: bytes, content_type: str) -> RearmRequest:
    """Read the checklist and reason from a form post or a JSON body.

    A form repeats ``checklist`` once per ticked item; JSON sends
    ``{"checklist": [...], "reason": "..."}``. Unknown items are ignored.
    """

    checklist: list[str] = []
    reason = ""
    if content_type.split(";", 1)[0].strip().lower() == "application/json":
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            return RearmRequest((), "", ("The request body must be a JSON object.",))
        raw_items = payload.get("checklist", [])
        checklist = [str(item) for item in raw_items] if isinstance(raw_items, list) else []
        reason = str(payload.get("reason") or "")
    else:
        form = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
        checklist = form.get("checklist", [])
        reason = form.get("reason", [""])[0]
    known = {key for key, _ in REARM_CHECKLIST}
    ticked = tuple(key for key in dict.fromkeys(checklist) if key in known)
    errors: list[str] = []
    missing = [key for key, _ in REARM_CHECKLIST if key not in ticked]
    if missing:
        errors.append(f"Confirm every checklist item. {len(missing)} not confirmed.")
    if not reason.strip():
        errors.append("Enter the cause-and-approval reference.")
    elif len(reason) > REASON_LIMIT:
        errors.append(f"Keep the reference to {REASON_LIMIT} characters or fewer.")
    return RearmRequest(ticked, reason, tuple(errors))


@dataclass(frozen=True, slots=True)
class ControlResult:
    """What a control did, rendered as the result page or returned as JSON."""

    action: str
    state: str
    changed: bool
    at: datetime
    role: str | None
    previous: str | None = None

    @property
    def label(self) -> str:
        return _ACTIONS[self.action][0]

    @property
    def heading(self) -> str:
        if self.action == "sign_out":
            return "Signed out" if self.changed else "No active session"
        return _ACTIONS[self.action][1] if self.changed else f"Already {self.state}"

    @property
    def change(self) -> str:
        if self.action == "sign_out":
            return (
                "Yes: the session was ended and its cookie cleared."
                if self.changed
                else "No: there was no active session. The cookie was cleared anyway."
            )
        if self.changed:
            return f"Yes: {self.previous} to {self.state}."
        return f"No: the kill switch was already {self.state}."

    @property
    def next_step(self) -> str:
        if self.action == "sign_out":
            return "Close this tab on a shared device. Sign in again to keep monitoring."
        if self.action == "rearm":
            if not self.changed:
                return "Nothing to re-arm. Keep monitoring."
            return (
                "Confirm the dashboard reports running and that no new error or divergence "
                "appears. PAUSE or EMERGENCY STOP at once if anything looks wrong."
            )
        if self.state == KillSwitchState.HALTED.value:
            if self.action == "pause":
                return (
                    "Nothing changed: PAUSE cannot lower a halt. Leaving halted requires an "
                    "administrator re-arm after the re-arm checklist."
                )
            return (
                "Leave it halted and escalate. An administrator re-arms only after the checklist."
            )
        return (
            "Investigate the cause. Only an administrator re-arm resumes trading, after "
            "the checklist."
        )

    @property
    def return_link(self) -> tuple[str, str]:
        if self.action == "sign_out":
            return ("/operator/login", "Sign in again")
        return ("/operator", "Return to dashboard")

    @property
    def tone(self) -> str:
        return {"running": "ok", "paused": "warn", "halted": "crit"}.get(self.state, "neutral")

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "state": self.state,
            "changed": self.changed,
            "role": self.role,
            "at": self.at.isoformat(),
        }
