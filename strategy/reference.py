"""Small reference strategies used as machine-validation instruments."""

from __future__ import annotations

from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5

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


class MovingAverageCrossStrategy:
    """Buy when the fast average of closes crosses above the slow one; sell on the reverse.

    The signal depends only on the closed bars in the market state, so every mode
    (backtest, replay, paper) produces the same signal, with the same identifier,
    for the same bar. Like ``AlwaysBuyStrategy`` it validates the machinery; it
    makes no profitability claim.
    """

    def __init__(self, fast: int = 3, slow: int = 8, quantity: Decimal = Decimal("1")) -> None:
        if fast < 1 or slow <= fast:
            raise ValueError("require 1 <= fast < slow")
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        self.fast = fast
        self.slow = slow
        self.quantity = quantity
        self.strategy_version = f"ma-cross-{fast}-{slow}-v1"

    def on_market_state(self, state: MarketState) -> Signal | None:
        closes = [candle.close for candle in state.candles]
        if len(closes) <= self.slow:
            return None
        before = _mean(closes[-self.fast - 1 : -1]) - _mean(closes[-self.slow - 1 : -1])
        after = _mean(closes[-self.fast :]) - _mean(closes[-self.slow :])
        if before <= 0 < after:
            side = OrderSide.BUY
        elif before >= 0 > after:
            side = OrderSide.SELL
        else:
            return None
        bar = state.candles[-1]
        key = f"{self.strategy_version}|{state.symbol}|{bar.closed_at.isoformat()}|{side.value}"
        return Signal(
            signal_id=uuid5(NAMESPACE_URL, f"algorithmic-crypto-trader:signal:{key}"),
            symbol=state.symbol,
            side=side,
            quantity=self.quantity,
            strategy_version=self.strategy_version,
            created_at=bar.closed_at,
            correlation_id=uuid5(NAMESPACE_URL, f"algorithmic-crypto-trader:correlation:{key}"),
        )


def _mean(values: list[Decimal]) -> Decimal:
    return sum(values, Decimal("0")) / Decimal(len(values))
