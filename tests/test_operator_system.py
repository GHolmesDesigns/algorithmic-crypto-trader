"""The System page (#176): cause first, evidence-based checks, and no side effects from viewing.

The behaviour protected here: an operator finds the recorded cause, the next safe step, saved
unresolved orders and bounded diagnostics on ``/operator/system``; a check without evidence says
so; viewing never recovers, writes at a provider or moves the kill switch; and every control it
points at is still the existing authenticated route with its own refusals.
"""

from __future__ import annotations

import re
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from api import system_routes
from api.alerts import Alert, AlertRouter
from api.dashboard import ROW_LIMIT, build_dashboard
from api.diagnostics import DiagnosticsUnavailable, SqlAlchemyDiagnostics
from api.system_view import build_system
from core.guards import CredentialScope, StartupSettings
from core.models import KillSwitchState, OrderStatus, TradingMode, utc_now
from execution.engine import ExecutionEngine, InMemoryOrderStore, PersistenceUnavailable
from portfolio.scheduler import ReconciliationStatus

from tests.test_operator_dashboard import (
    ADMIN,
    OPERATOR,
    FailingSink,
    FlakyBroker,
    HealthyBroker,
    RecordingSink,
    Scheduler,
    dashboard_app,
    has_pill,
    section,
)
from tests.test_order_closure import Venue, closing_app, seed_order

BROWSER = {"accept": "text/html,application/xhtml+xml"}
SYSTEM = "/operator/system"
SCRIPT_TAG = re.compile(r"<script[^>]*>", re.IGNORECASE)


@pytest.fixture(autouse=True)
def tokens(monkeypatch, tmp_path):
    monkeypatch.setenv("OPERATOR_TOKEN", "operator-secret")
    monkeypatch.setenv("OPERATOR_ADMIN_TOKEN", "admin-secret")
    monkeypatch.setenv("KILL_SWITCH_FILE", str(tmp_path / "kill-switch.json"))


class ReadOnlyBroker(HealthyBroker):
    """Healthy reads, and a record of anything else the page might ask of the venue."""

    capabilities = SimpleNamespace(balance_increments={})

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def get_balances(self):
        self.calls.append("get_balances")
        return await super().get_balances()

    async def get_positions(self):
        self.calls.append("get_positions")
        return await super().get_positions()

    async def get_order(self, client_order_id):
        self.calls.append("get_order")
        return None

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        raise AssertionError(f"viewing System must not call the broker's {name}")


class IntervalScheduler(Scheduler):
    def __init__(self, status, interval_seconds=60.0):
        super().__init__(status)
        self.interval_seconds = interval_seconds


async def fetch(application, headers=OPERATOR, path=SYSTEM, *, expect=200) -> str:
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(path, headers={**headers, **BROWSER})
    assert response.status_code == expect, (path, response.status_code, response.text)
    return response.text


def step(html: str, title: str) -> str:
    """The one recovery-checklist item with this title."""

    start = html.index(f"{title}</h3>")
    start = html.rindex('<li class="recovery-step', 0, start)
    return html[start : html.index("</li>", start)]


# Access and direct links


@pytest.mark.asyncio
async def test_anonymous_and_unauthenticated_requests_are_refused(monkeypatch):
    application = dashboard_app(monkeypatch, broker=HealthyBroker())
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get(SYSTEM)).status_code == 401
        assert (await client.get(SYSTEM, headers={"x-operator-token": "wrong"})).status_code == 401
        refused = await client.get(f"{SYSTEM}?token=operator-secret")
        assert refused.status_code == 400  # a token never travels in a URL


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [OPERATOR, ADMIN], ids=["operator", "administrator"])
async def test_direct_visit_and_reload_show_the_system_heading_in_the_shared_shell(
    monkeypatch, headers
):
    application = dashboard_app(monkeypatch, broker=ReadOnlyBroker())
    first = await fetch(application, headers)
    reloaded = await fetch(application, headers)
    for html in (first, reloaded):
        assert '<h1 id="system-heading">System</h1>' in html
        assert "<title>System · paper · Operator</title>" in html
        assert '<p class="page-name">System</p>' in html
        assert (
            '<a href="/operator/system" data-module="system" aria-current="page">System</a>' in html
        )
        assert 'id="workspace-nav" aria-label="Primary navigation"' in html
        assert 'data-utc-clock data-server-epoch-ms="' in html
        assert "At page load" in html


