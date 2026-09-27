"""Safety controls end on a result page; sign-out ends the session; re-arm is a recorded review."""

import re
import time
from html.parser import HTMLParser
from pathlib import Path

import httpx
import pytest
from api.controls import REARM_CHECKLIST, REASON_LIMIT
from api.dashboard import Status, build_rearm_review
from app.main import create_app
from core.models import KillSwitchState
from db.models import SystemEventRecord
from risk.kill_switch import KillSwitch
from risk.kill_switch_journal import EVENT_TYPE
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from tests.operator_support import REARM, journaled_app, sqlite_settings

OPERATOR_TOKEN = "operator-secret-7f3a9c"
ADMIN_TOKEN = "admin-secret-51d0e2"
OPERATOR = {"x-operator-token": OPERATOR_TOKEN}
ADMIN = {"x-operator-token": ADMIN_TOKEN}
# A no-JavaScript browser posts a form and asks for HTML.
BROWSER = {"accept": "text/html,application/xhtml+xml"}


@pytest.fixture
def tokens(monkeypatch, tmp_path):
    monkeypatch.setenv("OPERATOR_TOKEN", OPERATOR_TOKEN)
    monkeypatch.setenv("OPERATOR_ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("KILL_SWITCH_FILE", str(tmp_path / "kill-switch.json"))


def client_for(application) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test")


async def sign_in(client: httpx.AsyncClient, token: str) -> httpx.Response:
    response = await client.post(
        "/operator/login", data={"token": token}, headers=BROWSER, follow_redirects=False
    )
    assert response.status_code == 303
    return response


def saved_transitions(tmp_path: Path) -> list[dict]:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'trader.db'}", future=True)
    with Session(engine) as session:
        rows = session.scalars(
            select(SystemEventRecord)
            .where(SystemEventRecord.event_type == EVENT_TYPE)
            .order_by(SystemEventRecord.created_at)
        ).all()
        payloads = [dict(row.payload) for row in rows]
    engine.dispose()
    return payloads


def assert_result_page(html: str, *, action: str, heading: str, role: str) -> None:
    assert f'<h1 id="result-heading">{heading}</h1>' in html
    assert f"<dt>Action</dt><dd>{action}</dd>" in html
    assert "<dt>Resulting state</dt>" in html
    assert "<dt>Changed</dt><dd>" in html
    assert re.search(r'<dt>Time \(UTC\)</dt><dd><time datetime="[^"]+">[^<]+ UTC</time>', html)
    assert f"<dt>Acting role</dt><dd>{role}</dd>" in html
    assert "<strong>Next safe step:</strong>" in html
    assert "<script" not in html


# Result pages


@pytest.mark.asyncio
async def test_each_form_post_lands_on_a_result_page(tokens, tmp_path):
    async with client_for(journaled_app(tmp_path)) as client:
        await sign_in(client, OPERATOR_TOKEN)
        dashboard = (await client.get("/operator")).text
        assert (
            '<form class="signout" method="post" action="/operator/logout">'
            '<button class="button button-quiet" type="submit">Sign out</button></form>'
        ) in dashboard
        paused = await client.post("/operator/pause", headers=BROWSER)
        assert paused.status_code == 200
        assert_result_page(paused.text, action="PAUSE", heading="Pause applied", role="Operator")
        assert "Yes: running to paused." in paused.text
        assert 'aria-hidden="true">▲</span> paused</span>' in paused.text
        assert '<a class="button button-quiet" href="/operator">Return to dashboard</a>' in (
            paused.text
        )

        stopped = await client.post("/operator/emergency-stop", headers=BROWSER)
        assert_result_page(
            stopped.text, action="EMERGENCY STOP", heading="Emergency stop applied", role="Operator"
        )
        assert "Yes: paused to halted." in stopped.text
        assert "Leave it halted and escalate." in stopped.text

        again = await client.post("/operator/pause", headers=BROWSER)
        assert_result_page(again.text, action="PAUSE", heading="Already halted", role="Operator")
        assert "No: the kill switch was already halted." in again.text
        assert "PAUSE cannot lower a halt" in again.text

        out = await client.post("/operator/logout", headers=BROWSER)
        assert_result_page(out.text, action="Sign out", heading="Signed out", role="Operator")
        assert "Signed out. The session cookie is cleared." in out.text
        assert '<a class="button button-quiet" href="/operator/login">Sign in again</a>' in out.text
        assert "<dt>Signed in as</dt>\n            <dd>Signed out</dd>" in out.text
        assert 'action="/operator/logout"' not in out.text

        await sign_in(client, ADMIN_TOKEN)
        rearmed = await client.post("/operator/rearm", headers=BROWSER, data=REARM)
        assert rearmed.status_code == 200
        assert_result_page(
            rearmed.text, action="RE-ARM", heading="Re-arm applied", role="Administrator"
        )
        assert "Yes: halted to running." in rearmed.text
        assert "no new error or divergence" in rearmed.text


