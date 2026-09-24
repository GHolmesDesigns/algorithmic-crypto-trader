"""One trading loop for every mode: strategy, audit, risk, then RiskApproval-only execution.

Replay, paper, and live differ only in the broker and the market-data source
handed to ``TradingCycle``; the strategy object and the path it drives do not
change. Each loop:

1. reads the file/environment kill-switch flags;
2. resolves any order left pending or unknown by querying the broker by its
   ``client_order_id``, and takes no new entry while one stays unresolved;
3. records the strategy's signal, evaluates the ordered risk gates, and records
   that decision, whether approved or refused;
4. submits an approved order through ``ExecutionEngine``, which persists it
   before calling the broker.

A failed audit or order write halts trading rather than continuing un-audited.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from uuid import UUID

from brokers.interface import BrokerInterface
from core.models import (
    KillSwitchState,
    MarketState,
    Order,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    RiskApproval,
    Signal,
    utc_now,
)
from execution.audit import AuditStore
from execution.engine import ExecutionEngine, PersistenceUnavailable
from risk.engine import ExchangeConstraints, RiskInputs, RiskLimits, evaluate
from risk.kill_switch import KillSwitch
from strategy.backtest import Strategy

logger = logging.getLogger(__name__)


class CycleStatus(StrEnum):
    NO_SIGNAL = "no_signal"
    REFUSED = "refused"
    SUBMITTED = "submitted"
    REJECTED = "rejected"
    AMBIGUOUS = "ambiguous"
    UNRESOLVED = "unresolved"
    BROKER_ERROR = "broker_error"
    HALTED = "halted"


@dataclass(frozen=True, slots=True)
class CycleOutcome:
    status: CycleStatus
    detail: str
    signal: Signal | None = None
    decision: RiskApproval | None = None
    order: Order | None = None


@dataclass(frozen=True, slots=True)
class CycleContext:
    """What the loop itself knows that the risk inputs depend on."""

    kill_switch: KillSwitchState
    ordered_signal_ids: frozenset[UUID]
    last_order_at: datetime | None


class RiskInputSource(Protocol):
    async def risk_inputs(
        self, signal: Signal, state: MarketState, context: CycleContext
    ) -> RiskInputs: ...


class TradingCycle:
    def __init__(
        self,
        *,
        strategy: Strategy,
        execution: ExecutionEngine,
        audit: AuditStore,
        kill_switch: KillSwitch,
        risk_inputs: RiskInputSource,
        limits: RiskLimits | None = None,
        kill_switch_flag: Path | None = None,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.strategy = strategy
        self.execution = execution
        self.audit = audit
        self.kill_switch = kill_switch
        self.risk_inputs = risk_inputs
        self.limits = limits or RiskLimits()
        self.kill_switch_flag = kill_switch_flag
        self.environ = environ
        self.clock = clock
        self._ordered_signal_ids: set[UUID] = set()
        self._last_order_at: dict[str, datetime] = {}

    async def on_market_state(self, state: MarketState) -> CycleOutcome:
        self.kill_switch.sync_external_state(env=self.environ, flag_path=self.kill_switch_flag)

        try:
            pending = self.execution.store.pending()
            if pending:
                await self.execution.recover_pending()
                pending = self.execution.store.pending()
        except PersistenceUnavailable:
            return self._halt("order store unavailable while resolving pending orders")
        except Exception:
            logger.exception("pending orders could not be resolved")
            return CycleOutcome(
                CycleStatus.BROKER_ERROR, "pending orders could not be queried at the broker"
            )
        if pending:
            return CycleOutcome(
                CycleStatus.UNRESOLVED,
                f"{len(pending)} order(s) remain unresolved; no new entries until the broker "
                "confirms them",
            )

        signal = self.strategy.on_market_state(state)
        if signal is None:
            return CycleOutcome(CycleStatus.NO_SIGNAL, "strategy returned no signal")
        try:
            self.audit.record_signal(signal)
        except PersistenceUnavailable:
            return self._halt("signal could not be recorded", signal=signal)

        decision = await self._decide(signal, state)
        try:
            self.audit.record_risk_decision(decision)
        except PersistenceUnavailable:
            return self._halt("risk decision could not be recorded", signal=signal)
        if not decision.approved:
            return CycleOutcome(CycleStatus.REFUSED, decision.reason, signal, decision)

        request = OrderRequest(
            signal_id=signal.signal_id,
            strategy_version=signal.strategy_version,
            symbol=signal.symbol,
            side=signal.side,
            order_type=OrderType.MARKET,
            quantity=signal.quantity,
            correlation_id=signal.correlation_id,
        )
        self._ordered_signal_ids.add(signal.signal_id)
        self._last_order_at[signal.symbol] = self.clock()
        try:
            order = await self.execution.submit(request, decision)
        except PersistenceUnavailable:
            return self._halt(
                "order could not be persisted; refusing to trade un-audited",
                signal=signal,
                decision=decision,
            )
        except Exception as exc:
            return self._broker_failure(exc, signal, decision)
        return CycleOutcome(CycleStatus.SUBMITTED, "order submitted", signal, decision, order)

    async def _decide(self, signal: Signal, state: MarketState) -> RiskApproval:
        context = CycleContext(
            kill_switch=self.kill_switch.state,
            ordered_signal_ids=frozenset(self._ordered_signal_ids),
            last_order_at=self._last_order_at.get(signal.symbol),
        )
        try:
            inputs = await self.risk_inputs.risk_inputs(signal, state, context)
        except Exception:
            logger.exception("risk inputs could not be assembled")
            return RiskApproval(
                signal_id=signal.signal_id,
                approved=False,
                reason="risk inputs could not be assembled",
                failed_gate="risk_inputs",
                correlation_id=signal.correlation_id,
            )
        # The loop owns the kill-switch state; a stale copy from the source never counts.
        inputs = inputs.model_copy(update={"kill_switch": self.kill_switch.state})
        return evaluate(signal, inputs, self.limits)

    def _halt(
        self, reason: str, *, signal: Signal | None = None, decision: RiskApproval | None = None
    ) -> CycleOutcome:
        self.kill_switch.trip(f"trading cycle: {reason}")
        logger.error("trading halted: %s", reason)
        return CycleOutcome(CycleStatus.HALTED, reason, signal, decision)

    @staticmethod
    def _broker_failure(exc: Exception, signal: Signal, decision: RiskApproval) -> CycleOutcome:
        order = getattr(exc, "order", None)
        if isinstance(order, Order) and order.status is OrderStatus.UNKNOWN:
            return CycleOutcome(
                CycleStatus.AMBIGUOUS,
                "submission outcome unknown; the next loop queries client_order_id first",
                signal,
                decision,
                order,
            )
        if isinstance(order, Order) and order.status is OrderStatus.REJECTED:
            return CycleOutcome(
                CycleStatus.REJECTED, "broker rejected the order", signal, decision, order
            )
        logger.warning("broker failure during submission: %s", type(exc).__name__)
        return CycleOutcome(
            CycleStatus.BROKER_ERROR, f"broker failure: {type(exc).__name__}", signal, decision
        )


class BrokerRiskInputs:
    """Assemble risk inputs from the broker's current state and the closed-bar market state.

    Anything that cannot be read is left unset, so the risk engine refuses. The
    independent price reference is the market-data feed's last close, not the
    broker quote. Daily loss and drawdown are measured from equity observed by
    this process.
    """

    def __init__(
        self,
        broker: BrokerInterface,
        *,
        constraints: ExchangeConstraints,
        estimated_slippage: Decimal,
        quote_asset: str = "USD",
        cooldown: timedelta = timedelta(0),
        trading_window: Callable[[datetime], bool] | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.broker = broker
        self.constraints = constraints
        self.estimated_slippage = estimated_slippage
        self.quote_asset = quote_asset
        self.cooldown = cooldown
        self.trading_window = trading_window
        self.clock = clock
        self._peak_equity: Decimal | None = None
        self._day_start: tuple[str, Decimal] | None = None

    async def risk_inputs(
        self, signal: Signal, state: MarketState, context: CycleContext
    ) -> RiskInputs:
        try:
            quote = await self.broker.get_quote(signal.symbol)
            positions = await self.broker.get_positions()
            balances = await self.broker.get_balances()
        except Exception:
            logger.warning("broker state unavailable for risk inputs")
            now = self.clock()
            return RiskInputs(
                kill_switch=context.kill_switch,
                operator_paused=False,
                trading_window_open=self._window_open(now),
                broker_healthy=False,
                now=now,
            )
        # Read the clock after the quote: adapters stamp quotes when they fetch them, and a
        # quote timestamped after "now" is refused as coming from the future.
        now = self.clock()
        window_open = self._window_open(now)
        last_bar = state.candles[-1] if state.candles else None
        reference = last_bar.close if last_bar is not None else None
        volatility = (last_bar.high - last_bar.low) / last_bar.close if last_bar else None
        marks = {signal.symbol: reference} if reference is not None else {}
        held = {item.symbol: item for item in positions if item.quantity != 0}
        exposure: Decimal | None = Decimal("0")
        for symbol, position in held.items():
            mark = marks.get(symbol) or position.average_price
            if not mark:
                exposure = None  # an unpriced position leaves allocation unknown
                break
            assert exposure is not None
            exposure += abs(position.quantity) * mark
        cash = next(
            (item.available for item in balances if item.asset == self.quote_asset), Decimal("0")
        )
        daily_loss, drawdown = self._loss_measures(now, cash, exposure)
        last_order = context.last_order_at
        return RiskInputs(
            kill_switch=context.kill_switch,
            operator_paused=False,
            trading_window_open=window_open,
            broker_healthy=bool(getattr(self.broker, "healthy", True)),
            quote=quote,
            reference_price=reference,
            volatility=volatility,
            duplicate_signal_ids=context.ordered_signal_ids,
            symbol_cooldown_clear=last_order is None or now - last_order >= self.cooldown,
            open_positions=len(held),
            open_notional=exposure,
            symbol_position=held[signal.symbol].quantity if signal.symbol in held else Decimal("0"),
            aggregate_allocation=exposure,
            available_cash=cash,
            daily_loss=daily_loss,
            drawdown=drawdown,
            expected_price=quote.ask if signal.side is OrderSide.BUY else quote.bid,
            estimated_slippage=self.estimated_slippage,
            constraints=self.constraints,
            now=now,
        )

    def _window_open(self, now: datetime) -> bool:
        return self.trading_window(now) if self.trading_window is not None else True

    def _loss_measures(
        self, now: datetime, cash: Decimal, exposure: Decimal | None
    ) -> tuple[Decimal | None, Decimal | None]:
        if exposure is None:
            return None, None
        equity = cash + exposure
        day = now.date().isoformat()
        if self._day_start is None or self._day_start[0] != day:
            self._day_start = (day, equity)
        self._peak_equity = max(self._peak_equity or equity, equity)
        daily_loss = max(Decimal("0"), self._day_start[1] - equity)
        drawdown = (self._peak_equity - equity) / self._peak_equity if self._peak_equity else None
        return daily_loss, drawdown