@pytest.mark.asyncio
async def test_sections_read_cause_first_then_checks_then_diagnostics_then_detail(monkeypatch):
    application = dashboard_app(monkeypatch, broker=ReadOnlyBroker())
    html = await fetch(application)
    anchors = ("cause", "recovery", "orders", "diagnostics", "health", "history")
    positions = [html.index(f'<section id="{anchor}"') for anchor in anchors]
    assert positions == sorted(positions)
    for anchor in anchors:
        section(html, anchor)
    # Expandable detail, not a wall of rows.
    for details in ("alerts", "safety-history", "errors"):
        assert f'<details class="fold" id="{details}"' in html


@pytest.mark.asyncio
async def test_the_page_is_server_rendered_without_added_scripts(monkeypatch):
    application = dashboard_app(monkeypatch, broker=ReadOnlyBroker())
    html = await fetch(application, ADMIN)
    tags = SCRIPT_TAG.findall(html)
    assert len(tags) == 2  # the display-only clock and sidebar scripts, both same-origin
    assert all('src="/operator/static/' in tag for tag in tags)
    assert "http://" not in html.replace("http://test", "")
    assert "<form" in html and 'action="/operator/emergency-stop"' in html


# Cause and next safe step


@pytest.mark.asyncio
async def test_a_halt_shows_its_recorded_cause_and_next_safe_step(monkeypatch):
    application = dashboard_app(monkeypatch, broker=ReadOnlyBroker())
    application.state.operator_state.scheduled_reconciliation = IntervalScheduler(
        ReconciliationStatus(
            started_at=utc_now() - timedelta(hours=1),
            runs=5,
            clean_runs=4,
            diverged_runs=1,
            last_run_at=utc_now(),
            last_result="diverged",
            last_discrepancies=3,
        )
    )
    application.state.kill_switch.trip("scheduled reconciliation diverged")
    html = await fetch(application)
    cause = section(html, "cause")
    assert has_pill(cause, "halted", "crit")
    assert "<li>Scheduled reconciliation diverged: 3 difference(s) with the broker.</li>" in cause
    assert "<strong>Next safe step:</strong> Leave it halted and escalate." in cause
    assert "Last change:" in cause and "scheduled reconciliation diverged" in cause
    assert "Cause unavailable" not in cause
    # Another recorded condition links to Overview anchors, not to anchors this page lacks.
    assert 'href="/operator/history/discrepancies">Investigate</a>' in cause


@pytest.mark.asyncio
async def test_a_halt_with_no_recorded_cause_says_the_cause_is_unavailable(monkeypatch):
    application = dashboard_app(monkeypatch, broker=ReadOnlyBroker())
    # As if the state file said HALTED at start-up: no transition was recorded to explain it.
    application.state.kill_switch._state = KillSwitchState.HALTED
    html = await fetch(application)
    cause = section(html, "cause")
    assert "Cause unavailable" in cause and "Do not assume it is safe" in cause
    assert "Recorded cause" not in cause
    assert has_pill(step(html, "Read the recorded cause"), "unavailable", "unknown")


@pytest.mark.asyncio
async def test_running_is_never_presented_as_health_or_trading(monkeypatch):
    application = dashboard_app(monkeypatch, broker=ReadOnlyBroker())
    html = await fetch(application)
    cause = section(html, "cause")
    assert has_pill(cause, "running", "ok")
    assert "does not repair a cause, prove health, or show that trading is happening" in cause
    assert "it is not trading readiness" in html
    assert "RUNNING does not repair causes" in section(html, "recovery")


@pytest.mark.asyncio
async def test_an_incomplete_read_model_is_not_reported_as_no_incident():
    complete = {"updated_at": "2026-10-24T12:00:00+00:00", "risk": {"kill_switch": "running"}}
    view = build_system(complete, role="operator", diagnostics={"outcome": "failed"})
    assert view["cause"]["complete"] is False
    assert "incomplete; missing" in view["cause"]["incomplete_detail"]
    assert view["cause"]["no_incident"] is False
    unreadable = build_system(
        {"readable": False}, role="operator", diagnostics={"outcome": "failed"}
    )
    assert "unavailable" in unreadable["cause"]["incomplete_detail"]


# Checks come from evidence


