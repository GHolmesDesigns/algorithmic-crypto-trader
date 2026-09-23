"""Known-answer and safety-boundary tests for the research layer."""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from core.models import Candle, MarketState, OrderSide, Signal
from strategy.backtest import (
    BacktestConfig,
    Backtester,
    CostAssumptions,
    WalkForwardConfig,
    run_walk_forward,
)
from strategy.reference import AlwaysBuyStrategy

START = datetime(2024, 1, 1, tzinfo=UTC)


def candles(closes: list[str], *, symbol: str = "BTC-USD") -> tuple[Candle, ...]:
    result: list[Candle] = []
    for index, close in enumerate(closes):
        opened = START + timedelta(hours=index)
        ended = opened + timedelta(hours=1)
        price = Decimal(close)
        result.append(
            Candle(
                symbol=symbol,
                interval="ONE_HOUR",
                opened_at=opened,
                closed_at=ended,
                open=price,
                high=price,
                low=price,
                close=price,
                volume=Decimal("1"),
                source="coinbase-advanced-trade",
                as_of=ended,
                ingested_at=ended,
            )
        )
    return tuple(result)


def test_always_buy_matches_buy_and_hold_minus_fees_to_the_cent() -> None:
    series = candles(["100", "110", "125"])
    config = BacktestConfig(
        initial_cash=Decimal("1000"),
        granularity="ONE_HOUR",
        costs=CostAssumptions(taker_fee_rate=Decimal("0.01")),
    )

    report = Backtester(config).run(series, AlwaysBuyStrategy(Decimal("5")))

    # Buy 5 at 110 (after the first closed bar), then sell at 125. Fees are
    # charged on both sides and the final equity is reported to cents.
    expected = Decimal("1000") - Decimal("550") - Decimal("5.50")
    expected += Decimal("625") - Decimal("6.25")
    assert report.final_equity == expected.quantize(Decimal("0.01"))
    assert report.trade_count == 1
    assert report.cost_assumptions.fee_asset == "USD"
    assert report.strategy_version_hash


class FuturePeekCanary:
    strategy_version = "future-peek-canary-v1"

    def on_market_state(self, state: MarketState) -> Signal | None:
        # The last visible candle is the only permitted observation. When the
        # guard is removed, the full future series is visible and this canary
        # buys before the known future spike.
        if max(candle.close for candle in state.candles) >= Decimal("1000"):
            return Signal(
                symbol=state.symbol,
                side=OrderSide.BUY,
                quantity=Decimal("1"),
                strategy_version=self.strategy_version,
                correlation_id=uuid4(),
            )
        return None


def test_lookahead_canary_is_quiet_with_guard_and_absurd_without_it() -> None:
    series = candles(["100", "101", "102", "10000"])
    safe = Backtester(BacktestConfig(initial_cash=Decimal("1000"))).run(series, FuturePeekCanary())
    unsafe = Backtester(
        BacktestConfig(initial_cash=Decimal("1000"), enforce_closed_bar_guard=False)
    ).run(series, FuturePeekCanary())

    assert safe.trade_count == 0
    assert unsafe.trade_count == 1
    assert unsafe.final_equity > Decimal("9000")


def test_walk_forward_keeps_final_holdout_out_of_training() -> None:
    series = candles([str(value) for value in range(100, 112)])
    seen_training: list[tuple[Candle, ...]] = []

    def factory(training: tuple[Candle, ...]) -> AlwaysBuyStrategy:
        seen_training.append(training)
        return AlwaysBuyStrategy(Decimal("1"))

    result = run_walk_forward(
        series,
        factory,
        WalkForwardConfig(train_bars=4, test_bars=2, holdout_bars=2),
        backtest_config=BacktestConfig(initial_cash=Decimal("1000")),
    )

    assert len(result.windows) == 3
    assert result.holdout is not None
    assert seen_training[-1] == ()
    assert all(candle not in seen_training[-1] for candle in series[-2:])
    assert result.holdout.window_start == series[-2].opened_at


def test_report_contains_mandatory_research_fields() -> None:
    report = Backtester().run(candles(["100", "101", "99"]), AlwaysBuyStrategy())
    payload = report.model_dump()
    for field in (
        "data_source",
        "window_start",
        "window_end",
        "symbols",
        "granularity",
        "cost_assumptions",
        "trade_count",
        "exposure",
        "return_distribution",
        "max_drawdown",
        "max_drawdown_duration",
        "per_year",
        "per_regime",
        "strategy_version_hash",
    ):
        assert field in payload


def test_strategy_package_has_no_external_system_imports() -> None:
    banned_roots = {"brokers", "db", "httpx", "requests", "urllib", "socket", "websockets"}
    root = Path(__file__).parents[1] / "strategy"
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported = {alias.name.split(".", maxsplit=1)[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported = {node.module.split(".", maxsplit=1)[0]}
            else:
                continue
            assert imported.isdisjoint(banned_roots), f"{path} imports {imported & banned_roots}"
