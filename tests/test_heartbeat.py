"""Bounded, periodic heartbeat persistence: never every tick, always the same event type."""

from __future__ import annotations

import asyncio

import pytest
from api.operator import OperatorState
from api.system_events import HEARTBEAT_EVENT
from app.heartbeat import HeartbeatScheduler
from core.guards import CredentialScope, StartupSettings
from core.models import TradingMode
from risk.kill_switch import KillSwitch


class FakeJournal:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def record(self, event_type, payload, *, now=None) -> None:
        self.events.append((event_type, dict(payload)))


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
    journal = FakeJournal()
    scheduler = HeartbeatScheduler(operator_state(tmp_path), journal, interval_seconds=0.01)
    stop = asyncio.Event()

    task = asyncio.create_task(scheduler.run(stop))
    await asyncio.sleep(0.05)
    stop.set()
    await task

    assert len(journal.events) >= 2
    assert all(event_type == HEARTBEAT_EVENT for event_type, _ in journal.events)


@pytest.mark.asyncio
async def test_run_keeps_the_schedule_after_a_failed_sample(tmp_path) -> None:
    class FailingOnceJournal(FakeJournal):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def record(self, event_type, payload, *, now=None) -> None:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("database is unavailable")
            super().record(event_type, payload, now=now)

    journal = FailingOnceJournal()
    scheduler = HeartbeatScheduler(operator_state(tmp_path), journal, interval_seconds=0.01)
    stop = asyncio.Event()

    task = asyncio.create_task(scheduler.run(stop))
    await asyncio.sleep(0.05)
    stop.set()
    await task

    assert journal.calls >= 2
    assert journal.events  # a later sample succeeded despite the first failure
