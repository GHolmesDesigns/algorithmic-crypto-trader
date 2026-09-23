import httpx
import pytest
from app.main import create_app
from core.guards import CredentialScope, StartupSettings
from core.models import TradingMode


@pytest.mark.asyncio
async def test_health_endpoint_does_not_initialize_a_broker() -> None:
    settings = StartupSettings(
        TradingMode.BACKTEST, CredentialScope.NONE, "", "postgresql://unused", "INFO"
    )
    application = create_app(settings)
    route = next(route for route in application.routes if getattr(route, "path", None) == "/health")
    assert await route.endpoint() == {"status": "ok"}


@pytest.mark.asyncio
async def test_authenticated_operator_controls_work_without_javascript(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("OPERATOR_TOKEN", "operator-secret")
    monkeypatch.setenv("KILL_SWITCH_FILE", str(tmp_path / "kill-switch.json"))
    settings = StartupSettings(
        TradingMode.BACKTEST, CredentialScope.NONE, "", "postgresql://unused", "INFO"
    )
    application = create_app(settings)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (
            await client.get("/operator", params={"token": "operator-secret"})
        ).status_code == 200
        assert (
            await client.post("/operator/pause", params={"token": "operator-secret"})
        ).json() == {"state": "paused"}
        assert (
            await client.get(
                "/operator/kill-switch", headers={"x-operator-token": "operator-secret"}
            )
        ).json() == {"state": "paused"}
        assert (
            await client.post(
                "/operator/emergency-stop", headers={"x-operator-token": "operator-secret"}
            )
        ).json() == {"state": "halted"}
        assert (
            await client.post("/operator/rearm", params={"token": "operator-secret"})
        ).json() == {"state": "running"}
        assert (await client.post("/operator/pause")).status_code == 401