@pytest.mark.asyncio
async def test_scripts_receive_the_same_result_as_json(tokens, tmp_path):
    async with client_for(journaled_app(tmp_path)) as client:
        result = (await client.post("/operator/emergency-stop", headers=OPERATOR)).json()
        assert result.pop("at")
        assert result == {
            "action": "emergency_stop",
            "state": "halted",
            "changed": True,
            "role": "operator",
        }


# Sign-out


@pytest.mark.asyncio
async def test_after_sign_out_the_next_request_is_refused(tokens, tmp_path):
    async with client_for(journaled_app(tmp_path)) as client:
        await sign_in(client, OPERATOR_TOKEN)
        copied = client.cookies.get("operator_session")
        assert (await client.get("/operator")).status_code == 200
        out = await client.post("/operator/logout", headers=BROWSER)
        assert out.status_code == 200
        cleared = out.headers["set-cookie"]
        assert cleared.startswith('operator_session="";') or "Max-Age=0" in cleared
        assert "httponly" in cleared.lower() and "samesite=strict" in cleared.lower()
        assert (await client.get("/operator")).status_code == 401
        assert (await client.post("/operator/pause")).status_code == 401
        # A copy of the old cookie is refused too: the server revoked the session.
        replayed = {"cookie": f"operator_session={copied}"}
        assert (await client.get("/operator/state", headers=replayed)).status_code == 401
        assert (await client.post("/operator/pause", headers=replayed)).status_code == 401
    assert KillSwitch(tmp_path / "kill-switch.json").state is KillSwitchState.RUNNING


@pytest.mark.asyncio
async def test_sign_out_revokes_only_its_own_session(tokens, tmp_path):
    application = journaled_app(tmp_path)
    async with client_for(application) as first, client_for(application) as second:
        await sign_in(first, OPERATOR_TOKEN)
        await sign_in(second, OPERATOR_TOKEN)
        await first.post("/operator/logout")
        assert (await first.get("/operator")).status_code == 401
        assert (await second.get("/operator")).status_code == 200


@pytest.mark.asyncio
async def test_sign_out_without_a_session_still_clears_the_cookie(tokens, tmp_path):
    async with client_for(journaled_app(tmp_path)) as client:
        client.cookies.set("operator_session", "operator.1.forged.signature")
        out = await client.post("/operator/logout", headers=BROWSER)
        assert out.status_code == 200
        assert '<h1 id="result-heading">No active session</h1>' in out.text
        assert "None: no session was active" in out.text
        assert "operator_session" in out.headers["set-cookie"]
        assert (await client.post("/operator/logout")).json()["changed"] is False


# Tokens never travel in URLs


class _Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls: list[str] = []
        self.forms: list[dict[str, str | None]] = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        for name in ("href", "action", "src", "formaction"):
            if attributes.get(name):
                self.urls.append(str(attributes[name]))
        if tag == "form":
            self.forms.append(attributes)


def urls_in(html: str) -> _Links:
    links = _Links()
    links.feed(html)
    return links


