"""Small reference strategies used as machine-validation instruments."""

from __future__ import annotations

from decimal import Decimal

from core.models import MarketState, OrderSide, Signal

from strategy.backtest import deterministic_signal_id


class AlwaysBuyStrategy:
    """Buy once, then let the backtester close the position at the end.

    This is intentionally not a trading recommendation.  It is a known-answer
    benchmark for comparing the engine with buy-and-hold after modeled costs.
    """

    strategy_version = "always-buy-v1"

    def __init__(self, quantity: Decimal = Decimal("1")) -> None:
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        self.quantity = quantity
        self._emitted = False

    def on_market_state(self, state: MarketState) -> Signal | None:
        if self._emitted:
            return None
        self._emitted = True
        return Signal(
            symbol=state.symbol,
            side=OrderSide.BUY,
            quantity=self.quantity,
            strategy_version=self.strategy_version,
            correlation_id=deterministic_signal_id(self.strategy_version, state.symbol),
        )
