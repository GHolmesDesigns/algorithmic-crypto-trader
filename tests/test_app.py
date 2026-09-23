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
