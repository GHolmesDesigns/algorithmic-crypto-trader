"""The app's own fills reconcile at the venue's balance precision (#118).

On 2026-10-05 and 2026-10-06 the paper app bought and sold 0.0001 BTC on the Gemini Sandbox
and each order was followed by a ``reconciliation_divergence`` halt on **balance, USD,
available**. The ledger projects USD exactly (quantity x price has 6 decimals), the Sandbox
reports 5, and the reconciler compares for equality. The numbers below are the recorded
fills and the balances the dashboard showed afterwards.

Rounding the projected balance once, half up, fitted those two trades but not the sell of
2026-10-06 20:55 UTC, which filled in two pieces and halted on a 0.00002 USD gap. The Sandbox
settles every fill on its own and cuts the remainder below 0.00001 off, which is the only rule
that fits all four recorded trades.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from app.main import create_app, start_scheduled_reconciliation
from brokers.interface import BrokerCapabilities
from brokers.simulated import SimulatedBroker
from core.guards import CredentialScope, StartupSettings
from core.models import (
    Balance,
    Fill,
    KillSwitchState,
    Order,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    TradingMode,
    utc_now,
)
from portfolio.ledger import apply_fills
from portfolio.reconciliation import Discrepancy, PortfolioState, Reconciler
from portfolio.scheduler import ScheduledReconciler
from portfolio.store import SqlAlchemyPortfolioStore
from pydantic import ValidationError
from risk.kill_switch import KillSwitch

from tests.gate_support import sqlite_database
from tests.test_reconciliation import coinbase_venue, gemini_venue

FIVE_DECIMALS = {"USD": Decimal("0.00001")}
START = ("10000.00000", "0")  # the account's USD (available, hold) before the first trade
BUY_PRICE, SELL_PRICE = "85328.82", "85259.74"
# What the Sandbox reported after the buy and after the sell, to 5 decimals.
USD_AFTER_BUY, USD_AFTER_SELL = "9991.43299", "9999.92486"


class SandboxVenue(SimulatedBroker):
    """A venue that reports the strings it holds, like the Sandbox, and declares its precision."""

    def __init__(self, increments: dict[str, Decimal] | None = None) -> None:
        super().__init__()
        self.increments = FIVE_DECIMALS if increments is None else increments
        self.account: dict[str, tuple[str, str]] = {"USD": START}
        self.order: Order | None = None
        self.fills: tuple[Fill, ...] = ()

    @property
    def capabilities(self) -> BrokerCapabilities:
        declared = super().capabilities.model_dump()
        return BrokerCapabilities(**{**declared, "balance_increments": self.increments})

    async def get_balances(self) -> tuple[Balance, ...]:
        now = utc_now()
        return tuple(
            Balance(asset=asset, available=Decimal(available), hold=Decimal(hold), as_of=now)
            for asset, (available, hold) in sorted(self.account.items())
            if Decimal(available) or Decimal(hold)
        )

    async def get_positions(self) -> tuple[Position, ...]:
        now = utc_now()
        return tuple(
            Position(
                symbol=f"{asset}-USD",
                quantity=Decimal(available) + Decimal(hold),
                average_price=None,
                as_of=now,
            )
            for asset, (available, hold) in sorted(self.account.items())
            if asset != "USD" and (Decimal(available) or Decimal(hold))
        )

    async def get_order(self, client_order_id: str) -> Order | None:
        return self.order

    async def get_fills(self, client_order_id: str) -> tuple[Fill, ...]:
        return self.fills


def own_order(
    side: OrderSide,
    quantity: str,
    price: str,
    fee: str,
    *,
    status: OrderStatus = OrderStatus.FILLED,
    filled: str | None = None,
    second: int = 0,
) -> tuple[Order, Fill]:
    request = OrderRequest(
        signal_id=uuid4(),
        strategy_version="unit",
        symbol="BTC-USD",
        side=side,
        order_type=OrderType.MARKET,
        quantity=Decimal(quantity),
        correlation_id=uuid4(),
    )
    order = Order(
        order_id=uuid4(),
        request=request,
        status=status,
        filled_quantity=Decimal(filled or quantity),
    )
    fill = Fill(
        fill_id=str(uuid4()),
        order_id=order.order_id,
        symbol="BTC-USD",
        side=side,
        quantity=Decimal(filled or quantity),
        price=Decimal(price),
        fee=Decimal(fee),
        fee_asset="USD",
        occurred_at=datetime(2026, 10, 5, 12, 0, second, tzinfo=UTC),
    )
    return order, fill


class Run:
    """A scheduled reconciler watching a venue, so a test states the account after each trade."""

    def __init__(self, venue: SandboxVenue) -> None:
        self.venue = venue
        self.switch = KillSwitch()
        self.alerts: list[Discrepancy] = []

    async def start(self) -> Run:
        baseline = PortfolioState(
            balances=await self.venue.get_balances(), positions=await self.venue.get_positions()
        )
        self.scheduler = ScheduledReconciler(
            Reconciler(self.venue, self.switch, self.alerts.append),
            baseline=baseline,
            interval_seconds=60,
            balance_increments=self.venue.capabilities.balance_increments,
        )
        return self

    async def after(
        self,
        account: dict[str, tuple[str, str]],
        order: Order | None = None,
        fills: tuple[Fill, ...] = (),
        *,
        venue_fills: tuple[Fill, ...] | None = None,
    ) -> list[tuple[str, str]]:
        """The venue now reports ``account``; the app recorded ``order`` and ``fills``.

        The venue lists the same fills unless ``venue_fills`` says it knows of others.
        """

        self.venue.account, self.venue.order = account, order
        self.venue.fills = fills if venue_fills is None else venue_fills
        if order is not None:
            self.scheduler.observe(order, fills)
        result = await self.scheduler.run_once()
        assert result is not None
        return [(item.entity_type, item.entity_key) for item in result.discrepancies]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("buy_fee", "sell_fee"),
    [("0.03413", "0.0341"), ("0.034131528", "0.034103896")],
    ids=["fee as the dashboard shows it", "exact 0.4% fee"],
)
async def test_the_apps_own_buy_and_sell_reconcile_cleanly_at_the_sandbox_precision(
    buy_fee: str, sell_fee: str
) -> None:
    run = await Run(SandboxVenue()).start()

    buy, buy_fill = own_order(OrderSide.BUY, "0.0001", BUY_PRICE, buy_fee)
    assert (
        await run.after({"USD": (USD_AFTER_BUY, "0"), "BTC": ("0.0001", "0")}, buy, (buy_fill,))
        == []
    )
    assert run.switch.state is KillSwitchState.RUNNING

    sell, sell_fill = own_order(OrderSide.SELL, "0.0001", SELL_PRICE, sell_fee, second=1)
    assert (
        await run.after({"USD": (USD_AFTER_SELL, "0"), "BTC": ("0", "0")}, sell, (sell_fill,)) == []
    )
    assert run.switch.state is KillSwitchState.RUNNING
    assert run.alerts == []
    assert (run.scheduler.status.clean_runs, run.scheduler.status.diverged_runs) == (2, 0)


@pytest.mark.asyncio
async def test_a_sell_split_across_two_fills_reconciles_at_what_the_sandbox_credited() -> None:
    # 2026-10-06: the 19:40 buy, then the 20:55 sell that filled in two pieces. The Sandbox
    # credited 5.23220 and 3.20316, each piece's quantity x price cut to 5 decimals; rounding
    # the 9999.82605582 total once gave 9999.82606 and halted on a 0.00002 gap.
    venue = SandboxVenue()
    venue.account = {"USD": (USD_AFTER_SELL, "0")}
    run = await Run(venue).start()

    buy, buy_fill = own_order(OrderSide.BUY, "0.0001", "84665.9", "0.03386", second=2)
    assert (
        await run.after({"USD": ("9991.42441", "0"), "BTC": ("0.0001", "0")}, buy, (buy_fill,))
        == []
    )

    sell, piece_one = own_order(
        OrderSide.SELL, "0.0001", "84390.47", "0.02092", filled="0.000062", second=3
    )
    _, piece_two = own_order(
        OrderSide.SELL, "0.0001", "84293.86", "0.01281", filled="0.000038", second=3
    )
    sell = sell.model_copy(update={"filled_quantity": Decimal("0.0001")})
    piece_two = piece_two.model_copy(update={"order_id": sell.order_id})
    assert (
        await run.after(
            {"USD": ("9999.82604", "0"), "BTC": ("0", "0")}, sell, (piece_one, piece_two)
        )
        == []
    )
    assert run.switch.state is KillSwitchState.RUNNING
    assert run.alerts == []


@pytest.mark.parametrize(
    ("side", "usd"),
    [
        # Recorded: the first piece of the 20:55 sell, 5.23220914 credited as 5.23220.
        pytest.param(OrderSide.SELL, "1005.21128", id="sell"),
        # Inferred: no recorded buy had a remainder of half a unit or more. Every recorded buy
        # fits this rule, and a buy that does not would halt and show the gap.
        pytest.param(OrderSide.BUY, "994.74688", id="buy"),
    ],
)
def test_a_remainder_of_half_a_unit_or_more_is_cut_off_not_rounded_up(
    side: OrderSide, usd: str
) -> None:
    _, fill = own_order(side, "0.000062", "84390.47", "0.02092")

    projected = apply_fills(
        baseline(USD="1000.00000", BTC="1"), [fill], balance_increments=FIVE_DECIMALS
    )

    assert {item.asset: item.available for item in projected.balances}["USD"] == Decimal(usd)


def test_fills_are_settled_one_by_one_not_as_a_total() -> None:
    # The two pieces of the 20:55 sell without fees: one by one 5.23220 + 3.20316 = 8.43536,
    # where cutting the 8.43537582 total once would give 8.43537.
    _, piece_one = own_order(OrderSide.SELL, "0.000062", "84390.47", "0", second=1)
    _, piece_two = own_order(OrderSide.SELL, "0.000038", "84293.86", "0", second=2)

    projected = apply_fills(
        baseline(USD="0", BTC="1"), [piece_one, piece_two], balance_increments=FIVE_DECIMALS
    )

    assert {item.asset: item.available for item in projected.balances}["USD"] == Decimal("8.43536")


@pytest.mark.asyncio
async def test_a_partial_fill_then_its_completion_reconcile_cleanly() -> None:
    run = await Run(SandboxVenue()).start()

    # 0.00004 of a 0.0001 BTC buy fills: 3.4131528 notional plus a 0.0136526112 fee, which the
    # venue settles as 3.41315 and 0.01365.
    partial, first = own_order(
        OrderSide.BUY,
        "0.0001",
        BUY_PRICE,
        "0.0136526112",
        status=OrderStatus.PARTIALLY_FILLED,
        filled="0.00004",
    )
    assert (
        await run.after({"USD": ("9996.57320", "0"), "BTC": ("0.00004", "0")}, partial, (first,))
        == []
    )

    # The remaining 0.00006 fills at 85330: 5.1198 notional plus a 0.0204792 fee, settled as
    # 5.11980 and 0.02047.
    done = partial.model_copy(
        update={"status": OrderStatus.FILLED, "filled_quantity": Decimal("0.0001")}
    )
    _, rest = own_order(OrderSide.BUY, "0.0001", "85330", "0.0204792", filled="0.00006", second=1)
    rest = rest.model_copy(update={"order_id": done.order_id})
    assert (
        await run.after({"USD": ("9991.43293", "0"), "BTC": ("0.0001", "0")}, done, (first, rest))
        == []
    )
    assert run.switch.state is KillSwitchState.RUNNING


@pytest.mark.asyncio
@pytest.mark.parametrize("difference", ["0.01", "-0.01", "0.00001", "-0.00001"])
async def test_a_real_usd_difference_still_halts_and_alerts(difference: str) -> None:
    # One cent, and also one unit of the venue's own precision: rounding is not a tolerance.
    run = await Run(SandboxVenue()).start()
    buy, fill = own_order(OrderSide.BUY, "0.0001", BUY_PRICE, "0.03413")
    reported = Decimal(USD_AFTER_BUY) + Decimal(difference)

    found = await run.after({"USD": (str(reported), "0"), "BTC": ("0.0001", "0")}, buy, (fill,))

    assert found == [("balance", "USD")]
    assert run.switch.state is KillSwitchState.HALTED
    assert [(item.entity_type, item.entity_key) for item in run.alerts] == [("balance", "USD")]
    # The alert still carries the broker's own value, which becomes the baseline.
    assert run.alerts[0].broker.available == reported


@pytest.mark.asyncio
async def test_a_balance_change_the_app_did_not_place_still_halts() -> None:
    run = await Run(SandboxVenue()).start()

    # Someone bought 0.0001 BTC by hand: the account moved and the app recorded no fill.
    found = await run.after({"USD": (USD_AFTER_BUY, "0"), "BTC": ("0.0001", "0")})

    assert found == [("position", "BTC-USD"), ("balance", "BTC"), ("balance", "USD")]
    assert run.switch.state is KillSwitchState.HALTED


@pytest.mark.asyncio
async def test_an_outside_order_alongside_the_apps_fill_still_halts_on_what_stays_exact() -> None:
    buy, fill = own_order(OrderSide.BUY, "0.0001", BUY_PRICE, "0.03413")

    # One satoshi more BTC than the fill explains: BTC is never rounded, so USD agrees, BTC halts.
    run = await Run(SandboxVenue()).start()
    found = await run.after({"USD": (USD_AFTER_BUY, "0"), "BTC": ("0.00010001", "0")}, buy, (fill,))
    assert found == [("position", "BTC-USD"), ("balance", "BTC")]
    assert run.switch.state is KillSwitchState.HALTED

    # A resting order moved 100 USD to hold: the hold is carried over exactly, so it halts too.
    run = await Run(SandboxVenue()).start()
    found = await run.after({"USD": (USD_AFTER_BUY, "100"), "BTC": ("0.0001", "0")}, buy, (fill,))
    assert found == [("balance", "USD")]
    assert run.switch.state is KillSwitchState.HALTED

    # A fill the venue lists that the app never recorded: USD agrees, the fill alone halts.
    run = await Run(SandboxVenue()).start()
    _, stranger = own_order(OrderSide.BUY, "0.0001", BUY_PRICE, "0.03413", second=2)
    found = await run.after(
        {"USD": (USD_AFTER_BUY, "0"), "BTC": ("0.0001", "0")},
        buy,
        (fill,),
        venue_fills=(fill, stranger),
    )
    assert found == [("fill", stranger.fill_id)]
    assert run.switch.state is KillSwitchState.HALTED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("declared", "reported", "clean"),
    [
        pytest.param(FIVE_DECIMALS, "9991.43299", True, id="5 decimals declared and reported"),
        pytest.param({}, "9991.43299", False, id="nothing declared: compared exactly as before"),
        pytest.param({}, "9991.432986472", True, id="nothing declared, reported exactly"),
        pytest.param(
            {"USD": Decimal("0.00000001")}, "9991.43299", False, id="finer unit keeps it exact"
        ),
        pytest.param(
            {"USD": Decimal("0.00000001")}, "9991.43298648", True, id="finer unit, finer report"
        ),
    ],
)
async def test_the_precision_comes_from_what_the_venue_declares(
    declared: dict[str, Decimal], reported: str, clean: bool
) -> None:
    run = await Run(SandboxVenue(declared)).start()
    buy, fill = own_order(OrderSide.BUY, "0.0001", BUY_PRICE, "0.034131528")

    found = await run.after({"USD": (reported, "0"), "BTC": ("0.0001", "0")}, buy, (fill,))

    assert found == ([] if clean else [("balance", "USD")])
    assert run.switch.state is (KillSwitchState.RUNNING if clean else KillSwitchState.HALTED)


@pytest.mark.asyncio
async def test_only_the_gemini_sandbox_declares_a_balance_precision() -> None:
    gemini, coinbase = gemini_venue({}), coinbase_venue({})
    try:
        assert gemini.capabilities.balance_increments == FIVE_DECIMALS
        assert coinbase.capabilities.balance_increments == {}
        assert SimulatedBroker().capabilities.balance_increments == {}
    finally:
        await gemini.close()
        await coinbase.close()


@pytest.mark.parametrize("increment", ["0", "-0.00001"])
def test_a_balance_increment_must_be_positive(increment: str) -> None:
    declared = SimulatedBroker().capabilities.model_dump()
    with pytest.raises(ValidationError, match="balance increment for USD"):
        BrokerCapabilities(**{**declared, "balance_increments": {"USD": Decimal(increment)}})


def baseline(**available: str) -> PortfolioState:
    return PortfolioState(
        balances=tuple(
            Balance(asset=asset, available=Decimal(amount), as_of=utc_now())
            for asset, amount in available.items()
        )
    )


def test_the_ledger_rounds_only_listed_assets_that_a_fill_moved() -> None:
    _, fill = own_order(OrderSide.BUY, "0.0001", BUY_PRICE, "0.034131528")
    start = PortfolioState(
        balances=(
            Balance(asset="BTC", available=Decimal("0.123456789"), as_of=utc_now()),
            Balance(asset="EUR", available=Decimal("100.123456"), as_of=utc_now()),
            Balance(
                asset="USD",
                available=Decimal("10000.00000"),
                hold=Decimal("5.123456789"),
                as_of=utc_now(),
            ),
        )
    )

    projected = apply_fills(
        start, [fill], balance_increments={**FIVE_DECIMALS, "EUR": Decimal("0.01")}
    )

    assert {item.asset: (item.available, item.hold) for item in projected.balances} == {
        "BTC": (Decimal("0.123556789"), Decimal("0")),  # not listed: exact
        "EUR": (Decimal("100.123456"), Decimal("0")),  # listed, but no fill moved it
        "USD": (Decimal("9991.43299"), Decimal("5.123456789")),  # rounded; the hold is not
    }
    # No increments declared: nothing is rounded, as before.
    [btc, _, usd] = apply_fills(start, [fill]).balances
    assert usd.available == Decimal("9991.432986472") and btc.available == Decimal("0.123556789")


def test_the_ledger_still_refuses_a_negative_projection_that_rounding_would_hide() -> None:
    # 8.56701 USD cannot pay 8.567013528: short by 0.000003528, which rounds to zero.
    _, fill = own_order(OrderSide.BUY, "0.0001", BUY_PRICE, "0.034131528")
    with pytest.raises(ValueError, match="negative"):
        apply_fills(baseline(USD="8.56701"), [fill], balance_increments=FIVE_DECIMALS)


def test_the_ledger_refuses_a_projection_that_is_negative_only_as_settled() -> None:
    # Two sells credit 0.000009 each, settled as 0, and a buy debits 0.000015, settled as
    # 0.00001: 0.000003 left at full precision, 0.00001 short as the venue settles it.
    _, sell_one = own_order(OrderSide.SELL, "0.00000001", "900", "0", second=1)
    _, sell_two = own_order(OrderSide.SELL, "0.00000001", "900", "0", second=2)
    _, buy = own_order(OrderSide.BUY, "0.00000001", "1500", "0", second=3)
    with pytest.raises(ValueError, match="negative"):
        apply_fills(
            baseline(USD="0", BTC="1"), [sell_one, sell_two, buy], balance_increments=FIVE_DECIMALS
        )


@pytest.mark.asyncio
async def test_the_service_reconciler_projects_at_the_precision_its_broker_declares(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    engine, session_factory = sqlite_database(tmp_path)
    SqlAlchemyPortfolioStore(session_factory).save_snapshot(
        baseline(USD="10000.00000"), source="broker"
    )
    engine.dispose()
    settings = StartupSettings(
        TradingMode.PAPER,
        CredentialScope.NONE,
        "",
        f"sqlite+pysqlite:///{tmp_path / 'trader.db'}",
        "INFO",
    )
    application = create_app(settings, broker=SandboxVenue())

    stop = start_scheduled_reconciliation(application, interval_seconds=3600)
    assert stop is not None
    try:
        scheduler = application.state.operator_state.scheduled_reconciliation
        buy, fill = own_order(OrderSide.BUY, "0.0001", BUY_PRICE, "0.034131528")
        scheduler.observe(buy, (fill,))
        expected = scheduler.expected_state()
        assert {item.asset: item.available for item in expected.balances} == {
            "BTC": Decimal("0.0001"),
            "USD": Decimal(USD_AFTER_BUY),
        }
    finally:
        await stop()
