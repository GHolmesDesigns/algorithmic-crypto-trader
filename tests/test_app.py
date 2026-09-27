import httpx
import pytest
from api.alerts import Alert, AlertRouter
from app.main import create_app
from core.guards import CredentialScope, StartupSettings
from core.models import Balance, KillSwitchState, Position, TradingMode, utc_now
from risk.kill_switch import KillSwitch

from tests.operator_support import REARM, journaled_app

OPERATOR = {"x-operator-token": "operator-secret"}
ADMIN = {"x-operator-token": "admin-secret"}


class UnavailableBroker:
    async def get_balances(self):
        raise RuntimeError("provider payload must not reach the operator surface")

    async def get_positions(self):
        raise RuntimeError("provider payload must not reach the operator surface")


class HealthyBroker:
    async def get_balances(self):
        return (Balance(asset="USD", available="100", as_of=utc_now()),)

    async def get_positions(self):
        return (Position(symbol="BTC-USD", quantity="0.1", average_price="100", as_of=utc_now()),)


class RecordingSink:
    def __init__(self):
        self.alerts = []

    async def send(self, alert):
        self.alerts.append(alert)


@pytest.mark.asyncio
async def test_health_endpoint_does_not_initialize_a_broker() -> None:
    settings = StartupSettings(
        TradingMode.BACKTEST, CredentialScope.NONE, "", "postgresql://unused", "INFO"
    )
    application = create_app(settings)
    route = next(route for route in application.routes if getattr(route, "path", None) == "/health")
    assert await route.endpoint() == {"status": "ok"}


@pytest.mark.asyncio
async def test_scripts_use_the_header_and_url_tokens_are_refused(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("OPERATOR_TOKEN", "operator-secret")
    monkeypatch.setenv("KILL_SWITCH_FILE", str(tmp_path / "kill-switch.json"))
    application = journaled_app(tmp_path)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for method, path in (
            ("GET", "/operator"),
            ("GET", "/operator/state"),
            ("POST", "/operator/pause"),
            ("POST", "/operator/rearm"),
        ):
            refused = await client.request(method, path, params={"token": "operator-secret"})
            assert refused.status_code == 400, path
            assert "not accepted in URLs" in refused.json()["detail"]
            assert "set-cookie" not in refused.headers
        assert (await client.get("/operator/kill-switch", headers=OPERATOR)).json() == {
            "state": "running"
        }
        paused = (await client.post("/operator/pause", headers=OPERATOR)).json()
        assert paused["action"] == "pause"
        assert paused["state"] == "paused"
        assert paused["changed"] is True
        assert paused["role"] == "admin"  # a lone operator token has the administrator role
        stopped = await client.post("/operator/emergency-stop", headers=OPERATOR)
        assert stopped.json()["state"] == "halted"
        assert "set-cookie" not in stopped.headers
        rearmed = await client.post("/operator/rearm", headers=OPERATOR, json=REARM)
        assert rearmed.json()["state"] == "running"
        assert (await client.post("/operator/pause")).status_code == 401


@pytest.mark.asyncio
async def test_dashboard_is_degraded_when_broker_is_unavailable_and_does_not_render_token(
    monkeypatch,
):
    monkeypatch.setenv("OPERATOR_TOKEN", "operator-secret")
    settings = StartupSettings(
        TradingMode.PAPER, CredentialScope.NONE, "", "postgresql://unused", "INFO"
    )
    application = create_app(settings, broker=UnavailableBroker())
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/operator", headers={"x-operator-token": "operator-secret"})
        assert response.status_code == 200
        assert "broker is unavailable" in response.text
        assert "operator-secret" not in response.text
        state = await client.get("/operator/state", headers={"x-operator-token": "operator-secret"})
        assert state.json()["connectivity"]["status"] == "unavailable"
        assert state.json()["portfolio"]["status"] == "unavailable"
        assert state.json()["errors"][0]["condition"] == "broker_unavailable"


@pytest.mark.asyncio
async def test_form_login_establishes_cookie_for_javascript_free_controls(monkeypatch):
    monkeypatch.setenv("OPERATOR_TOKEN", "operator-secret")
    settings = StartupSettings(
        TradingMode.BACKTEST, CredentialScope.NONE, "", "postgresql://unused", "INFO"
    )
    application = create_app(settings)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        login = await client.post(
            "/operator/login",
            data={"token": "operator-secret"},
            headers={"accept": "text/html"},
            follow_redirects=False,
        )
        assert login.status_code == 303
        assert "operator_session" in login.headers["set-cookie"]
        dashboard = await client.get("/operator")
        assert dashboard.status_code == 200
        control = await client.post("/operator/pause", headers={"accept": "text/html"})
        assert control.status_code == 200
        assert "paused" in control.text


@pytest.mark.asyncio
async def test_current_portfolio_and_strategy_heartbeats_are_exposed(monkeypatch):
    monkeypatch.setenv("OPERATOR_TOKEN", "operator-secret")
    settings = StartupSettings(
        TradingMode.PAPER, CredentialScope.NONE, "", "postgresql://unused", "INFO"
    )
    application = create_app(settings, broker=HealthyBroker())
    application.state.operator_state.heartbeat("primary", detail="cycle complete")
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/health/detail", headers={"x-operator-token": "operator-secret"}
        )
        assert response.json()["status"] == "healthy"
        assert response.json()["strategies"][0]["status"] == "healthy"
        state = await client.get("/operator/state", headers={"x-operator-token": "operator-secret"})
        assert state.json()["portfolio"]["status"] == "current"
        assert state.json()["portfolio"]["balances"][0]["asset"] == "USD"


