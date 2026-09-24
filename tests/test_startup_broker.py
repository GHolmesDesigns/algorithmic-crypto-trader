import pytest
from app.startup_broker import build_startup_broker
from core.guards import CredentialScope, StartupGuardError, StartupSettings
from core.models import TradingMode


def settings(
    mode: TradingMode = TradingMode.PAPER,
    scope: CredentialScope = CredentialScope.VIEW,
) -> StartupSettings:
    return StartupSettings(mode, scope, "", "postgresql://unused", "INFO")


def test_no_provider_keeps_local_startup_without_a_broker(monkeypatch) -> None:
    monkeypatch.delenv("BROKER_PROVIDER", raising=False)

    assert build_startup_broker(settings(scope=CredentialScope.NONE)) is None


def test_gemini_sandbox_requires_credentials(monkeypatch) -> None:
    monkeypatch.setenv("BROKER_PROVIDER", "gemini-sandbox")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_SECRET", raising=False)

    with pytest.raises(StartupGuardError, match="GEMINI_API_KEY"):
        build_startup_broker(settings())


def test_gemini_sandbox_is_refused_for_live_mode(monkeypatch) -> None:
    monkeypatch.setenv("BROKER_PROVIDER", "gemini-sandbox")
    monkeypatch.setenv("GEMINI_API_KEY", "sandbox-key")
    monkeypatch.setenv("GEMINI_API_SECRET", "sandbox-secret")

    with pytest.raises(StartupGuardError, match="cannot be used for live"):
        build_startup_broker(settings(TradingMode.LIVE, CredentialScope.TRADE))
