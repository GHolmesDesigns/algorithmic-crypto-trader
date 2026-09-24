"""Phase 1 gate: one strategy across every mode, and backtest/replay parity.

Criteria covered:
- The same strategy runs through backtest, replay, SimulatedBroker, Gemini Sandbox,
  and Coinbase without strategy-code changes.
- Backtest/replay parity on at least three windows, including one high-volatility window.
"""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest
from app.replay import ReplayRunner
from app.trading import CycleStatus
from brokers.coinbase import CoinbaseBroker
from brokers.gemini import GeminiBroker
from brokers.simulated import SimulatedBroker
from core.models import OrderSide, Quote, Signal, utc_now
from core.resilience import TokenBucketRateLimiter
from execution.audit import InMemoryAuditStore, unlinked_orders
from execution.engine import InMemoryOrderStore
from strategy.backtest import BacktestConfig, Backtester, CostAssumptions
from strategy.reference import MovingAverageCrossStrategy

from tests.gate_support import (
    CONSTRAINTS,
    PARITY_LIMITS,
    PARITY_WINDOWS,
    FakeCoinbase,
    FakeGeminiSandbox,
    paper_cycle,
    state_at,
)

COSTS = CostAssumptions(
    taker_fee_rate=Decimal("0.006"), spread_bps=Decimal("10"), slippage_bps=Decimal("5")
)
CONFIG = BacktestConfig(initial_cash=Decimal("100000"), granularity="ONE_HOUR", costs=COSTS)


class RecordingStrategy:
    """Delegates to an unchanged strategy and records what it returned."""

    def __init__(self, inner: MovingAverageCrossStrategy) -> None:
        self.inner = inner
        self.strategy_version = inner.strategy_version
        self.signals: list[Signal] = []

    def on_market_state(self, state):
        signal = self.inner.on_market_state(state)
        if signal is not None:
            self.signals.append(signal)
        return signal


def signal_keys(signals) -> list[tuple[str, str]]:
    return [(str(item.signal_id), item.side.value) for item in signals]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(PARITY_WINDOWS))
async def test_backtest_and_replay_agree_to_the_cent(name: str) -> None:
    candles = PARITY_WINDOWS[name]
    report = Backtester(CONFIG).run(candles, MovingAverageCrossStrategy())
    replay = await ReplayRunner(config=CONFIG, limits=PARITY_LIMITS, constraints=CONSTRAINTS).run(
        candles, MovingAverageCrossStrategy()
    )

    assert report.trade_count >= 2, "a parity window must exercise round trips"
    # Every backtest entry and signal exit matches a replay fill in side, size, and price.
    expected = []
    for trade in report.trades:
        expected.append((OrderSide.BUY, trade.quantity, trade.entry_price, trade.entry_fee))
        if trade.exit_at != candles[-1].closed_at:
            expected.append((OrderSide.SELL, trade.quantity, trade.exit_price, trade.exit_fee))
    actual = [(fill.side, fill.quantity, fill.price, fill.fee) for fill in replay.fills]
    assert actual == expected
    assert replay.final_equity == report.final_equity
    # Any signal replay did not trade was refused by a recorded risk decision, and only
    # the long-only position gate refuses under these limits (the backtester likewise
    # ignores a buy while long and a sell while flat).
    assert {item.decision.failed_gate for item in replay.refusals} <= {"symbol_position"}
    traded = sum(1 for item in replay.outcomes if item.status is CycleStatus.SUBMITTED)
    assert traded + len(replay.refusals) == len(replay.signals)
    assert unlinked_orders(replay.audit) == ()


def test_parity_windows_include_a_high_volatility_window() -> None:
    def bar_range(candles) -> Decimal:
        return max((bar.high - bar.low) / bar.close for bar in candles)

    assert len(PARITY_WINDOWS) >= 3
    calm = max(bar_range(PARITY_WINDOWS["calm_trend"]), bar_range(PARITY_WINDOWS["calm_range"]))
    assert calm <= Decimal("0.01")
    assert bar_range(PARITY_WINDOWS["high_volatility"]) >= Decimal("0.07")