@pytest.mark.asyncio
async def test_no_token_appears_in_any_url_the_browser_flow_produces(tokens, tmp_path):
    seen: list[str] = []
    async with client_for(journaled_app(tmp_path)) as client:
        login_page = await client.get("/operator/login")
        links = urls_in(login_page.text)
        assert links.forms == [{"class": "panel", "method": "post", "action": "/operator/login"}]
        seen += links.urls
        for token in (OPERATOR_TOKEN, ADMIN_TOKEN):
            login = await sign_in(client, token)
            seen.append(login.headers["location"])
            pages = [
                await client.get("/operator"),
                await client.get("/operator/fragment"),
                await client.post("/operator/pause", headers=BROWSER),
                await client.post("/operator/emergency-stop", headers=BROWSER),
            ]
            if token == ADMIN_TOKEN:
                pages.append(await client.get("/operator/rearm"))
                pages.append(
                    await client.post(
                        "/operator/rearm", headers=BROWSER, data={"checklist": "cause_documented"}
                    )
                )
                pages.append(await client.post("/operator/rearm", headers=BROWSER, data=REARM))
            pages.append(await client.post("/operator/logout", headers=BROWSER))
            for response in pages:
                assert response.status_code in {200, 422}, response.request.url
                seen.append(str(response.request.url))
                seen += urls_in(response.text).urls
                for form in urls_in(response.text).forms:
                    assert form["method"] == "post"
    assert seen
    for url in seen:
        assert "token" not in url.lower(), url
        assert OPERATOR_TOKEN not in url and ADMIN_TOKEN not in url, url


# Re-arm review page


@pytest.mark.asyncio
async def test_rearm_review_repeats_the_warnings_and_requires_every_item(tokens, tmp_path):
    application = journaled_app(tmp_path)
    application.state.kill_switch.trip("startup recovery: broker has no record of an order")
    async with client_for(application) as client:
        await sign_in(client, ADMIN_TOKEN)
        html = (await client.get("/operator/rearm")).text
    assert '<h1 id="rearm-heading">Re-arm review</h1>' in html
    for label in (
        "Startup recovery",
        "Reconciliation",
        "Pending or unknown orders",
        "Strategy heartbeat",
    ):
        assert f"<dt>{label}</dt>" in html
    assert "Last change:</strong> running to halted by The system (automatic)" in html
    assert "startup recovery: broker has no record of an order" in html
    assert html.count('type="checkbox" name="checklist"') == len(REARM_CHECKLIST) == 7
    for key, text in REARM_CHECKLIST:
        assert f'value="{key}" required>' in html
        assert text in html
    assert (
        f'<textarea id="rearm-reason" name="reason" rows="3" required maxlength="{REASON_LIMIT}"'
    ) in html
    assert '<form class="panel rearm-form" method="post" action="/operator/rearm">' in html


@pytest.mark.asyncio
async def test_rearm_review_has_nothing_to_submit_while_running(tokens, tmp_path):
    async with client_for(journaled_app(tmp_path)) as client:
        html = (await client.get("/operator/rearm", headers=ADMIN)).text
    assert "Nothing to re-arm: the kill switch is running." in html
    assert 'action="/operator/rearm"' not in html


@pytest.mark.asyncio
async def test_operator_role_cannot_open_or_submit_the_review(tokens, tmp_path):
    application = journaled_app(tmp_path)
    application.state.kill_switch.trip("test halt")
    async with client_for(application) as client:
        assert (await client.get("/operator/rearm", headers=OPERATOR)).status_code == 403
        assert (
            await client.post("/operator/rearm", headers=OPERATOR, json=REARM)
        ).status_code == 403
        await sign_in(client, OPERATOR_TOKEN)
        assert (await client.get("/operator/rearm")).status_code == 403
        denied = await client.post("/operator/rearm", headers=BROWSER, data=REARM)
        assert denied.status_code == 403
        client.cookies.clear()
        assert (await client.post("/operator/rearm", data=REARM)).status_code == 401
    assert application.state.kill_switch.state is KillSwitchState.HALTED
    assert [event["to"] for event in saved_transitions(tmp_path)] == ["halted"]


