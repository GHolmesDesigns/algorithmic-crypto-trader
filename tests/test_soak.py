"""Soak console: a day is never pass on partial data, and a criterion never passes
without a recorded attestation. Evidence is admin-only and redacted before it saves.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from api.soak import (
    CRITERIA,
    DIGEST_WINDOW,
    READINESS_GATES,
    SOAK_CRITERIA,
    SqlAlchemySoak,
    parse_evidence,
    parse_incident_close,
    parse_incident_open,
)
from api.soak_view import build_soak_view
from api.system_events import DISCONNECT_EVENT, GAP_FILL_EVENT, HEARTBEAT_EVENT, RESTART_EVENT
from api.trends import trends_query
from app.main import create_app
from db.models import (
    Base,
    DiscrepancyRecord,
    EquitySnapshotRecord,
    EvidenceRecord,
    FillRecord,
    IncidentRecord,
    PortfolioSnapshotRecord,
    RiskDecisionRecord,
    SystemEventRecord,
)
from risk.kill_switch_journal import EVENT_TYPE as KILL_SWITCH_EVENT
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from tests.operator_support import history_app, sqlite_settings

OPERATOR_TOKEN = "operator-secret-soak-c94a"
ADMIN_TOKEN = "admin-secret-soak-2b71"
OPERATOR = {"x-operator-token": OPERATOR_TOKEN}
ADMIN = {"x-operator-token": ADMIN_TOKEN}
BROWSER = {**OPERATOR, "accept": "text/html,application/xhtml+xml"}
ADMIN_BROWSER = {**ADMIN, "accept": "text/html,application/xhtml+xml"}
PATH = "/operator/history/soak"
EVIDENCE_PATH = f"{PATH}/evidence"
INCIDENT_OPEN_PATH = f"{PATH}/incidents"
INCIDENT_CLOSE_PATH = f"{PATH}/incidents/close"
NOW = datetime(2026, 9, 27, 14, 23, 45, tzinfo=UTC)


@pytest.fixture(autouse=True)
def environment(monkeypatch, tmp_path):
    monkeypatch.setenv("OPERATOR_TOKEN", OPERATOR_TOKEN)
    monkeypatch.setenv("OPERATOR_ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("KILL_SWITCH_FILE", str(tmp_path / "kill-switch.json"))


def client_for(application) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test")


def database(tmp_path: Path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'trader.db'}", future=True)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def add(tmp_path: Path, *records) -> None:
    engine, session_factory = database(tmp_path)
    with session_factory() as session:
        session.add_all(records)
        session.commit()
    engine.dispose()


def reader(tmp_path: Path) -> SqlAlchemySoak:
    engine, session_factory = database(tmp_path)
    Base.metadata.create_all(engine)
    engine.dispose()
    return SqlAlchemySoak(session_factory)


def fill(at: datetime) -> FillRecord:
    return FillRecord(
        fill_id=uuid4(),
        order_id=uuid4(),
        broker_fill_id=str(uuid4()),
        quantity=Decimal("1"),
        price=Decimal("60000"),
        fee=Decimal("1"),
        occurred_at=at,
    )


def refusal(at: datetime) -> RiskDecisionRecord:
    return RiskDecisionRecord(
        approval_id=uuid4(),
        signal_id=uuid4(),
        approved=False,
        reason="refused at stale_price",
        failed_gate="stale_price",
        correlation_id=uuid4(),
        decided_at=at,
    )


def run(at: datetime) -> PortfolioSnapshotRecord:
    return PortfolioSnapshotRecord(batch_id=uuid4(), source="broker", recorded_at=at)


def discrepancy(at: datetime) -> DiscrepancyRecord:
    return DiscrepancyRecord(
        discrepancy_id=uuid4(),
        entity_type="balance",
        entity_key=str(uuid4()),
        local_payload={"available": "1"},
        broker_payload={"available": "2"},
        safety_action="halted",
        created_at=at,
    )


def kill_switch_event(at: datetime) -> SystemEventRecord:
    return SystemEventRecord(
        event_id=uuid4(),
        event_type=KILL_SWITCH_EVENT,
        correlation_id=uuid4(),
        payload={"from": "running", "to": "halted"},
        created_at=at,
    )


def equity(at: datetime, value: str = "10000") -> EquitySnapshotRecord:
    return EquitySnapshotRecord(
        snapshot_id=uuid4(), equity=Decimal(value), as_of=at, source="broker"
    )


def heartbeat(at: datetime) -> SystemEventRecord:
    return SystemEventRecord(
        event_id=uuid4(),
        event_type=HEARTBEAT_EVENT,
        correlation_id=uuid4(),
        payload={"runtime_status": "running"},
        created_at=at,
    )


def hourly_heartbeats(start: datetime, hours: int = 24) -> list[SystemEventRecord]:
    return [heartbeat(start + timedelta(hours=index)) for index in range(hours)]


def restart(at: datetime, kind: str = "clean") -> SystemEventRecord:
    return SystemEventRecord(
        event_id=uuid4(),
        event_type=RESTART_EVENT,
        correlation_id=uuid4(),
        payload={"kind": kind, "status": "reconciled"},
        created_at=at,
    )


def disconnect(at: datetime) -> SystemEventRecord:
    return SystemEventRecord(
        event_id=uuid4(),
        event_type=DISCONNECT_EVENT,
        correlation_id=uuid4(),
        payload={},
        created_at=at,
    )


def gap_fill_event(at: datetime) -> SystemEventRecord:
    return SystemEventRecord(
        event_id=uuid4(),
        event_type=GAP_FILL_EVENT,
        correlation_id=uuid4(),
        payload={"symbol": "BTC-USD"},
        created_at=at,
    )


def incident(
    opened_at: datetime, *, closed_at: datetime | None = None, cause: str = "drill"
) -> IncidentRecord:
    return IncidentRecord(
        incident_id=uuid4(),
        cause=cause,
        opened_by="admin",
        opened_at=opened_at,
        closed_note="resolved" if closed_at else None,
        closed_by="admin" if closed_at else None,
        closed_at=closed_at,
    )


def evidence(
    criterion: str, status: str, *, note: str = "PR #80, local run green"
) -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id=uuid4(),
        criterion=criterion,
        kind="local",
        status=status,
        note=note,
        recorded_by="admin",
        recorded_at=NOW,
    )


def days_by_date(payload) -> dict:
    return {day["date"]: day for day in payload["days"]}


def all_criteria(payload) -> dict:
    return {
        item["key"]: item for item in payload["criteria"]["soak"] + payload["criteria"]["readiness"]
    }


# Digest


def test_a_day_stays_incomplete_even_with_every_recorded_field_present(tmp_path):
    edges = trends_query(DIGEST_WINDOW, NOW).edges
    at = edges[3]
    soak = reader(tmp_path)
    add(
        tmp_path,
        fill(at),
        refusal(at),
        run(at),
        discrepancy(at),
        kill_switch_event(at),
        equity(at, "12345.5"),
    )
    payload = soak.read(now=NOW)
    day = days_by_date(payload)[at.date().isoformat()]
    assert (day["trades"], day["refusals"], day["reconciliation_runs"]) == (1, 1, 1)
    assert (day["divergences"], day["kill_switch_events"]) == (1, 1)
    assert day["equity"] == "12345.5"
    counts = (day["heartbeats"], day["restarts"], day["disconnects"], day["gap_fills"])
    assert counts == (0, 0, 0, 0)
    assert day["open_incidents"] == 0
    # No heartbeat was recorded for this fully elapsed day, so its uptime has a gap.
    assert set(day["missing"]) == {"uptime"}
    assert day["status"] == "incomplete"


def test_an_empty_day_counts_zero_and_is_still_incomplete(tmp_path):
    soak = reader(tmp_path)
    payload = soak.read(now=NOW)
    for day in payload["days"]:
        assert (day["trades"], day["refusals"], day["reconciliation_runs"]) == (0, 0, 0)
        assert (day["divergences"], day["kill_switch_events"]) == (0, 0)
        assert day["equity"] is None
        assert "equity" in day["missing"]
        assert day["status"] == "incomplete"
    assert len(payload["days"]) == 30
    assert payload["days"][-1]["in_progress"] is True
    assert payload["days"][0]["in_progress"] is False


def test_full_heartbeat_coverage_one_clean_restart_and_no_open_incident_reads_pass(tmp_path):
    edges = trends_query(DIGEST_WINDOW, NOW).edges
    start = edges[3]
    soak = reader(tmp_path)
    add(tmp_path, *hourly_heartbeats(start), restart(start, "clean"), equity(start))
    payload = soak.read(now=NOW)
    day = days_by_date(payload)[start.date().isoformat()]
    assert day["heartbeats"] == 24
    assert (day["restarts"], day["recovered_restarts"]) == (1, 0)
    assert (day["disconnects"], day["gap_fills"], day["open_incidents"]) == (0, 0, 0)
    assert day["missing"] == []
    assert day["status"] == "pass"


def test_a_missing_heartbeat_interval_leaves_uptime_incomplete(tmp_path):
    edges = trends_query(DIGEST_WINDOW, NOW).edges
    start = edges[3]
    soak = reader(tmp_path)
    # 23 of the 24 expected hourly samples: the hour at index 12 never reported.
    samples = [sample for index, sample in enumerate(hourly_heartbeats(start)) if index != 12]
    add(tmp_path, *samples, equity(start))
    payload = soak.read(now=NOW)
    day = days_by_date(payload)[start.date().isoformat()]
    assert day["heartbeats"] == 23
    assert "uptime" in day["missing"]
    assert day["status"] == "incomplete"


def test_uptime_is_not_held_to_a_full_day_while_it_is_still_in_progress(tmp_path):
    edges = trends_query(DIGEST_WINDOW, NOW).edges
    today = edges[-2]
    soak = reader(tmp_path)
    # NOW is 14:23:45; only the first 14 hourly intervals have fully elapsed.
    add(tmp_path, *hourly_heartbeats(today, hours=14))
    payload = soak.read(now=NOW)
    day = days_by_date(payload)[today.date().isoformat()]
    assert day["in_progress"] is True
    assert "uptime" not in day["missing"]


def test_a_restart_is_recorded_and_distinguishes_clean_from_recovered(tmp_path):
    edges = trends_query(DIGEST_WINDOW, NOW).edges
    start = edges[3]
    soak = reader(tmp_path)
    add(tmp_path, restart(start, "clean"), restart(start + timedelta(hours=1), "recovered"))
    payload = soak.read(now=NOW)
    day = days_by_date(payload)[start.date().isoformat()]
    assert (day["restarts"], day["recovered_restarts"]) == (2, 1)


def test_a_disconnect_and_its_gap_fill_are_both_recorded(tmp_path):
    edges = trends_query(DIGEST_WINDOW, NOW).edges
    start = edges[3]
    soak = reader(tmp_path)
    add(tmp_path, disconnect(start), gap_fill_event(start))
    payload = soak.read(now=NOW)
    day = days_by_date(payload)[start.date().isoformat()]
    assert (day["disconnects"], day["gap_fills"]) == (1, 1)


def test_two_disconnects_do_not_mark_a_complete_day_for_storm_review(tmp_path):
    edges = trends_query(DIGEST_WINDOW, NOW).edges
    start = edges[3]
    soak = reader(tmp_path)
    add(
        tmp_path,
        *hourly_heartbeats(start),
        restart(start, "clean"),
        equity(start),
        disconnect(start),
        disconnect(start + timedelta(seconds=30)),
    )

    day = days_by_date(soak.read(now=NOW))[start.date().isoformat()]

    assert day["disconnects"] == 2
    assert day["reconnect_storm"] is False
    assert day["status"] == "pass"


def test_three_disconnects_in_the_window_mark_a_day_incomplete_for_review(tmp_path):
    edges = trends_query(DIGEST_WINDOW, NOW).edges
    start = edges[3]
    soak = reader(tmp_path)
    add(
        tmp_path,
        *hourly_heartbeats(start),
        restart(start, "clean"),
        equity(start),
        disconnect(start),
        disconnect(start + timedelta(seconds=30)),
        disconnect(start + timedelta(seconds=60)),
    )

    day = days_by_date(soak.read(now=NOW))[start.date().isoformat()]

    assert day["disconnects"] == 3
    assert day["reconnect_storm"] is True
    assert day["review"] == "reconnect storm"
    assert "reconnect_storm" in day["missing"]
    assert day["status"] == "incomplete"

    view = build_soak_view(payload=soak.read(now=NOW), now=NOW)
    viewed_day = next(item for item in view["days"] if item["date"] == start.date().isoformat())
    assert viewed_day["review"] == "reconnect storm"
    assert "Reconnect storm review" in viewed_day["missing"]


def test_an_open_incident_leaves_its_day_incomplete_until_it_is_closed(tmp_path):
    edges = trends_query(DIGEST_WINDOW, NOW).edges
    start = edges[3]
    soak = reader(tmp_path)
    add(tmp_path, incident(start))
    open_payload = soak.read(now=NOW)
    open_day = days_by_date(open_payload)[start.date().isoformat()]
    assert open_day["open_incidents"] == 1
    assert "incidents" in open_day["missing"]

    engine, session_factory = database(tmp_path)
    with session_factory() as session:
        record = session.scalars(select(IncidentRecord)).one()
        record.closed_at = start + timedelta(hours=1)
        record.closed_by = "admin"
        record.closed_note = "resolved"
        session.add(record)
        session.commit()
    engine.dispose()

    closed_payload = soak.read(now=NOW)
    closed_day = days_by_date(closed_payload)[start.date().isoformat()]
    assert closed_day["open_incidents"] == 0
    assert "incidents" not in closed_day["missing"]


# Incidents


def test_parse_incident_open_refuses_an_empty_or_too_long_cause():
    assert parse_incident_open({"cause": ""}).errors
    assert parse_incident_open({"cause": "x" * 501}).errors
    assert not parse_incident_open({"cause": "the feed stalled for ten minutes"}).errors


def test_parse_incident_close_refuses_a_missing_or_invalid_reference():
    assert parse_incident_close({"incident_id": "", "note": "fixed"}).errors
    assert parse_incident_close({"incident_id": "not-a-uuid", "note": "fixed"}).errors
    assert parse_incident_close({"incident_id": str(uuid4()), "note": ""}).errors
    assert parse_incident_close({"incident_id": str(uuid4()), "note": "x" * 501}).errors
    valid = parse_incident_close({"incident_id": str(uuid4()), "note": "fixed"})
    assert not valid.errors


def test_opening_and_closing_an_incident_redacts_both_notes(tmp_path):
    soak = reader(tmp_path)
    cause = "Reported by ops@example.test; api_key=abc123 caused the stall"
    opened = soak.open_incident(cause, opened_by="admin", now=NOW)
    assert "ops@example.test" not in opened["cause"]
    assert "abc123" not in opened["cause"]

    note = "Fixed by ops@example.test after rotating token=super-secret-value"
    closed = soak.close_incident(
        UUID(opened["incident_id"]), note, closed_by="admin", now=NOW + timedelta(hours=1)
    )
    assert closed is not None
    assert "ops@example.test" not in closed["closed_note"]
    assert "super-secret-value" not in closed["closed_note"]
    assert closed["closed_by"] == "admin"


def test_closing_an_unknown_or_already_closed_incident_returns_none(tmp_path):
    soak = reader(tmp_path)
    assert soak.close_incident(uuid4(), "fixed", closed_by="admin", now=NOW) is None
    opened = soak.open_incident("cause", opened_by="admin", now=NOW)
    incident_id = UUID(opened["incident_id"])
    first = soak.close_incident(incident_id, "fixed", closed_by="admin", now=NOW)
    assert first is not None
    second = soak.close_incident(incident_id, "fixed again", closed_by="admin", now=NOW)
    assert second is None


@pytest.mark.asyncio
async def test_operator_cannot_open_or_close_an_incident(tmp_path):
    application = history_app(tmp_path)
    async with client_for(application) as client:
        opened = await client.post(
            INCIDENT_OPEN_PATH, headers=OPERATOR, data={"cause": "the feed stalled"}
        )
        assert opened.status_code == 403
        closed = await client.post(
            INCIDENT_CLOSE_PATH,
            headers=OPERATOR,
            data={"incident_id": str(uuid4()), "note": "fixed"},
        )
        assert closed.status_code == 403
    engine, session_factory = database(tmp_path)
    with session_factory() as session:
        assert session.scalars(select(IncidentRecord)).all() == []
    engine.dispose()


@pytest.mark.asyncio
async def test_administrator_can_open_and_close_an_incident(tmp_path):
    application = history_app(tmp_path)
    async with client_for(application) as client:
        opened = await client.post(
            INCIDENT_OPEN_PATH, headers=ADMIN, data={"cause": "the feed stalled for ten minutes"}
        )
        assert opened.status_code == 200
        page = await client.get(PATH, headers=ADMIN)
        [incident_view] = page.json()["open_incidents"]
        incident_id = incident_view["incident_id"]
        closed = await client.post(
            INCIDENT_CLOSE_PATH,
            headers=ADMIN,
            data={"incident_id": incident_id, "note": "reconnected and gap-filled"},
        )
        assert closed.status_code == 200
        page = await client.get(PATH, headers=ADMIN)
        assert page.json()["open_incidents"] == []


@pytest.mark.asyncio
async def test_closing_an_incident_with_no_note_is_refused_via_the_route(tmp_path):
    application = history_app(tmp_path)
    async with client_for(application) as client:
        response = await client.post(
            INCIDENT_CLOSE_PATH,
            headers=ADMIN,
            data={"incident_id": str(uuid4()), "note": ""},
        )
        assert response.status_code == 422
        assert response.json()["detail"]["status"] == "refused"


@pytest.mark.asyncio
async def test_closing_an_unknown_incident_is_refused_via_the_route(tmp_path):
    application = history_app(tmp_path)
    async with client_for(application) as client:
        response = await client.post(
            INCIDENT_CLOSE_PATH,
            headers=ADMIN,
            data={"incident_id": str(uuid4()), "note": "fixed"},
        )
        assert response.status_code == 422
        assert response.json()["detail"]["status"] == "refused"
        page = await client.post(
            INCIDENT_CLOSE_PATH,
            headers=ADMIN_BROWSER,
            data={"incident_id": str(uuid4()), "note": "fixed"},
        )
        assert page.status_code == 422
        assert "could not be closed" in page.text


@pytest.mark.asyncio
async def test_the_rendered_page_never_leaks_a_redacted_incident_cause(tmp_path):
    application = history_app(tmp_path)
    cause = "Seen by ops@example.test; token=super-secret-incident-value caused the stall"
    engine, session_factory = database(tmp_path)
    SqlAlchemySoak(session_factory).open_incident(cause, opened_by="admin", now=NOW)
    engine.dispose()
    async with client_for(application) as client:
        page = await client.get(PATH, headers=ADMIN_BROWSER)
    assert "ops@example.test" not in page.text
    assert "super-secret-incident-value" not in page.text


@pytest.mark.asyncio
async def test_opening_an_incident_with_an_empty_cause_is_refused_via_the_route(tmp_path):
    application = history_app(tmp_path)
    async with client_for(application) as client:
        response = await client.post(INCIDENT_OPEN_PATH, headers=ADMIN, data={"cause": ""})
        assert response.status_code == 422
        assert response.json()["detail"]["status"] == "refused"
        page = await client.post(INCIDENT_OPEN_PATH, headers=ADMIN_BROWSER, data={"cause": ""})
        assert page.status_code == 422
        assert "could not be opened" in page.text
    engine, session_factory = database(tmp_path)
    with session_factory() as session:
        assert session.scalars(select(IncidentRecord)).all() == []
    engine.dispose()


@pytest.mark.asyncio
async def test_administrator_can_open_and_close_an_incident_via_the_browser_form(tmp_path):
    application = history_app(tmp_path)
    async with client_for(application) as client:
        opened = await client.post(
            INCIDENT_OPEN_PATH, headers=ADMIN_BROWSER, data={"cause": "the feed stalled"}
        )
        assert opened.status_code == 303
        assert opened.headers["location"] == PATH
        page = await client.get(PATH, headers=ADMIN)
        [incident_view] = page.json()["open_incidents"]
        closed = await client.post(
            INCIDENT_CLOSE_PATH,
            headers=ADMIN_BROWSER,
            data={"incident_id": incident_view["incident_id"], "note": "reconnected"},
        )
        assert closed.status_code == 303
        assert closed.headers["location"] == PATH


@pytest.mark.asyncio
async def test_a_write_failure_answers_unavailable_for_incident_open_and_close(tmp_path):
    application = history_app(tmp_path)
    no_tables = create_engine(f"sqlite+pysqlite:///{tmp_path / 'none.db'}", future=True)
    application.state.soak = SqlAlchemySoak(sessionmaker(bind=no_tables, expire_on_commit=False))
    async with client_for(application) as client:
        opened = await client.post(
            INCIDENT_OPEN_PATH, headers=ADMIN, data={"cause": "the feed stalled"}
        )
        assert opened.status_code == 503
        assert opened.json()["detail"]["status"] == "refused"
        opened_page = await client.post(
            INCIDENT_OPEN_PATH, headers=ADMIN_BROWSER, data={"cause": "the feed stalled"}
        )
        assert opened_page.status_code == 503
        assert "Not available" in opened_page.text

        closed = await client.post(
            INCIDENT_CLOSE_PATH,
            headers=ADMIN,
            data={"incident_id": str(uuid4()), "note": "fixed"},
        )
        assert closed.status_code == 503
        assert closed.json()["detail"]["status"] == "refused"


# Criterion tracker


def test_every_criterion_starts_incomplete_with_no_evidence(tmp_path):
    soak = reader(tmp_path)
    payload = soak.read(now=NOW)
    criteria = all_criteria(payload)
    assert set(criteria) == set(CRITERIA)
    for item in criteria.values():
        assert item["status"] == "incomplete"
        assert item["evidence"] is None


def test_the_latest_evidence_record_decides_pass_or_fail(tmp_path):
    key = SOAK_CRITERIA[0][0]
    older = evidence(key, "pass")
    older.recorded_at = NOW.replace(hour=1)
    newer = evidence(key, "fail")
    newer.recorded_at = NOW.replace(hour=12)
    soak = reader(tmp_path)
    add(tmp_path, older, newer)
    payload = soak.read(now=NOW)
    tracked = all_criteria(payload)[key]
    assert tracked["status"] == "fail"
    assert tracked["evidence"]["status"] == "fail"


def test_a_readiness_gate_is_tracked_the_same_way(tmp_path):
    key = READINESS_GATES[0][0]
    soak = reader(tmp_path)
    add(tmp_path, evidence(key, "pass"))
    payload = soak.read(now=NOW)
    assert all_criteria(payload)[key]["status"] == "pass"


# Evidence validation and redaction


def test_parse_evidence_refuses_unknown_or_missing_fields():
    cases = [
        {"criterion": "not-a-real-criterion", "kind": "local", "status": "pass", "note": "x"},
        {"criterion": SOAK_CRITERIA[0][0], "kind": "made-up", "status": "pass", "note": "x"},
        {"criterion": SOAK_CRITERIA[0][0], "kind": "local", "status": "maybe", "note": "x"},
        {"criterion": SOAK_CRITERIA[0][0], "kind": "local", "status": "pass", "note": ""},
        {"criterion": SOAK_CRITERIA[0][0], "kind": "local", "status": "pass", "note": "x" * 501},
    ]
    for form in cases:
        submitted = parse_evidence(form)
        assert submitted.errors, form


def test_recorded_evidence_is_redacted_before_it_is_stored(tmp_path):
    soak = reader(tmp_path)
    key = SOAK_CRITERIA[0][0]
    note = "Confirmed by ops@example.test; api_key=abc123 see https://ntfy.example.test/secret"
    result = soak.record_evidence(key, "local", "pass", note, recorded_by="admin", now=NOW)
    assert "ops@example.test" not in result["note"]
    assert "abc123" not in result["note"]
    assert "ntfy.example.test" not in result["note"]
    engine, session_factory = database(tmp_path)
    with session_factory() as session:
        stored = session.scalars(select(EvidenceRecord)).one()
    engine.dispose()
    assert "ops@example.test" not in stored.note
    assert "abc123" not in stored.note


# Routes: read is bounded, write is admin-only


@pytest.mark.asyncio
async def test_the_view_takes_no_parameters(tmp_path):
    application = history_app(tmp_path)
    async with client_for(application) as client:
        response = await client.get(PATH, headers=OPERATOR, params={"window": "7d"})
        assert response.status_code == 422
        assert response.json()["detail"]["status"] == "refused"
        page = await client.get(PATH, headers=BROWSER, params={"window": "7d"})
        assert page.status_code == 422
        assert "Request refused. Nothing was read." in page.text


@pytest.mark.asyncio
async def test_unavailable_when_the_soak_state_is_not_attached(tmp_path):
    application = create_app(sqlite_settings(tmp_path))
    async with client_for(application) as client:
        response = await client.get(PATH, headers=OPERATOR)
        assert response.status_code == 503
        assert (
            response.json()["detail"]["reason"]
            == "the soak console is not configured in this process"
        )
        page = await client.get(PATH, headers=BROWSER)
        assert page.status_code == 503
        assert "Not available" in page.text


@pytest.mark.asyncio
async def test_operator_can_read_but_not_record_evidence(tmp_path):
    application = history_app(tmp_path)
    key = SOAK_CRITERIA[0][0]
    async with client_for(application) as client:
        read = await client.get(PATH, headers=OPERATOR)
        assert read.status_code == 200
        refused = await client.post(
            EVIDENCE_PATH,
            headers=OPERATOR,
            data={"criterion": key, "kind": "local", "status": "pass", "note": "PR #1"},
        )
        assert refused.status_code == 403
    engine, session_factory = database(tmp_path)
    with session_factory() as session:
        assert session.scalars(select(EvidenceRecord)).all() == []
    engine.dispose()


@pytest.mark.asyncio
async def test_administrator_can_record_evidence_and_it_appears_in_the_tracker(tmp_path):
    application = history_app(tmp_path)
    key = SOAK_CRITERIA[0][0]
    async with client_for(application) as client:
        response = await client.post(
            EVIDENCE_PATH,
            headers=ADMIN,
            data={"criterion": key, "kind": "ci", "status": "pass", "note": "PR #80 CI green"},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "recorded"
        page = await client.get(PATH, headers=ADMIN)
        assert page.json()["criteria"]["soak"][0]["status"] == "pass"


@pytest.mark.asyncio
async def test_an_invalid_evidence_submission_is_refused_and_saves_nothing(tmp_path):
    application = history_app(tmp_path)
    async with client_for(application) as client:
        response = await client.post(
            EVIDENCE_PATH,
            headers=ADMIN,
            data={"criterion": "unknown", "kind": "local", "status": "pass", "note": "x"},
        )
        assert response.status_code == 422
        assert response.json()["detail"]["status"] == "refused"
        page = await client.post(
            EVIDENCE_PATH,
            headers=ADMIN_BROWSER,
            data={"criterion": "unknown", "kind": "local", "status": "pass", "note": "x"},
        )
        assert page.status_code == 422
        assert "Evidence refused. Nothing was saved." in page.text
    engine, session_factory = database(tmp_path)
    with session_factory() as session:
        assert session.scalars(select(EvidenceRecord)).all() == []
    engine.dispose()


@pytest.mark.asyncio
async def test_evidence_form_is_shown_only_to_administrators(tmp_path):
    application = history_app(tmp_path)
    async with client_for(application) as client:
        operator_page = await client.get(PATH, headers=BROWSER)
        admin_page = await client.get(PATH, headers=ADMIN_BROWSER)
    assert 'action="/operator/history/soak/evidence"' not in operator_page.text
    assert 'action="/operator/history/soak/evidence"' in admin_page.text


@pytest.mark.asyncio
async def test_the_rendered_page_never_leaks_a_redacted_secret(tmp_path):
    application = history_app(tmp_path)
    key = SOAK_CRITERIA[0][0]
    note = "See ops@example.test and token=super-secret-value-123 for the run"
    engine, session_factory = database(tmp_path)
    SqlAlchemySoak(session_factory).record_evidence(
        key, "local", "pass", note, recorded_by="admin", now=NOW
    )
    engine.dispose()
    async with client_for(application) as client:
        page = await client.get(PATH, headers=ADMIN_BROWSER)
    assert "ops@example.test" not in page.text
    assert "super-secret-value-123" not in page.text


@pytest.mark.asyncio
async def test_administrator_can_record_evidence_via_the_browser_form(tmp_path):
    application = history_app(tmp_path)
    key = SOAK_CRITERIA[0][0]
    async with client_for(application) as client:
        response = await client.post(
            EVIDENCE_PATH,
            headers=ADMIN_BROWSER,
            data={"criterion": key, "kind": "ci", "status": "pass", "note": "PR #80 CI green"},
        )
    assert response.status_code == 303
    assert response.headers["location"] == PATH


@pytest.mark.asyncio
async def test_a_write_failure_and_its_fallback_reread_both_answer_unavailable(tmp_path):
    application = history_app(tmp_path)
    no_tables = create_engine(f"sqlite+pysqlite:///{tmp_path / 'none.db'}", future=True)
    application.state.soak = SqlAlchemySoak(sessionmaker(bind=no_tables, expire_on_commit=False))
    key = SOAK_CRITERIA[0][0]
    form = {"criterion": key, "kind": "ci", "status": "pass", "note": "PR #80 CI green"}
    async with client_for(application) as client:
        response = await client.post(EVIDENCE_PATH, headers=ADMIN, data=form)
        assert response.status_code == 503
        assert response.json()["detail"]["status"] == "refused"
        page = await client.post(EVIDENCE_PATH, headers=ADMIN_BROWSER, data=form)
    assert page.status_code == 503
    assert "Not available" in page.text
