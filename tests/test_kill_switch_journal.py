"""Kill-switch transitions are durable: stops never wait on the database, re-arms always do."""

import logging

import pytest
from core.models import KillSwitchState
from db.models import SystemEventRecord
from risk.kill_switch import KillSwitch, TransitionNotRecorded
from risk.kill_switch_journal import SqlAlchemyKillSwitchJournal
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

CHECKLIST = ("cause_documented",)


class MemoryJournal:
    def __init__(self, history=()):
        self.saved: list[dict] = list(history)
        self.fail = False
        self.calls = 0

    def record(self, events):
        self.calls += 1
        if self.fail:
            raise RuntimeError("database is down")
        known = {event["event_id"] for event in self.saved}
        self.saved += [dict(event) for event in events if event["event_id"] not in known]

    def recent(self, limit):
        if self.fail:
            raise RuntimeError("database is down")
        return [dict(event) for event in self.saved[-limit:]]


def changes(events):
    return [(event["from"], event["to"]) for event in events]


def test_a_stop_takes_effect_when_the_journal_fails_and_is_saved_later(tmp_path, caplog):
    journal = MemoryJournal()
    switch = KillSwitch(tmp_path / "kill-switch.json")
    switch.attach_journal(journal)
    journal.fail = True

    with caplog.at_level(logging.ERROR, logger="risk.kill_switch"):
        switch.trip("reconciliation divergence")

    assert switch.state is KillSwitchState.HALTED
    assert KillSwitch(tmp_path / "kill-switch.json").state is KillSwitchState.HALTED
    assert journal.saved == []
    assert "1 transition(s) pending" in caplog.text
    journal.fail = False
    switch.rearm(actor="admin", reason="INC-1 approved", checklist=CHECKLIST)
    # The re-arm saves the halt it follows first, so history never has a gap.
    assert changes(journal.saved) == [("running", "halted"), ("halted", "running")]
    assert journal.saved[1]["actor"] == "admin"
    assert journal.saved[1]["checklist"] == list(CHECKLIST)


def test_a_rearm_that_cannot_be_saved_leaves_the_switch_halted(tmp_path):
    journal = MemoryJournal()
    switch = KillSwitch(tmp_path / "kill-switch.json")
    switch.attach_journal(journal)
    switch.trip("startup recovery: divergence")
    journal.fail = True

    with pytest.raises(TransitionNotRecorded):
        switch.rearm(actor="admin", reason="INC-2", checklist=CHECKLIST)

    assert switch.state is KillSwitchState.HALTED
    assert KillSwitch(tmp_path / "kill-switch.json").state is KillSwitchState.HALTED
    assert changes(switch.audit_events) == [("running", "halted")]
    journal.fail = False
    assert switch.rearm(actor="admin", reason="INC-2", checklist=CHECKLIST) is True
    assert changes(journal.saved) == [("running", "halted"), ("halted", "running")]


def test_a_rearm_needs_a_journal(tmp_path):
    switch = KillSwitch(tmp_path / "kill-switch.json")
    switch.set_state(KillSwitchState.PAUSED, reason="operator pause")

    with pytest.raises(TransitionNotRecorded):
        switch.rearm(actor="admin", reason="INC-3", checklist=CHECKLIST)

    assert switch.state is KillSwitchState.PAUSED


def test_rearm_while_running_changes_nothing():
    journal = MemoryJournal()
    switch = KillSwitch()
    switch.attach_journal(journal)
    assert switch.rearm(actor="admin", reason="INC-4", checklist=CHECKLIST) is False
    assert journal.saved == [] and switch.audit_events == []


def test_attaching_loads_history_and_saves_earlier_transitions():
    journal = MemoryJournal()
    earlier = KillSwitch()
    earlier.attach_journal(journal)
    earlier.trip("first process halt")
    restarted = KillSwitch()
    restarted.set_state(KillSwitchState.PAUSED, reason="before the journal was attached")

    restarted.attach_journal(journal)

    assert changes(restarted.audit_events) == [("running", "halted"), ("running", "paused")]
    assert changes(journal.saved) == [("running", "halted"), ("running", "paused")]


def test_unreadable_history_is_empty_but_the_journal_still_guards_rearm(caplog):
    journal = MemoryJournal()
    journal.fail = True
    switch = KillSwitch()
    with caplog.at_level(logging.ERROR, logger="risk.kill_switch"):
        switch.attach_journal(journal)
    assert "history could not be loaded" in caplog.text
    assert switch.journal is journal and switch.audit_events == []
    switch.trip("halt while the database is down")
    with pytest.raises(TransitionNotRecorded):
        switch.rearm(actor="admin", reason="INC-5", checklist=CHECKLIST)
    assert switch.state is KillSwitchState.HALTED


def sql_journal(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'trader.db'}", future=True)
    SystemEventRecord.__table__.create(engine)
    return engine, SqlAlchemyKillSwitchJournal(sessionmaker(bind=engine))


def test_sql_journal_saves_each_transition_once_and_reads_the_newest(tmp_path):
    engine, journal = sql_journal(tmp_path)
    switch = KillSwitch()
    switch.attach_journal(journal)
    switch.tighten(KillSwitchState.PAUSED, reason="operator pause", actor="operator")
    switch.trip("scheduled reconciliation failed")
    switch.rearm(actor="admin", reason="INC-6", checklist=CHECKLIST)

    # A retry after an ambiguous commit resends saved events; nothing is duplicated.
    journal.record(switch.audit_events)

    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(SystemEventRecord)) == 3
    assert changes(journal.recent(10)) == [
        ("running", "paused"),
        ("paused", "halted"),
        ("halted", "running"),
    ]
    assert changes(journal.recent(2)) == [("paused", "halted"), ("halted", "running")]
    newest = journal.recent(1)[0]
    assert newest["actor"] == "admin" and newest["automatic"] is False
    assert newest["reason"] == "INC-6" and newest["checklist"] == list(CHECKLIST)
    journal.record(())
    engine.dispose()
