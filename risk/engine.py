"""Ordered, fail-closed risk gates for every order-producing path."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from core.models import (
    FrozenModel,
    KillSwitchState,
    OrderSide,
    Quote,
    RiskApproval,
    Signal,
    utc_now,
)
from pydantic import Field


class ExchangeConstraints(FrozenModel):
    min_quantity: Decimal
    max_quantity: Decimal | None = None
    quantity_increment: Decimal
    min_notional: Decimal
    max_notional: Decimal | None = None
    price_increment: Decimal


class RiskLimits(FrozenModel):
    max_quote_age_seconds: int = 60
    max_volatility: Decimal = Decimal("0.10")
    max_reference_divergence: Decimal = Decimal("0.02")
    max_open_positions: int = 5
    max_trade_notional: Decimal = Decimal("1000")
    max_symbol_position: Decimal = Decimal("1")
    max_aggregate_allocation: Decimal = Decimal("10000")
    min_cash_reserve: Decimal = Decimal("100")
    max_daily_loss: Decimal = Decimal("100")
    max_drawdown: Decimal = Decimal("0.20")
    max_slippage: Decimal = Decimal("0.01")


class RiskInputs(FrozenModel):
    """Inputs are optional deliberately: omission is a refusal, never a pass."""

    kill_switch: KillSwitchState | None = None
    operator_paused: bool | None = None
    trading_window_open: bool | None = None
    broker_healthy: bool | None = None
    quote: Quote | None = None
    reference_price: Decimal | None = None
    volatility: Decimal | None = None
    duplicate_signal_ids: frozenset[UUID] | None = None
    symbol_cooldown_clear: bool | None = None
    open_positions: int | None = None
    open_notional: Decimal | None = None
    symbol_position: Decimal | None = None
    aggregate_allocation: Decimal | None = None
    available_cash: Decimal | None = None
    daily_loss: Decimal | None = None
    drawdown: Decimal | None = None
    expected_price: Decimal | None = None
    estimated_slippage: Decimal | None = None
    constraints: ExchangeConstraints | None = None
    now: datetime = Field(default_factory=utc_now)


def _reject(signal: Signal, gate: str, reason: str) -> RiskApproval:
    return RiskApproval(
        signal_id=signal.signal_id,
        approved=False,
        reason=reason,
        failed_gate=gate,
        correlation_id=signal.correlation_id,
    )


def evaluate(signal: Signal, inputs: RiskInputs, limits: RiskLimits | None = None) -> RiskApproval:
    """Evaluate all gates in their safety order and stop at the first refusal."""

    config = limits or RiskLimits()
    quote = inputs.quote

    if inputs.kill_switch is None:
        return _reject(signal, "kill_switch", "kill-switch state is unavailable")
    if inputs.kill_switch is not KillSwitchState.RUNNING:
        return _reject(signal, "kill_switch", f"kill switch is {inputs.kill_switch.value}")

    if inputs.operator_paused is None or inputs.trading_window_open is None:
        return _reject(
            signal, "operator_pause_window", "operator pause/window state is unavailable"
        )
    if inputs.operator_paused:
        return _reject(signal, "operator_pause_window", "operator pause is active")
    if not inputs.trading_window_open:
        return _reject(signal, "operator_pause_window", "trading window is closed")

    if inputs.broker_healthy is None:
        return _reject(signal, "broker_health", "broker health is unavailable")
    if not inputs.broker_healthy:
        return _reject(signal, "broker_health", "broker is unavailable or unhealthy")

    if not isinstance(quote, Quote) or quote.symbol != signal.symbol:
        return _reject(signal, "stale_price", "current quote is unavailable")
    age = (inputs.now - quote.as_of).total_seconds()
    if age < 0 or age > config.max_quote_age_seconds:
        return _reject(signal, "stale_price", "quote is stale or from the future")

    if inputs.volatility is None:
        return _reject(signal, "abnormal_volatility", "volatility input is unavailable")
    if inputs.volatility < 0 or inputs.volatility > config.max_volatility:
        return _reject(signal, "abnormal_volatility", "volatility exceeds the configured limit")

    if inputs.reference_price is None or inputs.expected_price is None:
        return _reject(signal, "price_reference", "price/reference input is unavailable")
    if inputs.reference_price <= 0 or inputs.expected_price <= 0:
        return _reject(signal, "price_reference", "price/reference must be positive")
    divergence = abs(inputs.expected_price - inputs.reference_price) / inputs.reference_price
    if divergence > config.max_reference_divergence:
        return _reject(signal, "price_reference", "price/reference divergence exceeds the limit")

    if inputs.duplicate_signal_ids is None:
        return _reject(signal, "duplicate_prevention", "duplicate-order state is unavailable")
    if signal.signal_id in inputs.duplicate_signal_ids:
        return _reject(signal, "duplicate_prevention", "signal already has an order")

    if inputs.symbol_cooldown_clear is None:
        return _reject(signal, "symbol_cooldown", "symbol cooldown state is unavailable")
    if not inputs.symbol_cooldown_clear:
        return _reject(signal, "symbol_cooldown", "symbol cooldown is active")

    # Exposure and loss limits stop a buy from adding risk. A sell only reduces exposure and
    # raises cash, so it is checked against the held position (shorting is not supported)
    # and never trapped in a position by those limits. Every input must still be known.
    buying = signal.side is OrderSide.BUY
    if inputs.open_positions is None or (
        buying and inputs.open_positions >= config.max_open_positions
    ):
        return _reject(
            signal, "maximum_open_positions", "maximum open positions reached or unknown"
        )

    notional = signal.quantity * inputs.expected_price
    if inputs.open_notional is None:
        return _reject(signal, "trade_notional", "open notional is unavailable")
    if notional > config.max_trade_notional:
        return _reject(signal, "trade_notional", "trade notional exceeds the limit")

    if inputs.symbol_position is None:
        return _reject(signal, "symbol_position", "per-symbol position is unknown")
    if not buying and signal.quantity > inputs.symbol_position:
        return _reject(
            signal, "symbol_position", "sell exceeds the held position; shorting is not supported"
        )
    if buying and inputs.symbol_position + signal.quantity > config.max_symbol_position:
        return _reject(signal, "symbol_position", "per-symbol position limit reached")

    if inputs.aggregate_allocation is None or (
        buying and inputs.aggregate_allocation + notional > config.max_aggregate_allocation
    ):
        return _reject(
            signal, "aggregate_allocation", "aggregate allocation limit reached or unknown"
        )

    if inputs.available_cash is None or (
        buying and inputs.available_cash - notional < config.min_cash_reserve
    ):
        return _reject(signal, "cash_reserve", "cash reserve is insufficient or unknown")

    if (
        inputs.daily_loss is None
        or inputs.daily_loss < 0
        or (buying and inputs.daily_loss > config.max_daily_loss)
    ):
        return _reject(signal, "daily_loss", "daily loss limit reached or unknown")

    if (
        inputs.drawdown is None
        or inputs.drawdown < 0
        or (buying and inputs.drawdown > config.max_drawdown)
    ):
        return _reject(signal, "drawdown", "drawdown limit reached or unknown")

    if (
        inputs.estimated_slippage is None
        or inputs.estimated_slippage < 0
        or inputs.estimated_slippage > config.max_slippage
    ):
        return _reject(signal, "slippage", "slippage input exceeds the limit or is unknown")

    constraints = inputs.constraints
    if constraints is None:
        return _reject(
            signal, "exchange_constraints", "current exchange constraints are unavailable"
        )
    if (
        constraints.min_quantity <= 0
        or constraints.quantity_increment <= 0
        or constraints.price_increment <= 0
    ):
        return _reject(signal, "exchange_constraints", "exchange increments are invalid")
    if signal.quantity < constraints.min_quantity:
        return _reject(signal, "exchange_constraints", "quantity is below the exchange minimum")
    if constraints.max_quantity is not None and signal.quantity > constraints.max_quantity:
        return _reject(signal, "exchange_constraints", "quantity exceeds the exchange maximum")
    if constraints.max_notional is not None and notional > constraints.max_notional:
        return _reject(signal, "exchange_constraints", "notional exceeds the exchange maximum")
    if notional < constraints.min_notional:
        return _reject(signal, "exchange_constraints", "notional is below the exchange minimum")
    if (signal.quantity - constraints.min_quantity) % constraints.quantity_increment != 0:
        return _reject(
            signal, "exchange_constraints", "quantity does not match the exchange increment"
        )
    if inputs.expected_price % constraints.price_increment != 0:
        return _reject(
            signal, "exchange_constraints", "price does not match the exchange increment"
        )

    return RiskApproval(
        signal_id=signal.signal_id,
        approved=True,
        reason="all ordered risk gates passed",
        correlation_id=signal.correlation_id,
    )
