"""Daily equity (#100): every UTC day gets an end-of-day value the soak digest can pass on.

The recorded value follows the loss-limit gate's definition. A holding with no live
quote is valued at its last-known price and flagged; one that never had a price makes
the snapshot partial instead of counting as zero.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from api.soak import DIGEST_WINDOW
from api.trends import trends_query
from app.trading import BrokerRiskInputs
from brokers.simulated import SimulatedBroker
from core.models import Balance, Position, Quote, utc_now
from db.models import Base, EquityHoldingRecord, EquitySnapshotRecord, LastPriceRecord
from portfolio.equity import EquitySampler, SqlAlchemyEquityStore
from portfolio.reconciliation import PortfolioState, Reconciler
from portfolio.scheduler import ScheduledReconciler
from portfolio.valuation import LAST_PRICE_MAX_AGE
from risk.kill_switch import KillSwitch
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import sessionmaker

from tests.gate_support import CONSTRAINTS
from tests.test_soak import (
    NOW,
    PATH,
    SqlAlchemySoak,
    days_by_date,
    hourly_heartbeats,
    restart,
)
from tests.test_trading_cycle import STATE

BCH = "BCH-USD"


class Venue:
    """A broker stand-in whose quotes can be switched on and off per symbol."""

    healthy = True

    def __init__(self, positions=(), cash="1000", hold="0", prices=None) -> None:
        self.positions = positions
        self.cash = Decimal(cash)
        self.hold = Decimal(hold)
        self.prices = dict(prices or {})
        self.at = utc_now()

    async def get_balances(self):
        return (Balance(asset="USD", available=self.cash, hold=self.hold, as_of=utc_now()),)

    async def get_positions(self):
        return self.positions

    async def get_quote(self, symbol):
        if symbol not in self.prices:
            raise ConnectionError("ticker unavailable")
        price = self.prices[symbol]
        return Quote(symbol=symbol, bid=price, ask=price, as_of=self.at, source="unit-ticker")


def holding(symbol: str, quantity: str) -> Position:
    return Position(symbol=symbol, quantity=Decimal(quantity), average_price=None, as_of=utc_now())


def database(tmp_path: Path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'trader.db'}", future=True)
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def sampler_for(venue, session_factory, clock) -> EquitySampler:
    store = SqlAlchemyEquityStore(session_factory)
    return EquitySampler(venue, store, store, clock=clock)


def snapshots(session_factory) -> list[EquitySnapshotRecord]:
    with session_factory() as session:
        return list(
            session.scalars(select(EquitySnapshotRecord).order_by(EquitySnapshotRecord.as_of))
        )


def holdings(session_factory) -> list[EquityHoldingRecord]:
    with session_factory() as session:
        return list(session.scalars(select(EquityHoldingRecord)))


# Valuation


@pytest.mark.asyncio
async def test_recorded_equity_equals_the_value_the_loss_limit_gate_uses(tmp_path) -> None:
    engine, factory = database(tmp_path)
    venue = Venue(
        (holding("ETH-USD", "2"), holding(BCH, "3")),
        cash="7000",
        hold="500",
        prices={"ETH-USD": Decimal("1800.5"), BCH: Decimal("400")},
    )
    now = datetime(2026, 9, 27, 12, tzinfo=UTC)
    state_file = tmp_path / "loss.json"
    gate = BrokerRiskInputs(
        venue,
        constraints=CONSTRAINTS,
        estimated_slippage=Decimal("0"),
        clock=lambda: now,
        loss_state_path=state_file,
    )
    # A candle-close reference for the signal's own symbol would differ, so observe a
    # state for a symbol the account does not hold: every holding is valued at its bid.
    other = STATE.model_copy(update={"symbol": "SOL-USD"})
    await gate.observe(other)
    gate_equity = Decimal(json.loads(state_file.read_text())["day_start_equity"])

    sample = await sampler_for(venue, factory, lambda: now).sample()
    assert sample is not None
    assert sample.equity == gate_equity == Decimal("12301.0")  # 7500 + 2*1800.5 + 3*400
    assert snapshots(factory)[0].equity == gate_equity
    engine.dispose()


@pytest.mark.asyncio
async def test_a_failed_quote_keeps_the_old_price_which_ages_and_is_never_zero(tmp_path) -> None:
    engine, factory = database(tmp_path)
    venue = Venue((holding(BCH, "2"),), cash="100", prices={BCH: Decimal("400")})
    quoted_at = datetime(2026, 9, 27, 9, tzinfo=UTC)
    venue.at = quoted_at
    clock = [quoted_at]
    sampler = sampler_for(venue, factory, lambda: clock[0])

    first = await sampler.sample()
    assert first is not None and first.equity == Decimal("900")
    assert holdings(factory) == []  # a live quote needs no flag

    venue.prices.clear()  # the ticker fails
    clock[0] = quoted_at + timedelta(hours=3)
    second = await sampler.sample()
    assert second is not None and second.equity == Decimal("900")  # the last price, not zero
    (flag,) = holdings(factory)
    assert (flag.symbol, flag.basis, flag.price) == (BCH, "last_known", Decimal("400"))
    assert flag.price_age_seconds == 3 * 3600  # from the quote's own time
    with factory() as session:
        stored = session.get(LastPriceRecord, BCH)
        assert stored.bid == Decimal("400")  # the failure did not overwrite it
    engine.dispose()


@pytest.mark.asyncio
async def test_a_symbol_never_quoted_makes_a_partial_snapshot_not_a_zero(tmp_path) -> None:
    engine, factory = database(tmp_path)
    venue = Venue((holding(BCH, "2"),), cash="100")
    sample = await sampler_for(venue, factory, utc_now).sample()
    assert sample is not None and sample.partial
    (row,) = snapshots(factory)
    assert row.partial is True and row.equity is None
    (flag,) = holdings(factory)
    assert (flag.symbol, flag.basis, flag.price) == (BCH, "unpriced", None)
    engine.dispose()


@pytest.mark.asyncio
async def test_a_stored_price_survives_a_restart(tmp_path) -> None:
    engine, factory = database(tmp_path)
    venue = Venue((holding(BCH, "1"),), cash="0", prices={BCH: Decimal("410")})
    venue.at = datetime(2026, 9, 27, 9, tzinfo=UTC)
    await sampler_for(venue, factory, lambda: venue.at).sample()
    engine.dispose()

    engine, factory = database(tmp_path)  # a new process, the same database
    venue.prices.clear()
    later = await sampler_for(venue, factory, lambda: venue.at + timedelta(hours=1)).sample()
    assert later is not None and later.equity == Decimal("410")
    assert holdings(factory)[0].price_age_seconds == 3600
    engine.dispose()


@pytest.mark.asyncio
async def test_a_persistence_or_broker_failure_records_nothing_and_never_raises(tmp_path) -> None:
    engine, factory = database(tmp_path)

    class Down:
        async def get_balances(self):
            raise ConnectionError("broker unavailable")

    assert await sampler_for(Down(), factory, utc_now).sample() is None

    class Broken(SqlAlchemyEquityStore):
        def save(self, sample, *, source):
            from sqlalchemy.exc import OperationalError

            raise OperationalError("insert", {}, RuntimeError("database is locked"))

    broken = Broken(factory)
    venue = Venue(cash="50")
    assert await EquitySampler(venue, broken, broken).sample() is None
    assert snapshots(factory) == []
    engine.dispose()


# Scheduling


@pytest.mark.asyncio
async def test_a_reconciliation_records_equity_for_the_day(tmp_path) -> None:
    engine, factory = database(tmp_path)
    broker = SimulatedBroker()
    baseline = PortfolioState(balances=await broker.get_balances())
    store = SqlAlchemyEquityStore(factory)
    scheduler = ScheduledReconciler(
        Reconciler(broker, KillSwitch()),
        baseline=baseline,
        interval_seconds=60,
        on_reconciled=EquitySampler(broker, store, store).sample,
    )
    await scheduler.run_once()
    (row,) = snapshots(factory)
    assert row.equity is not None and row.equity > 0
    engine.dispose()


@pytest.mark.asyncio
async def test_a_failing_post_reconciliation_hook_does_not_disturb_the_run() -> None:
    broker = SimulatedBroker()

    async def boom(_state) -> None:
        raise RuntimeError("hook failed")

    scheduler = ScheduledReconciler(
        Reconciler(broker, KillSwitch()),
        baseline=PortfolioState(balances=await broker.get_balances()),
        interval_seconds=60,
        on_reconciled=boom,
    )
    result = await scheduler.run_once()
    assert result is not None and result.discrepancies == ()
    assert scheduler.status.last_result == "clean"


@pytest.mark.asyncio
async def test_the_scheduled_sampler_reads_under_the_trading_lock(tmp_path) -> None:
    engine, factory = database(tmp_path)
    sampler = sampler_for(Venue(cash="10"), factory, utc_now)
    stop, lock = asyncio.Event(), asyncio.Lock()
    await lock.acquire()  # trading holds the lock
    task = asyncio.create_task(sampler.run(stop, 0.01, lock=lock))
    await asyncio.sleep(0.1)
    assert snapshots(factory) == []
    lock.release()
    for _ in range(100):
        if snapshots(factory):
            break
        await asyncio.sleep(0.01)
    stop.set()
    await task
    assert snapshots(factory)
    with pytest.raises(ValueError):
        await sampler.run(asyncio.Event(), 0)
    engine.dispose()


# Digest


async def sampled_day(tmp_path, venue, age: timedelta):
    """A UTC day with full uptime whose final snapshot values BCH at a price ``age`` old."""

    engine, factory = database(tmp_path)
    edges = trends_query(DIGEST_WINDOW, NOW).edges
    start = edges[3]
    final_at = start + timedelta(hours=23)
    venue.at = final_at - age  # when the ticker last answered
    await sampler_for(venue, factory, lambda: venue.at).sample()  # a live quote, stored
    venue.prices.clear()  # the ticker then fails
    await sampler_for(venue, factory, lambda: final_at).sample()
    with factory() as session:
        session.add_all([*hourly_heartbeats(start), restart(start, "clean")])
        session.commit()
    engine.dispose()
    engine, factory = database(tmp_path)
    return days_by_date(SqlAlchemySoak(factory).read(now=NOW))[start.date().isoformat()]


@pytest.mark.asyncio
async def test_a_last_price_under_the_limit_shows_an_asterisk_and_the_day_can_pass(tmp_path):
    venue = Venue((holding(BCH, "2"),), cash="100", prices={BCH: Decimal("400")})
    day = await sampled_day(tmp_path, venue, timedelta(hours=3))
    assert day["equity"] == "900" and day["equity_approximate"] is True
    assert day["equity_notes"] == ["includes BCH-USD at its last price, 3 h old"]
    assert day["missing"] == [] and day["status"] == "pass"


@pytest.mark.asyncio
async def test_a_last_price_at_or_over_the_limit_keeps_the_day_incomplete(tmp_path):
    just_under = LAST_PRICE_MAX_AGE - timedelta(minutes=1)
    venue = Venue((holding(BCH, "2"),), cash="100", prices={BCH: Decimal("400")})
    under = tmp_path / "a"
    under.mkdir()
    passing = await sampled_day(under, venue, just_under)
    assert passing["status"] == "pass" and passing["equity_approximate"] is True

    over = tmp_path / "b"
    over.mkdir()
    venue = Venue((holding(BCH, "2"),), cash="100", prices={BCH: Decimal("400")})
    day = await sampled_day(over, venue, LAST_PRICE_MAX_AGE)
    assert day["status"] == "incomplete" and day["missing"] == ["equity_price_age"]
    assert day["equity"] == "900"
    (note,) = day["equity_notes"]
    assert "BCH-USD" in note and "24 h old" in note


@pytest.mark.asyncio
async def test_a_holding_that_never_had_a_price_leaves_the_day_incomplete(tmp_path):
    venue = Venue((holding(BCH, "2"),), cash="100")
    day = await sampled_day(tmp_path, venue, timedelta(hours=1))
    assert day["equity"] is None and day["equity_approximate"] is False
    assert "equity" in day["missing"] and day["status"] == "incomplete"
    assert day["equity_notes"] == ["BCH-USD has never had a price, so equity could not be valued"]


def test_a_day_without_a_snapshot_still_reads_not_recorded(tmp_path):
    engine, factory = database(tmp_path)
    day = days_by_date(SqlAlchemySoak(factory).read(now=NOW))
    for item in day.values():
        assert item["equity"] is None and item["equity_notes"] == []
        assert "equity" in item["missing"]
    engine.dispose()


@pytest.mark.asyncio
async def test_the_page_shows_the_asterisk_and_the_footnote(tmp_path, monkeypatch) -> None:
    from tests.operator_support import history_app
    from tests.test_soak import ADMIN_BROWSER, client_for

    monkeypatch.setenv("OPERATOR_TOKEN", "operator-secret-soak-c94a")
    monkeypatch.setenv("OPERATOR_ADMIN_TOKEN", "admin-secret-soak-2b71")
    monkeypatch.setenv("KILL_SWITCH_FILE", str(tmp_path / "kill-switch.json"))
    engine, factory = database(tmp_path)
    venue = Venue((holding(BCH, "2"),), cash="100", prices={BCH: Decimal("400")})
    venue.at = utc_now() - timedelta(hours=5)
    await sampler_for(venue, factory, lambda: venue.at).sample()
    venue.prices.clear()
    await sampler_for(venue, factory, lambda: venue.at + timedelta(hours=3)).sample()
    engine.dispose()

    application = history_app(tmp_path)
    async with client_for(application) as client:
        page = await client.get(PATH, headers=ADMIN_BROWSER)
    assert page.status_code == 200
    assert "900*" in page.text or "900<span" in page.text
    assert "includes BCH-USD at its last price, 3 h old" in page.text


# Migration


@pytest.fixture
def keep_logging():
    """Alembic's env.py reconfigures logging; put it back so later tests still see records."""

    root = logging.getLogger()
    saved = (list(root.handlers), root.level)
    loggers = {
        name: (item.disabled, item.propagate)
        for name, item in logging.root.manager.loggerDict.items()
        if isinstance(item, logging.Logger)
    }
    yield
    root.handlers[:] = saved[0]
    root.setLevel(saved[1])
    for name, (disabled, propagate) in loggers.items():
        item = logging.getLogger(name)
        item.disabled, item.propagate = disabled, propagate


def test_the_migration_keeps_existing_equity_and_can_roll_back(
    tmp_path, monkeypatch, keep_logging
) -> None:
    url = f"sqlite+pysqlite:///{tmp_path / 'migrate.db'}"
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    config.set_main_option("script_location", str(Path(__file__).parents[1] / "alembic"))
    monkeypatch.setenv("DATABASE_URL", url)
    engine = create_engine(url, future=True)
    command.upgrade(config, "0010_unknown_cost_basis")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO equity_curve (snapshot_id, equity, as_of, source) VALUES "
                "('a', 100, '2026-09-27 12:00:00', 'broker')"
            )
        )
    command.upgrade(config, "head")
    inspector = inspect(engine)
    assert {"equity_snapshot_holdings", "last_known_prices"} <= set(inspector.get_table_names())
    columns = {c["name"]: c for c in inspector.get_columns("equity_curve")}
    assert columns["equity"]["nullable"] is True and "partial" in columns
    with engine.connect() as connection:
        row = connection.execute(text("SELECT equity, partial FROM equity_curve")).one()
    assert float(row[0]) == 100 and not row[1]
    command.downgrade(config, "0010_unknown_cost_basis")
    command.upgrade(config, "head")
    engine.dispose()
