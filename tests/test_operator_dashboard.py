"""Server-rendered operator dashboard: every state renders truthfully and safely."""

import hashlib
import re
import struct
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path

import httpx
import pytest
from api.alerts import Alert, AlertRouter, build_alert_router
from api.dashboard import ROW_LIMIT, build_dashboard
from app.main import create_app
from app.recovery import StartupRecoveryResult
from core.guards import CredentialScope, StartupSettings
from core.models import (
    Balance,
    KillSwitchState,
    Order,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    Signal,
    TradingMode,
    utc_now,
)
from portfolio.scheduler import ReconciliationStatus
from risk.kill_switch import KillSwitch

from tests.operator_support import ICON_LINK

OPERATOR = {"x-operator-token": "operator-secret"}
ADMIN = {"x-operator-token": "admin-secret"}
MARKS = "●▲■○◐"
CSS = Path(__file__).resolve().parents[1] / "api" / "templates" / "operator.css"
ICON_DIR = Path(__file__).resolve().parents[1] / "api" / "static" / "icons"


class HealthyBroker:
    async def get_balances(self):
        return (
            Balance(asset="USD", available="100", hold="2.5", as_of=utc_now()),
            Balance(asset="BTC", available="0", as_of=utc_now()),
        )

    async def get_positions(self):
        return (
            Position(symbol="BTC-USD", quantity="0.1", average_price=None, as_of=utc_now()),
            Position(symbol="ETH-USD", quantity="2", average_price="1800.5", as_of=utc_now()),
        )


class EmptyBroker:
    async def get_balances(self):
        return ()

    async def get_positions(self):
        return ()


class FlakyBroker(HealthyBroker):
    def __init__(self):
        self.down = False

    async def get_balances(self):
        if self.down:
            raise RuntimeError("provider payload {'account': 'acct-7781'} must stay hidden")
        return await super().get_balances()


class RecordingSink:
    async def send(self, alert):
        return None


class FailingSink:
    async def send(self, alert):
        raise RuntimeError("smtp 550 rejected ops-pager@example.test")


class Scheduler:
    def __init__(self, status):
        self.status = status