@pytest.mark.asyncio
async def test_checks_without_evidence_are_unavailable_not_verified(monkeypatch):
    application = dashboard_app(monkeypatch, broker=ReadOnlyBroker())
    html = await fetch(application)
    recovery = section(html, "recovery")
    assert has_pill(step(recovery, "Check startup recovery"), "unavailable", "unknown")
    assert has_pill(
        step(recovery, "Review the latest scheduled reconciliation"), "unavailable", "unknown"
    )
    assert "No scheduled reconciliation has run in this process (none is scheduled)." in recovery
    assert "Nothing was compared with a broker at start." in recovery
    # With nothing saved to read, even "no pending orders" is not claimed.
    assert has_pill(step(recovery, "Resolve orders waiting on the venue"), "unavailable", "unknown")


@pytest.mark.asyncio
async def test_a_clean_recent_reconciliation_is_verified_and_an_old_one_is_stale(monkeypatch):
    application = dashboard_app(monkeypatch, broker=ReadOnlyBroker())
    state = application.state.operator_state
    state.scheduled_reconciliation = IntervalScheduler(
        ReconciliationStatus(
            started_at=utc_now() - timedelta(hours=3),
            runs=9,
            clean_runs=9,
            last_run_at=utc_now() - timedelta(seconds=30),
            last_result="clean",
        )
    )
    fresh = step(await fetch(application), "Review the latest scheduled reconciliation")
    assert has_pill(fresh, "verified", "ok")
    assert "a clean result is not proof that the next run will be clean" in fresh

    state.scheduled_reconciliation = IntervalScheduler(
        ReconciliationStatus(
            started_at=utc_now() - timedelta(hours=3),
            runs=9,
            clean_runs=9,
            last_run_at=utc_now() - timedelta(minutes=30),
            last_result="clean",
        )
    )
    old = step(await fetch(application), "Review the latest scheduled reconciliation")
    assert has_pill(old, "stale", "warn") and "verified" not in old
    assert "says nothing about the broker now" in old


@pytest.mark.asyncio
async def test_startup_recovery_is_verified_only_for_process_start(monkeypatch):
    from app.recovery import StartupRecoveryResult

    application = dashboard_app(monkeypatch, broker=ReadOnlyBroker())
    application.state.operator_state.startup_recovery = StartupRecoveryResult(
        status="reconciled",
        detail="startup recovery complete",
        pending_orders=0,
        recovered_orders=0,
        discrepancies=0,
        completed_at=utc_now().isoformat(),
    )
    recovery = step(await fetch(application), "Check startup recovery")
    assert has_pill(recovery, "verified", "ok")
    assert "not a current comparison with the broker" in recovery


@pytest.mark.asyncio
async def test_an_unreachable_broker_is_required_and_leaks_no_provider_text(monkeypatch):
    broker = FlakyBroker()
    broker.down = True
    broker.capabilities = SimpleNamespace(balance_increments={})
    application = dashboard_app(monkeypatch, broker=broker)
    html = await fetch(application)
    check = step(html, "Check broker access and the portfolio snapshot")
    assert has_pill(check, "required", "crit")
    assert "acct-7781" not in html and "provider payload" not in html
    assert has_pill(section(html, "health"), "unavailable", "crit")


@pytest.mark.asyncio
async def test_the_checklist_is_guidance_that_never_claims_a_recovery_ran(monkeypatch):
    application = dashboard_app(monkeypatch, broker=ReadOnlyBroker())
    html = await fetch(application)
    recovery = section(html, "recovery")
    assert "Nothing here runs a recovery" in recovery
    assert "does not mean the broker was reconciled just now" in recovery
    assert "reconciliation completed" not in html.lower()


# Orders waiting on the venue


@pytest.mark.asyncio
async def test_unchecked_and_unreadable_saved_orders_do_not_read_as_resolved(monkeypatch, tmp_path):
    unchecked = dashboard_app(monkeypatch, broker=ReadOnlyBroker())
    orders = section(await fetch(unchecked), "orders")
    assert has_pill(orders, "not checked", "unknown")
    assert "none can be confirmed resolved" in orders and "0</span> saved orders" not in orders

    class Unreadable(InMemoryOrderStore):
        def pending(self):
            raise PersistenceUnavailable("the database is down: secret-host.internal")

    application, _ = closing_app(tmp_path, Venue(), broker=Venue())
    application.state.execution = ExecutionEngine(Venue(), Unreadable())
    html = await fetch(application, ADMIN)
    orders = section(html, "orders")
    assert has_pill(orders, "unreadable", "crit")
    assert "/operator/orders/close" not in orders and "secret-host.internal" not in html
    assert has_pill(step(html, "Resolve orders waiting on the venue"), "required", "crit")


