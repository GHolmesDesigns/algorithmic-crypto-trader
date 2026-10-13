"""A position's average price from our saved fills (#162), shown only when it is trustworthy.

Gemini and Coinbase report no cost basis, so a position has ``average_price=None``. The
dashboard shows the size-weighted average of the fills this system saved only when they
account for exactly the quantity the broker holds. Anything else stays "not known" with a
reason, and the derived figure is display-only.

SQLite keeps ``Numeric`` as a float, so the values here are exact binary fractions.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from api.operator import OperatorState
from brokers.simulated import SimulatedBroker
from core.guards import CredentialScope, StartupSettings
from core.models import Balance, OrderSide, Position, TradingMode, utc_now
from db.models import Base, FillRecord, OrderRecord, PositionSnapshotRecord
from portfolio.fill_cost import (
    EXCESS,
    NO_FILLS,
    NOT_COVERED,
    UNREADABLE,
    FillCost,
    FillCostReader,
)
from portfolio.reconciliation import PortfolioState, Reconciler
from risk.kill_switch import KillSwitch
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker

from tests.test_operator_dashboard import OPERATOR, dashboard_app, page

BASE = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)


def database(tmp_path: Path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'trader.db'}", future=True)
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


class Ledger:
    """Saves orders and fills the way the order store does, one fill per call."""

    def __init__(self, factory) -> None:
        self.factory = factory
        self.count = 0

    def fill(
        self,
        side: OrderSide | str | None,
        quantity: str,
        price: str,
        *,
        symbol: str | None = "BTC-USD",
        fee: str = "0",
        minutes: int | None = None,
        fill_id: str | None = None,
    ) -> None:
        self.count += 1
        order_id = uuid4()
        with self.factory() as session:
            session.add(
                OrderRecord(
                    order_id=order_id,
                    signal_id=uuid4(),
                    client_order_id=uuid4(),
                    strategy_version="v1",
                    risk_approval_id=uuid4(),
                    symbol=symbol,
                    side=side.value if isinstance(side, OrderSide) else side,
                    order_type="market",
                    quantity=Decimal(quantity),
                    correlation_id=uuid4(),
                    status="filled",
                    created_at=BASE,
                )
            )
            session.add(
                FillRecord(
                    fill_id=uuid4(),
                    order_id=order_id,
                    broker_fill_id=fill_id or f"fill-{self.count:03d}",
                    quantity=Decimal(quantity),
                    price=Decimal(price),
                    fee=Decimal(fee),
                    occurred_at=BASE
                    + timedelta(minutes=self.count if minutes is None else minutes),
                )
            )
            session.commit()

    def buy(self, quantity: str, price: str, **kwargs) -> None:
        self.fill(OrderSide.BUY, quantity, price, **kwargs)

    def sell(self, quantity: str, price: str, **kwargs) -> None:
        self.fill(OrderSide.SELL, quantity, price, **kwargs)


def held(quantity: str, symbol: str = "BTC-USD", average: str | None = None) -> Position:
    return Position(
        symbol=symbol,
        quantity=Decimal(quantity),
        average_price=None if average is None else Decimal(average),
        as_of=utc_now(),
    )


def costs(factory, *positions: Position) -> dict[str, FillCost]:
    return FillCostReader(factory).read(positions)


def one(factory, position: Position) -> FillCost:
    return costs(factory, position)[position.symbol]


# The reader


def test_buys_from_zero_give_the_exact_size_weighted_average(tmp_path) -> None:
    _, factory = database(tmp_path)
    ledger = Ledger(factory)
    ledger.buy("2", "100", fee="1")
    ledger.buy("1", "130", fee="1.5")  # fees are excluded from the price

    result = one(factory, held("3"))

    assert result.average_price == Decimal("110")  # (2 * 100 + 1 * 130) / 3
    assert result.reason is None
    assert isinstance(result.average_price, Decimal)


def test_a_partial_sell_keeps_the_average_and_the_quantity_must_match(tmp_path) -> None:
    _, factory = database(tmp_path)
    ledger = Ledger(factory)
    ledger.buy("2", "100")
    ledger.buy("1", "130")
    ledger.sell("1", "500")  # the sell price never moves the average

    assert one(factory, held("2")).average_price == Decimal("110")
    stale = one(factory, held("3"))  # the broker shows more than the fills leave
    assert stale.average_price is None and stale.reason == NOT_COVERED
    short = one(factory, held("1"))  # the broker shows less than the fills leave
    assert short.average_price is None and short.reason == EXCESS


def test_a_full_close_then_a_rebuy_starts_the_average_again(tmp_path) -> None:
    _, factory = database(tmp_path)
    ledger = Ledger(factory)
    ledger.buy("1", "100")
    ledger.sell("1", "120")
    ledger.buy("2", "150")

    assert one(factory, held("2")).average_price == Decimal("150")


def test_a_sell_larger_than_the_saved_buys_restarts_the_walk_after_it(tmp_path) -> None:
    _, factory = database(tmp_path)
    ledger = Ledger(factory)
    ledger.buy("1", "100")
    ledger.sell("3", "110")  # two of these units came from outside our fills
    ledger.buy("2", "50")

    assert one(factory, held("2")).average_price == Decimal("50")  # the 100 buy is gone


def test_an_oversell_with_no_buy_after_it_leaves_nothing_covered(tmp_path) -> None:
    _, factory = database(tmp_path)
    ledger = Ledger(factory)
    ledger.buy("1", "100")
    ledger.sell("3", "110")

    result = one(factory, held("1"))

    assert result.average_price is None and result.reason == NOT_COVERED


def test_a_pre_funded_balance_with_no_fills_stays_unknown(tmp_path) -> None:
    _, factory = database(tmp_path)

    result = one(factory, held("0.0003"))

    assert result.average_price is None and result.reason == NO_FILLS


def test_fills_that_cover_only_part_of_the_quantity_give_no_partial_price(tmp_path) -> None:
    _, factory = database(tmp_path)
    Ledger(factory).buy("1", "100")

    result = one(factory, held("1.5"))  # 0.5 came from elsewhere

    assert result.average_price is None
    assert result.reason == NOT_COVERED


def test_fills_that_record_more_than_the_broker_holds_give_no_price(tmp_path) -> None:
    _, factory = database(tmp_path)
    Ledger(factory).buy("1", "100")

    result = one(factory, held("0.5"))  # some left the account

    assert result.average_price is None and result.reason == EXCESS


def test_a_fee_charged_in_the_base_asset_is_netted_out_of_the_quantity(tmp_path) -> None:
    _, factory = database(tmp_path)
    Ledger(factory).buy("1", "100", fee="0.125")

    # The fills table does not store the fee asset. A quote fee would leave 1 unit, so only the
    # base-asset reading ends on the broker's exact quantity.
    result = one(factory, held("0.875"))

    assert result.average_price == Decimal("100")


def test_a_fee_in_the_quote_asset_leaves_the_quantity_alone(tmp_path) -> None:
    _, factory = database(tmp_path)
    Ledger(factory).buy("1", "100", fee="5")

    assert one(factory, held("1")).average_price == Decimal("100")


def test_a_fee_that_neither_reading_explains_gives_no_price(tmp_path) -> None:
    _, factory = database(tmp_path)
    Ledger(factory).buy("1", "100", fee="0.125")

    result = one(factory, held("2"))  # neither 1 nor 0.875

    assert result.average_price is None and result.reason == NOT_COVERED


def test_a_base_asset_fee_on_a_sell_comes_out_of_the_quantity_too(tmp_path) -> None:
    _, factory = database(tmp_path)
    ledger = Ledger(factory)
    ledger.buy("2", "100")
    ledger.sell("0.5", "120", fee="0.25")

    assert one(factory, held("1.25")).average_price == Decimal("100")


def test_fills_are_walked_in_time_order_not_insertion_order(tmp_path) -> None:
    _, factory = database(tmp_path)
    ledger = Ledger(factory)
    # By time: buy 1 at 100, sell 1, buy 1 at 200 leaves one unit at 200. In insertion order
    # (200 buy, sell, 100 buy) the same fills would leave one unit at 100.
    ledger.buy("1", "200", minutes=3, fill_id="c")
    ledger.sell("1", "150", minutes=2, fill_id="b")
    ledger.buy("1", "100", minutes=1, fill_id="a")

    assert one(factory, held("1")).average_price == Decimal("200")


def test_fills_at_the_same_instant_are_walked_in_fill_id_order(tmp_path) -> None:
    _, factory = database(tmp_path)
    ledger = Ledger(factory)
    ledger.buy("1", "100", minutes=5, fill_id="a")
    ledger.sell("1", "100", minutes=5, fill_id="b")
    ledger.buy("1", "300", minutes=5, fill_id="c")

    assert one(factory, held("1")).average_price == Decimal("300")


def test_symbols_are_kept_apart(tmp_path) -> None:
    _, factory = database(tmp_path)
    ledger = Ledger(factory)
    ledger.buy("1", "100", symbol="BTC-USD")
    ledger.buy("4", "2000", symbol="ETH-USD")

    result = costs(factory, held("1"), held("4", "ETH-USD"), held("9", "SOL-USD"))

    assert result["BTC-USD"].average_price == Decimal("100")
    assert result["ETH-USD"].average_price == Decimal("2000")
    assert result["SOL-USD"].reason == NO_FILLS


def test_a_venue_reported_price_is_never_replaced(tmp_path) -> None:
    _, factory = database(tmp_path)
    Ledger(factory).buy("1", "100")

    assert costs(factory, held("1", average="90")) == {}


def test_a_zero_venue_price_is_a_price_and_is_left_alone(tmp_path) -> None:
    _, factory = database(tmp_path)
    Ledger(factory).buy("1", "100")

    assert costs(factory, held("1", average="0")) == {}


def test_an_order_without_a_symbol_or_side_is_not_counted(tmp_path) -> None:
    _, factory = database(tmp_path)
    ledger = Ledger(factory)
    ledger.fill(OrderSide.BUY, "1", "100", symbol=None)
    ledger.fill(None, "1", "100")

    assert one(factory, held("1")).reason == NO_FILLS


def test_an_unreadable_fills_table_gives_unknown_with_a_reason(tmp_path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'empty.db'}", future=True)

    result = one(sessionmaker(bind=engine), held("1"))  # no tables at all

    assert result.average_price is None and result.reason == UNREADABLE


def test_a_stored_side_that_is_not_a_side_is_unreadable_not_guessed(tmp_path) -> None:
    _, factory = database(tmp_path)
    Ledger(factory).fill("hold", "1", "100")

    assert one(factory, held("1")).reason == UNREADABLE


def test_a_new_reader_over_the_same_database_gives_the_same_figure(tmp_path) -> None:
    _, factory = database(tmp_path)
    ledger = Ledger(factory)
    ledger.buy("2", "100")
    ledger.buy("1", "130")

    before = one(factory, held("3"))
    after = FillCostReader(factory).read([held("3")])["BTC-USD"]  # a restarted process

    assert before == after and after.average_price == Decimal("110")


def test_reading_writes_nothing(tmp_path) -> None:
    engine, factory = database(tmp_path)
    ledger = Ledger(factory)
    ledger.buy("1", "100")
    statements: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def record(_conn, _cursor, statement, *_rest) -> None:
        statements.append(statement.lstrip().split(None, 1)[0].upper())

    one(factory, held("1"))

    assert statements and set(statements) == {"SELECT"}
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(FillRecord)) == 1
        assert session.scalar(select(func.count()).select_from(OrderRecord)) == 1


# The operator state


class FixedBroker:
    def __init__(self, positions: tuple[Position, ...]) -> None:
        self.positions = positions

    async def get_balances(self):
        return (Balance(asset="USD", available="100", as_of=utc_now()),)

    async def get_positions(self):
        return self.positions


class BrokenReader:
    def read(self, positions):
        raise RuntimeError("the reader failed")


def operator_state(broker) -> OperatorState:
    settings = StartupSettings(
        TradingMode.PAPER, CredentialScope.VIEW, "", "postgresql://unused", "INFO"
    )
    return OperatorState(settings=settings, kill_switch=KillSwitch(), broker=broker)


def payload_positions(state: OperatorState) -> dict[str, dict]:
    return {item["symbol"]: item for item in state.to_dict()["portfolio"]["positions"]}


@pytest.mark.asyncio
async def test_refresh_attaches_the_derived_average_beside_the_broker_position(tmp_path) -> None:
    _, factory = database(tmp_path)
    ledger = Ledger(factory)
    ledger.buy("2", "100")
    ledger.buy("1", "130")
    broker_positions = (held("3"), held("0.0003", "LTC-USD"), held("1", "ETH-USD", "1800.5"))
    state = operator_state(FixedBroker(broker_positions))
    state.fill_costs = FillCostReader(factory)

    refreshed = await state.refresh()
    positions = payload_positions(state)

    assert refreshed.positions == broker_positions  # the broker's own positions, unchanged
    assert positions["BTC-USD"]["average_price"] is None
    assert positions["BTC-USD"]["cost_from_fills"] == {"average_price": "110", "reason": None}
    assert positions["LTC-USD"]["cost_from_fills"] == {"average_price": None, "reason": NO_FILLS}
    assert "cost_from_fills" not in positions["ETH-USD"]  # the venue's price wins
    assert positions["ETH-USD"]["average_price"] == "1800.5"


@pytest.mark.asyncio
async def test_a_failed_read_never_blocks_the_balances_and_positions(tmp_path) -> None:
    state = operator_state(FixedBroker((held("1"),)))
    state.fill_costs = BrokenReader()

    refreshed = await state.refresh()

    assert refreshed.portfolio_status == "current"
    assert len(refreshed.balances) == 1 and len(refreshed.positions) == 1
    assert payload_positions(state)["BTC-USD"]["cost_from_fills"] == {
        "average_price": None,
        "reason": UNREADABLE,
    }


@pytest.mark.asyncio
async def test_without_a_reader_the_payload_is_what_it_was() -> None:
    state = operator_state(FixedBroker((held("1"),)))

    await state.refresh()

    assert "cost_from_fills" not in payload_positions(state)["BTC-USD"]


@pytest.mark.asyncio
async def test_a_figure_for_a_quantity_that_has_since_changed_is_not_attached(tmp_path) -> None:
    _, factory = database(tmp_path)
    Ledger(factory).buy("1", "100")
    broker = FixedBroker((held("1"),))
    state = operator_state(broker)
    state.fill_costs = FillCostReader(factory)
    await state.refresh()

    broker.positions = (held("1.5"),)  # a later broker refresh, before the fills are re-read
    await state._refresh_portfolio()

    assert "cost_from_fills" not in payload_positions(state)["BTC-USD"]


@pytest.mark.asyncio
async def test_the_derived_average_stays_out_of_reconciliation_and_the_saved_snapshots(
    tmp_path,
) -> None:
    _, factory = database(tmp_path)
    Ledger(factory).buy("1", "100")
    broker = SimulatedBroker()
    broker._positions["BTC-USD"] = (Decimal("1"), None)
    state = operator_state(broker)
    state.fill_costs = FillCostReader(factory)

    await state.refresh()

    assert payload_positions(state)["BTC-USD"]["cost_from_fills"]["average_price"] == "100"
    # Every consumer reads the broker's positions, which still have no cost basis. Had the
    # derived 100 reached them it would differ from this local 999 and halt trading.
    assert [item.average_price for item in state.snapshot.positions] == [None]
    local = PortfolioState(
        positions=(held("1", average="999"),), balances=await broker.get_balances()
    )
    result = await Reconciler(broker, KillSwitch()).reconcile(local)
    assert result.discrepancies == () and result.safety_tripped is False
    # Nothing was written to the saved portfolio snapshots either.
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(PositionSnapshotRecord)) == 0


# The dashboard


def cell(html: str, symbol: str) -> str:
    row = re.search(rf'<tr><th scope="row">{symbol}</th>(.*?)</tr>', html, re.S)
    assert row is not None, symbol
    return row.group(1)


def dashboard_with(monkeypatch, tmp_path, positions, *, build=None):
    _, factory = database(tmp_path)
    if build:
        build(Ledger(factory))
    application = dashboard_app(monkeypatch, broker=FixedBroker(positions))
    application.state.operator_state.fill_costs = FillCostReader(factory)
    return application, factory


@pytest.mark.asyncio
async def test_the_dashboard_labels_the_figure_as_from_our_fills(monkeypatch, tmp_path) -> None:
    def build(ledger: Ledger) -> None:
        ledger.buy("2", "100")
        ledger.buy("1", "130")

    application, factory = dashboard_with(monkeypatch, tmp_path, (held("3"),), build=build)

    html = await page(application)

    row = cell(html, "BTC-USD")
    assert '<td class="num">110<span class="note fact-note">from our fills</span></td>' in row
    assert "not known" not in row
    with factory() as session:  # rendering wrote nothing
        assert session.scalar(select(func.count()).select_from(FillRecord)) == 2
        assert session.scalar(select(func.count()).select_from(OrderRecord)) == 2


@pytest.mark.asyncio
async def test_the_dashboard_says_why_a_price_is_not_known(monkeypatch, tmp_path) -> None:
    def build(ledger: Ledger) -> None:
        ledger.buy("1", "100", symbol="ETH-USD")

    positions = (held("0.0003"), held("1.5", "ETH-USD"))
    application, _ = dashboard_with(monkeypatch, tmp_path, positions, build=build)

    html = await page(application)

    pre_funded = cell(html, "BTC-USD")
    assert "not known" in pre_funded and NO_FILLS in pre_funded
    partial = cell(html, "ETH-USD")
    assert "not known" in partial and NOT_COVERED in partial
    assert "from our fills" not in html
    assert not re.search(r"\b0\b", re.sub(r"<[^>]+>", "", partial.split("</td>")[1]))


@pytest.mark.asyncio
async def test_an_unreadable_fills_table_leaves_the_rest_of_the_dashboard_refreshing(
    monkeypatch, tmp_path
) -> None:
    application = dashboard_app(monkeypatch, broker=FixedBroker((held("1"),)))
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'none.db'}", future=True)
    application.state.operator_state.fill_costs = FillCostReader(sessionmaker(bind=engine))

    html = await page(application)

    row = cell(html, "BTC-USD")
    assert "not known" in row and UNREADABLE in row
    assert '<th scope="row">USD</th>' in html  # the balances refreshed too


@pytest.mark.asyncio
async def test_a_venue_price_is_shown_without_the_label(monkeypatch, tmp_path) -> None:
    application, _ = dashboard_with(
        monkeypatch,
        tmp_path,
        (held("1", average="90"),),
        build=lambda ledger: ledger.buy("1", "100"),
    )

    row = cell(await page(application), "BTC-USD")

    assert '<td class="num">90</td>' in row and "from our fills" not in row


@pytest.mark.asyncio
async def test_the_status_payload_carries_the_derived_average(monkeypatch, tmp_path) -> None:
    application, _ = dashboard_with(
        monkeypatch, tmp_path, (held("1"),), build=lambda ledger: ledger.buy("1", "100")
    )
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/operator/state", headers=OPERATOR)
    assert response.status_code == 200, response.text
    position = response.json()["portfolio"]["positions"][0]
    assert position["average_price"] is None
    assert position["cost_from_fills"] == {"average_price": "100", "reason": None}


@pytest.mark.asyncio
async def test_the_service_gives_the_operator_state_a_fills_reader(tmp_path, monkeypatch) -> None:
    from app.main import create_app, start_scheduled_reconciliation

    monkeypatch.setenv("APP_ENV", "test")
    engine, _ = database(tmp_path)
    engine.dispose()
    settings = StartupSettings(
        TradingMode.PAPER,
        CredentialScope.NONE,
        "",
        f"sqlite+pysqlite:///{tmp_path / 'trader.db'}",
        "INFO",
    )
    application = create_app(settings, broker=SimulatedBroker())
    stop = start_scheduled_reconciliation(application, interval_seconds=3600)
    assert stop is not None
    try:
        assert isinstance(application.state.operator_state.fill_costs, FillCostReader)
    finally:
        await stop()