# Server enforcement


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [key for key, _ in REARM_CHECKLIST])
async def test_rearm_missing_any_checklist_item_is_refused(tokens, tmp_path, missing):
    application = journaled_app(tmp_path)
    application.state.kill_switch.trip("test halt")
    body = {**REARM, "checklist": [key for key in REARM["checklist"] if key != missing]}
    async with client_for(application) as client:
        response = await client.post("/operator/rearm", headers=ADMIN, json=body)
    assert response.status_code == 422
    assert response.json()["detail"]["missing_checklist"] == [missing]
    assert response.json()["detail"]["state"] == "halted"
    assert application.state.kill_switch.state is KillSwitchState.HALTED
    assert KillSwitch(tmp_path / "kill-switch.json").state is KillSwitchState.HALTED
    assert [event["to"] for event in saved_transitions(tmp_path)] == ["halted"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason",
    ["", "   \n\t ", "x" * (REASON_LIMIT + 1)],
    ids=["empty", "blank", "too-long"],
)
async def test_rearm_without_a_usable_reason_is_refused(tokens, tmp_path, reason):
    application = journaled_app(tmp_path)
    application.state.kill_switch.tighten(KillSwitchState.PAUSED, reason="operator pause")
    async with client_for(application) as client:
        response = await client.post(
            "/operator/rearm", headers=ADMIN, json={**REARM, "reason": reason}
        )
        missing_field = await client.post(
            "/operator/rearm", headers=ADMIN, json={"checklist": REARM["checklist"]}
        )
    assert response.status_code == 422
    assert response.json()["detail"]["missing_checklist"] == []
    assert missing_field.status_code == 422
    assert application.state.kill_switch.state is KillSwitchState.PAUSED
    assert [event["to"] for event in saved_transitions(tmp_path)] == ["paused"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "content_type"),
    [(b"not json", "application/json"), (b"[1, 2]", "application/json"), (b"", "")],
    ids=["malformed-json", "json-array", "empty-body"],
)
async def test_rearm_with_an_unreadable_body_is_refused(tokens, tmp_path, body, content_type):
    application = journaled_app(tmp_path)
    application.state.kill_switch.trip("test halt")
    async with client_for(application) as client:
        response = await client.post(
            "/operator/rearm", headers={**ADMIN, "content-type": content_type}, content=body
        )
    assert response.status_code == 422
    assert application.state.kill_switch.state is KillSwitchState.HALTED


@pytest.mark.asyncio
async def test_a_refused_browser_rearm_redisplays_the_review(tokens, tmp_path):
    application = journaled_app(tmp_path)
    application.state.kill_switch.trip("test halt")
    ticked = ["cause_documented", "orders_resolved"]
    async with client_for(application) as client:
        await sign_in(client, ADMIN_TOKEN)
        response = await client.post(
            "/operator/rearm",
            headers=BROWSER,
            data={"checklist": ticked, "reason": "INC-7 <b>draft</b>"},
        )
    assert response.status_code == 422
    html = response.text
    assert 'role="alert"' in html
    assert "Re-arm refused. The kill switch is unchanged." in html
    assert "Confirm every checklist item. 5 not confirmed." in html
    for key, _ in REARM_CHECKLIST:
        checked = f'value="{key}" required checked>' in html
        assert checked is (key in ticked), key
    # The reference is shown back escaped, never as markup.
    assert "INC-7 &lt;b&gt;draft&lt;/b&gt;</textarea>" in html
    assert application.state.kill_switch.state is KillSwitchState.HALTED