@pytest.mark.asyncio
async def test_pending_and_unknown_orders_link_to_their_saved_identifiers(monkeypatch, tmp_path):
    venue = Venue()
    application, factory = closing_app(tmp_path, venue, broker=venue)
    pending, _, _ = seed_order(factory)
    unknown, _, _ = seed_order(factory, status=OrderStatus.UNKNOWN)
    seed_order(factory, status=OrderStatus.FILLED)  # resolved orders are not listed
    html = await fetch(application, OPERATOR)
    orders = section(html, "orders")
    for order in (pending, unknown):
        assert f'<a class="mono" href="/operator/history/orders/{order.order_id}">' in orders
        assert str(order.order_id) in orders
    assert orders.count('<li class="panel">') == 2
    assert has_pill(orders, "pending_submit", "warn") and has_pill(orders, "unknown", "crit")
    assert "Never resubmit it" in orders and "2 waiting" in orders
    assert has_pill(step(html, "Resolve orders waiting on the venue"), "required", "crit")
    # The operator role sees the identifiers and details, but no close control.
    assert "<form" not in orders and "Only an administrator can close an order" in orders


@pytest.mark.asyncio
async def test_only_the_administrator_is_offered_the_existing_close_control(monkeypatch, tmp_path):
    venue = Venue()
    application, factory = closing_app(tmp_path, venue, broker=venue)
    order, _, _ = seed_order(factory)
    html = await fetch(application, ADMIN)
    orders = section(html, "orders")
    assert '<form method="post" action="/operator/orders/close">' in orders
    assert f'name="client_order_id" value="{order.order_id}"' in orders
    assert 'required maxlength="500"' in orders
    assert "It never resubmits, creates a replacement, or re-arms." in orders

    looked_up = list(venue.lookups)  # the page's own diagnostics read, made before any close
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        forbidden = await client.post(
            "/operator/orders/close",
            headers=OPERATOR,
            data={"client_order_id": str(order.order_id), "reason": "operator tries it"},
        )
        assert forbidden.status_code == 403
        # The server's own refusal still holds: no reason, no close, and nothing is resubmitted.
        refused = await client.post(
            "/operator/orders/close",
            headers=ADMIN,
            data={"client_order_id": str(order.order_id), "reason": ""},
        )
        assert refused.status_code == 422
    assert venue.writes == [] and venue.lookups == looked_up


@pytest.mark.asyncio
async def test_only_the_administrator_reaches_rearm_from_system(monkeypatch):
    application = dashboard_app(monkeypatch, broker=ReadOnlyBroker())
    application.state.kill_switch.trip("test halt")
    operator_html = await fetch(application, OPERATOR)
    admin_html = await fetch(application, ADMIN)
    assert 'href="/operator/rearm"' not in operator_html
    assert "Your operator role cannot re-arm. Ask an administrator." in operator_html
    assert '<a class="button button-rearm" href="/operator/rearm">Review and re-arm</a>' in (
        admin_html
    )
    for html in (operator_html, admin_html):
        assert "RUNNING does not repair causes" in section(html, "recovery")
        assert "<built-in" not in html
        assert "EMERGENCY STOP stays in the safety bar above and works without JavaScript" in html
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/operator/rearm", headers=OPERATOR)).status_code == 403
        assert (
            await client.post("/operator/rearm", headers=OPERATOR, data={"reason": "x"})
        ).status_code == 403
        # A re-arm missing its checklist is still refused by the server, with state unchanged.
        refused = await client.post(
            "/operator/rearm", headers=ADMIN, data={"reason": "INC-1 approved"}
        )
        assert refused.status_code == 422
    assert application.state.kill_switch.state is KillSwitchState.HALTED


# Diagnostics


@pytest.mark.asyncio
async def test_diagnostics_are_bounded_and_state_what_the_venue_said(monkeypatch, tmp_path):
    venue = Venue()
    application, factory = closing_app(tmp_path, venue, broker=venue)
    order, _, _ = seed_order(factory)
    html = await fetch(application, ADMIN)
    diagnostics = section(html, "diagnostics")
    assert "Newest 5 rows per section" in diagnostics
    assert "venue has no record" in diagnostics and "not found" in diagnostics
    assert str(order.order_id) not in diagnostics  # short references only
    assert str(order.order_id)[:8] in diagnostics
    assert 'href="/operator/diagnostics"' in diagnostics


