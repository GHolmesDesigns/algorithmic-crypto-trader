"""Persistent, restart-safe kill switch with independent external actuation paths."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import RLock

from core.models import KillSwitchState, utc_now

_SEVERITY = {KillSwitchState.RUNNING: 0, KillSwitchState.PAUSED: 1, KillSwitchState.HALTED: 2}


class KillSwitch:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._lock = RLock()
        self._state = KillSwitchState.RUNNING
        self.audit_events: list[dict[str, str]] = []
        self.reload()

    @property
    def state(self) -> KillSwitchState:
        with self._lock:
            return self._state

    def reload(self) -> KillSwitchState:
        with self._lock:
            if self.path is not None and self.path.exists():
                payload = json.loads(self.path.read_text(encoding="utf-8"))
                self._state = KillSwitchState(payload["state"])
            return self._state

    def set_state(
        self, state: KillSwitchState, *, automatic: bool = False, reason: str = ""
    ) -> None:
        with self._lock:
            if (
                self._state is KillSwitchState.HALTED
                and state is KillSwitchState.RUNNING
                and automatic
            ):
                raise PermissionError("HALTED requires manual re-arm")
            if automatic and state is KillSwitchState.RUNNING:
                raise PermissionError("automatic actuation cannot re-arm the kill switch")
            previous = self._state
            self._state = state
            event = {
                "event": "kill_switch_transition",
                "from": previous.value,
                "to": state.value,
                "automatic": str(automatic).lower(),
                "reason": reason,
                "created_at": utc_now().isoformat(),
            }
            self.audit_events.append(event)
            self._persist()

    def trip(self, reason: str) -> None:
        self.set_state(KillSwitchState.HALTED, automatic=True, reason=reason)

    def sync_external_state(
        self, *, env: Mapping[str, str] | None = None, flag_path: Path | None = None
    ) -> KillSwitchState:
        """Apply the file and environment flags, read on every trading loop.

        The flags can only pause or halt. A flag left at ``running`` must not undo an
        operator's emergency stop, so re-arming stays on the authenticated path. An
        unreadable or unrecognised flag halts trading.
        """

        values = env if env is not None else os.environ
        requests = [("environment", values.get("TRADING_KILL_SWITCH", ""))]
        if flag_path is not None and flag_path.exists():
            try:
                requests.append(("file", flag_path.read_text(encoding="utf-8")))
            except OSError:
                requests.append(("file", "unreadable"))
        for source, raw in requests:
            value = raw.strip().lower()
            if not value:
                continue
            try:
                requested = KillSwitchState(value)
            except ValueError:
                self.trip(f"external actuation ({source}): unrecognised kill-switch flag")
                continue
            if _SEVERITY[requested] > _SEVERITY[self.state]:
                self.set_state(requested, automatic=True, reason=f"external actuation ({source})")
        return self.state

    def _persist(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent, delete=False) as temp:
            json.dump({"state": self._state.value}, temp)
            temp.flush()
            temporary = Path(temp.name)
        temporary.replace(self.path)
