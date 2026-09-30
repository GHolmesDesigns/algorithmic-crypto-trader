"""An unknown cost basis is ``None`` from the venue adapter to the stored snapshot (#30)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from brokers.simulated import SimulatedBroker
from core.models import Balance, Fill, OrderSide, Position, utc_now
from db.models import (
    BalanceSnapshotRecord,
    Base,
    PortfolioSnapshotRecord,
    PositionSnapshotRecord,
)
from portfolio.ledger import apply_fills
from portfolio.reconciliation import PortfolioState, Reconciler
from portfolio.store import SqlAlchemyPortfolioStore
from risk.kill_switch import KillSwitch
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from tests.test_broker_adapters import GEMINI_BALANCES, gemini_with_balances


def position(quantity: str, average: str | None) -> Position:
    return Position(
        symbol="BTC-USD",
        quantity=Decimal(quantity),
        average_price=None if average is None else Decimal(average),
        as_of=utc_now(),
    )


def trade(quantity: str, price: str, *, second: int = 0, side: OrderSide = OrderSide.BUY) -> Fill:
    return Fill(
        fill_id=str(uuid4()),
        order_id=uuid4(),
        symbol="BTC-USD",
        side=side,
        quantity=Decimal(quantity),
        price=Decimal(price),
        fee=Decimal("0"),
        fee_asset="USD",
        occurred_at=datetime(2026, 9, 24, tzinfo=UTC) + timedelta(seconds=second),
    )


def baseline(*positions: Position) -> PortfolioState:
    return PortfolioState(
        positions=positions,
        balances=(
            Balance(asset="USD", available=Decimal("100000"), as_of=utc_now()),
            *(Balance(asset="BTC", available=item.quantity, as_of=utc_now()) for item in positions),
        ),
    )


def test_a_position_defaults_to_an_unknown_cost_basis_and_zero_is_a_real_price() -> None:
    unknown = Position(symbol="BTC-USD", quantity=Decimal("1"), as_of=utc_now())
    assert unknown.average_price is None
    assert position("1", "0").average_price == Decimal("0")
    with pytest.raises(ValueError):
        position("1", "-1")


@pytest.mark.asyncio
async def test_venue_adapters_report_no_cost_basis_as_none() -> None:
    broker = gemini_with_balances(GEMINI_BALANCES)
    try:
        positions = await broker.get_positions()
    finally:
        await broker.close()
    assert positions and all(item.average_price is None for item in positions)


def test_a_buy_onto_an_unknown_cost_basis_keeps_it_unknown() -> None:
    projected = apply_fills(baseline(position("2", None)), [trade("1", "60000")])
    [held] = projected.positions
    # Averaging against a placeholder 0 would have reported 20000 here.
    assert (held.quantity, held.average_price) == (Decimal("3"), None)


def test_a_sell_leaves_the_cost_basis_unchanged_known_or_unknown() -> None:
    sell = trade("1", "70000", side=OrderSide.SELL)
    [unknown] = apply_fills(baseline(position("2", None)), [sell]).positions
    [known] = apply_fills(baseline(position("2", "50000")), [sell]).positions
    assert (unknown.average_price, known.average_price) == (None, Decimal("50000"))


def test_a_new_position_has_a_known_cost_and_a_known_basis_still_blends() -> None:
    [opened] = apply_fills(baseline(), [trade("2", "100")]).positions
    assert opened.average_price == Decimal("100")
    [blended] = apply_fills(baseline(position("1", "100")), [trade("1", "200")]).positions
    assert blended.average_price == Decimal("150")
    # Selling everything and buying again starts a fresh, known cost basis.
    [reopened] = apply_fills(
        baseline(position("1", None)),
        [trade("1", "10", second=1, side=OrderSide.SELL), trade("1", "30", second=2)],
    ).positions
    assert reopened.average_price == Decimal("30")


async def discrepancies(*, remote: str | None, local: str | None) -> tuple[str, ...]:
    broker = SimulatedBroker()
    broker._positions["BTC-USD"] = (Decimal("1"), None if remote is None else Decimal(remote))
    state = PortfolioState(positions=(position("1", local),), balances=await broker.get_balances())
    result = await Reconciler(broker, KillSwitch()).reconcile(state)
    return tuple(item.entity_type for item in result.discrepancies)


@pytest.mark.asyncio
async def test_a_known_average_price_against_an_unknown_one_is_not_a_discrepancy() -> None:
    assert await discrepancies(remote=None, local="60000") == ()
    assert await discrepancies(remote="60000", local=None) == ()
    assert await discrepancies(remote=None, local=None) == ()


@pytest.mark.asyncio
async def test_two_known_average_prices_that_differ_are_a_discrepancy_even_at_zero() -> None:
    assert await discrepancies(remote="61000", local="60000") == ("position",)
    assert await discrepancies(remote="0", local="60000") == ("position",)
    assert await discrepancies(remote="60000", local="60000") == ()


@pytest.mark.asyncio
async def test_a_quantity_mismatch_still_halts_when_the_cost_basis_is_unknown() -> None:
    broker = SimulatedBroker()
    broker._positions["BTC-USD"] = (Decimal("2"), None)
    state = PortfolioState(positions=(position("1", None),), balances=await broker.get_balances())
    result = await Reconciler(broker, KillSwitch()).reconcile(state)
    assert [item.entity_type for item in result.discrepancies] == ["position"]
    assert result.safety_tripped is True


def test_the_store_writes_and_reads_null_and_keeps_a_real_zero(tmp_path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'trader.db'}", future=True)
    for record in (PortfolioSnapshotRecord, PositionSnapshotRecord, BalanceSnapshotRecord):
        record.__table__.create(engine)
    store = SqlAlchemyPortfolioStore(sessionmaker(bind=engine, expire_on_commit=False))
    airdrop = Position(
        symbol="ETH-USD", quantity=Decimal("3"), average_price=Decimal("0"), as_of=utc_now()
    )
    store.save_snapshot(PortfolioState(positions=(position("1", None), airdrop)), source="broker")
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT symbol, average_price FROM positions_snapshot")
        ).all()
    stored = {symbol: price for symbol, price in rows}
    assert stored["BTC-USD"] is None and stored["ETH-USD"] == 0
    latest = store.latest_state()
    assert latest is not None
    assert {item.symbol: item.average_price for item in latest.positions} == {
        "BTC-USD": None,
        "ETH-USD": Decimal("0"),
    }
    engine.dispose()


# --- migration ----------------------------------------------------------------------


def test_the_migration_turns_zero_into_null_and_back(tmp_path, monkeypatch) -> None:
    from alembic import command
    from alembic.config import Config

    url = f"sqlite+pysqlite:///{tmp_path / 'migrated.db'}"
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parents[1] / "alembic"))
    monkeypatch.setenv("DATABASE_URL", url)
    engine = create_engine(url, future=True)
    command.upgrade(config, "0009_watchlist")
    insert = (
        "INSERT INTO positions_snapshot (snapshot_id, symbol, quantity, average_price, as_of, "
        "source) VALUES ('{id}', '{symbol}', 1, {price}, '2026-09-27 12:00:00', 'broker')"
    )
    with engine.begin() as connection:
        connection.execute(text(insert.format(id="a", symbol="BTC-USD", price=0)))
        connection.execute(text(insert.format(id="b", symbol="ETH-USD", price="1800.5")))

    def averages() -> dict[str, object]:
        with engine.connect() as connection:
            rows = connection.execute(
                text("SELECT symbol, average_price FROM positions_snapshot")
            ).all()
        return {symbol: price for symbol, price in rows}

    command.upgrade(config, "head")
    upgraded = averages()
    assert upgraded["BTC-USD"] is None and float(upgraded["ETH-USD"]) == 1800.5
    with engine.begin() as connection:  # a NULL is now accepted
        connection.execute(text(insert.format(id="c", symbol="SOL-USD", price="NULL")))
    columns = {c["name"]: c for c in inspect(engine).get_columns("positions_snapshot")}
    assert columns["average_price"]["nullable"] is True
    assert Base.metadata.tables["positions_snapshot"].c.average_price.nullable is True

    command.downgrade(config, "0009_watchlist")
    downgraded = averages()
    assert downgraded["BTC-USD"] == 0 and downgraded["SOL-USD"] == 0
    assert float(downgraded["ETH-USD"]) == 1800.5
    with engine.connect() as connection:
        version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    assert version == "0009_watchlist"
    command.upgrade(config, "head")
    assert averages()["BTC-USD"] is None
    engine.dispose()