def dashboard_app(monkeypatch, *, mode=TradingMode.PAPER, broker=None, admin_token=True, **kw):
    monkeypatch.setenv("OPERATOR_TOKEN", "operator-secret")
    if admin_token:
        monkeypatch.setenv("OPERATOR_ADMIN_TOKEN", "admin-secret")
    else:
        monkeypatch.delenv("OPERATOR_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("KILL_SWITCH_FILE", raising=False)
    settings = StartupSettings(mode, CredentialScope.VIEW, "", "postgresql://unused", "INFO")
    return create_app(settings, broker=broker, **kw)


async def page(application, headers=OPERATOR, path="/operator") -> str:
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(path, headers=headers)
    assert response.status_code == 200, response.text
    return response.text


def section(html: str, anchor: str) -> str:
    start = html.index(f'<section id="{anchor}"')
    return html[start : html.index("</section>", start)]


def safety_bar(html: str) -> str:
    start = html.index('<section class="safety')
    return html[start : html.index("</section>", start)]


def card(html: str, label: str) -> str:
    start = html.index(f">{label}</a></h3>")
    start = html.rindex("<li", 0, start)
    return html[start : html.index("</li>", start)]


def has_pill(html: str, word: str, tone: str) -> bool:
    """A status pill: its tone, its decorative mark, and the word that carries the meaning."""

    mark = {"ok": "●", "warn": "▲", "crit": "■", "unknown": "○", "neutral": "◐"}[tone]
    pattern = (
        rf'<span class="pill tone-{tone}(?: pill-large)?"><span class="mark" '
        rf'aria-hidden="true">{mark}</span> {re.escape(word)}</span>'
    )
    return re.search(pattern, html) is not None


def order(status: OrderStatus, minutes: int = 0) -> Order:
    signal = Signal(symbol="BTC-USD", side=OrderSide.BUY, quantity="0.01", strategy_version="v1")
    request = OrderRequest(
        signal_id=signal.signal_id,
        strategy_version="v1",
        symbol="BTC-USD",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity="0.01",
        limit_price="65000",
        correlation_id=signal.correlation_id,
    )
    return Order(request=request, status=status, updated_at=utc_now() - timedelta(minutes=minutes))


# Sessions and roles


@pytest.mark.asyncio
async def test_header_shows_the_role_the_server_granted(monkeypatch):
    application = dashboard_app(monkeypatch, broker=HealthyBroker())
    application.state.kill_switch.set_state(KillSwitchState.PAUSED)
    operator = await page(application, OPERATOR)
    admin = await page(application, ADMIN)
    assert "<dt>Signed in as</dt>\n        <dd>Operator</dd>" in operator
    assert "<dt>Signed in as</dt>\n        <dd>Administrator</dd>" in admin
    assert "/operator/rearm" not in operator
    assert "Your operator role cannot re-arm" in operator
    assert 'href="/operator/rearm"' in section(admin, "safety")


@pytest.mark.asyncio
async def test_a_lone_operator_token_is_shown_as_administrator(monkeypatch):
    application = dashboard_app(monkeypatch, admin_token=False)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        login = await client.post(
            "/operator/login",
            data={"token": "operator-secret"},
            headers={"accept": "text/html"},
            follow_redirects=False,
        )
        assert login.status_code == 303
        html = (await client.get("/operator")).text
    assert "<dd>Administrator</dd>" in html
    assert "Nothing to re-arm: the kill switch is running." in html


# Modes and broker states


@pytest.mark.asyncio
async def test_backtest_without_a_broker_shows_absent_capabilities_not_zeros(monkeypatch):
    application = dashboard_app(monkeypatch, mode=TradingMode.BACKTEST)
    html = await page(application)
    assert '<span class="mode tone-accent">' in html
    assert "◆</span> backtest</span>" in html
    assert "<title>running · backtest · Operator dashboard</title>" in html
    assert has_pill(card(html, "Broker"), "not_configured", "unknown")
    assert has_pill(card(html, "Startup recovery"), "not_run", "unknown")
    assert has_pill(card(html, "Reconciliation"), "not_scheduled", "unknown")
    assert has_pill(card(html, "Paper runtime"), "not_started", "unknown")
    portfolio = section(html, "portfolio")
    assert "Unavailable: no broker is configured</span>. This is not a zero balance." in portfolio
    assert '<span class="count">0</span> balances' not in portfolio
    health = section(html, "health")
    # Recovery never ran, so its counts are unknown, not zero.
    assert (
        '<dt>Pending orders</dt><dd><span class="absent"><span class="mark" '
        'aria-hidden="true">○</span> not recorded</span></dd>'
    ) in health
    assert '<dt>Runs</dt><dd><span class="absent">' in health


@pytest.mark.asyncio
async def test_paper_with_a_healthy_broker_shows_current_data_in_a_neutral_tone(monkeypatch):
    application = dashboard_app(monkeypatch, broker=HealthyBroker())
    html = await page(application)
    assert has_pill(card(html, "Broker"), "healthy", "ok")
    assert "Checked <time datetime=" in card(html, "Broker")
    # "Current" is freshness, not correctness: never the green "ok" tone.
    assert has_pill(card(html, "Portfolio data"), "current", "neutral")
    assert not has_pill(html, "current", "ok")
    portfolio = section(html, "portfolio")
    assert '<th scope="row">USD</th><td class="num">100</td><td class="num">2.5</td>' in portfolio
    # A zero balance is a real value, printed as a number.
    assert '<th scope="row">BTC</th><td class="num">0</td>' in portfolio
    # Venues report no cost basis as None: that is unknown, not a price.
    assert (
        '<th scope="row">BTC-USD</th><td class="num">0.1</td><td class="num">'
        '<span class="absent"><span class="mark" aria-hidden="true">○</span> not known</span>'
    ) in portfolio
    assert '<td class="num">1800.5</td>' in portfolio
    assert "last-known" not in portfolio


@pytest.mark.asyncio
async def test_empty_broker_portfolio_is_a_real_zero(monkeypatch):
    html = await page(dashboard_app(monkeypatch, broker=EmptyBroker()))
    portfolio = section(html, "portfolio")
    assert '<span class="count">0</span> balances reported by the broker.' in portfolio
    assert '<span class="count">0</span> positions reported by the broker.' in portfolio
    assert "not a zero balance" not in portfolio


@pytest.mark.asyncio
async def test_unavailable_broker_keeps_last_known_values_marked_as_such(monkeypatch):
    broker = FlakyBroker()
    application = dashboard_app(monkeypatch, broker=broker)
    await page(application)
    broker.down = True
    html = await page(application)
    assert has_pill(card(html, "Broker"), "unavailable", "crit")
    assert "broker is unavailable" in card(html, "Broker")
    assert has_pill(card(html, "Portfolio data"), "last known", "warn")
    portfolio = section(html, "portfolio")
    assert "Last-known values, kept for diagnosis only" in portfolio
    assert '<table class="data last-known">' in portfolio
    assert '<th scope="row">USD</th><td class="num">100</td>' in portfolio
    assert "broker_unavailable" in section(html, "alerts")
    assert "provider payload" not in html
    assert "acct-7781" not in html


# Recovery, reconciliation, and the kill switch


@pytest.mark.asyncio
async def test_startup_recovery_halt_is_shown_as_the_cause(monkeypatch):
    application = dashboard_app(monkeypatch, broker=HealthyBroker())
    completed = datetime(2026, 9, 27, 17, 0, tzinfo=UTC)
    application.state.operator_state.startup_recovery = StartupRecoveryResult(
        status="halted",
        detail="broker reconciliation was unavailable during startup recovery",
        pending_orders=2,
        recovered_orders=1,
        discrepancies=0,
        completed_at=completed.isoformat(),
    )
    application.state.kill_switch.trip("startup recovery")
    html = await page(application)
    bar = safety_bar(html)
    assert has_pill(bar, "halted", "crit")
    assert (
        "<li>Startup recovery halted: broker reconciliation was unavailable during startup "
        "recovery.</li>"
    ) in bar
    assert has_pill(card(html, "Startup recovery"), "halted", "crit")
    assert 'Completed <time datetime="2026-09-27T17:00:00+00:00">' in html
    health = section(html, "health")
    assert '<dt>Pending orders</dt><dd><span class="count">2</span></dd>' in health
    assert '<dt>Recovered orders</dt><dd><span class="count">1</span></dd>' in health
    # A recorded zero is a count, not an absence.
    assert '<dt>Discrepancies</dt><dd><span class="count">0</span></dd>' in health


@pytest.mark.asyncio
async def test_clean_reconciliation_totals(monkeypatch):
    application = dashboard_app(monkeypatch, broker=HealthyBroker())
    application.state.operator_state.scheduled_reconciliation = Scheduler(
        ReconciliationStatus(
            started_at=utc_now() - timedelta(hours=1),
            runs=12,
            clean_runs=12,
            last_run_at=utc_now() - timedelta(seconds=30),
            last_result="clean",
        )
    )
    html = await page(application)
    assert has_pill(card(html, "Reconciliation"), "clean", "ok")
    assert "0 difference(s) in the last run; 12 run(s) since start." in html
    assert "Last run <time datetime=" in card(html, "Reconciliation")
    health = section(html, "health")
    assert '<dt>Clean runs</dt><dd><span class="count">12</span></dd>' in health
    assert '<dt>Diverged runs</dt><dd><span class="count">0</span></dd>' in health


@pytest.mark.asyncio
async def test_diverged_reconciliation_is_critical_and_named_as_a_cause(monkeypatch):
    application = dashboard_app(monkeypatch, broker=HealthyBroker())
    application.state.operator_state.scheduled_reconciliation = Scheduler(
        ReconciliationStatus(
            started_at=utc_now(),
            runs=5,
            clean_runs=4,
            diverged_runs=1,
            last_run_at=utc_now(),
            last_result="diverged",
            last_discrepancies=3,
        )
    )
    application.state.kill_switch.trip("scheduled reconciliation diverged")
    html = await page(application)
    assert has_pill(card(html, "Reconciliation"), "diverged", "crit")
    assert "<li>Scheduled reconciliation diverged: 3 difference(s) with the broker.</li>" in (
        safety_bar(html)
    )


@pytest.mark.asyncio
async def test_running_kill_switch_offers_pause_and_stop_without_a_cause(monkeypatch):
    html = await page(dashboard_app(monkeypatch, broker=HealthyBroker()))
    bar = safety_bar(html)
    assert has_pill(bar, "running", "ok")
    assert "Running does not mean the system is healthy or trading." in bar
    assert 'action="/operator/pause"' in bar
    assert 'action="/operator/emergency-stop"' in bar
    assert "Cause" not in bar


@pytest.mark.asyncio
async def test_paused_kill_switch_shows_the_recorded_change_as_its_cause(monkeypatch):
    application = dashboard_app(monkeypatch, broker=HealthyBroker())
    application.state.kill_switch.tighten(
        KillSwitchState.PAUSED, reason="operator pause", actor="operator"
    )
    html = await page(application)
    bar = safety_bar(html)
    assert has_pill(bar, "paused", "warn")
    assert "<li>Last change: running to paused by Operator (manual), <time datetime=" in bar
    assert "operator pause</li>" in bar
    assert "Not recorded" not in bar
    assert 'action="/operator/pause"' in bar
    assert 'action="/operator/emergency-stop"' in bar
    assert "<title>paused · paper · Operator dashboard</title>" in html


@pytest.mark.asyncio
async def test_paused_kill_switch_without_a_recorded_change_says_so(monkeypatch, tmp_path):
    # The state file says paused, but no saved transition explains it.
    switch_path = tmp_path / "kill-switch.json"
    switch_path.write_text('{"state": "paused"}', encoding="utf-8")
    application = dashboard_app(monkeypatch, broker=HealthyBroker())
    monkeypatch.setenv("KILL_SWITCH_FILE", str(switch_path))
    application.state.operator_state.kill_switch = KillSwitch(switch_path)
    bar = safety_bar(await page(application))
    assert has_pill(bar, "paused", "warn")
    assert "<p>Not recorded. No saved kill-switch change explains this state" in bar
    assert "Last change" not in bar


@pytest.mark.asyncio
async def test_halted_kill_switch_hides_pause_for_every_role(monkeypatch):
    application = dashboard_app(monkeypatch, broker=HealthyBroker())
    application.state.kill_switch.set_state(KillSwitchState.HALTED, reason="operator stop")
    for headers in (OPERATOR, ADMIN):
        html = await page(application, headers)
        bar = safety_bar(html)
        assert has_pill(bar, "halted", "crit")
        assert 'action="/operator/pause"' not in html
        assert "administrator re-arm required" in bar
        assert 'action="/operator/emergency-stop"' in bar


@pytest.mark.asyncio
async def test_controls_work_as_plain_forms_without_javascript(monkeypatch, tmp_path):
    application = dashboard_app(monkeypatch, broker=HealthyBroker())
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/operator/login", data={"token": "operator-secret"}, headers={"accept": "text/html"}
        )
        html = (await client.get("/operator")).text
        assert "<script" not in html
        assert "hx-" not in html
        assert '<form method="post" action="/operator/pause">' in html
        paused = await client.post("/operator/pause", headers={"accept": "text/html"})
        assert "paused" in paused.text
        assert 'action="/operator/pause"' in (await client.get("/operator")).text
        stopped = await client.post("/operator/emergency-stop", headers={"accept": "text/html"})
        assert "halted" in stopped.text
        halted = (await client.get("/operator")).text
    assert 'action="/operator/pause"' not in halted
    assert '<form method="post" action="/operator/emergency-stop">' in halted


