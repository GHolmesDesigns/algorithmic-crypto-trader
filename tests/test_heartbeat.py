"""Bounded, periodic heartbeat persistence: never every tick, always the same event type."""

from __future__ import annotations

import asyncio
import time

import pytest
from api.operator import OperatorState
from api.system_events import HEARTBEAT_EVENT
from app.heartbeat import HeartbeatScheduler
from core.guards import CredentialScope, StartupSettings
from core.models import TradingMode
from risk.kill_switch import KillSwitch

# A bound on waiting for the loop, so a real hang fails the test and never stalls the suite.
WAIT_SECONDS = 5


class FakeJournal:
    def __init__(self, *, wanted: int = 0) -> None:
        self.events: list[tuple[str, dict]] = []
        # Set once ``wanted`` samples are saved, so a test waits for progress, not for the clock.
        self.wanted = wanted
        self.enough = asyncio.Event()

    def record(self, event_type, payload, *, now=None) -> None:
        self.events.append((event_type, dict(payload)))
        if self.wanted and len(self.events) >= self.wanted:
            self.enough.set()


async def run_until_enough(scheduler: HeartbeatScheduler, journal: FakeJournal) -> None:
    """Run the loop until the journal has the samples the test needs, then stop it."""

    stop = asyncio.Event()
    task = asyncio.create_task(scheduler.run(stop))
    try:
        await asyncio.wait_for(journal.enough.wait(), timeout=WAIT_SECONDS)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=WAIT_SECONDS)


def operator_state(tmp_path) -> OperatorState:
    return OperatorState(
        settings=StartupSettings(
            TradingMode.PAPER, CredentialScope.VIEW, "", "postgresql://unused", "INFO"
        ),
        kill_switch=KillSwitch(tmp_path / "switch.json"),
    )


def test_heartbeat_interval_must_be_positive(tmp_path) -> None:
    with pytest.raises(ValueError):
        HeartbeatScheduler(operator_state(tmp_path), FakeJournal(), interval_seconds=0)


def test_sample_persists_one_heartbeat_event_with_the_current_snapshot(tmp_path) -> None:
    state = operator_state(tmp_path)
    state.heartbeat("primary", status="healthy", detail="ok")
    journal = FakeJournal()
    scheduler = HeartbeatScheduler(state, journal, interval_seconds=3600)

    scheduler.sample()

    [(event_type, payload)] = journal.events
    assert event_type == HEARTBEAT_EVENT
    assert payload["runtime_status"] == state.snapshot.runtime_status
    [strategy] = payload["strategies"]
    assert strategy["name"] == "primary"
    assert strategy["status"] == "healthy"
    assert strategy["last_seen"] is not None


@pytest.mark.asyncio
async def test_run_samples_on_each_interval_until_stopped(tmp_path) -> None:
    journal = FakeJournal(wanted=2)
    scheduler = HeartbeatScheduler(operator_state(tmp_path), journal, interval_seconds=0.01)

    await run_until_enough(scheduler, journal)

    assert len(journal.events) >= 2
    assert all(event_type == HEARTBEAT_EVENT for event_type, _ in journal.events)


@pytest.mark.asyncio
async def test_run_stops_when_told_and_takes_no_further_sample(tmp_path) -> None:
    journal = FakeJournal(wanted=1)
    scheduler = HeartbeatScheduler(operator_state(tmp_path), journal, interval_seconds=0.01)

    await run_until_enough(scheduler, journal)
    taken = len(journal.events)
    await asyncio.sleep(0.05)

    assert len(journal.events) == taken


@pytest.mark.asyncio
async def test_run_still_samples_when_the_first_sample_stalls_the_loop(tmp_path) -> None:
    # A stall on a busy machine (a garbage collection, coverage tracing, antivirus) left a fixed
    # 0.05 s wait with fewer than two samples. Waiting for the samples does not depend on it.
    class StallingJournal(FakeJournal):
        def record(self, event_type, payload, *, now=None) -> None:
            if not self.events:
                time.sleep(0.3)
            super().record(event_type, payload, now=now)

    journal = StallingJournal(wanted=2)
    scheduler = HeartbeatScheduler(operator_state(tmp_path), journal, interval_seconds=0.01)

    await run_until_enough(scheduler, journal)

    assert len(journal.events) >= 2


@pytest.mark.asyncio
async def test_run_keeps_the_schedule_after_a_failed_sample(tmp_path) -> None:
    class FailingOnceJournal(FakeJournal):
        def __init__(self, **options) -> None:
            super().__init__(**options)
            self.calls = 0

        def record(self, event_type, payload, *, now=None) -> None:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("database is unavailable")
            super().record(event_type, payload, now=now)

    journal = FailingOnceJournal(wanted=1)
    scheduler = HeartbeatScheduler(operator_state(tmp_path), journal, interval_seconds=0.01)

    await run_until_enough(scheduler, journal)

    assert journal.calls >= 2
    assert journal.events  # a later sample succeeded despite the first failure