@pytest.mark.asyncio
async def test_a_failed_diagnostic_read_is_unavailable_and_redacted(monkeypatch):
    async def explode(*args, **kwargs):
        raise RuntimeError("provider payload {'account': 'acct-7781'} postgres://secret@db")

    monkeypatch.setattr(system_routes, "build_diagnostics", explode)
    application = dashboard_app(monkeypatch, broker=ReadOnlyBroker())
    html = await fetch(application)
    diagnostics = section(html, "diagnostics")
    assert has_pill(diagnostics, "unavailable", "unknown")
    assert "This is not an empty or clean result" in diagnostics
    assert "acct-7781" not in html and "postgres://" not in html


@pytest.mark.asyncio
async def test_diagnostics_are_refused_with_a_trade_capable_scope(monkeypatch):
    monkeypatch.setenv("OPERATOR_TOKEN", "operator-secret")
    monkeypatch.setenv("OPERATOR_ADMIN_TOKEN", "admin-secret")
    from app.main import create_app

    settings = StartupSettings(
        TradingMode.PAPER, CredentialScope.TRADE, "", "postgresql://unused", "INFO"
    )
    application = create_app(settings, broker=ReadOnlyBroker())
    html = await fetch(application)
    diagnostics = section(html, "diagnostics")
    assert has_pill(diagnostics, "refused", "warn")
    assert "refused in live mode or with a trade-capable credential scope" in diagnostics


@pytest.mark.asyncio
async def test_a_section_that_cannot_be_read_is_named_and_the_rest_still_print(
    monkeypatch, tmp_path
):
    venue = Venue()
    application, factory = closing_app(tmp_path, venue, broker=venue)
    seed_order(factory)

    class Broken(SqlAlchemyDiagnostics):
        def discrepancies(self, limit):
            raise DiagnosticsUnavailable("stored state could not be read")

    application.state.diagnostics = Broken(factory)
    diagnostics = section(await fetch(application), "diagnostics")
    assert has_pill(diagnostics, "partly unavailable", "warn")
    assert "Unavailable: stored state could not be read. This is not a zero count." in diagnostics
    assert "Pending orders and the venue" in diagnostics and "venue has no record" in diagnostics


# Viewing is read-only


@pytest.mark.asyncio
async def test_viewing_system_changes_no_state_and_writes_nothing_at_the_venue(monkeypatch):
    broker = ReadOnlyBroker()
    application = dashboard_app(monkeypatch, broker=broker)
    switch = application.state.kill_switch
    switch.trip("test halt")
    before_state, before_events = switch.state, list(switch.audit_events)
    for headers in (OPERATOR, ADMIN):
        for _ in range(2):
            await fetch(application, headers)
    assert switch.state is before_state is KillSwitchState.HALTED
    assert switch.audit_events == before_events
    assert set(broker.calls) <= {"get_balances", "get_positions", "get_order"}


@pytest.mark.asyncio
async def test_emergency_stop_and_pause_work_as_plain_forms(monkeypatch):
    application = dashboard_app(monkeypatch, broker=ReadOnlyBroker())
    html = await fetch(application)
    assert '<form method="post" action="/operator/pause">' in html
    assert '<form method="post" action="/operator/emergency-stop">' in html
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        stopped = await client.post("/operator/emergency-stop", headers={**OPERATOR, **BROWSER})
        assert stopped.status_code == 200 and "halted" in stopped.text
        assert application.state.kill_switch.state is KillSwitchState.HALTED
        # A pause can never lower a halt, and the halted page no longer offers one.
        await client.post("/operator/pause", headers={**OPERATOR, **BROWSER})
        assert application.state.kill_switch.state is KillSwitchState.HALTED
    halted = await fetch(application)
    assert 'action="/operator/pause"' not in halted and "PAUSE hidden" in halted
    assert 'action="/operator/emergency-stop"' in halted


# History: transitions and alerts


