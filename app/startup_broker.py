"""Build the explicitly configured broker used by startup recovery."""

from __future__ import annotations

import os

from brokers.coinbase import CoinbaseBroker
from brokers.gemini import GeminiBroker
from brokers.interface import BrokerInterface
from core.guards import CredentialScope, StartupGuardError, StartupSettings
from core.models import TradingMode


def build_startup_broker(settings: StartupSettings) -> BrokerInterface | None:
    """Construct the adapter that holds this mode's orders, if one is selected.

    Recovery reconciles persisted orders and portfolio state against this broker,
    so it must be the venue that actually received the orders: Coinbase in
    `live`, Gemini Sandbox in `paper`. Paper orders against live Coinbase prices
    go to the in-memory simulator, so reconciling them against a real Coinbase
    account would halt every start and overwrite the paper baseline. An empty
    provider keeps credential-free deployments deterministic. Any mismatch or
    missing credential fails before the application accepts requests.
    """

    provider = os.environ.get("BROKER_PROVIDER", "").strip().lower()
    if not provider:
        return None
    if settings.credential_scope is CredentialScope.NONE:
        raise StartupGuardError(
            "CREDENTIAL_SCOPE must be view or trade when BROKER_PROVIDER is selected"
        )
    if provider == "coinbase":
        if settings.trading_mode is not TradingMode.LIVE:
            raise StartupGuardError(
                "BROKER_PROVIDER=coinbase is only valid in live mode; paper orders go to the "
                "simulator or gemini-sandbox"
            )
        return CoinbaseBroker(
            api_key=_required("COINBASE_API_KEY"),
            private_key=_required("COINBASE_PRIVATE_KEY"),
        )
    if provider in {"gemini", "gemini-sandbox"}:
        if settings.trading_mode is not TradingMode.PAPER:
            raise StartupGuardError("BROKER_PROVIDER=gemini-sandbox is only valid in paper mode")
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