# Strategy, P/L, runtime, activity


@pytest.mark.asyncio
async def test_unknown_strategy_heartbeat_is_unknown_and_never_seen(monkeypatch):
    html = await page(dashboard_app(monkeypatch))
    assert has_pill(card(html, "Strategy"), "unknown", "unknown")
    assert "Heartbeat never received" in card(html, "Strategy")
    heartbeats = section(html, "health").split('id="heartbeats-heading"')[1]
    assert '<th scope="row">primary</th>' in heartbeats
    assert '<span class="mark" aria-hidden="true">○</span> never</span>' in heartbeats


@pytest.mark.asyncio
async def test_unavailable_pnl_never_renders_as_zero(monkeypatch):
    html = await page(dashboard_app(monkeypatch, broker=HealthyBroker()))
    pnl = card(html, "P/L")
    assert has_pill(pnl, "unavailable", "unknown")
    assert "Unavailable is not zero." in pnl
    assert not re.search(r"\d", re.sub(r"<[^>]+>", "", pnl))
    paragraph = re.search(r'<p class="pnl">.*?</p>', html, re.S)
    assert paragraph is not None
    assert has_pill(paragraph.group(0), "unavailable", "unknown")
    assert not re.search(r"\d", re.sub(r"<[^>]+>", "", paragraph.group(0)))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "tone"),
    [
        ("not_started", "unknown"),
        ("running", "ok"),
        ("degraded", "warn"),
        ("failed", "crit"),
        ("halted", "crit"),
        ("stopped", "warn"),
        ("disabled", "neutral"),
    ],
)
async def test_every_paper_runtime_status_renders(monkeypatch, status, tone):
    application = dashboard_app(monkeypatch, broker=HealthyBroker())
    cycle_at = utc_now() - timedelta(minutes=2)
    application.state.operator_state.set_runtime(
        status, f"{status} detail", cycle_status="no_signal", cycle_at=cycle_at
    )
    html = await page(application)
    runtime = card(html, "Paper runtime")
    assert has_pill(runtime, status, tone)
    assert f"{status} detail" in runtime
    assert "Last cycle no_signal <time datetime=" in runtime
    health = section(html, "health")
    assert has_pill(health, "no_signal", "ok")
    if status in {"failed", "halted"}:
        application.state.kill_switch.trip("runtime")
        bar = safety_bar(await page(application))
        assert f"<li>Paper runtime {status}: {status} detail.</li>" in bar


