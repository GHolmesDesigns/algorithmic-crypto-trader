"""Child process for the SIGKILL idempotency test, plus the file-backed venue it uses.

The venue keeps its orders in a JSON file so they outlive the killed process,
the way a real exchange outlives a crashed client. Run as
``python -m tests.sigkill_child DB_PATH VENUE_PATH KILL_POINT``; the process
kills itself with SIGKILL (hard termination on Windows) at ``KILL_POINT``:

- ``before_venue``: after the pre-submit record is committed, before the venue sees the order;
- ``after_venue``: after the venue has filled the order, before the response is recorded.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from brokers.interface import BrokerCapabilities, BrokerInterface
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
    Signal,
    utc_now,
)

PRICE = Decimal("30000")
SIGNAL = Signal(
    signal_id=UUID("aaaaaaaa-0000-4000-8000-000000000001"),
    symbol="BTC-USD",
    side=OrderSide.BUY,
    quantity=Decimal("0.5"),
    strategy_version="sigkill-v1",
    correlation_id=UUID("aaaaaaaa-0000-4000-8000-000000000002"),
)
APPROVAL = RiskApproval(
    approval_id=UUID("aaaaaaaa-0000-4000-8000-000000000003"),
    signal_id=SIGNAL.signal_id,
    approved=True,
    reason="all ordered risk gates passed",
    correlation_id=SIGNAL.correlation_id,
)
REQUEST = OrderRequest(
    signal_id=SIGNAL.signal_id,
    strategy_version=SIGNAL.strategy_version,
    symbol=SIGNAL.symbol,
    side=SIGNAL.side,
    order_type=OrderType.MARKET,
    quantity=SIGNAL.quantity,
    correlation_id=SIGNAL.correlation_id,
)


def hard_kill() -> None:
    # SIGKILL cannot be caught; Windows has no SIGKILL, and os.kill there calls
    # TerminateProcess, which is equally abrupt: no finally blocks, no flushing.
    os.kill(os.getpid(), getattr(signal, "SIGKILL", signal.SIGTERM))


class JsonFileVenue(BrokerInterface):
    def __init__(self, path: Path, *, kill_point: str | None = None) -> None:
        self.path = path
        self.kill_point = kill_point
        if not path.exists():
            self._write({"orders": {}, "balances": {"USD": "100000"}, "submissions": 0})

    @property
    def capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities(
            provider="json-file-venue",
            environment="local",
            streaming=False,
            historical_candles=False,
            native_order_edit=False,
            preview_orders=False,
            order_types=("market",),
            price_increment=Decimal("0.01"),
            quantity_increment=Decimal("0.00000001"),
            max_quote_age_seconds=60,
        )

    def state(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _write(self, state: dict) -> None:
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state), encoding="utf-8")
        temporary.replace(self.path)

    async def get_quote(self, symbol: str) -> Quote:
        return Quote(symbol=symbol, bid=PRICE, ask=PRICE, as_of=utc_now(), source="json-venue")

    async def get_balances(self) -> tuple[Balance, ...]:
        now = utc_now()
        return tuple(
            Balance(asset=asset, available=Decimal(amount), as_of=now)
            for asset, amount in sorted(self.state()["balances"].items())
            if Decimal(amount) != 0
        )

    async def get_positions(self) -> tuple[Position, ...]:
        held = Decimal(self.state()["balances"].get("BTC", "0"))
        if not held:
            return ()
        return (Position(symbol="BTC-USD", quantity=held, average_price=PRICE, as_of=utc_now()),)

    async def submit_order(self, request: OrderRequest, approval: RiskApproval) -> Order:
        if not approval.approved or approval.signal_id != request.signal_id:
            raise PermissionError("risk approval required")
        if self.kill_point == "before_venue":
            hard_kill()
        state = self.state()
        key = str(request.client_order_id)
        state["submissions"] += 1
        if key not in state["orders"]:
            # Like Coinbase, a reused client_order_id returns the existing order.
            state["orders"][key] = {"size": str(request.quantity), "price": str(PRICE)}
            balances = state["balances"]
            balances["BTC"] = str(Decimal(balances.get("BTC", "0")) + request.quantity)
            balances["USD"] = str(Decimal(balances["USD"]) - request.quantity * PRICE)
        self._write(state)
        if self.kill_point == "after_venue":
            hard_kill()
        order = await self.get_order(key)
        assert order is not None
        return order

    async def get_order(self, client_order_id: str) -> Order | None:
        raw = self.state()["orders"].get(client_order_id)
        if raw is None:
            return None
        return Order(
            order_id=UUID(client_order_id),
            request=REQUEST.model_copy(update={"client_order_id": UUID(client_order_id)}),
            status=OrderStatus.FILLED,
            filled_quantity=Decimal(raw["size"]),
            average_fill_price=Decimal(raw["price"]),
        )

    async def get_fills(self, client_order_id: str) -> tuple[Fill, ...]:
        raw = self.state()["orders"].get(client_order_id)
        if raw is None:
            return ()
        return (
            Fill(
                fill_id=f"venue-fill-{client_order_id}",
                order_id=UUID(client_order_id),
                symbol="BTC-USD",
                side=OrderSide.BUY,
                quantity=Decimal(raw["size"]),
                price=Decimal(raw["price"]),
                fee=Decimal("0"),
                fee_asset="USD",
                occurred_at=utc_now(),
            ),
        )


def main() -> None:
    from execution.audit import SqlAlchemyAuditStore
    from execution.engine import ExecutionEngine
    from execution.persistence import SqlAlchemyOrderStore
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    database, venue_path, kill_point = sys.argv[1:4]
    engine = create_engine(f"sqlite+pysqlite:///{database}", future=True)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    audit = SqlAlchemyAuditStore(session_factory)
    audit.record_signal(SIGNAL)
    audit.record_risk_decision(APPROVAL)
    venue = JsonFileVenue(Path(venue_path), kill_point=kill_point)
    asyncio.run(
        ExecutionEngine(venue, SqlAlchemyOrderStore(session_factory)).submit(REQUEST, APPROVAL)
    )
    raise SystemExit("the venue call returned; the kill point was not reached")


if __name__ == "__main__":
    main()
