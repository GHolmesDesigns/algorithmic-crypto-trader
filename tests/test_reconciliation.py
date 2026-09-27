import base64
import json
from collections.abc import Callable
from decimal import Decimal

import httpx
import pytest
from brokers.coinbase import CoinbaseBroker
from brokers.gemini import GeminiBroker
from brokers.simulated import SimulatedBroker
from core.models import Balance, Position, utc_now
from core.resilience import TokenBucketRateLimiter
from db.models import (
    BalanceSnapshotRecord,
    EquitySnapshotRecord,
    PortfolioSnapshotRecord,
    PositionSnapshotRecord,
)
from portfolio.ledger import apply_fills
from portfolio.reconciliation import PortfolioState, Reconciler
from portfolio.store import SqlAlchemyPortfolioStore
from risk.kill_switch import KillSwitch
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker


@pytest.mark.asyncio
async def test_reconciliation_is_broker_authoritative_and_trips_on_divergence() -> None:
    broker = SimulatedBroker()
    balances = await broker.get_balances()
    alerts = []
    switch = KillSwitch()
    local = PortfolioState(
        positions=(
            Position(
                symbol="BTC-USD",
                quantity=Decimal("1"),
                average_price=Decimal("60000"),
                as_of=utc_now(),
            ),
        ),
        balances=balances,
    )
    result = await Reconciler(broker, switch, alerts.append).reconcile(local)
    assert result.safety_tripped is True
    assert result.authoritative.positions == ()
    assert [(item.asset, item.available, item.hold) for item in result.authoritative.balances] == [
        (item.asset, item.available, item.hold) for item in balances
    ]
    assert any(item.entity_type == "position" for item in result.discrepancies)
    assert alerts
    assert switch.state.value == "halted"


@pytest.mark.asyncio
async def test_reconciliation_accepts_matching_empty_portfolio() -> None:
    broker = SimulatedBroker()
    local = PortfolioState(balances=await broker.get_balances())
    result = await Reconciler(broker, KillSwitch()).reconcile(local)
    assert result.discrepancies == ()
    assert result.safety_tripped is False


@pytest.mark.asyncio
async def test_reconciliation_ignores_an_unavailable_provider_cost_basis() -> None:
    broker = SimulatedBroker()
    broker._positions["BTC-USD"] = (Decimal("1"), Decimal("0"))
    local = PortfolioState(
        positions=(
            Position(
                symbol="BTC-USD",
                quantity=Decimal("1"),
                average_price=Decimal("60000"),
                as_of=utc_now(),
            ),
        ),
        balances=await broker.get_balances(),
    )

    result = await Reconciler(broker, KillSwitch()).reconcile(local)

    assert result.discrepancies == ()
    assert result.safety_tripped is False


# Each venue's account listing, built from {currency: (available, hold)}.
VenueState = dict[str, tuple[str, str]]


def gemini_venue(state: VenueState) -> GeminiBroker | CoinbaseBroker:
    """Gemini's documented rows: ``amount`` is the total, ``available`` excludes holds."""

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(base64.b64decode(request.headers["x-gemini-payload"]))
        assert payload["request"] == "/v1/balances"
        rows = [
            {
                "type": "exchange",
                "currency": currency,
                "amount": str(Decimal(available) + Decimal(hold)),
                "available": available,
                "availableForWithdrawal": available,
            }
            for currency, (available, hold) in state.items()
        ]
        return httpx.Response(200, json=rows, request=request)

    return GeminiBroker(
        api_key="sandbox-key",
        api_secret="sandbox-secret",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        rate_limiter=TokenBucketRateLimiter(100, 100),
    )


def coinbase_venue(state: VenueState) -> GeminiBroker | CoinbaseBroker:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/accounts")
        accounts = [
            {
                "currency": currency,
                "available_balance": {"value": available, "currency": currency},
                "hold": {"value": hold, "currency": currency},
            }
            for currency, (available, hold) in state.items()
        ]
        return httpx.Response(
            200,
            json={"accounts": accounts, "has_next": False, "cursor": "", "size": len(accounts)},
            request=request,
        )

    return CoinbaseBroker(
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        auth_token="test-token",
        rate_limiter=TokenBucketRateLimiter(100, 100),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("venue", [gemini_venue, coinbase_venue])
async def test_a_resting_sell_is_not_a_position_divergence_from_the_fill_ledger(
    venue: Callable[[VenueState], GeminiBroker | CoinbaseBroker],
) -> None:
    state = {"BTC": ("1", "0"), "USD": ("1000", "0")}
    broker = venue(state)
    switch = KillSwitch()
    try:
        baseline = PortfolioState(
            balances=await broker.get_balances(), positions=await broker.get_positions()
        )
        # A 0.4 BTC sell limit now rests: the venue moves it to hold, and nothing has filled.
        state["BTC"] = ("0.6", "0.4")
        result = await Reconciler(broker, switch).reconcile(apply_fills(baseline, []))
    finally:
        await broker.close()

    assert [(item.symbol, item.quantity) for item in result.authoritative.positions] == [
        ("BTC-USD", Decimal("1"))
    ]
    # The ledger does not model holds, so the moved balance still halts (fail closed).
    assert [(item.entity_type, item.entity_key) for item in result.discrepancies] == [
        ("balance", "BTC")
    ]
    assert switch.state.value == "halted"


def test_portfolio_store_persists_position_balance_and_equity_snapshots() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    for table in (
        PortfolioSnapshotRecord.__table__,
        PositionSnapshotRecord.__table__,
        BalanceSnapshotRecord.__table__,
        EquitySnapshotRecord.__table__,
    ):
        table.create(engine)
    store = SqlAlchemyPortfolioStore(sessionmaker(bind=engine, expire_on_commit=False))
    position = Position(
        symbol="BTC-USD",
        quantity=Decimal("0.1"),
        average_price=Decimal("60000"),
        as_of=utc_now(),
    )
    balance = Balance(asset="USD", available=Decimal("1000"), as_of=utc_now())
    store.save_snapshot(
        PortfolioState(positions=(position,), balances=(balance,)),
        equity=Decimal("7000"),
        source="broker",
    )
    with sessionmaker(bind=engine)() as session:
        assert session.scalar(select(PositionSnapshotRecord.snapshot_id)) is not None
        assert session.scalar(select(BalanceSnapshotRecord.snapshot_id)) is not None
        assert session.scalar(select(EquitySnapshotRecord.equity)) == Decimal("7000")
    engine.dispose()