@pytest.mark.asyncio
async def test_replay_differences_under_tight_limits_are_explained_by_refusals() -> None:
    candles = PARITY_WINDOWS["high_volatility"]
    tight = PARITY_LIMITS.model_copy(update={"max_volatility": Decimal("0.05")})
    replay = await ReplayRunner(config=CONFIG, limits=tight, constraints=CONSTRAINTS).run(
        candles, MovingAverageCrossStrategy()
    )
    report = Backtester(CONFIG).run(candles, MovingAverageCrossStrategy())

    assert len(replay.fills) < 2 * report.trade_count
    assert "abnormal_volatility" in {item.decision.failed_gate for item in replay.refusals}
    recorded = replay.audit.decisions.values()
    assert sum(1 for item in recorded if not item.approved) == len(replay.refusals)


@pytest.mark.asyncio
async def test_one_strategy_runs_unchanged_in_every_mode() -> None:
    candles = PARITY_WINDOWS["calm_range"]
    zero_costs = BacktestConfig(initial_cash=Decimal("100000"), granularity="ONE_HOUR")

    backtest_strategy = RecordingStrategy(MovingAverageCrossStrategy())
    report = Backtester(zero_costs).run(candles, backtest_strategy)

    replay = await ReplayRunner(
        config=zero_costs, limits=PARITY_LIMITS, constraints=CONSTRAINTS
    ).run(candles, MovingAverageCrossStrategy())

    simulated = SimulatedBroker(
        Quote(
            symbol="BTC-USD",
            bid=candles[0].open,
            ask=candles[0].open,
            as_of=candles[0].opened_at,
            source="gate",
        )
    )
    gemini_venue = FakeGeminiSandbox()
    coinbase_venue = FakeCoinbase()
    limiter = TokenBucketRateLimiter(10_000, 10_000)
    gemini = GeminiBroker(
        api_key="sandbox-key",
        api_secret="sandbox-secret",
        client=httpx.AsyncClient(transport=httpx.MockTransport(gemini_venue.handler)),
        rate_limiter=limiter,
    )
    coinbase = CoinbaseBroker(
        auth_token="gate-token",
        client=httpx.AsyncClient(transport=httpx.MockTransport(coinbase_venue.handler)),
        rate_limiter=limiter,
    )
    live_modes = {}
    try:
        for label, broker in (("paper", simulated), ("gemini", gemini), ("coinbase", coinbase)):
            store = InMemoryOrderStore()
            audit = InMemoryAuditStore(store)
            cycle = paper_cycle(broker, MovingAverageCrossStrategy(), store=store, audit=audit)
            outcomes = []
            for index in range(len(candles) - 1):
                next_open = candles[index + 1].open
                if broker is simulated:
                    simulated.set_quote(
                        Quote(
                            symbol="BTC-USD",
                            bid=next_open,
                            ask=next_open,
                            source="gate",
                            as_of=utc_now(),  # a live venue quotes the current time
                        )
                    )
                else:
                    (gemini_venue if broker is gemini else coinbase_venue).price = next_open
                outcomes.append(await cycle.on_market_state(state_at(candles, index)))
            live_modes[label] = (outcomes, store, audit, broker)
    finally:
        await gemini.close()
        await coinbase.close()

    last = report.trades[-1]
    # The backtester liquidates a position still open at the end; the venues keep it.
    held_at_end = last.quantity if last.exit_at == candles[-1].closed_at else Decimal("0")
    expected_signals = signal_keys(backtest_strategy.signals)
    assert expected_signals, "the window must produce signals"
    assert signal_keys(replay.signals) == expected_signals
    submitted_by_mode = {}
    for label, (outcomes, store, audit, broker) in live_modes.items():
        signals = [item.signal for item in outcomes if item.signal is not None]
        assert signal_keys(signals) == expected_signals, label
        assert unlinked_orders(audit) == (), label
        submitted_by_mode[label] = sorted(store.orders)
        positions = {item.symbol: item.quantity for item in await broker.get_positions()}
        assert positions.get("BTC-USD", Decimal("0")) == held_at_end, label
    replay_orders = sorted(
        str(item.order.request.client_order_id) for item in replay.outcomes if item.order
    )
    # Identical signals produce identical deterministic client order IDs in every mode.
    assert len(replay_orders) >= 4
    assert submitted_by_mode["paper"] == replay_orders
    assert submitted_by_mode["gemini"] == replay_orders
    assert submitted_by_mode["coinbase"] == replay_orders
    assert sorted(gemini_venue.orders) == replay_orders
    assert sorted(coinbase_venue.orders) == replay_orders
