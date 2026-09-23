import pytest
from core.guards import (
    CredentialScope,
    StartupGuardError,
    StartupSettings,
    assert_startup_safe,
    load_startup_settings,
)
from core.models import TradingMode


def settings(mode: TradingMode, scope: CredentialScope, confirmation: str = "") -> StartupSettings:
    return StartupSettings(
        mode, scope, confirmation, "postgresql+psycopg://trader:trader@localhost/trader", "INFO"
    )


def test_literal_modes_are_parsed() -> None:
    loaded = load_startup_settings({"TRADING_MODE": "paper", "CREDENTIAL_SCOPE": "none"})
    assert loaded.trading_mode is TradingMode.PAPER


@pytest.mark.parametrize("mode", [TradingMode.BACKTEST, TradingMode.REPLAY, TradingMode.PAPER])
def test_trade_scope_is_rejected_before_non_live_start(mode: TradingMode) -> None:
    with pytest.raises(StartupGuardError, match="trade-capable"):
        assert_startup_safe(settings(mode, CredentialScope.TRADE))


def test_live_requires_trade_scope_and_explicit_confirmation() -> None:
    with pytest.raises(StartupGuardError, match="trade-capable"):
        assert_startup_safe(
            settings(TradingMode.LIVE, CredentialScope.VIEW, "I_UNDERSTAND_LIVE_TRADING")
        )
    with pytest.raises(StartupGuardError, match="explicit"):
        assert_startup_safe(settings(TradingMode.LIVE, CredentialScope.TRADE))
    assert_startup_safe(
        settings(TradingMode.LIVE, CredentialScope.TRADE, "I_UNDERSTAND_LIVE_TRADING")
    )


def test_unknown_mode_fails_closed() -> None:
    with pytest.raises(StartupGuardError, match="TRADING_MODE"):
        load_startup_settings({"TRADING_MODE": "production", "CREDENTIAL_SCOPE": "none"})