@pytest.mark.asyncio
async def test_complete_admin_rearm_runs_and_persists_role_reason_and_checklist(tokens, tmp_path):
    application = journaled_app(tmp_path)
    async with client_for(application) as client:
        await client.post("/operator/emergency-stop", headers=OPERATOR)
        reason = (
            f"INC-9 approved by ops-lead@example.test token={OPERATOR_TOKEN} key "
            f"{ADMIN_TOKEN} see https://notify.example.test/private-topic "
            "ref 9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
        )
        response = await client.post(
            "/operator/rearm", headers=ADMIN, json={**REARM, "reason": reason}
        )
        assert response.status_code == 200
        assert response.json()["state"] == "running"
        assert response.json()["role"] == "admin"
        state = (await client.get("/operator/state", headers=ADMIN)).json()
    assert application.state.kill_switch.state is KillSwitchState.RUNNING
    assert KillSwitch(tmp_path / "kill-switch.json").state is KillSwitchState.RUNNING
    stop, rearm = saved_transitions(tmp_path)
    assert (stop["from"], stop["to"], stop["actor"], stop["automatic"]) == (
        "running",
        "halted",
        "operator",
        False,
    )
    assert stop["reason"] == "operator emergency stop"
    assert (rearm["from"], rearm["to"], rearm["actor"], rearm["automatic"]) == (
        "halted",
        "running",
        "admin",
        False,
    )
    assert rearm["checklist"] == [key for key, _ in REARM_CHECKLIST]
    assert rearm["reason"].startswith("INC-9 approved by [REDACTED]")
    for secret in (
        OPERATOR_TOKEN,
        ADMIN_TOKEN,
        "ops-lead@example.test",
        "notify.example.test",
        "private-topic",
        "9f86d081884c7d659a2feaa0",
    ):
        assert secret not in rearm["reason"], secret
        assert secret not in str(state), secret
    newest = state["risk"]["transitions"][0]
    assert newest["actor"] == "admin" and newest["to"] == "running"
    assert newest["reason"] == rearm["reason"]


class _BrokenJournal:
    def record(self, events):
        raise RuntimeError("database is down")

    def recent(self, limit):
        return []


@pytest.mark.asyncio
async def test_rearm_is_refused_when_it_cannot_be_recorded(tokens, tmp_path):
    application = journaled_app(tmp_path)
    switch = application.state.kill_switch
    switch.trip("test halt")
    switch.attach_journal(_BrokenJournal())
    async with client_for(application) as client:
        response = await client.post("/operator/rearm", headers=ADMIN, json=REARM)
        assert response.status_code == 503
        assert "could not be recorded" in response.json()["detail"]["errors"][0]
        await sign_in(client, ADMIN_TOKEN)
        page = await client.post("/operator/rearm", headers=BROWSER, data=REARM)
        assert page.status_code == 503
        assert "could not be recorded in the audit history" in page.text
    assert switch.state is KillSwitchState.HALTED
    assert KillSwitch(tmp_path / "kill-switch.json").state is KillSwitchState.HALTED
    assert [event["to"] for event in switch.audit_events] == ["halted"]


@pytest.mark.asyncio
async def test_rearm_is_refused_without_transition_history(tokens, tmp_path):
    # create_app alone has no journal: only the service lifespan attaches one.
    application = create_app(sqlite_settings(tmp_path))
    application.state.kill_switch.trip("test halt")
    async with client_for(application) as client:
        response = await client.post("/operator/rearm", headers=ADMIN, json=REARM)
    assert response.status_code == 503
    assert application.state.kill_switch.state is KillSwitchState.HALTED


# Persisted history