@pytest.mark.asyncio
async def test_runtime_without_a_cycle_says_none_has_run(monkeypatch):
    html = await page(dashboard_app(monkeypatch))
    assert "No strategy cycle has run" in card(html, "Paper runtime")
    assert "no cycle has run" in section(html, "health")


@pytest.mark.asyncio
async def test_activity_counts_orders_and_flags_unknown_orders(monkeypatch):
    application = dashboard_app(monkeypatch, broker=HealthyBroker())
    orders = tuple(order(status, index) for index, status in enumerate(OrderStatus))
    application.state.operator_state.set_local_data(orders=orders)
    activity = section(await page(application), "activity")
    assert '<dt>Orders</dt><dd><span class="count">7</span></dd>' in activity
    assert '<dt>Fills</dt><dd><span class="count">0</span></dd>' in activity
    assert 'Risk decisions</dt><dd><span class="absent">' in activity
    for status, tone in [
        ("pending_submit", "warn"),
        ("unknown", "crit"),
        ("open", "neutral"),
        ("partially_filled", "neutral"),
        ("filled", "ok"),
        ("canceled", "neutral"),
        ("rejected", "crit"),
    ]:
        assert has_pill(activity, status, tone)
    assert "◆" not in activity
    assert activity.count("Never resubmit it.") == 1
    assert str(orders[0].request.client_order_id) in activity
    assert '<span class="count">0</span> fills recorded by this process.' in activity


