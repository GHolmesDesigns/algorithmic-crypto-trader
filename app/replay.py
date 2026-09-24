"""Replay recorded closed bars through the real strategy, risk, and execution path.

The replay mirrors the backtester's timing so the two can be compared: the
strategy sees bar ``n`` once it has closed, and its order fills at bar ``n+1``'s
open. ``SimulatedBroker`` applies the backtest's spread plus slippage as one
far-side adjustment and charges the same taker fee, so any difference between
the two results comes from the risk engine refusing an order. Every refusal is
recorded as a risk decision.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from brokers.simulated import SimulatedBroker, SimulationConfig
from core.models import Candle, Fill, MarketState, Quote, Signal
from execution.audit import InMemoryAuditStore
from execution.engine import ExecutionEngine, InMemoryOrderStore
from risk.engine import ExchangeConstraints, RiskLimits
from risk.kill_switch import KillSwitch
from strategy.backtest import (
    BacktestConfig,
    Strategy,
    execution_cost_rate,
    money,
    validate_series,
)

from app.trading import BrokerRiskInputs, CycleOutcome, CycleStatus, TradingCycle


@dataclass(frozen=True, slots=True)
class ReplayResult:
    outcomes: tuple[CycleOutcome, ...]
    fills: tuple[Fill, ...]
    final_equity: Decimal
    audit: InMemoryAuditStore

    @property
    def signals(self) -> tuple[Signal, ...]:
        return tuple(item.signal for item in self.outcomes if item.signal is not None)

    @property
    def refusals(self) -> tuple[CycleOutcome, ...]:
        return tuple(item for item in self.outcomes if item.status is CycleStatus.REFUSED)


class ReplayRunner:
    def __init__(
        self,
        *,
        config: BacktestConfig | None = None,
        limits: RiskLimits | None = None,
        constraints: ExchangeConstraints,
    ) -> None:
        self.config = config or BacktestConfig()
        if self.config.costs.partial_fill_ratio != 1:
            raise ValueError("replay fills market orders in full; partial_fill_ratio must be 1")
        self.limits = limits or RiskLimits()
        self.constraints = constraints

    async def run(self, candles: Iterable[Candle], strategy: Strategy) -> ReplayResult:
        series = tuple(candles)
        validate_series(series)
        symbol = series[0].symbol
        costs = self.config.costs
        adjustment = execution_cost_rate(costs)
        broker = SimulatedBroker(
            _quote(symbol, series[0].open, series[0].opened_at),
            config=SimulationConfig(
                slippage=adjustment,
                fee_rate=costs.taker_fee_rate,
                fee_asset=costs.fee_asset,
                initial_quote_balance=self.config.initial_cash,
            ),
        )
        clock = _ReplayClock(series[0].opened_at)
        orders = InMemoryOrderStore()
        audit = InMemoryAuditStore(orders)
        cycle = TradingCycle(
            strategy=strategy,
            execution=ExecutionEngine(broker, orders),
            audit=audit,
            kill_switch=KillSwitch(),
            risk_inputs=BrokerRiskInputs(
                broker,
                constraints=self.constraints,
                estimated_slippage=adjustment,
                quote_asset=self.config.quote_asset,
                clock=clock,
            ),
            limits=self.limits,
            environ={},
            clock=clock,
        )
        outcomes = []
        for index, candle in enumerate(series[:-1]):
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
                candles=series[: index + 1],
                observed_at=candle.closed_at,
            )
            upcoming = series[index + 1]
            # The order reaches the market when the next bar opens.
            clock.now = upcoming.opened_at
            broker.set_quote(_quote(symbol, upcoming.open, upcoming.opened_at))
            outcomes.append(await cycle.on_market_state(state))

        final = series[-1]
        base, quote_asset = symbol.split("-", maxsplit=1)
        balances = {item.asset: item.available for item in await broker.get_balances()}
        cash = balances.get(quote_asset, Decimal("0"))
        held = balances.get(base, Decimal("0"))
        # Value any open position as the backtester does: sold at the final close.
        exit_price = final.close * (Decimal("1") - adjustment)
        liquidation = held * exit_price - held * exit_price * costs.taker_fee_rate
        return ReplayResult(
            outcomes=tuple(outcomes),
            fills=tuple(orders.fills.values()),
            final_equity=money(cash + liquidation),
            audit=audit,
        )


class _ReplayClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def _quote(symbol: str, price: Decimal, at: datetime) -> Quote:
    return Quote(symbol=symbol, bid=price, ask=price, as_of=at, source="replay", received_at=at)
