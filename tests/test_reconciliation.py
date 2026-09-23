from decimal import Decimal

import pytest
from brokers.simulated import SimulatedBroker
from core.models import Balance, Position, utc_now
from db.models import BalanceSnapshotRecord, EquitySnapshotRecord, PositionSnapshotRecord
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


def test_portfolio_store_persists_position_balance_and_equity_snapshots() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    for table in (
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
