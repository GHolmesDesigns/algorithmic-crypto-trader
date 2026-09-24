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
        if settings.trading_mode is TradingMode.LIVE:
            raise StartupGuardError("live mode requires BROKER_PROVIDER=coinbase")
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


async def assert_live_key_scope(settings: StartupSettings, broker: BrokerInterface | None) -> None:
    """In live mode, require a Coinbase key that can trade and cannot move funds.

    ``CREDENTIAL_SCOPE=trade`` is the operator's declaration; this asks Coinbase
    what the configured key can actually do before anything else starts.
    """

    if settings.trading_mode is not TradingMode.LIVE:
        return
    key_permissions = getattr(broker, "key_permissions", None)
    if key_permissions is None:
        raise StartupGuardError("live mode requires the Coinbase broker")
    permissions = await key_permissions()
    if permissions.get("can_trade") is not True:
        raise StartupGuardError("live mode requires a trade-capable Coinbase key")
    if permissions.get("can_transfer") is not False:
        raise StartupGuardError("live mode refuses a Coinbase key that can transfer funds")


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise StartupGuardError(f"{name} must be configured when BROKER_PROVIDER is selected")
    return value