def test_long_lists_are_capped_with_a_note():
    snapshot = {"orders": [{"status": "open", "updated_at": None}] * (ROW_LIMIT + 5)}
    view = build_dashboard(snapshot, role="operator")
    assert len(view["activity"]["orders"]) == ROW_LIMIT
    assert view["activity"]["counts"]["orders"] == ROW_LIMIT + 5


# Alerts and redaction


@pytest.mark.asyncio
async def test_alert_with_a_failed_delivery_shows_each_destination(monkeypatch):
    router = AlertRouter(phone_push=RecordingSink(), email=FailingSink())
    application = dashboard_app(monkeypatch, broker=HealthyBroker(), alert_router=router)
    await application.state.operator_state.emit_alert(
        Alert(condition="market_data_disconnected", severity="warning", message="feed lost")
    )
    html = await page(application)
    alerts = section(html, "alerts")
    assert has_pill(alerts, "warning", "warn")
    assert re.search(r"Phone push <span class=\"pill tone-ok\">.{0,60}> sent</span>", alerts)
    assert re.search(r"Email <span class=\"pill tone-crit\">.{0,60}> failed</span>", alerts)
    assert "1 delivery attempt(s) failed." in card(html, "Alerts")
    assert has_pill(alerts, "configured", "ok")
    assert "smtp 550" not in html
    assert "ops-pager@example.test" not in html


