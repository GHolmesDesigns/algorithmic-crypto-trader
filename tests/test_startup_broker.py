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

    with pytest.raises(StartupGuardError, match="only valid in paper mode"):
        build_startup_broker(settings(TradingMode.LIVE, CredentialScope.TRADE))


@pytest.mark.parametrize("mode", [TradingMode.PAPER, TradingMode.BACKTEST, TradingMode.REPLAY])
def test_coinbase_is_refused_outside_live_mode(monkeypatch, mode) -> None:
    # Paper orders go to the simulator; reconciling them against a real Coinbase
    # account would halt every start and overwrite the paper baseline.
    monkeypatch.setenv("BROKER_PROVIDER", "coinbase")
    monkeypatch.setenv("COINBASE_API_KEY", "organizations/x/apiKeys/y")
    monkeypatch.setenv("COINBASE_PRIVATE_KEY", "not-used")

    with pytest.raises(StartupGuardError, match="only valid in live mode"):
        build_startup_broker(settings(mode))


@pytest.mark.parametrize("mode", [TradingMode.BACKTEST, TradingMode.REPLAY])
def test_gemini_sandbox_is_refused_outside_paper_mode(monkeypatch, mode) -> None:
    monkeypatch.setenv("BROKER_PROVIDER", "gemini-sandbox")
    monkeypatch.setenv("GEMINI_API_KEY", "sandbox-key")
    monkeypatch.setenv("GEMINI_API_SECRET", "sandbox-secret")

    with pytest.raises(StartupGuardError, match="only valid in paper mode"):
        build_startup_broker(settings(mode))


def test_coinbase_live_and_gemini_paper_build_their_adapters(monkeypatch) -> None:
    monkeypatch.setenv("BROKER_PROVIDER", "coinbase")
    monkeypatch.setenv("COINBASE_API_KEY", "organizations/x/apiKeys/y")
    monkeypatch.setenv("COINBASE_PRIVATE_KEY", "not-used-until-a-request")
    coinbase = build_startup_broker(settings(TradingMode.LIVE, CredentialScope.TRADE))
    assert coinbase is not None and coinbase.capabilities.environment == "production"

    monkeypatch.setenv("BROKER_PROVIDER", "gemini-sandbox")
    monkeypatch.setenv("GEMINI_API_KEY", "sandbox-key")
    monkeypatch.setenv("GEMINI_API_SECRET", "sandbox-secret")
    gemini = build_startup_broker(settings(TradingMode.PAPER))
    assert gemini is not None and gemini.capabilities.environment == "sandbox"