@pytest.mark.asyncio
async def test_admin_authorization_and_alert_fanout(monkeypatch, tmp_path):
    monkeypatch.setenv("OPERATOR_TOKEN", "operator-secret")
    monkeypatch.setenv("OPERATOR_ADMIN_TOKEN", "admin-secret")
    phone = RecordingSink()
    email = RecordingSink()
    router = AlertRouter(phone_push=phone, email=email)
    application = journaled_app(tmp_path, alert_router=router)
    await application.state.operator_state.emit_alert(
        Alert(condition="broker_unavailable", severity="critical", message="broker offline")
    )
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        stop = await client.post("/operator/emergency-stop", headers=ADMIN)
        assert stop.json()["state"] == "halted"
        assert (
            await client.post("/operator/rearm", headers=OPERATOR, json=REARM)
        ).status_code == 403
        assert (await client.post("/operator/rearm", headers=ADMIN, json=REARM)).json()[
            "state"
        ] == "running"
        state = await client.get("/operator/state", headers=ADMIN)
        assert state.json()["alert_destinations"] == ["phone_push", "email"]
        assert [item.condition for item in phone.alerts] == ["broker_unavailable"]
        assert [item.condition for item in email.alerts] == ["broker_unavailable"]


def _control_app(monkeypatch, tmp_path):
    monkeypatch.setenv("OPERATOR_TOKEN", "operator-secret")
    monkeypatch.setenv("OPERATOR_ADMIN_TOKEN", "admin-secret")
    monkeypatch.setenv("KILL_SWITCH_FILE", str(tmp_path / "kill-switch.json"))
    return journaled_app(tmp_path)


def _transitions(application):
    return [(event["from"], event["to"]) for event in application.state.kill_switch.audit_events]


async def _login(client, token="operator-secret"):
    login = await client.post(
        "/operator/login",
        data={"token": token},
        headers={"accept": "text/html"},
        follow_redirects=False,
    )
    assert login.status_code == 303
    return login


@pytest.mark.asyncio
async def test_operator_pause_cannot_lower_a_halt_with_a_token(monkeypatch, tmp_path):
    application = _control_app(monkeypatch, tmp_path)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        stop = await client.post("/operator/emergency-stop", headers=OPERATOR)
        assert stop.json()["state"] == "halted"
        pause = await client.post("/operator/pause", headers=OPERATOR)
        assert pause.status_code == 200
        assert pause.json()["state"] == "halted"
        assert pause.json()["changed"] is False
        again = await client.post("/operator/emergency-stop", headers=OPERATOR)
        assert again.json()["state"] == "halted"
        page = await client.post("/operator/pause", headers={**OPERATOR, "accept": "text/html"})
        assert "Already halted" in page.text
        assert "administrator re-arm" in page.text
        status = await client.get("/operator/kill-switch", headers=OPERATOR)
        assert status.json() == {"state": "halted"}
    assert KillSwitch(tmp_path / "kill-switch.json").state is KillSwitchState.HALTED
    assert _transitions(application) == [("running", "halted")]


