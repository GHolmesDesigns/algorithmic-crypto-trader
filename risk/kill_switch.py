"""Persistent, restart-safe kill switch with independent external actuation paths."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import RLock
from typing import Any, Protocol
from uuid import uuid4

from core.models import KillSwitchState, utc_now

_SEVERITY = {KillSwitchState.RUNNING: 0, KillSwitchState.PAUSED: 1, KillSwitchState.HALTED: 2}
HISTORY_LIMIT = 50

logger = logging.getLogger(__name__)


class TransitionJournal(Protocol):
    """Durable store for kill-switch transitions."""

    def record(self, events: Sequence[Mapping[str, Any]]) -> None:
        """Save every event not saved yet, all or none."""

    def recent(self, limit: int) -> list[dict[str, Any]]:
        """Return up to ``limit`` of the newest events, oldest first."""


class TransitionNotRecorded(RuntimeError):
    """A re-arm was refused because its transition could not be recorded."""


class KillSwitch:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._lock = RLock()
        self._state = KillSwitchState.RUNNING
        self.journal: TransitionJournal | None = None
        # Oldest first: transitions loaded from the journal, then this process's.
        self.audit_events: list[dict[str, Any]] = []
        self._unrecorded: list[dict[str, Any]] = []
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

    def attach_journal(self, journal: TransitionJournal) -> None:
        """Record transitions in ``journal`` from now on and load the recent history.

        An unreadable history is logged and left empty; stops still work, and a
        re-arm still has to record its own transition before the switch lowers.
        """

        with self._lock:
            self.journal = journal
            try:
                loaded = journal.recent(HISTORY_LIMIT)
            except Exception:
                logger.exception("kill-switch history could not be loaded")
                loaded = []
            known = {event.get("event_id") for event in loaded}
            self.audit_events = loaded + [
                event for event in self.audit_events if event.get("event_id") not in known
            ]
            if self._unrecorded:
                self._flush()

    def set_state(
        self,
        state: KillSwitchState,
        *,
        automatic: bool = False,
        reason: str = "",
        actor: str | None = None,
        checklist: Sequence[str] = (),
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
            if state is not KillSwitchState.RUNNING and _SEVERITY[state] < _SEVERITY[self._state]:
                raise PermissionError("only a manual re-arm can lower the kill switch")
            previous = self._state
            if state is previous:
                # Rewrite even an unchanged state, so a lost file is restored instead of
                # reading back as running after a restart.
                self._persist()
                return
            event: dict[str, Any] = {
                "event_id": str(uuid4()),
                "event": "kill_switch_transition",
                "from": previous.value,
                "to": state.value,
                "actor": actor or ("system" if automatic else "operator"),
                "automatic": automatic,
                "reason": reason,
                "checklist": list(checklist),
                "created_at": utc_now().isoformat(),
            }
            if _SEVERITY[state] < _SEVERITY[previous]:
                # Lowering is recorded first: a re-arm that cannot be recorded does not happen.
                self._record(event, required=True)
                self._state = state
                self._persist()
            else:
                # Raising takes effect first: a stop never waits on the database.
                self._state = state
                self._persist()
                self._record(event, required=False)

    def rearm(self, *, actor: str, reason: str, checklist: Sequence[str]) -> bool:
        """Lower the switch to running after an administrator review.

        Returns whether anything changed. Refuses, leaving the state as it is, when no
        journal is attached or the transition cannot be recorded.
        """

        with self._lock:
            if self.journal is None:
                raise TransitionNotRecorded("kill-switch transition history is not configured")
            if self._state is KillSwitchState.RUNNING:
                self._persist()
                return False
            self.set_state(KillSwitchState.RUNNING, reason=reason, actor=actor, checklist=checklist)
            return True

    def tighten(
        self, state: KillSwitchState, *, reason: str = "", actor: str | None = None
    ) -> bool:
        """Raise severity to ``state`` and return whether anything changed.

        A request at or below the current severity leaves the state as it is, so this
        path can never turn a halt into a pause. Only a re-arm lowers the switch.
        """

        with self._lock:
            if _SEVERITY[state] > _SEVERITY[self._state]:
                self.set_state(state, reason=reason, actor=actor)
                return True
            self._persist()
            return False

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

    def _record(self, event: dict[str, Any], *, required: bool) -> None:
        """Save ``event`` with any earlier transitions that are still unsaved.

        Without a journal the event stays in memory until one is attached.
        """

        self._unrecorded.append(event)
        if self.journal is not None and not self._flush():
            if required:
                self._unrecorded.remove(event)
                raise TransitionNotRecorded("kill-switch transition could not be recorded")
        self.audit_events.append(event)

    def _flush(self) -> bool:
        assert self.journal is not None
        try:
            self.journal.record(tuple(self._unrecorded))
        except Exception:
            logger.exception(
                "kill-switch transition history could not be saved; %d transition(s) pending",
                len(self._unrecorded),
            )
            return False
        self._unrecorded.clear()
        return True

    def _persist(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent, delete=False) as temp:
            json.dump({"state": self._state.value}, temp)
            temp.flush()
            temporary = Path(temp.name)
        temporary.replace(self.path)
