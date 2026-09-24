"""Build the explicitly configured broker used by startup recovery."""

from __future__ import annotations

import os

from brokers.coinbase import CoinbaseBroker
from brokers.gemini import GeminiBroker
from brokers.interface import BrokerInterface
from core.guards import CredentialScope, StartupGuardError, StartupSettings
from core.models import TradingMode


def build_startup_broker(settings: StartupSettings) -> BrokerInterface | None:
    """Construct a provider adapter only when the operator selected one.

    An empty provider keeps backtest/replay and paper deployments without
    credentials deterministic. Once a provider is selected, missing credentials
    or a live/sandbox mismatch fail before the application accepts requests.
    """

    provider = os.environ.get("BROKER_PROVIDER", "").strip().lower()
    if not provider:
        return None
    if settings.credential_scope is CredentialScope.NONE:
        raise StartupGuardError(
            "CREDENTIAL_SCOPE must be view or trade when BROKER_PROVIDER is selected"
        )
    if provider == "coinbase":
        return CoinbaseBroker(
            api_key=_required("COINBASE_API_KEY"),
            private_key=_required("COINBASE_PRIVATE_KEY"),
        )
    if provider in {"gemini", "gemini-sandbox"}:
        if settings.trading_mode is TradingMode.LIVE:
            raise StartupGuardError("Gemini Sandbox cannot be used for live mode")
        return GeminiBroker(
            api_key=_required("GEMINI_API_KEY"),
            api_secret=_required("GEMINI_API_SECRET"),
        )
    raise StartupGuardError("BROKER_PROVIDER must be coinbase or gemini-sandbox")


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise StartupGuardError(f"{name} must be configured when BROKER_PROVIDER is selected")
    return value