@pytest.mark.asyncio
async def test_failed_delivery_alone_is_never_neutral(monkeypatch):
    router = AlertRouter(email=FailingSink())
    application = dashboard_app(monkeypatch, alert_router=router)
    await application.state.operator_state.emit_alert(
        Alert(condition="note", severity="info", message="heads up")
    )
    html = await page(application)
    assert has_pill(card(html, "Alerts"), "delivery failed", "warn")
    assert has_pill(section(html, "alerts"), "info", "neutral")


@pytest.mark.asyncio
async def test_undelivered_alert_says_nobody_was_notified(monkeypatch):
    application = dashboard_app(monkeypatch)
    await application.state.operator_state.emit_alert(
        Alert(condition="broker_unavailable", severity="critical", message="broker offline")
    )
    html = await page(application)
    assert has_pill(card(html, "Alerts"), "1 critical", "crit")
    assert "Not delivered: no destinations are configured." in section(html, "alerts")
    assert "No destination is configured, so nobody is notified." in card(html, "Alerts")
    assert has_pill(section(html, "alerts"), "not configured", "unknown")


@pytest.mark.asyncio
async def test_no_token_recipient_topic_or_provider_payload_reaches_the_page(monkeypatch):
    environ = {
        "ALERT_NTFY_TOPIC_URL": "https://ntfy.example.test/private-topic-9f2c",
        "ALERT_NTFY_TOKEN": "tk_ntfy_secret_value",
        "ALERT_SMTP_HOST": "smtp.example.test",
        "ALERT_EMAIL_FROM": "trader@example.test",
        "ALERT_EMAIL_TO": "ops-pager@example.test",
        "ALERT_SMTP_USERNAME": "smtp-user",
        "ALERT_SMTP_PASSWORD": "smtp-password-value",
    }
    broker = FlakyBroker()
    application = dashboard_app(
        monkeypatch, broker=broker, alert_router=build_alert_router(environ)
    )
    await page(application)
    broker.down = True
    for path in ("/operator", "/operator/fragment"):
        for headers in (OPERATOR, ADMIN):
            html = await page(application, headers, path)
            for secret in (
                "operator-secret",
                "admin-secret",
                "private-topic-9f2c",
                "ntfy.example.test",
                "tk_ntfy_secret_value",
                "smtp.example.test",
                "trader@example.test",
                "ops-pager@example.test",
                "smtp-user",
                "smtp-password-value",
                "acct-7781",
                "provider payload",
                "token=",
            ):
                assert secret not in html, secret
    assert section(html, "alerts").count('aria-hidden="true">●</span> configured</span>') == 2


# Structure, accessibility, and styling


class _Outline(HTMLParser):
    def __init__(self):
        super().__init__()
        self.headings: list[int] = []
        self.live: list[str] = []
        self.hidden_depth = 0
        self.stack: list[bool] = []
        self.visible_marks = 0

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if re.fullmatch(r"h[1-6]", tag):
            self.headings.append(int(tag[1]))
        if "aria-live" in attributes:
            self.live.append(attributes.get("class") or tag)
        if tag in {"meta", "link", "br", "input", "img"}:
            return
        hidden = attributes.get("aria-hidden") == "true"
        self.stack.append(hidden)
        self.hidden_depth += hidden

    def handle_endtag(self, tag):
        if tag in {"meta", "link", "br", "input", "img"} or not self.stack:
            return
        self.hidden_depth -= self.stack.pop()

    def handle_data(self, data):
        if self.hidden_depth == 0 and self.lasttag not in {"style", "title"}:
            self.visible_marks += sum(data.count(mark) for mark in MARKS + "◆")


