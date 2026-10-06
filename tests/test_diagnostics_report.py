"""The read-only diagnostic report behind ``drill.sh diagnose`` (#117).

The behaviour protected here: after a halt, an operator or an agent can see the pending orders and
whether the venue knows them, the stored discrepancy values, and how the projected balance adds up
from the latest fills, without querying the database by hand. The report reads and never writes,
refuses to build under ``live`` or a trade-capable scope, and prints no secret, token, host name, or
address.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from api.diagnostics import (
    PENDING_LOOKUPS,
    REFUSAL,
    SqlAlchemyDiagnostics,
    permitted,
)
from app.main import attach_history, attach_kill_switch_journal, create_app
from brokers.http import ProviderHTTPError
from core.guards import CredentialScope
from core.models import (
    Balance,
    Fill,
    KillSwitchState,
    OrderSide,
    OrderStatus,
    TradingMode,
    utc_now,
)
from db.models import (
    BalanceSnapshotRecord,
    Base,
    DiscrepancyRecord,
    FillRecord,
    OrderRecord,
    PortfolioSnapshotRecord,
)
from portfolio.explain import explain_window
from portfolio.ledger import apply_fills
from portfolio.reconciliation import PortfolioState
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from tests.operator_support import sqlite_settings
from tests.test_order_closure import Venue, seed_order, venue_order

OPERATOR_TOKEN = "operator-secret-diag-3c1d"
ADMIN_TOKEN = "admin-secret-diag-9b2e"
OPERATOR = {"x-operator-token": OPERATOR_TOKEN}
T0 = datetime(2026, 10, 6, 11, 15, tzinfo=UTC)
FILL_AT = T0 + timedelta(minutes=2)
T1 = T0 + timedelta(minutes=5)


@pytest.fixture(autouse=True)
def environment(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("OPERATOR_TOKEN", OPERATOR_TOKEN)
    monkeypatch.setenv("OPERATOR_ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("KILL_SWITCH_FILE", str(tmp_path / "kill-switch.json"))


class RoundingVenue(Venue):
    """A venue that reports USD in half-dollar units, so a projection has rounding to show."""

    @property
    def capabilities(self):
        return super().capabilities.model_copy(
            update={"balance_increments": {"USD": Decimal("0.5")}}
        )


def diagnostics_app(
    tmp_path, venue, mode=TradingMode.PAPER, scope=CredentialScope.VIEW, *, database=True
):
    settings = sqlite_settings(tmp_path, mode, scope)
    engine = create_engine(settings.database_url, future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    application = create_app(settings, broker=venue)
    attach_kill_switch_journal(application)
    if database:
        attach_history(application)
    return application, factory


def client_for(application) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test")


async def report_of(application, query: str = "", headers=OPERATOR) -> httpx.Response:
    async with client_for(application) as client:
        return await client.get(f"/operator/diagnostics{query}", headers=headers)


def snapshot(factory, at: datetime, **available: str) -> None:
    batch = uuid4()
    with factory() as session:
        session.add(PortfolioSnapshotRecord(batch_id=batch, source="broker", recorded_at=at))
        session.add_all(
            BalanceSnapshotRecord(
                snapshot_id=uuid4(),
                batch_id=batch,
                asset=asset,
                available=Decimal(amount),
                hold=Decimal("0"),
                as_of=at,
                source="broker",
            )
            for asset, amount in available.items()
        )
        session.commit()


def discrepancy(factory, kind: str, key: str, local: dict, broker: dict, at: datetime) -> None:
    with factory() as session:
        session.add(
            DiscrepancyRecord(
                discrepancy_id=uuid4(),
                entity_type=kind,
                entity_key=key,
                local_payload=local,
                broker_payload=broker,
                safety_action="halted",
                created_at=at,
            )
        )
        session.commit()


def balance_payload(asset: str, available: str) -> dict:
    return Balance(asset=asset, available=Decimal(available), as_of=T1).model_dump(mode="json")


def seed_fill(
    factory, *, broker_fill_id="tid-9001", quantity="0.25", price="64000.5", fee="0.0625"
):
    """A filled BTC-USD buy and its fill; every number is exact in binary, which SQLite needs."""

    order, _, _ = seed_order(factory, status=OrderStatus.FILLED)
    with factory() as session:
        session.add(
            FillRecord(
                fill_id=uuid4(),
                order_id=order.order_id,
                broker_fill_id=broker_fill_id,
                quantity=Decimal(quantity),
                price=Decimal(price),
                fee=Decimal(fee),
                occurred_at=FILL_AT,
            )
        )
        session.commit()
    return order


def row_counts(factory) -> dict[str, int]:
    with factory() as session:
        return {
            table.name: session.scalar(select(func.count()).select_from(table))
            for table in Base.metadata.sorted_tables
        }


# The projected balance, itemized


def a_fill(**changes) -> Fill:
    values = {
        "fill_id": "tid-1",
        "order_id": uuid4(),
        "symbol": "BTC-USD",
        "side": OrderSide.BUY,
        "quantity": Decimal("0.0001"),
        "price": Decimal("64321.05"),
        "fee": Decimal("0.01286421"),
        "fee_asset": "USD",
        "occurred_at": FILL_AT,
    }
    return Fill(**{**values, **changes})


def books(**available: str) -> PortfolioState:
    return PortfolioState(
        balances=tuple(
            Balance(asset=asset, available=Decimal(amount), as_of=T0)
            for asset, amount in available.items()
        )
    )


def test_a_two_millionths_gap_is_itemized_as_notional_fee_and_rounding() -> None:
    # The shape of the 2026-10-05 and 2026-10-06 divergences: USD to five decimals, off by 0.000002.
    explained = explain_window(
        books(USD="1000"),
        books(USD="993.555032", BTC="0.0001"),
        [a_fill()],
        balance_increments={"USD": Decimal("0.00001")},
    )

    usd = next(item for item in explained if item.asset == "USD")
    assert [(item.label, item.amount) for item in usd.items] == [
        ("fill tid-1 quantity × price", Decimal("-6.432105")),
        ("fill tid-1 fee", Decimal("-0.01286421")),
    ]
    assert usd.before == Decimal("1000")
    assert usd.exact == Decimal("993.55503079")
    assert usd.rounding == Decimal("993.55503") - Decimal("993.55503079")
    assert usd.projected == Decimal("993.55503")
    assert usd.broker == Decimal("993.555032")
    assert usd.difference == Decimal("0.000002")


def test_the_itemized_pieces_always_add_up_to_what_the_ledger_projects() -> None:
    increments = {"USD": Decimal("0.00001")}
    before = books(USD="5000", BTC="1")
    fills = [
        a_fill(fill_id="a"),
        a_fill(
            fill_id="b", side=OrderSide.SELL, quantity=Decimal("0.0003"), price=Decimal("64000.07")
        ),
    ]
    projected = {
        item.asset: item.available
        for item in apply_fills(before, fills, balance_increments=increments).balances
    }

    explained = explain_window(before, before, fills, balance_increments=increments)

    assert {item.asset for item in explained} == {"BTC", "USD"}
    for item in explained:
        # Starting balance + every item + rounding is the projection, and nothing is left over.
        assert item.before + sum(entry.amount for entry in item.items) == item.exact
        assert item.exact + item.rounding == item.projected == projected[item.asset]


def test_a_projection_that_goes_negative_cannot_be_itemized() -> None:
    with pytest.raises(ValueError, match="negative"):
        explain_window(books(USD="1"), books(USD="1"), [a_fill()])


# The endpoint


@pytest.mark.asyncio
async def test_the_report_shows_orders_discrepancies_and_the_itemized_balance(tmp_path) -> None:
    venue = RoundingVenue()
    application, factory = diagnostics_app(tmp_path, venue)
    application.state.kill_switch.trip("test: the loop halted")
    pending, _, _ = seed_order(factory)
    with factory() as session:  # five minutes old
        session.execute(
            update(OrderRecord)
            .where(OrderRecord.order_id == pending.order_id)
            .values(created_at=utc_now() - timedelta(minutes=5))
        )
        session.commit()
    filled = seed_fill(factory)
    snapshot(factory, T0, USD="100000")
    snapshot(factory, T1, USD="84000.5", BTC="0.25")
    discrepancy(
        factory,
        "balance",
        "USD",
        balance_payload("USD", "84000"),
        balance_payload("USD", "84000.5"),
        T1,
    )
    discrepancy(
        factory,
        "order",
        str(pending.order_id),
        {"value": "('pending_submit', '0')"},
        {},
        T1 - timedelta(hours=1),
    )

    response = await report_of(application)

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["mode"], body["credential_scope"], body["kill_switch"]) == (
        "paper",
        "view",
        "halted",
    )
    [order] = body["pending_orders"]["orders"]
    assert order | {"age_seconds": 0} == {
        "ref": str(pending.order_id)[:8],
        "status": "pending_submit",
        "symbol": "BTC-USD",
        "side": "buy",
        "quantity": "0.0001",
        "age_seconds": 0,
        "broker": "not found",
    }
    assert 295 <= order["age_seconds"] <= 330
    balance, order_row = body["discrepancies"]["rows"]
    assert balance["fields"] == [
        {"field": "available", "local": "84000", "broker": "84000.5", "delta": "+0.5"}
    ]
    assert (order_row["kind"], order_row["key"]) == ("order", str(pending.order_id)[:8])
    assert order_row["fields"] == [
        {"field": "presence", "local": "('pending_submit', '0')", "broker": "absent", "delta": None}
    ]
    [fill] = body["fills"]["rows"]
    assert fill | {"at": ""} == {
        "at": "",
        "ref": "tid-9001",
        "order": str(filled.order_id)[:8],
        "symbol": "BTC-USD",
        "side": "buy",
        "quantity": "0.25",
        "price": "64000.5",
        "notional": "16000.125",
        "fee": "0.0625",
    }
    [check] = body["fills"]["checks"]
    assert (check["status"], check["fills"]) == ("itemized", ["tid-9001"])
    btc, usd = check["assets"]
    assert usd == {
        "asset": "USD",
        "before": "100000",
        "items": [
            {"label": "fill tid-9001 quantity × price", "amount": "-16000.125"},
            {"label": "fill tid-9001 fee", "amount": "-0.0625"},
        ],
        "exact": "83999.8125",
        "rounding": "+0.1875",
        "projected": "84000",
        "broker": "84000.5",
        "difference": "+0.5",
    }
    assert (btc["asset"], btc["projected"], btc["broker"], btc["difference"]) == (
        "BTC",
        "0.25",
        "0.25",
        "0",
    )
    lines = body["report"]
    assert "kill_switch=halted" in lines and "pending_orders=1" in lines
    assert "    available local=84000 broker=84000.5 delta(broker-local)=+0.5" in lines
    assert "      rounding to the venue's unit +0.1875" in lines
    assert "      difference (broker - projected) +0.5" in lines
    assert any(
        line.startswith(f"  order {str(pending.order_id)[:8]} pending_submit buy") for line in lines
    )


@pytest.mark.asyncio
async def test_the_report_performs_no_write_and_asks_the_venue_only_to_look(tmp_path) -> None:
    venue = RoundingVenue()
    application, factory = diagnostics_app(tmp_path, venue)
    application.state.kill_switch.trip("test: the loop halted")
    seed_order(factory)
    seed_fill(factory)
    snapshot(factory, T0, USD="100000")
    snapshot(factory, T1, USD="84000.5", BTC="0.25")
    before = row_counts(factory)
    switch_before = application.state.kill_switch.state

    for query in ("", "?limit=1", "?limit=20"):
        assert (await report_of(application, query)).status_code == 200

    assert row_counts(factory) == before  # not one row inserted, changed, or removed
    assert application.state.kill_switch.state is switch_before is KillSwitchState.HALTED
    assert venue.writes == []  # nothing submitted, cancelled, or edited
    assert set(venue.lookups) == {"get_order"}  # the pending order was looked up, nothing else


@pytest.mark.asyncio
async def test_the_report_names_no_secret_token_host_or_address(tmp_path) -> None:
    error = ProviderHTTPError(
        500,
        "boom",
        payload={"reason": "SystemError", "message": "internal token=LEAKED-123 host=10.1.2.3"},
        path="/v1/order/status",
    )
    application, factory = diagnostics_app(tmp_path, RoundingVenue(order_error=error))
    seed_order(factory)
    seed_fill(factory)
    snapshot(factory, T0, USD="100000")
    snapshot(factory, T1, USD="84000.5", BTC="0.25")

    response = await report_of(application)

    text = response.text
    assert (
        "lookup failed: ProviderHTTPError HTTP 500 path=/v1/order/status reason=SystemError" in text
    )
    for forbidden in (OPERATOR_TOKEN, ADMIN_TOKEN, "LEAKED", "internal", "10.1.2.3"):
        assert forbidden not in text
    assert not re.search(r"https?://|\b\d{1,3}(\.\d{1,3}){3}\b", text)
    # No full order or client identifier: orders and fills show an eight-character reference.
    assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}", text)


@pytest.mark.asyncio
async def test_the_pending_order_lookups_are_bounded(tmp_path) -> None:
    venue = RoundingVenue()
    application, factory = diagnostics_app(tmp_path, venue)
    for _ in range(PENDING_LOOKUPS + 1):
        seed_order(factory)

    orders = (await report_of(application)).json()["pending_orders"]["orders"]

    assert [row["broker"] for row in orders] == ["not found"] * PENDING_LOOKUPS + [
        "not asked (lookup limit reached)"
    ]
    assert venue.lookups == ["get_order"] * PENDING_LOOKUPS


@pytest.mark.asyncio
async def test_an_order_the_venue_has_is_reported_with_its_status(tmp_path) -> None:
    application, factory = diagnostics_app(tmp_path, RoundingVenue())
    _, _, request = seed_order(factory)
    application.state.operator_state.broker = RoundingVenue(
        order=venue_order(request, OrderStatus.OPEN)
    )

    [row] = (await report_of(application)).json()["pending_orders"]["orders"]

    assert row["broker"] == "found (open)"


@pytest.mark.asyncio
async def test_a_section_that_cannot_be_read_says_so_and_the_rest_still_print(tmp_path) -> None:
    application, _ = diagnostics_app(tmp_path, RoundingVenue())

    def broken_session():
        raise OperationalError("SELECT 1", {}, Exception("database is down"))

    application.state.diagnostics = SqlAlchemyDiagnostics(broken_session)

    body = (await report_of(application)).json()

    assert body["kill_switch"] == "running"
    for section in ("pending_orders", "discrepancies", "fills"):
        assert body[section] == {"readable": False, "reason": body[section]["reason"]}
    assert "pending_orders: unavailable (stored state could not be read)" in body["report"]
    assert "down" not in json.dumps(body)


@pytest.mark.asyncio
async def test_without_a_database_the_report_still_gives_the_kill_switch_and_recovery(
    tmp_path,
) -> None:
    application, _ = diagnostics_app(tmp_path, RoundingVenue(), database=False)

    body = (await report_of(application)).json()

    assert body["recovery"]["status"] == "not_run"
    assert body["pending_orders"] == {
        "readable": False,
        "reason": "no database is configured in this process",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "scope"),
    [
        (TradingMode.LIVE, CredentialScope.TRADE),
        (TradingMode.LIVE, CredentialScope.VIEW),
        (TradingMode.PAPER, CredentialScope.TRADE),
        (TradingMode.BACKTEST, CredentialScope.TRADE),
    ],
)
async def test_the_report_refuses_live_mode_and_a_trade_capable_scope(
    tmp_path, mode, scope
) -> None:
    venue = RoundingVenue()
    application, factory = diagnostics_app(tmp_path, venue, mode, scope)
    seed_order(factory)

    response = await report_of(application)

    assert response.status_code == 403
    assert response.json() == {"detail": REFUSAL}
    assert venue.lookups == [] and venue.writes == []  # the venue was not even asked
    assert not permitted(mode, scope)


@pytest.mark.parametrize("mode", [TradingMode.BACKTEST, TradingMode.REPLAY, TradingMode.PAPER])
@pytest.mark.parametrize("scope", [CredentialScope.NONE, CredentialScope.VIEW])
def test_a_sandbox_or_offline_mode_without_trade_credentials_is_permitted(mode, scope) -> None:
    assert permitted(mode, scope)


@pytest.mark.asyncio
async def test_the_report_needs_an_operator_and_takes_only_a_bounded_limit(tmp_path) -> None:
    application, factory = diagnostics_app(tmp_path, RoundingVenue())
    for index in range(3):
        discrepancy(
            factory,
            "balance",
            "USD",
            balance_payload("USD", "1"),
            balance_payload("USD", "2"),
            T1 + timedelta(minutes=index),
        )

    assert (await report_of(application, headers={})).status_code == 401
    assert (await report_of(application, headers={"x-operator-token": "wrong"})).status_code == 401
    assert (await report_of(application, f"?token={OPERATOR_TOKEN}")).status_code == 400
    for query in ("?limit=0", "?limit=21", "?limit=-1", "?limit=abc", "?limit=1&x=1", "?other=1"):
        refused = await report_of(application, query)
        assert refused.status_code == 422, query
        assert OPERATOR_TOKEN not in refused.text
    one = (await report_of(application, "?limit=1")).json()
    assert len(one["discrepancies"]["rows"]) == 1
    assert len((await report_of(application)).json()["discrepancies"]["rows"]) == 3
    assert (
        await report_of(application, headers={"x-operator-token": ADMIN_TOKEN})
    ).status_code == 200


@pytest.mark.asyncio
async def test_one_odd_stored_row_is_shown_as_stored_and_does_not_hide_the_rest(tmp_path) -> None:
    application, factory = diagnostics_app(tmp_path, RoundingVenue())
    discrepancy(factory, "balance", "USD", {"asset": "USD", "available": "not-a-number"}, {}, T1)
    discrepancy(
        factory,
        "balance",
        "USD",
        balance_payload("USD", "1"),
        balance_payload("USD", "2"),
        T1 - timedelta(minutes=1),
    )

    response = await report_of(application)

    assert response.status_code == 200
    odd, good = response.json()["discrepancies"]["rows"]
    assert odd["fields"][0]["field"] == "presence" and odd["fields"][0]["broker"] == "absent"
    assert good["fields"] == [{"field": "available", "local": "1", "broker": "2", "delta": "+1"}]


@pytest.mark.asyncio
async def test_a_window_whose_projection_went_negative_says_so_instead_of_failing(tmp_path) -> None:
    application, factory = diagnostics_app(tmp_path, RoundingVenue())
    seed_fill(factory)  # spends 16000.125 USD
    snapshot(factory, T0, USD="1")  # but the broker's earlier snapshot held only one dollar
    snapshot(factory, T1, USD="1", BTC="0.25")

    response = await report_of(application)

    assert response.status_code == 200
    [check] = response.json()["fills"]["checks"]
    assert check["status"] == "the projection went negative; it cannot be itemized"
    assert "assets" not in check


@pytest.mark.asyncio
async def test_fills_outside_any_stored_window_are_labelled_not_guessed(tmp_path) -> None:
    application, factory = diagnostics_app(tmp_path, RoundingVenue())
    seed_fill(factory)

    not_yet = (await report_of(application)).json()["fills"]["checks"]
    snapshot(factory, T1, USD="84000.5", BTC="0.25")  # one snapshot after the fill, none before
    no_earlier = (await report_of(application)).json()["fills"]["checks"]

    assert not_yet == [{"fills": ["tid-9001"], "status": "not yet reconciled"}]
    assert no_earlier == [{"fills": ["tid-9001"], "status": "no earlier broker snapshot is stored"}]