@pytest.mark.asyncio
async def test_operator_pause_cannot_lower_a_halt_with_a_session(monkeypatch, tmp_path):
    application = _control_app(monkeypatch, tmp_path)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await _login(client)
        assert (await client.post("/operator/emergency-stop")).json()["state"] == "halted"
        page = await client.post("/operator/pause", headers={"accept": "text/html"})
        assert page.status_code == 200
        assert "Already halted" in page.text
        assert "administrator re-arm" in page.text
        assert (await client.post("/operator/pause")).json()["state"] == "halted"
        assert (await client.post("/operator/rearm", data=REARM)).status_code == 403
    assert KillSwitch(tmp_path / "kill-switch.json").state is KillSwitchState.HALTED
    assert _transitions(application) == [("running", "halted")]


@pytest.mark.asyncio
async def test_operator_stops_only_ever_tighten(monkeypatch, tmp_path):
    application = _control_app(monkeypatch, tmp_path)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.post("/operator/pause", headers=OPERATOR)).json()["state"] == "paused"
        page = await client.post("/operator/pause", headers={**OPERATOR, "accept": "text/html"})
        assert "Already paused" in page.text
        assert "PAUSE cannot lower a halt" not in page.text
        status = await client.get("/operator/kill-switch", headers=OPERATOR)
        assert status.json() == {"state": "paused"}
        stop = await client.post("/operator/emergency-stop", headers=OPERATOR)
        assert stop.json()["state"] == "halted"
    assert _transitions(application) == [("running", "paused"), ("paused", "halted")]


@pytest.mark.asyncio
async def test_only_an_administrator_rearm_lowers_the_switch(monkeypatch, tmp_path):
    application = _control_app(monkeypatch, tmp_path)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for stop in ("/operator/emergency-stop", "/operator/pause"):
            await client.post(stop, headers=OPERATOR)
            denied = await client.post("/operator/rearm", headers=OPERATOR, json=REARM)
            assert denied.status_code == 403
            rearm = await client.post(
                "/operator/rearm", headers={**ADMIN, "accept": "text/html"}, data=REARM
            )
            assert "Re-arm applied" in rearm.text
            status = await client.get("/operator/kill-switch", headers=ADMIN)
            assert status.json() == {"state": "running"}
        repeat = await client.post(
            "/operator/rearm", headers={**ADMIN, "accept": "text/html"}, data=REARM
        )
        assert "Already running" in repeat.text
    assert KillSwitch(tmp_path / "kill-switch.json").state is KillSwitchState.RUNNING
    assert _transitions(application) == [
        ("running", "halted"),
        ("halted", "running"),
        ("running", "paused"),
        ("paused", "running"),
    ]


@pytest.mark.asyncio
async def test_dashboard_hides_pause_while_halted(monkeypatch, tmp_path):
    application = _control_app(monkeypatch, tmp_path)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        running = (await client.get("/operator/fragment", headers=OPERATOR)).text
        assert 'action="/operator/pause"' in running
        await client.post("/operator/emergency-stop", headers=OPERATOR)
        halted = (await client.get("/operator/fragment", headers=OPERATOR)).text
        assert 'action="/operator/pause"' not in halted
        assert 'action="/operator/emergency-stop"' in halted
        assert "administrator re-arm required" in halted
        assert "/operator/rearm" not in halted
        admin_view = (await client.get("/operator", headers=ADMIN)).text
        assert 'action="/operator/pause"' not in admin_view
        assert 'href="/operator/rearm"' in admin_view
        # The dashboard links to the review; it never posts a re-arm by itself.
        assert 'action="/operator/rearm"' not in admin_view
