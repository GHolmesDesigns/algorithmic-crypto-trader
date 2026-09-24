"""Shared fixtures for the Phase 1 gate acceptance tests.

Everything here is deterministic and offline: synthetic candle windows, a SQLite
audit database, and stateful fakes of the Gemini Sandbox and Coinbase Advanced
Trade wire formats served through ``httpx.MockTransport``.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from itertools import count
from typing import Any

import httpx
from app.trading import BrokerRiskInputs, TradingCycle
from brokers.interface import BrokerInterface
from core.models import Candle, MarketState, Quote
from db.models import (
    BalanceSnapshotRecord,
    EquitySnapshotRecord,
    FillRecord,
    OrderRecord,
    PortfolioSnapshotRecord,
    PositionSnapshotRecord,
    RiskDecisionRecord,
    SignalRecord,
)
from execution.audit import AuditStore
from execution.engine import ExecutionEngine, OrderStore
from portfolio.store import SqlAlchemyPortfolioStore
from risk.engine import ExchangeConstraints, RiskLimits
from risk.kill_switch import KillSwitch
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from strategy.backtest import Strategy

CENT = Decimal("0.01")
START = datetime(2024, 3, 1, tzinfo=UTC)

# Every table the gate needs that SQLite can create (JSONB tables are PostgreSQL-only).
SQLITE_TABLES = (
    SignalRecord.__table__,
    RiskDecisionRecord.__table__,
    OrderRecord.__table__,
    FillRecord.__table__,
    PortfolioSnapshotRecord.__table__,
    PositionSnapshotRecord.__table__,
    BalanceSnapshotRecord.__table__,
    EquitySnapshotRecord.__table__,
)

CONSTRAINTS = ExchangeConstraints(
    min_quantity=Decimal("0.00000001"),
    quantity_increment=Decimal("0.00000001"),
    min_notional=Decimal("1"),
    price_increment=CENT,
)

# Limits wide enough that only the position gate (long-only, one unit) shapes trading,
# which mirrors the backtester's "buy only when flat, sell only when long" rule.
PARITY_LIMITS = RiskLimits(
    max_quote_age_seconds=60,
    max_volatility=Decimal("0.10"),
    max_reference_divergence=Decimal("0.02"),
    max_open_positions=5,
    max_trade_notional=Decimal("10000000"),
    max_symbol_position=Decimal("1"),
    max_aggregate_allocation=Decimal("10000000"),
    min_cash_reserve=Decimal("100"),
    max_daily_loss=Decimal("10000000"),
    max_drawdown=Decimal("0.99"),
    max_slippage=Decimal("0.01"),
)


def window(
    moves_bps: Sequence[int],
    *,
    start_price: str = "30000",
    range_fraction: str = "0.002",
    symbol: str = "BTC-USD",
    start: datetime = START,
    bar: timedelta = timedelta(hours=1),
    tick: Decimal = CENT,
) -> tuple[Candle, ...]:
    """Contiguous bars: each opens at the prior close and moves by ``moves_bps``."""

    spread = Decimal(range_fraction)
    price = Decimal(start_price)
    candles = []
    for index, move in enumerate(moves_bps):
        opened = start + bar * index
        close = (price * (Decimal("1") + Decimal(move) / Decimal("10000"))).quantize(tick)
        high = (max(price, close) * (1 + spread)).quantize(tick, rounding=ROUND_CEILING)
        low = (min(price, close) * (1 - spread)).quantize(tick, rounding=ROUND_FLOOR)
        candles.append(
            Candle(
                symbol=symbol,
                interval="ONE_HOUR",
                opened_at=opened,
                closed_at=opened + bar,
                open=price,
                high=high,
                low=low,
                close=close,
                volume=Decimal("10"),
                source="gate-fixture",
                as_of=opened + bar,
                ingested_at=opened + bar,
            )
        )
        price = close
    return tuple(candles)


def legs(up: int, down: int, *, bars: int = 6, repeats: int = 4) -> list[int]:
    return ([up] * bars + [down] * bars) * repeats


# Three parity windows: a calm uptrend, a calm range, and a high-volatility swing.
CALM_TREND = window(legs(60, -30), range_fraction="0.002")
CALM_RANGE = window(legs(40, -40, bars=5, repeats=5), range_fraction="0.002")
HIGH_VOLATILITY = window(
    legs(400, -350, bars=4, repeats=6), start_price="42000", range_fraction="0.02"
)
# SQLite stores Numeric as a binary float, so restart tests that compare balances read
# back from SQLite use whole-dollar prices, which floats hold exactly. PostgreSQL is exact.
WHOLE_DOLLAR_RANGE = window(legs(40, -40, bars=5, repeats=5), tick=Decimal("1"))
PARITY_WINDOWS = {
    "calm_trend": CALM_TREND,
    "calm_range": CALM_RANGE,
    "high_volatility": HIGH_VOLATILITY,
}


def state_at(candles: Sequence[Candle], index: int) -> MarketState:
    """The closed-bar market state a strategy sees after bar ``index`` closes."""

    bar = candles[index]
    return MarketState(
        symbol=bar.symbol,
        quote=Quote(
            symbol=bar.symbol,
            bid=bar.close,
            ask=bar.close,
            as_of=bar.closed_at,
            source="gate-fixture",
            received_at=bar.closed_at,
        ),
        candles=tuple(candles[: index + 1]),
        observed_at=bar.closed_at,
    )


def sqlite_database(tmp_path, name: str = "trader.db"):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / name}", future=True)
    for table in SQLITE_TABLES:
        table.create(engine, checkfirst=True)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


class RecordingPortfolioStore(SqlAlchemyPortfolioStore):
    """SQLite cannot create the JSONB discrepancy table, so discrepancies stay in memory."""

    def __init__(self, session_factory) -> None:
        super().__init__(session_factory)
        self.discrepancies: list[tuple[Any, str]] = []

    def save_discrepancy(self, discrepancy, *, safety_action: str) -> None:
        self.discrepancies.append((discrepancy, safety_action))


def paper_cycle(
    broker: BrokerInterface,
    strategy: Strategy,
    *,
    store: OrderStore,
    audit: AuditStore,
    kill_switch: KillSwitch | None = None,
    limits: RiskLimits = PARITY_LIMITS,
    engine: ExecutionEngine | None = None,
    **options: Any,
) -> TradingCycle:
    return TradingCycle(
        strategy=strategy,
        execution=engine or ExecutionEngine(broker, store),
        audit=audit,
        kill_switch=kill_switch or KillSwitch(),
        risk_inputs=BrokerRiskInputs(
            broker, constraints=CONSTRAINTS, estimated_slippage=Decimal("0")
        ),
        limits=limits,
        environ=options.pop("environ", {}),
        **options,
    )


class FakeVenue:
    """A stateful spot venue: market orders fill in full at the current price."""

    def __init__(self, *, cash: str = "100000") -> None:
        self.price = Decimal("30000")
        self.balances: dict[str, Decimal] = {"USD": Decimal(cash)}
        self.orders: dict[str, dict[str, Any]] = {}
        self.requests: list[tuple[str, str]] = []
        self._ids = count(1)

    def fill_market(self, client_order_id: str, symbol: str, side: str, size: Decimal) -> dict:
        base, quote = symbol.split("-")
        notional = size * self.price
        sign = 1 if side == "buy" else -1
        self.balances[base] = self.balances.get(base, Decimal("0")) + sign * size
        self.balances[quote] = self.balances.get(quote, Decimal("0")) - sign * notional
        number = next(self._ids)
        order = {
            "number": number,
            "client_order_id": client_order_id,
            "symbol": symbol,
            "side": side,
            "size": size,
            "price": self.price,
        }
        self.orders[client_order_id] = order
        return order

    def by_number(self, number: str) -> dict[str, Any] | None:
        return next(
            (order for order in self.orders.values() if str(order["number"]) == number), None
        )


class FakeGeminiSandbox(FakeVenue):
    """Gemini Sandbox REST shapes for ticker, balances, order/new, and order/status.

    As documented, New Order returns the order without trades, and Order Status
    returns a ``trades`` array only when ``include_trades`` is requested.
    """

    def order_payload(self, order: dict[str, Any], *, include_trades: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "order_id": str(order["number"]),
            "client_order_id": order["client_order_id"],
            "symbol": order["symbol"].replace("-", "").lower(),
            "side": order["side"],
            "type": "exchange market",
            "original_amount": str(order["size"]),
            "executed_amount": str(order["size"]),
            "remaining_amount": "0",
            "avg_execution_price": str(order["price"]),
            "is_live": False,
            "is_cancelled": False,
            "timestampms": 1709251200000,
        }
        if include_trades:
            payload["trades"] = [
                {
                    "tid": 900000 + order["number"],
                    "order_id": str(order["number"]),
                    "price": str(order["price"]),
                    "amount": str(order["size"]),
                    "type": order["side"].capitalize(),
                    "aggressor": True,
                    "fee_currency": "USD",
                    "fee_amount": "0",
                    "timestampms": 1709251200000,
                }
            ]
        return payload

    async def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.sandbox.gemini.com"
        if request.method == "GET" and request.url.path.startswith("/v2/ticker/"):
            self.requests.append(("ticker", ""))
            price = str(self.price)
            return httpx.Response(200, json={"bid": price, "ask": price}, request=request)
        assert request.headers["x-gemini-apikey"] == "sandbox-key"
        payload = json.loads(base64.b64decode(request.headers["x-gemini-payload"]))
        path = payload["request"]
        self.requests.append((path, str(payload.get("client_order_id", ""))))
        if path == "/v1/balances":
            rows = [
                {
                    "currency": asset,
                    "available": str(amount),
                    "available_for_withdrawal": str(amount),
                }
                for asset, amount in self.balances.items()
                if amount
            ]
            return httpx.Response(200, json=rows, request=request)
        if path == "/v1/order/new":
            symbol = payload["symbol"].upper()
            order = self.fill_market(
                payload["client_order_id"],
                f"{symbol[:-3]}-{symbol[-3:]}",
                payload["side"],
                Decimal(payload["amount"]),
            )
            return httpx.Response(
                200, json=self.order_payload(order, include_trades=False), request=request
            )
        if path == "/v1/order/status":
            assert not ("order_id" in payload and "client_order_id" in payload)
            order = None
            if "client_order_id" in payload:
                order = self.orders.get(payload["client_order_id"])
            elif "order_id" in payload:
                assert isinstance(payload["order_id"], int)
                order = self.by_number(str(payload["order_id"]))
            if order is None:
                return httpx.Response(404, json={"reason": "OrderNotFound"}, request=request)
            body = self.order_payload(order, include_trades=bool(payload.get("include_trades")))
            return httpx.Response(200, json=body, request=request)
        raise AssertionError(f"unexpected Gemini request {path}")


COINBASE_PREFIX = "/api/v3/brokerage"


class FakeCoinbase(FakeVenue):
    """Coinbase Advanced Trade shapes as documented for the endpoints the adapter uses."""

    def order_id(self, order: dict[str, Any]) -> str:
        return f"00000000-0000-4000-8000-{order['number']:012d}"

    def order_payload(self, order: dict[str, Any]) -> dict[str, Any]:
        return {
            "order_id": self.order_id(order),
            "client_order_id": order["client_order_id"],
            "product_id": order["symbol"],
            "side": order["side"].upper(),
            "status": "FILLED",
            "filled_size": str(order["size"]),
            "average_filled_price": str(order["price"]),
            "created_time": "2024-03-01T00:00:00Z",
        }

    async def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.coinbase.com"
        path = request.url.path.removeprefix(COINBASE_PREFIX)
        self.requests.append((request.method, path))
        if path.endswith("/ticker"):
            assert "authorization" not in request.headers
            price = str(self.price)
            return httpx.Response(
                200, json={"trades": [], "best_bid": price, "best_ask": price}, request=request
            )
        assert request.headers["authorization"] == "Bearer gate-token"
        if request.method == "GET" and path == "/accounts":
            accounts = [
                {
                    "uuid": f"account-{asset}",
                    "currency": asset,
                    "available_balance": {"value": str(amount), "currency": asset},
                    "hold": {"value": "0", "currency": asset},
                }
                for asset, amount in self.balances.items()
                if amount
            ]
            return httpx.Response(
                200,
                json={"accounts": accounts, "has_next": False, "cursor": "", "size": len(accounts)},
                request=request,
            )
        if request.method == "POST" and path == "/orders":
            body = json.loads(request.content)
            size = Decimal(body["order_configuration"]["market_market_ioc"]["base_size"])
            order = self.fill_market(
                body["client_order_id"], body["product_id"], body["side"].lower(), size
            )
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "success_response": {
                        "order_id": self.order_id(order),
                        "product_id": order["symbol"],
                        "side": body["side"],
                        "client_order_id": order["client_order_id"],
                    },
                },
                request=request,
            )
        if request.method == "GET" and path == "/orders/historical/batch":
            rows = [self.order_payload(order) for order in self.orders.values()]
            return httpx.Response(
                200, json={"orders": rows, "has_next": False, "cursor": ""}, request=request
            )
        if request.method == "GET" and path == "/orders/historical/fills":
            wanted = request.url.params["order_ids"]
            rows = [
                {
                    "entry_id": f"entry-{order['number']}",
                    "trade_id": f"trade-{order['number']}",
                    "order_id": self.order_id(order),
                    "trade_time": "2024-03-01T00:00:00Z",
                    "price": str(order["price"]),
                    "size": str(order["size"]),
                    "commission": "0",
                    "side": order["side"].upper(),
                    "product_id": order["symbol"],
                }
                for order in self.orders.values()
                if self.order_id(order) == wanted
            ]
            return httpx.Response(200, json={"fills": rows, "cursor": ""}, request=request)
        if request.method == "GET" and path.startswith("/orders/historical/"):
            wanted = path.rsplit("/", 1)[1]
            for order in self.orders.values():
                if self.order_id(order) == wanted:
                    return httpx.Response(
                        200, json={"order": self.order_payload(order)}, request=request
                    )
            return httpx.Response(404, json={"error": "NOT_FOUND"}, request=request)
        raise AssertionError(f"unexpected Coinbase request {request.method} {path}")