@pytest.mark.asyncio
async def test_no_recorded_transitions_is_distinct_from_a_recorded_history(monkeypatch):
    application = dashboard_app(monkeypatch, broker=ReadOnlyBroker())
    empty = section(await fetch(application), "history")
    assert "No recorded kill-switch change." in empty
    assert "not proof that nothing changed" in empty

    application.state.kill_switch.tighten(
        KillSwitchState.PAUSED, reason="operator pause", actor="operator"
    )
    recorded = section(await fetch(application), "history")
    assert "No recorded kill-switch change." not in recorded
    assert "running to paused" in recorded and "operator pause" in recorded


@pytest.mark.asyncio
async def test_a_failed_alert_delivery_is_not_a_trading_condition_or_a_cleared_halt(monkeypatch):
    router = AlertRouter(email=FailingSink())
    application = dashboard_app(monkeypatch, broker=ReadOnlyBroker(), alert_router=router)
    await application.state.operator_state.emit_alert(
        Alert(condition="reconciliation_diverged", severity="critical", message="3 differences")
    )
    html = await fetch(application, ADMIN)
    alerts = section(html, "history")
    assert '<details class="fold" id="alerts" open>' in alerts  # failures are never collapsed
    assert "Delivery failed for 1 alert(s). Someone may not have been told." in alerts
    assert "The conditions themselves are unchanged." in alerts
    assert "does not say whether the trading condition changed" in alerts
    assert "it does not clear a halt or resolve its cause" in alerts
    assert "smtp 550" not in html and "ops-pager@example.test" not in html
    assert 'action="/operator/alerts/dismiss"' not in html  # dismissal stays an Overview action
    assert '<a href="/operator#alerts">Overview</a>' in alerts


@pytest.mark.asyncio
async def test_alert_history_is_bounded(monkeypatch):
    router = AlertRouter(phone_push=RecordingSink())
    application = dashboard_app(monkeypatch, broker=ReadOnlyBroker(), alert_router=router)
    for index in range(ROW_LIMIT + 3):
        await application.state.operator_state.emit_alert(
            Alert(condition=f"condition_{index}", severity="warning", message="m")
        )
    alerts = section(await fetch(application), "history")
    assert alerts.count('<li class="event">') <= ROW_LIMIT
    assert f"Showing the newest {ROW_LIMIT} of {ROW_LIMIT + 3} new alerts." in alerts


@pytest.mark.asyncio
async def test_application_version_timestamps_and_identity_are_preserved(monkeypatch):
    application = dashboard_app(monkeypatch, broker=ReadOnlyBroker())
    health = section(await fetch(application), "health")
    for label in (
        "Application version",
        "Snapshot updated",
        "Trading mode",
        "Credential scope",
        "Strategy version",
        "Heartbeat",
        "Pending orders",
        "Recovered orders",
        "Last result",
        "Persistence and history",
        "Strategy heartbeats",
    ):
        assert label in health, label
    assert "<dt>Trading mode</dt><dd>paper</dd>" in health
    assert "<dt>Credential scope</dt><dd>view</dd>" in health


@pytest.mark.asyncio
async def test_the_existing_overview_endpoints_and_secondary_links_remain(monkeypatch):
    application = dashboard_app(monkeypatch, broker=ReadOnlyBroker())
    html = await fetch(application)
    for destination in (
        "/operator/diagnostics",
        "/operator/history/trends",
        "/operator/history/soak",
        "/operator/history/risk",
    ):
        assert f'href="{destination}"' in html, destination
    overview = await fetch(application, path="/operator")
    for anchor in ("overview", "portfolio", "activity", "alerts", "safety", "health"):
        assert f'<section id="{anchor}"' in overview
    state = build_dashboard(application.state.operator_state.to_dict(), role="operator")
    assert state["header"]["role"] == "Operator"


# The view model, from complete synthetic snapshots


def snapshot_with(**updates):
    from tests.test_operator_dashboard import attention_snapshot

    return attention_snapshot(**updates)


def steps_by_title(view):
    return {item.title: item for item in view["steps"]}