@pytest.mark.asyncio
async def test_page_structure_supports_keyboard_and_screen_reader_use(monkeypatch):
    application = dashboard_app(monkeypatch, broker=HealthyBroker())
    application.state.kill_switch.set_state(KillSwitchState.HALTED)
    html = await page(application, ADMIN)
    assert '<a class="skip-link" href="#main">Skip to main content</a>' in html
    assert '<main id="main" tabindex="-1">' in html
    assert '<header class="topbar">' in html
    assert '<nav class="sections" aria-label="Dashboard sections">' in html
    assert '<section class="safety tone-crit" aria-labelledby="safety-heading">' in html
    for anchor in ("overview", "portfolio", "activity", "alerts", "safety", "health"):
        assert f'<a href="#{anchor}">' in html
        assert f'<section id="{anchor}"' in html
    outline = _Outline()
    outline.feed(html)
    assert outline.headings.count(1) == 1
    assert outline.headings[0] == 1
    assert all(b - a <= 1 for a, b in zip(outline.headings, outline.headings[1:], strict=False))
    # Only the state summary announces changes, not the whole page.
    assert outline.live == ["safety-summary"]
    assert '<span class="visually-hidden">Kill switch halted. </span>' in html
    # Marks are decorative; the word next to each one carries the meaning.
    assert outline.visible_marks == 0


@pytest.mark.asyncio
async def test_fragment_is_the_dashboard_body_without_the_page_shell(monkeypatch):
    html = await page(dashboard_app(monkeypatch), path="/operator/fragment")
    assert html.lstrip().startswith('<div id="operator-state" class="shell">')
    assert "<html" not in html
    assert "<style>" not in html
    assert 'action="/operator/emergency-stop"' in html


@pytest.mark.asyncio
async def test_styles_are_local_and_inline(monkeypatch):
    html = await page(dashboard_app(monkeypatch))
    assert "<style>" in html
    assert "--crit: #96382f;" in html
    assert html.count("<link") == 1 and ICON_LINK in html
    assert "http://" not in html
    assert "https://" not in html
    assert "@import" not in html


def test_stylesheet_meets_the_accessibility_rules():
    css = CSS.read_text(encoding="utf-8")
    sizes = [float(value) for value in re.findall(r"font-size:\s*([\d.]+)rem", css)]
    sizes += [float(value) for value in re.findall(r"font:\s*\d+\s+([\d.]+)rem", css)]
    assert sizes and min(sizes) >= 0.75  # 12 px
    assert not re.search(r"font(-size)?:[^;]*\d+px", css)
    assert ":focus-visible" in css
    assert "@media (prefers-reduced-motion: reduce)" in css
    assert "@media (prefers-color-scheme: dark)" in css
    small_screens = css.split("@media (max-width: 44.99em)")[1].split("\n}\n")[0]
    assert re.search(r"\.controls \{[^}]*position: fixed;[^}]*bottom: 0;", small_screens)
    assert "{#" not in css  # would start a Jinja comment inside the inlined stylesheet


# Presenter edge cases


def test_times_show_utc_and_age_and_absences_say_why():
    now = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
    snapshot = {
        "updated_at": "2026-09-27T11:58:30",
        "connectivity": {"status": "healthy", "checked_at": "not a time"},
        "strategies": [{"name": "primary", "status": "healthy", "last_seen": None}],
        "risk": {"kill_switch": "unexpected"},
    }
    view = build_dashboard(snapshot, role="operator", now=now)
    updated = view["header"]["updated"]
    assert updated.text == "2026-09-27 11:58:30 UTC"
    assert updated.age == "1 min ago"
    assert updated.iso == "2026-09-27T11:58:30+00:00"
    broker = next(item for item in view["cards"] if item.label == "Broker")
    assert broker.fresh is not None and broker.fresh.text == "unreadable time"
    assert view["strategies"][0]["last_seen"].text == "never"
    # An unrecognised kill-switch value fails closed to the critical tone.
    assert view["safety"].status.tone == "crit"
    assert view["header"]["role"] == "Operator"


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(-5, "0 s ago"), (59, "59 s ago"), (3599, "59 min ago"), (7500, "2 h 5 min ago")],
)
def test_ages_are_readable(seconds, expected):
    now = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
    moment = (now - timedelta(seconds=seconds)).isoformat()
    view = build_dashboard({"updated_at": moment}, role="admin", now=now)
    assert view["header"]["updated"].age == expected


def test_days_old_values_show_days():
    now = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
    view = build_dashboard({"updated_at": "2026-09-25T09:00:00+00:00"}, role="admin", now=now)
    assert view["header"]["updated"].age == "2 d 3 h ago"


