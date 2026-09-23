"""Closed-bar backtesting and walk-forward evaluation.

The backtester deliberately depends only on the canonical market models and the
strategy protocol.  It does not know about brokers, persistence, HTTP clients,
or network transports.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from hashlib import sha256
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from core.models import Candle, FrozenModel, MarketState, OrderSide, Quote, Signal
from pydantic import Field, model_validator

ZERO = Decimal("0")
BPS = Decimal("10000")
CENT = Decimal("0.01")


class Strategy(Protocol):
    """A strategy observes a market state and returns a signal or no signal."""

    strategy_version: str

    def on_market_state(self, state: MarketState) -> Signal | None: ...


class CostAssumptions(FrozenModel):
    """Explicit execution assumptions used by a backtest."""

    maker_fee_rate: Decimal = Field(default=ZERO, ge=ZERO)
    taker_fee_rate: Decimal = Field(default=ZERO, ge=ZERO)
    spread_bps: Decimal = Field(default=ZERO, ge=ZERO)
    slippage_bps: Decimal = Field(default=ZERO, ge=ZERO)
    fee_asset: str = Field(default="USD", min_length=1)
    partial_fill_ratio: Decimal = Field(default=Decimal("1"), gt=ZERO, le=Decimal("1"))

    @model_validator(mode="after")
    def validate_execution_price(self) -> CostAssumptions:
        if self.spread_bps / (BPS * Decimal("2")) + self.slippage_bps / BPS >= 1:
            raise ValueError("spread and slippage would make a sell execution price non-positive")
        return self


class BacktestConfig(FrozenModel):
    """Configuration that is part of the reproducibility record."""

    initial_cash: Decimal = Field(default=Decimal("100000"), gt=ZERO)
    data_source: str = Field(default="coinbase-advanced-trade", min_length=1)
    granularity: str = Field(default="ONE_MINUTE", min_length=1)
    quote_asset: str = Field(default="USD", min_length=1)
    costs: CostAssumptions = Field(default_factory=CostAssumptions)
    enforce_closed_bar_guard: bool = True


class Trade(FrozenModel):
    """A completed round trip, including execution costs."""

    symbol: str
    quantity: Decimal = Field(gt=ZERO)
    entry_at: datetime
    exit_at: datetime
    entry_price: Decimal = Field(gt=ZERO)
    exit_price: Decimal = Field(gt=ZERO)
    entry_fee: Decimal = Field(ge=ZERO)
    exit_fee: Decimal = Field(ge=ZERO)
    fee_asset: str = Field(min_length=1)
    pnl: Decimal


class ReturnDistribution(FrozenModel):
    """Summary statistics for the per-bar equity return distribution."""

    count: int = Field(ge=0)
    minimum: Decimal = ZERO
    maximum: Decimal = ZERO
    mean: Decimal = ZERO
    median: Decimal = ZERO
    positive_count: int = Field(ge=0)
    negative_count: int = Field(ge=0)


class PeriodResult(FrozenModel):
    """A compact result for one calendar year or market regime."""

    period: str
    return_pct: Decimal
    trade_count: int = Field(ge=0)
    exposure: Decimal = Field(ge=ZERO, le=Decimal("1"))
    max_drawdown: Decimal = Field(ge=ZERO)


class BacktestReport(FrozenModel):
    """Reproducible report containing the mandatory research fields."""

    data_source: str
    window_start: datetime
    window_end: datetime
    symbols: tuple[str, ...] = Field(min_length=1)
    granularity: str
    cost_assumptions: CostAssumptions
    trade_count: int = Field(ge=0)
    exposure: Decimal = Field(ge=ZERO, le=Decimal("1"))
    return_distribution: ReturnDistribution
    max_drawdown: Decimal = Field(ge=ZERO)
    max_drawdown_duration: timedelta = timedelta(0)
    per_year: dict[str, PeriodResult]
    per_regime: dict[str, PeriodResult]
    strategy_version: str
    strategy_version_hash: str = Field(min_length=64, max_length=64)
    initial_cash: Decimal = Field(gt=ZERO)
    final_equity: Decimal = Field(ge=ZERO)
    total_return_pct: Decimal
    trades: tuple[Trade, ...] = ()


class WalkForwardWindow(FrozenModel):
    """One training/test slice in a walk-forward run."""

    train_start: datetime
    train_end: datetime
    test_start: datetime
    test_end: datetime
    report: BacktestReport


class WalkForwardConfig(FrozenModel):
    """Window sizes for expanding, stepwise walk-forward evaluation."""

    train_bars: int = Field(gt=0)
    test_bars: int = Field(gt=0)
    step_bars: int | None = Field(default=None, gt=0)
    holdout_bars: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def default_step(self) -> WalkForwardConfig:
        if self.step_bars is None:
            object.__setattr__(self, "step_bars", self.test_bars)
        return self


class WalkForwardReport(FrozenModel):
    """Walk-forward reports plus a sealed final holdout report."""

    windows: tuple[WalkForwardWindow, ...] = Field(min_length=1)
    holdout: BacktestReport | None = None


def _money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _execution_price(price: Decimal, side: OrderSide, costs: CostAssumptions) -> Decimal:
    half_spread = costs.spread_bps / (BPS * Decimal("2"))
    slippage = costs.slippage_bps / BPS
    adjustment = half_spread + slippage
    if side is OrderSide.BUY:
        return price * (Decimal("1") + adjustment)
    return price * (Decimal("1") - adjustment)


def _return_distribution(values: Sequence[Decimal]) -> ReturnDistribution:
    if not values:
        return ReturnDistribution(count=0, positive_count=0, negative_count=0)
    ordered = sorted(values)
    midpoint = len(ordered) // 2
    median = ordered[midpoint]
    if len(ordered) % 2 == 0:
        median = (ordered[midpoint - 1] + ordered[midpoint]) / Decimal("2")
    return ReturnDistribution(
        count=len(values),
        minimum=min(values),
        maximum=max(values),
        mean=sum(values, ZERO) / Decimal(len(values)),
        median=median,
        positive_count=sum(1 for value in values if value > ZERO),
        negative_count=sum(1 for value in values if value < ZERO),
    )


def _max_drawdown(
    equity: Sequence[Decimal], timestamps: Sequence[datetime]
) -> tuple[Decimal, timedelta]:
    peak = ZERO
    peak_at = timestamps[0]
    largest = ZERO
    duration = timedelta(0)
    for value, timestamp in zip(equity, timestamps, strict=True):
        if value > peak:
            peak = value
            peak_at = timestamp
        if peak > ZERO:
            drawdown = (peak - value) / peak
            if drawdown > largest:
                largest = drawdown
                duration = timestamp - peak_at
    return largest, duration


def _period_result(
    period: str,
    values: Sequence[Decimal],
    equity: Sequence[Decimal],
    trade_count: int,
    exposure: Decimal,
) -> PeriodResult:
    if not equity:
        return PeriodResult(
            period=period,
            return_pct=ZERO,
            trade_count=trade_count,
            exposure=exposure,
            max_drawdown=ZERO,
        )
    start = equity[0]
    end = equity[-1]
    peak = start
    drawdown = ZERO
    for value in equity:
        peak = max(peak, value)
        if peak > ZERO:
            drawdown = max(drawdown, (peak - value) / peak)
    return PeriodResult(
        period=period,
        return_pct=(end - start) / start if start else ZERO,
        trade_count=trade_count,
        exposure=exposure,
        max_drawdown=drawdown,
    )


class Backtester:
    """Execute a strategy against a sequence of closed candles."""

    def __init__(self, config: BacktestConfig | None = None) -> None:
        self.config = config or BacktestConfig()

    def run(self, candles: Iterable[Candle], strategy: Strategy) -> BacktestReport:
        series = tuple(candles)
        self._validate_series(series)
        symbol = series[0].symbol
        cash = self.config.initial_cash
        position = ZERO
        entry_price = ZERO
        entry_fee = ZERO
        entry_at = series[0].opened_at
        trades: list[Trade] = []
        equity_curve: list[Decimal] = []
        timestamps: list[datetime] = []
        position_flags: list[bool] = []

        for index, candle in enumerate(series):
            state_candles = series[: index + 1]
            if not self.config.enforce_closed_bar_guard:
                state_candles = series
            state = MarketState(
                symbol=symbol,
                quote=Quote(
                    symbol=symbol,
                    bid=candle.close,
                    ask=candle.close,
                    as_of=candle.closed_at,
                    source=self.config.data_source,
                    received_at=candle.closed_at,
                ),
                candles=state_candles,
                observed_at=candle.closed_at,
            )

            position_flags.append(position > ZERO)

            if index < len(series) - 1:
                signal = strategy.on_market_state(state)
                if signal is not None and signal.symbol != symbol:
                    raise ValueError("strategy signal symbol does not match the backtest series")
                if signal is not None and signal.side is OrderSide.BUY and position == ZERO:
                    position, cash, entry_price, entry_fee, entry_at = self._buy(
                        signal.quantity,
                        series[index + 1],
                        cash,
                    )
                elif signal is not None and signal.side is OrderSide.SELL and position > ZERO:
                    cash, trade = self._sell(
                        position,
                        entry_price,
                        entry_fee,
                        entry_at,
                        series[index + 1],
                        cash,
                    )
                    trades.append(trade)
                    position = ZERO

            equity_curve.append(cash + position * candle.close)
            timestamps.append(candle.closed_at)

        if position > ZERO:
            cash, trade = self._sell(
                position,
                entry_price,
                entry_fee,
                entry_at,
                series[-1],
                cash,
            )
            trades.append(trade)
            position = ZERO
            equity_curve[-1] = cash

        returns = tuple(
            (current - previous) / previous
            for previous, current in zip(equity_curve, equity_curve[1:], strict=False)
            if previous != ZERO
        )
        drawdown, drawdown_duration = _max_drawdown(equity_curve, timestamps)
        yearly = self._group_periods(series, equity_curve, trades, position_flags, by_year=True)
        regimes = self._group_periods(series, equity_curve, trades, position_flags, by_year=False)
        final_equity = _money(cash)
        return BacktestReport(
            data_source=self.config.data_source,
            window_start=series[0].opened_at,
            window_end=series[-1].closed_at,
            symbols=(symbol,),
            granularity=self.config.granularity,
            cost_assumptions=self.config.costs,
            trade_count=len(trades),
            exposure=Decimal(sum(position_flags)) / Decimal(len(series)),
            return_distribution=_return_distribution(returns),
            max_drawdown=drawdown,
            max_drawdown_duration=drawdown_duration,
            per_year=yearly,
            per_regime=regimes,
            strategy_version=strategy.strategy_version,
            strategy_version_hash=sha256(strategy.strategy_version.encode("utf-8")).hexdigest(),
            initial_cash=self.config.initial_cash,
            final_equity=final_equity,
            total_return_pct=(final_equity - self.config.initial_cash) / self.config.initial_cash,
            trades=tuple(trades),
        )

    @staticmethod
    def _validate_series(series: tuple[Candle, ...]) -> None:
        if len(series) < 2:
            raise ValueError("a backtest requires at least two closed candles")
        symbol = series[0].symbol
        for previous, current in zip(series, series[1:], strict=False):
            if current.symbol != symbol:
                raise ValueError("a backtest cannot mix symbols")
            if current.opened_at <= previous.opened_at:
                raise ValueError("candles must be strictly ordered")
            if previous.closed_at > current.opened_at:
                raise ValueError("candles must not overlap")

    def _buy(
        self,
        requested_quantity: Decimal,
        candle: Candle,
        cash: Decimal,
    ) -> tuple[Decimal, Decimal, Decimal, Decimal, datetime]:
        quantity = requested_quantity * self.config.costs.partial_fill_ratio
        price = _execution_price(candle.open, OrderSide.BUY, self.config.costs)
        fee = quantity * price * self.config.costs.taker_fee_rate
        total = quantity * price + fee
        if total > cash:
            raise ValueError("strategy requested a buy larger than available cash")
        return quantity, cash - total, price, fee, candle.opened_at

    def _sell(
        self,
        quantity: Decimal,
        entry_price: Decimal,
        entry_fee: Decimal,
        entry_at: datetime,
        candle: Candle,
        cash: Decimal,
    ) -> tuple[Decimal, Trade]:
        price = _execution_price(candle.close, OrderSide.SELL, self.config.costs)
        fee = quantity * price * self.config.costs.taker_fee_rate
        proceeds = quantity * price - fee
        return cash + proceeds, Trade(
            symbol=candle.symbol,
            quantity=quantity,
            entry_at=entry_at,
            exit_at=candle.closed_at,
            entry_price=entry_price,
            exit_price=price,
            entry_fee=entry_fee,
            exit_fee=fee,
            fee_asset=self.config.costs.fee_asset,
            pnl=quantity * (price - entry_price) - entry_fee - fee,
        )

    def _group_periods(
        self,
        series: tuple[Candle, ...],
        equity: Sequence[Decimal],
        trades: Sequence[Trade],
        position_flags: Sequence[bool],
        *,
        by_year: bool,
    ) -> dict[str, PeriodResult]:
        indexes: dict[str, list[int]] = defaultdict(list)
        for index, candle in enumerate(series):
            if by_year:
                key = str(candle.closed_at.year)
            elif index == 0:
                key = "flat"
            else:
                key = "up" if candle.close >= series[index - 1].close else "down"
            indexes[key].append(index)
        result: dict[str, PeriodResult] = {}
        for key, values in indexes.items():
            period_trades = (
                sum(trade.entry_at.year == int(key) for trade in trades) if by_year else 0
            )
            result[key] = _period_result(
                key,
                (),
                [equity[index] for index in values],
                period_trades,
                Decimal(sum(position_flags[index] for index in values)) / Decimal(len(values)),
            )
        return result


def run_walk_forward(
    candles: Sequence[Candle],
    strategy_factory: Callable[[tuple[Candle, ...]], Strategy],
    config: WalkForwardConfig,
    *,
    backtest_config: BacktestConfig | None = None,
) -> WalkForwardReport:
    """Run train/test windows and keep the final holdout sealed.

    The factory receives only the training candles for each test window.  For
    the final holdout it receives an empty tuple, making accidental holdout
    training observable in tests and impossible through this runner's contract.
    """

    series = tuple(candles)
    available = len(series) - config.holdout_bars
    if available < config.train_bars + config.test_bars:
        raise ValueError("not enough candles for the requested walk-forward windows")
    windows: list[WalkForwardWindow] = []
    start = 0
    runner = Backtester(backtest_config)
    while start + config.train_bars + config.test_bars <= available:
        train_end = start + config.train_bars
        test_end = train_end + config.test_bars
        training = series[start:train_end]
        testing = series[train_end:test_end]
        strategy = strategy_factory(training)
        report = runner.run(testing, strategy)
        windows.append(
            WalkForwardWindow(
                train_start=training[0].opened_at,
                train_end=training[-1].closed_at,
                test_start=testing[0].opened_at,
                test_end=testing[-1].closed_at,
                report=report,
            )
        )
        start += config.step_bars or config.test_bars
    holdout = None
    if config.holdout_bars:
        holdout_candles = series[available:]
        holdout = runner.run(holdout_candles, strategy_factory(()))
    return WalkForwardReport(windows=tuple(windows), holdout=holdout)


def deterministic_signal_id(strategy_version: str, symbol: str) -> UUID:
    """Return a stable correlation identifier for reference strategies."""

    return uuid5(NAMESPACE_URL, f"algorithmic-crypto-trader:{strategy_version}:{symbol}")
