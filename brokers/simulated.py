"""Deterministic local broker used by tests, paper mode, and replay."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from core.models import (
    Balance,
    Fill,
    Order,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    Quote,
    RiskApproval,
    utc_now,
)

from brokers.interface import BrokerCapabilities, BrokerInterface


class SimulatedFault(StrEnum):
    """Faults that can be injected without contacting an external provider."""

    REJECT = "reject"
    TIMEOUT = "timeout"
    DUPLICATE_ACK = "duplicate_ack"
    OUT_OF_ORDER_FILLS = "out_of_order_fills"
    RATE_LIMITED = "rate_limited"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class SimulationConfig:
    """Execution assumptions for a deterministic simulation."""

    slippage: Decimal = Decimal("0")
    fee_rate: Decimal = Decimal("0")
    fee_asset: str = "USD"
    partial_fill_schedule: tuple[Decimal, ...] = (Decimal("0.5"), Decimal("1"))
    initial_quote_balance: Decimal = Decimal("100000")

    def __post_init__(self) -> None:
        if self.slippage < 0 or self.slippage >= 1:
            raise ValueError("slippage must be in the range [0, 1)")
        if self.fee_rate < 0:
            raise ValueError("fee_rate must not be negative")
        if not self.fee_asset.strip():
            raise ValueError("fee_asset must not be empty")
        if self.initial_quote_balance < 0:
            raise ValueError("initial_quote_balance must not be negative")
        if not self.partial_fill_schedule:
            raise ValueError("partial_fill_schedule must contain at least one ratio")
        previous = Decimal("0")
        for ratio in self.partial_fill_schedule:
            if ratio <= previous or ratio > 1:
                raise ValueError("partial_fill_schedule must increase from 0 to 1")
            previous = ratio
        if previous != 1:
            raise ValueError("partial_fill_schedule must end at 1")


@dataclass(frozen=True, slots=True)
class FaultPlan:
    """Per-operation fault queues. Each queued event is consumed once."""

    submit: tuple[SimulatedFault, ...] = ()
    get_quote: tuple[SimulatedFault, ...] = ()
    get_order: tuple[SimulatedFault, ...] = ()
    get_fills: tuple[SimulatedFault, ...] = ()
    get_balances: tuple[SimulatedFault, ...] = ()
    get_positions: tuple[SimulatedFault, ...] = ()
    unavailable: bool = False


class SimulatedBrokerError(RuntimeError):
    """Base error for an intentionally injected simulated provider failure."""

    def __init__(
        self,
        message: str,
        *,
        fault: SimulatedFault,
        order: Order | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.fault = fault
        self.order = order
        self.retry_after = retry_after


class OrderRejectedError(SimulatedBrokerError):
    def __init__(self, order: Order) -> None:
        super().__init__(
            "simulated broker rejected the order", fault=SimulatedFault.REJECT, order=order
        )


class SubmissionTimeoutError(SimulatedBrokerError):
    def __init__(self, order: Order) -> None:
        super().__init__(
            "simulated submission timed out; query client_order_id before retrying",
            fault=SimulatedFault.TIMEOUT,
            order=order,
        )


class DuplicateAcknowledgementError(SimulatedBrokerError):
    def __init__(self, order: Order) -> None:
        super().__init__(
            "simulated broker returned a duplicate acknowledgement",
            fault=SimulatedFault.DUPLICATE_ACK,
            order=order,
        )


class RateLimitError(SimulatedBrokerError):
    def __init__(self, retry_after: float = 1.0) -> None:
        super().__init__(
            "simulated broker returned HTTP 429",
            fault=SimulatedFault.RATE_LIMITED,
            retry_after=retry_after,
        )


class BrokerUnavailableError(SimulatedBrokerError):
    def __init__(self) -> None:
        super().__init__("simulated broker is unavailable", fault=SimulatedFault.UNAVAILABLE)


SubmissionTimeout = SubmissionTimeoutError
RateLimitExceeded = RateLimitError
BrokerUnavailable = BrokerUnavailableError

Operation = Literal[
    "submit",
    "get_quote",
    "get_order",
    "get_fills",
    "get_balances",
    "get_positions",
]


class SimulatedBroker(BrokerInterface):
    """A deterministic matching engine with explicit, injectable failure paths."""

    def __init__(
        self,
        quote: Quote | None = None,
        *,
        config: SimulationConfig | None = None,
        fault_plan: FaultPlan | None = None,
        faults: tuple[SimulatedFault, ...] = (),
    ) -> None:
        if fault_plan is not None and faults:
            raise ValueError("use fault_plan or faults, not both")
        self.config = config or SimulationConfig()
        self._quote = quote or Quote(
            symbol="BTC-USD",
            bid=Decimal("59999"),
            ask=Decimal("60001"),
            as_of=utc_now(),
            source="simulated",
        )
        plan = fault_plan or FaultPlan(submit=faults)
        self._faults: dict[Operation, list[SimulatedFault]] = {
            "submit": list(plan.submit),
            "get_quote": list(plan.get_quote),
            "get_order": list(plan.get_order),
            "get_fills": list(plan.get_fills),
            "get_balances": list(plan.get_balances),
            "get_positions": list(plan.get_positions),
        }
        self._always_unavailable = plan.unavailable
        self._orders: dict[str, Order] = {}
        self._fills: dict[str, list[Fill]] = {}
        self._limit_schedule_index: dict[str, int] = {}
        self._unknown_requests: dict[str, OrderRequest] = {}
        self._out_of_order: set[str] = set()
        self._acknowledgements: dict[str, int] = {}
        quote_asset = self._quote.symbol.split("-", maxsplit=1)[1]
        self._balances: dict[str, Decimal] = {quote_asset: self.config.initial_quote_balance}
        self._balances.setdefault(self.config.fee_asset, Decimal("0"))
        self._positions: dict[str, tuple[Decimal, Decimal]] = {}

    @property
    def capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities(
            provider="simulated",
            environment="local",
            streaming=True,
            historical_candles=True,
            native_order_edit=False,
            preview_orders=True,
            order_types=("market", "limit"),
            price_increment=Decimal("0.01"),
            quantity_increment=Decimal("0.00000001"),
            max_quote_age_seconds=60,
        )

    async def get_quote(self, symbol: str) -> Quote:
        self._raise_for_fault("get_quote")
        if symbol != self._quote.symbol:
            raise KeyError(symbol)
        return self._quote

    async def get_balances(self) -> tuple[Balance, ...]:
        self._raise_for_fault("get_balances")
        now = utc_now()
        return tuple(
            Balance(asset=asset, available=amount, as_of=now)
            for asset, amount in sorted(self._balances.items())
            if amount != 0
        )

    async def get_positions(self) -> tuple[Position, ...]:
        self._raise_for_fault("get_positions")
        now = utc_now()
        return tuple(
            Position(symbol=symbol, quantity=quantity, average_price=average, as_of=now)
            for symbol, (quantity, average) in sorted(self._positions.items())
            if quantity != 0
        )

    async def submit_order(self, request: OrderRequest, approval: RiskApproval) -> Order:
        """Persist one idempotent order and apply the configured matching rules."""
        if self._always_unavailable:
            raise BrokerUnavailableError()
        if approval.signal_id != request.signal_id:
            raise ValueError("risk approval does not belong to order request")
        if not approval.approved:
            raise PermissionError("risk approval is not approved")

        key = str(request.client_order_id)
        existing = self._orders.get(key)
        if existing is not None:
            self._acknowledgements[key] = self._acknowledgements.get(key, 0) + 1
            return existing

        injected = self._take_fault("submit")
        if injected is SimulatedFault.REJECT:
            order = self._store_rejected(request)
            raise OrderRejectedError(order)
        if injected is SimulatedFault.RATE_LIMITED:
            raise RateLimitError()
        if injected is SimulatedFault.UNAVAILABLE:
            raise BrokerUnavailableError()
        if injected is SimulatedFault.TIMEOUT:
            unknown = Order(
                order_id=request.client_order_id,
                request=request,
                status=OrderStatus.UNKNOWN,
            )
            self._orders[key] = unknown
            self._fills[key] = []
            self._unknown_requests[key] = request
            self._acknowledgements[key] = 1
            raise SubmissionTimeoutError(unknown)

        if injected is SimulatedFault.OUT_OF_ORDER_FILLS:
            self._out_of_order.add(key)
        order = self._accept_order(request)
        self._acknowledgements[key] = 1
        if injected is SimulatedFault.DUPLICATE_ACK:
            raise DuplicateAcknowledgementError(order)
        return order

    async def get_order(self, client_order_id: str) -> Order | None:
        self._raise_for_fault("get_order")
        request = self._unknown_requests.pop(client_order_id, None)
        if request is not None:
            self._orders.pop(client_order_id, None)
            self._fills.pop(client_order_id, None)
            self._limit_schedule_index.pop(client_order_id, None)
            self._accept_order(request)
        return self._orders.get(client_order_id)

    async def get_fills(self, client_order_id: str) -> tuple[Fill, ...]:
        fault = self._raise_for_fault("get_fills")
        if client_order_id in self._unknown_requests:
            return ()
        fills = tuple(self._fills.get(client_order_id, ()))
        if client_order_id in self._out_of_order or fault is SimulatedFault.OUT_OF_ORDER_FILLS:
            return tuple(reversed(fills))
        return fills

    async def advance(self) -> tuple[Order, ...]:
        """Advance one deterministic limit-fill step and return changed orders."""
        changed: list[Order] = []
        for _key, order in tuple(self._orders.items()):
            if order.request.order_type is not OrderType.LIMIT:
                continue
            if order.status in {OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED}:
                continue
            if not self._limit_is_marketable(order.request):
                continue
            updated = self._fill_next_limit(order)
            if updated != order:
                changed.append(updated)
        return tuple(changed)

    advance_market = advance

    def set_quote(self, quote: Quote) -> None:
        """Replace the quote used by subsequent matching decisions."""
        if quote.symbol != self._quote.symbol:
            raise ValueError("simulator supports one configured symbol")
        self._quote = quote

    def acknowledgement_count(self, client_order_id: str) -> int:
        return self._acknowledgements.get(client_order_id, 0)

    def _raise_for_fault(self, operation: Operation) -> SimulatedFault | None:
        fault = self._take_fault(operation)
        if fault is not None and fault is not SimulatedFault.OUT_OF_ORDER_FILLS:
            if fault is SimulatedFault.RATE_LIMITED:
                raise RateLimitError()
            if fault is SimulatedFault.UNAVAILABLE:
                raise BrokerUnavailableError()
            raise SimulatedBrokerError(f"simulated {operation} fault: {fault.value}", fault=fault)
        return fault

    def _take_fault(self, operation: Operation) -> SimulatedFault | None:
        if self._always_unavailable:
            raise BrokerUnavailableError()
        if not self._faults[operation]:
            return None
        return self._faults[operation].pop(0)

    def _store_rejected(self, request: OrderRequest) -> Order:
        order = Order(
            order_id=request.client_order_id,
            request=request,
            status=OrderStatus.REJECTED,
        )
        key = str(request.client_order_id)
        self._orders[key] = order
        self._fills[key] = []
        return order

    def _accept_order(self, request: OrderRequest) -> Order:
        key = str(request.client_order_id)
        order = Order(order_id=request.client_order_id, request=request, status=OrderStatus.OPEN)
        self._orders[key] = order
        self._fills[key] = []
        self._limit_schedule_index[key] = 0
        if request.order_type is OrderType.MARKET:
            return self._apply_fill(order, request.quantity, self._market_price(request.side))
        if self._limit_is_marketable(request):
            return self._fill_next_limit(order)
        return order

    def _limit_is_marketable(self, request: OrderRequest) -> bool:
        if request.limit_price is None:
            return False
        if request.side is OrderSide.BUY:
            return request.limit_price >= self._quote.ask
        return request.limit_price <= self._quote.bid

    def _fill_next_limit(self, order: Order) -> Order:
        key = str(order.request.client_order_id)
        index = self._limit_schedule_index.get(key, 0)
        if index >= len(self.config.partial_fill_schedule):
            return order
        target = order.request.quantity * self.config.partial_fill_schedule[index]
        quantity = target - order.filled_quantity
        self._limit_schedule_index[key] = index + 1
        if quantity <= 0:
            return order
        return self._apply_fill(order, quantity, order.request.limit_price or Decimal("0"))

    def _apply_fill(self, order: Order, quantity: Decimal, price: Decimal) -> Order:
        key = str(order.request.client_order_id)
        fills = self._fills.setdefault(key, [])
        fill = Fill(
            fill_id=f"sim-{key}-{len(fills) + 1}",
            order_id=order.order_id,
            symbol=order.request.symbol,
            side=order.request.side,
            quantity=quantity,
            price=price,
            fee=quantity * price * self.config.fee_rate,
            fee_asset=self.config.fee_asset,
            occurred_at=utc_now(),
        )
        fills.append(fill)
        new_quantity = order.filled_quantity + quantity
        previous_value = (order.average_fill_price or Decimal("0")) * order.filled_quantity
        average = (previous_value + quantity * price) / new_quantity
        status = (
            OrderStatus.FILLED
            if new_quantity >= order.request.quantity
            else OrderStatus.PARTIALLY_FILLED
        )
        updated = order.model_copy(
            update={
                "status": status,
                "filled_quantity": new_quantity,
                "average_fill_price": average,
                "updated_at": utc_now(),
            }
        )
        self._orders[key] = updated
        self._apply_accounting(order.request, quantity, price, fill.fee)
        return updated

    def _market_price(self, side: OrderSide) -> Decimal:
        if side is OrderSide.BUY:
            return self._quote.ask * (Decimal("1") + self.config.slippage)
        return self._quote.bid * (Decimal("1") - self.config.slippage)

    def _apply_accounting(
        self, request: OrderRequest, quantity: Decimal, price: Decimal, fee: Decimal
    ) -> None:
        base_asset, quote_asset = request.symbol.split("-", maxsplit=1)
        notional = quantity * price
        self._balances.setdefault(base_asset, Decimal("0"))
        self._balances.setdefault(quote_asset, Decimal("0"))
        self._balances.setdefault(self.config.fee_asset, Decimal("0"))
        position_quantity, average_price = self._positions.get(
            request.symbol, (Decimal("0"), Decimal("0"))
        )
        if request.side is OrderSide.BUY:
            self._balances[base_asset] += quantity
            self._balances[quote_asset] -= notional
            self._balances[self.config.fee_asset] -= fee
            total_quantity = position_quantity + quantity
            average_price = (
                (position_quantity * average_price) + (quantity * price)
            ) / total_quantity
            position_quantity = total_quantity
        else:
            self._balances[base_asset] -= quantity
            self._balances[quote_asset] += notional
            self._balances[self.config.fee_asset] -= fee
            position_quantity -= quantity
        if position_quantity == 0:
            self._positions.pop(request.symbol, None)
        else:
            self._positions[request.symbol] = (position_quantity, average_price)