def test_live_mode_badge_is_critical():
    view = build_dashboard({"trading": {"mode": "live"}}, role="operator")
    assert view["header"]["mode_tone"] == "crit"


def test_rare_states_still_say_what_is_known():
    snapshot = {
        "risk": {"kill_switch": "halted"},
        "reconciliation": {"last_result": "unavailable", "runs": 3},
        "strategies": [],
        "portfolio": {
            "status": "current",
            "positions": [{"symbol": "BTC-USD", "quantity": "1", "average_price": "n/a"}],
        },
        "alert_destinations": ["email"],
        "alerts": [{"condition": "x", "severity": "critical", "message": "m", "deliveries": []}],
    }
    view = build_dashboard(snapshot, role="admin")
    assert view["safety"].causes == ("Scheduled reconciliation could not read the broker.",)
    strategy = next(item for item in view["cards"] if item.label == "Strategy")
    assert (strategy.status.word, strategy.status.tone) == ("none registered", "unknown")
    assert view["portfolio"]["positions"][0]["average_price"].text == "not known"
    assert view["alerts"]["rows"][0]["undelivered"] == "No delivery was recorded."


def test_a_real_zero_average_price_is_shown_as_a_price_and_only_none_is_unknown():
    def shown(value):
        snapshot = {
            "portfolio": {
                "status": "current",
                "positions": [{"symbol": "ETH-USD", "quantity": "1", "average_price": value}],
            }
        }
        return build_dashboard(snapshot, role="admin")["portfolio"]["positions"][0]["average_price"]

    assert (shown("0").kind, shown("0").text) == ("text", "0")  # an airdrop cost nothing
    assert shown(None).text == "not known"


def test_strategy_card_shows_the_worst_heartbeat():
    snapshot = {
        "strategies": [
            {"name": "primary", "version": "v1", "status": "healthy", "detail": "ok"},
            {"name": "hedge", "version": "v2", "status": "unhealthy", "detail": "cycle failed"},
            {"name": "probe", "version": "v3", "status": "unknown", "detail": "no heartbeat"},
        ]
    }
    strategy = next(
        item
        for item in build_dashboard(snapshot, role="operator")["cards"]
        if item.label == "Strategy"
    )
    assert (strategy.status.word, strategy.status.tone) == ("unhealthy", "crit")
    assert strategy.detail == "hedge (v2): cycle failed; 2 more in System health"


@pytest.mark.asyncio
async def test_state_text_is_escaped_not_rendered_as_markup(monkeypatch):
    application = dashboard_app(monkeypatch)
    await application.state.operator_state.emit_alert(
        Alert(condition="<b>x</b>", severity="critical", message="<script>alert(1)</script>")
    )
    application.state.operator_state.set_runtime("degraded", '<img src=x onerror="alert(1)">')
    html = await page(application)
    assert "<script" not in html
    assert "<img" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "&lt;b&gt;x&lt;/b&gt;" in html


# Browser-tab icon


def icon_sizes(data: bytes) -> list[int]:
    """Edge lengths listed in an ICO directory; a stored 0 means 256."""

    reserved, kind, count = struct.unpack_from("<HHH", data, 0)
    assert (reserved, kind) == (0, 1)
    return [data[6 + 16 * index] or 256 for index in range(count)]


@pytest.mark.asyncio
async def test_favicon_is_served_without_a_session_as_a_cached_icon(monkeypatch):
    transport = httpx.ASGITransport(app=dashboard_app(monkeypatch))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/favicon.ico")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/x-icon"
    assert response.headers["cache-control"] == "public, max-age=86400"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "set-cookie" not in response.headers
    assert response.content.startswith(b"\x00\x00\x01\x00")
    assert {16, 32, 48, 256} <= set(icon_sizes(response.content))


def test_committed_icon_has_the_recorded_checksum():
    digest = hashlib.sha256((ICON_DIR / "bitcoin.ico").read_bytes()).hexdigest().upper()
    assert f"SHA-256: `{digest}`" in (ICON_DIR / "README.md").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_dashboard_and_sign_in_pages_link_the_icon(monkeypatch):
    application = dashboard_app(monkeypatch)
    assert ICON_LINK in await page(application)
    assert ICON_LINK in await page(application, headers={}, path="/operator/login")