def test_halted_recovery_and_an_unreadable_broker_are_required_with_their_evidence():
    snapshot = snapshot_with(
        risk={"kill_switch": "halted", "transitions": []},
        recovery={"status": "halted", "detail": "a pending order was missing"},
        reconciliation={"last_result": "unavailable", "last_run_at": "2026-10-24T12:00:00+00:00"},
        connectivity={"status": "unavailable", "detail": "broker is unavailable"},
        runtime={"status": "failed", "detail": "paper runtime failed"},
    )
    view = build_system(snapshot, role="admin", diagnostics={"outcome": "failed"})
    steps = steps_by_title(view)
    recovery = steps["Check startup recovery"]
    assert (recovery.status.word, recovery.status.tone) == ("required", "crit")
    assert "a pending order was missing" in recovery.detail
    reconciliation = steps["Review the latest scheduled reconciliation"]
    assert (reconciliation.status.word, reconciliation.status.tone) == ("required", "crit")
    assert "could not read the broker" in reconciliation.detail
    runtime = steps["Check the runtime and strategy heartbeat"]
    assert runtime.status.tone == "crit" and "No strategy heartbeat" in runtime.detail
    assert steps["Check broker access and the portfolio snapshot"].status.tone == "crit"
    assert view["cause"]["known"] is True  # the halt is explained by the recorded conditions
    assert any("Startup recovery halted" in item for item in view["safety"].causes)
    assert [item.number for item in view["steps"]] == list(range(1, 8))
    assert view["diagnostics"]["outcome"] == "failed"


def test_the_venue_answers_and_balance_checks_in_a_report_keep_their_own_words():
    report = {
        "generated_at": "2026-10-24T12:00:00+00:00",
        "limit": 5,
        "pending_orders": {
            "readable": True,
            "orders": [
                {
                    "ref": ref,
                    "status": "unknown",
                    "symbol": "BTC-USD",
                    "side": "buy",
                    "quantity": "0.0001",
                    "age_seconds": 125,
                    "broker": answer,
                }
                for ref, answer in (
                    ("aaaaaaaa", "found (open)"),
                    ("bbbbbbbb", "lookup timed out"),
                    ("cccccccc", "lookup failed: HTTP 503"),
                    ("dddddddd", "not asked (lookup limit reached)"),
                )
            ],
        },
        "discrepancies": {"readable": True, "rows": []},
        "fills": {
            "readable": True,
            "rows": [],
            "checks": [
                {
                    "fills": ["1234abcd"],
                    "from": "2026-10-24T11:00:00+00:00",
                    "to": "2026-10-24T11:05:00+00:00",
                    "status": "itemized",
                    "assets": [{"asset": "USD", "difference": "0"}],
                },
                {"fills": ["5678abcd"], "status": "not yet reconciled"},
            ],
        },
    }
    view = build_system(
        snapshot_with(), role="operator", diagnostics={"outcome": "report", "report": report}
    )
    rows = view["diagnostics"]["pending"]["rows"]
    assert [row["venue"].word for row in rows] == [
        "venue has it",
        "lookup timed out",
        "lookup failed",
        "not asked",
    ]
    assert rows[0]["answer"] == "found (open)" and rows[0]["age"] == "2 min"
    checks = view["diagnostics"]["fills"]["checks"]
    assert checks[0]["itemized"] and checks[0]["assets"] == [{"asset": "USD", "difference": "0"}]
    assert checks[1]["status"] == "not yet reconciled" and checks[1]["from"] is None


def test_a_naive_time_ages_normally_and_an_unreadable_one_is_not_verified():
    clean = {
        "last_result": "clean",
        "last_discrepancies": 0,
        "runs": 1,
        "last_run_at": "2026-10-24T11:50:00",
    }
    now = __import__("datetime").datetime(2026, 10, 24, 12, 0, tzinfo=__import__("datetime").UTC)
    view = build_system(
        snapshot_with(reconciliation=clean),
        role="operator",
        diagnostics={"outcome": "failed"},
        reconcile_interval=30.0,
        now=now,
    )
    assert steps_by_title(view)["Review the latest scheduled reconciliation"].status.word == (
        "stale"
    )
    garbled = build_system(
        snapshot_with(reconciliation={**clean, "last_run_at": "not a time"}),
        role="operator",
        diagnostics={"outcome": "failed"},
        reconcile_interval=30.0,
        now=now,
    )
    # Without a readable time there is no age to judge, so it is not called verified either.
    assert steps_by_title(garbled)["Review the latest scheduled reconciliation"].status.word == (
        "unavailable"
    )


def test_a_long_stored_value_is_clipped_in_the_page_but_not_in_the_report():
    from api.system_view import VALUE_LIMIT, _clip

    assert _clip("short") == "short"
    clipped = _clip("x" * (VALUE_LIMIT + 50))
    assert len(clipped) == VALUE_LIMIT and clipped.endswith("\u2026")
