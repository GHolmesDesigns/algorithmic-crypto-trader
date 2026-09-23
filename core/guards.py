"""Boot-time configuration and live-mode safety checks."""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum

from core.models import TradingMode


class CredentialScope(StrEnum):
    NONE = "none"
    VIEW = "view"
    TRADE = "trade"


class StartupGuardError(RuntimeError):
    """Raised before application components initialize when safety checks fail."""


@dataclass(frozen=True)
class StartupSettings:
    trading_mode: TradingMode
    credential_scope: CredentialScope
    live_confirmation: str
    database_url: str
    log_level: str


def _enum_value(enum_type: type[TradingMode] | type[CredentialScope], raw: str, name: str):
    try:
        return enum_type(raw.strip().lower())
    except ValueError as exc:
        allowed = ", ".join(item.value for item in enum_type)
        raise StartupGuardError(f"{name} must be one of: {allowed}") from exc


def load_startup_settings(environ: dict[str, str] | None = None) -> StartupSettings:
    values = os.environ if environ is None else environ
    mode = _enum_value(TradingMode, values.get("TRADING_MODE", "backtest"), "TRADING_MODE")
    scope = _enum_value(CredentialScope, values.get("CREDENTIAL_SCOPE", "none"), "CREDENTIAL_SCOPE")
    settings = StartupSettings(
        trading_mode=mode,
        credential_scope=scope,
        live_confirmation=values.get("LIVE_CONFIRMATION", ""),
        database_url=values.get(
            "DATABASE_URL", "postgresql+psycopg://trader:trader@localhost:5432/trader"
        ),
        log_level=values.get("LOG_LEVEL", "INFO").upper(),
    )
    assert_startup_safe(settings)
    return settings


def assert_startup_safe(settings: StartupSettings) -> None:
    non_live = {TradingMode.BACKTEST, TradingMode.REPLAY, TradingMode.PAPER}
    if settings.trading_mode in non_live and settings.credential_scope is CredentialScope.TRADE:
        raise StartupGuardError(
            f"refusing {settings.trading_mode.value}: trade-capable credentials are not allowed"
        )
    if settings.trading_mode is TradingMode.LIVE:
        if settings.credential_scope is not CredentialScope.TRADE:
            raise StartupGuardError("live mode requires a trade-capable credential scope")
        if settings.live_confirmation != "I_UNDERSTAND_LIVE_TRADING":
            raise StartupGuardError("live mode requires explicit LIVE_CONFIRMATION")


def startup_banner(settings: StartupSettings) -> str:
    confirmation = (
        "explicitly confirmed" if settings.trading_mode is TradingMode.LIVE else "not applicable"
    )
    return (
        f"TRADING SERVICE | mode={settings.trading_mode.value.upper()} | "
        f"credential_scope={settings.credential_scope.value} | live_confirmation={confirmation}"
    )