@pytest.mark.asyncio
async def test_kill_switch_transitions_survive_an_app_restart(tokens, tmp_path):
    # Only system_events exists, so startup recovery cannot read orders and halts.
    settings = sqlite_settings(tmp_path)

    first = create_app(settings, recover_on_start=True)
    async with first.router.lifespan_context(first), client_for(first) as client:
        assert first.state.operator_state.startup_recovery.status == "halted"
        await client.post("/operator/rearm", headers=ADMIN, json=REARM)
        await client.post("/operator/pause", headers=OPERATOR)

    second = create_app(settings, recover_on_start=True)
    async with second.router.lifespan_context(second), client_for(second) as client:
        transitions = (await client.get("/operator/state", headers=OPERATOR)).json()["risk"][
            "transitions"
        ]
        review = (await client.get("/operator/rearm", headers=ADMIN)).text

    changes = [(item["from"], item["to"], item["actor"], item["automatic"]) for item in transitions]
    assert changes == [
        ("paused", "halted", "system", True),
        ("running", "paused", "operator", False),
        ("halted", "running", "admin", False),
        ("running", "halted", "system", True),
    ]
    assert transitions[0]["reason"].startswith("startup recovery: persisted orders")
    assert transitions[2]["reason"] == REARM["reason"]
    assert all(item["created_at"] for item in transitions)
    assert review.count('<p class="event-meta">The system (automatic) · <time') == 2
    assert '<p class="event-meta">Operator (manual) · <time' in review
    assert '<p class="event-meta">Administrator (manual) · <time' in review
    assert len(saved_transitions(tmp_path)) == 4


@pytest.mark.asyncio
async def test_forged_expired_and_old_format_session_cookies_are_refused(tokens, tmp_path):
    application = journaled_app(tmp_path)
    async with client_for(application) as client:
        await sign_in(client, ADMIN_TOKEN)
        role, issued, nonce, signature = client.cookies["operator_session"].split(".")
        forged = {
            "tampered role": f"operator.{issued}.{nonce}.{signature}",
            "tampered signature": f"{role}.{issued}.{nonce}.{signature[::-1]}",
            "unknown role": f"root.{issued}.{nonce}.{signature}",
            "expired": f"{role}.{int(issued) - 9 * 60 * 60}.{nonce}.{signature}",
            "unreadable time": f"{role}.soon.{nonce}.{signature}",
            # The format before sign-out existed: role.issued.signature.
            "previous format": f"{role}.{issued}.{signature}",
        }
        client.cookies.clear()
        for name, value in forged.items():
            response = await client.get(
                "/operator/state", headers={"cookie": f"operator_session={value}"}
            )
            assert response.status_code == 401, name


@pytest.mark.asyncio
async def test_revoked_sessions_are_forgotten_once_they_would_have_expired(
    tokens, tmp_path, monkeypatch
):
    application = journaled_app(tmp_path)
    async with client_for(application) as client:
        await sign_in(client, OPERATOR_TOKEN)
        await client.post("/operator/logout")
        assert len(application.state.revoked_sessions) == 1
        clock = time.time() + 9 * 60 * 60
        monkeypatch.setattr(time, "time", lambda: clock)
        await sign_in(client, OPERATOR_TOKEN)
        await client.post("/operator/logout")
    # The first session has expired, so only the second is still held.
    assert len(application.state.revoked_sessions) == 1


def test_review_flags_pending_and_unknown_orders_and_a_missing_heartbeat():
    def snapshot(*statuses):
        return {
            "risk": {"kill_switch": "halted", "transitions": []},
            "orders": [{"status": status} for status in statuses],
            "strategies": [],
        }

    def orders_row(review):
        return next(
            row for row in review["warnings"] if row["label"] == "Pending or unknown orders"
        )

    unknown = build_rearm_review(snapshot("unknown", "pending_submit", "filled"))
    assert orders_row(unknown)["status"] == Status("1 unknown", "crit")
    assert (
        "1 pending submission and 1 unknown among the 3 order(s)" in orders_row(unknown)["detail"]
    )
    pending = build_rearm_review(snapshot("pending_submit"))
    assert orders_row(pending)["status"] == Status("1 pending", "warn")
    clear = build_rearm_review(snapshot("filled"))
    assert orders_row(clear)["status"] == Status("none", "ok")
    heartbeat = clear["warnings"][-1]
    assert heartbeat["status"] == Status("none registered", "unknown")
    # Recovery, reconciliation, and the heartbeat are unknown here; the orders are clear.
    assert clear["attention"] == 3
    assert clear["transitions"] == []
    assert clear["safety"].last_change is None
