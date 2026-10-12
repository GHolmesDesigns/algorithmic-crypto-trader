"""Today's P/L (#154): the dashboard shows an equity change only when it is trustworthy.

The figure is the latest equity snapshot minus the UTC day's first. Every path that cannot
give a real figure reports unavailable with its reason, never zero.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from api.operator import OperatorState
from app.main import (
    PNL_STALE_CAP,
    create_app,
    start_scheduled_reconciliation,
)
from brokers.simulated import SimulatedBroker
from core.guards import CredentialScope, StartupSettings
from core.models import TradingMode
from db.models import Base, EquityHoldingRecord, EquitySnapshotRecord
from portfolio.day_pnl import DayPnl, DayPnlReader, format_amount
from risk.kill_switch import KillSwitch
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from tests.test_operator_dashboard import (
    OPERATOR,
    HealthyBroker,
    card,
    dashboard_app,
    has_pill,
    page,
)

NOW = datetime(2026, 10, 12, 14, 0, tzinfo=UTC)
STALE = timedelta(hours=2)


def database(tmp_path: Path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'trader.db'}", future=True)
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def snapshot(session_factory, at: datetime, equity: str | None, *, partial: bool = False):
    snapshot_id = uuid4()
    with session_factory() as session:
        session.add(
            EquitySnapshotRecord(
                snapshot_id=snapshot_id,
                equity=None if equity is None else Decimal(equity),
                as_of=at,
                source="broker",
                partial=partial,
            )
        )
        session.commit()
    return snapshot_id


def last_known(session_factory, snapshot_id, symbol: str) -> None:
    with session_factory() as session:
        session.add(
            EquityHoldingRecord(
                holding_id=uuid4(),
                snapshot_id=snapshot_id,
                symbol=symbol,
                basis="last_known",
                quantity=Decimal("1"),
                price=Decimal("10"),
                price_observed_at=NOW - timedelta(hours=3),
                price_age_seconds=10800,
            )
        )
        session.commit()


def reader(session_factory, *, stale_after: timedelta = STALE) -> DayPnlReader:
    return DayPnlReader(session_factory, stale_after=stale_after, clock=lambda: NOW)


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, 12, hour, minute, tzinfo=UTC)


# The reader


def test_two_good_snapshots_today_give_their_exact_difference(tmp_path) -> None:
    _, factory = database(tmp_path)
    # SQLite keeps Numeric as a float, so the stored values are exact binary fractions.
    snapshot(factory, at(0, 5), "10000.25")
    snapshot(factory, at(9), "10050")
    snapshot(factory, at(13, 30), "10012.75")

    result = reader(factory).read()

    assert result.status == "current"
    assert result.amount == Decimal("12.5")
    assert result.as_of == at(13, 30)
    assert result.detail.startswith("+12.50 USD since the first equity snapshot today")
    assert "2026-10-12 00:05:00 UTC" in result.detail
    assert "as of 2026-10-12 13:30:00 UTC" in result.detail
    assert "includes last-known prices" not in result.detail
    assert "deposit or withdrawal would move it" in result.detail


def test_a_loss_is_shown_signed_and_a_real_zero_is_a_figure(tmp_path) -> None:
    _, factory = database(tmp_path)
    snapshot(factory, at(0, 5), "10000")
    snapshot(factory, at(13), "9987.5")
    assert reader(factory).read().detail.startswith("-12.50 USD since")

    (tmp_path / "flat").mkdir()
    _, flat = database(tmp_path / "flat")
    snapshot(flat, at(0, 5), "10000")
    snapshot(flat, at(13), "10000.000000000000000000")
    result = reader(flat).read()
    assert result.status == "current"
    assert result.amount == Decimal("0")
    assert result.detail.startswith("0.00 USD since")


def test_no_snapshot_today_is_unavailable_and_yesterday_never_counts(tmp_path) -> None:
    _, factory = database(tmp_path)
    empty = reader(factory).read()
    assert empty.status == "unavailable" and empty.amount is None
    assert "no equity snapshot has been recorded yet today" in empty.detail

    snapshot(factory, at(0, 5) - timedelta(days=1), "9000")
    snapshot(factory, at(23, 55) - timedelta(days=1), "9100")
    still_empty = reader(factory).read()
    assert still_empty.status == "unavailable"
    assert "no equity snapshot has been recorded yet today" in still_empty.detail


def test_one_snapshot_today_is_unavailable_even_with_yesterdays_rows(tmp_path) -> None:
    _, factory = database(tmp_path)
    snapshot(factory, at(23, 55) - timedelta(days=1), "9000")
    snapshot(factory, at(13, 30), "10000")
    result = reader(factory).read()
    assert result.status == "unavailable" and result.amount is None
    assert "only one equity snapshot" in result.detail


def test_the_day_starts_again_at_midnight_utc(tmp_path) -> None:
    _, factory = database(tmp_path)
    snapshot(factory, at(23, 55) - timedelta(days=1), "9000")
    snapshot(factory, at(0, 5), "10000")
    snapshot(factory, at(13, 30), "10010")
    result = reader(factory).read()
    assert result.amount == Decimal("10")


@pytest.mark.parametrize("which", ["first", "latest"])
@pytest.mark.parametrize("null_equity", [True, False])
def test_a_partial_snapshot_gives_unavailable_never_zero(tmp_path, which, null_equity) -> None:
    _, factory = database(tmp_path)
    first_partial, latest_partial = which == "first", which == "latest"
    # A partial snapshot is flagged and carries no value; the flag alone must also be enough.
    snapshot(
        factory,
        at(0, 5),
        None if first_partial and null_equity else "10000",
        partial=first_partial,
    )
    snapshot(
        factory,
        at(13, 30),
        None if latest_partial and null_equity else "10010",
        partial=latest_partial,
    )
    result = reader(factory).read()
    assert result.status == "unavailable" and result.amount is None
    assert f"{'first' if first_partial else 'latest'} equity snapshot" in result.detail
    assert "is partial" in result.detail and "not counted as zero" in result.detail


def test_a_stale_latest_snapshot_is_not_shown_as_current(tmp_path) -> None:
    _, factory = database(tmp_path)
    snapshot(factory, at(0, 5), "10000")
    snapshot(factory, at(11, 59), "10010")  # 2 h 1 min before NOW
    stale = reader(factory).read()
    assert stale.status == "unavailable" and stale.amount is None
    assert "2026-10-12 11:59:00 UTC" in stale.detail and "older than 2 hours" in stale.detail

    snapshot(factory, at(12), "10020")  # exactly the limit is still current
    assert reader(factory).read().amount == Decimal("20")


def test_last_known_prices_are_named_in_the_figure(tmp_path) -> None:
    _, factory = database(tmp_path)
    first = snapshot(factory, at(0, 5), "10000")
    latest = snapshot(factory, at(13, 30), "10010")
    last_known(factory, first, "BCH-USD")
    last_known(factory, latest, "BCH-USD")
    last_known(factory, latest, "ADA-USD")
    result = reader(factory).read()
    assert result.status == "current"
    assert "includes last-known prices for ADA-USD, BCH-USD" in result.detail


def test_an_unreadable_history_is_unavailable(tmp_path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'empty.db'}", future=True)
    result = reader(sessionmaker(bind=engine)).read()  # no equity_curve table
    assert result.status == "unavailable" and result.amount is None
    assert result.detail == "the saved equity history could not be read"


def test_reading_writes_nothing(tmp_path) -> None:
    _, factory = database(tmp_path)
    snapshot(factory, at(0, 5), "10000")
    snapshot(factory, at(13), "10010")
    reader(factory).read()
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(EquitySnapshotRecord)) == 2


def test_the_stale_limit_must_be_positive(tmp_path) -> None:
    _, factory = database(tmp_path)
    with pytest.raises(ValueError, match="positive"):
        DayPnlReader(factory, stale_after=timedelta(0))


@pytest.mark.parametrize(
    ("value", "shown"),
    [
        ("12.5", "+12.50"),
        ("-12.6", "-12.60"),
        ("0", "0.00"),
        ("0E-18", "0.00"),
        ("100", "+100.00"),
        ("1E+2", "+100.00"),
        ("0.000000000000000001", "+0.000000000000000001"),
        ("-0.125", "-0.125"),
    ],
)
def test_amounts_show_cents_and_every_significant_digit(value, shown) -> None:
    assert format_amount(Decimal(value)) == shown


# The operator state


def operator_state(broker=None) -> OperatorState:
    settings = StartupSettings(
        TradingMode.PAPER, CredentialScope.VIEW, "", "postgresql://unused", "INFO"
    )
    return OperatorState(settings=settings, kill_switch=KillSwitch(), broker=broker)


class BrokenReader:
    def read(self) -> DayPnl:
        raise RuntimeError("the reader failed")


@pytest.mark.asyncio
async def test_refresh_sets_the_pnl_from_the_reader(tmp_path) -> None:
    _, factory = database(tmp_path)
    snapshot(factory, at(0, 5), "10000")
    snapshot(factory, at(13, 30), "10012.5")
    state = operator_state(HealthyBroker())
    state.day_pnl = reader(factory)

    refreshed = await state.refresh()

    assert refreshed.pnl_status == "current"
    assert refreshed.pnl_detail.startswith("+12.50 USD")
    assert refreshed.pnl_as_of == at(13, 30)


@pytest.mark.asyncio
async def test_a_failed_pnl_read_never_blocks_the_portfolio_refresh() -> None:
    state = operator_state(HealthyBroker())
    state.day_pnl = BrokenReader()

    refreshed = await state.refresh()

    assert refreshed.portfolio_status == "current"
    assert len(refreshed.balances) == 2 and len(refreshed.positions) == 2
    assert refreshed.pnl_status == "unavailable"
    assert refreshed.pnl_detail == "the saved equity history could not be read"
    assert refreshed.pnl_as_of is None


@pytest.mark.asyncio
async def test_a_stale_figure_is_replaced_by_unavailable_on_the_next_refresh(tmp_path) -> None:
    _, factory = database(tmp_path)
    snapshot(factory, at(0, 5), "10000")
    snapshot(factory, at(13, 30), "10012.5")
    state = operator_state(HealthyBroker())
    clock = {"now": NOW}
    state.day_pnl = DayPnlReader(factory, stale_after=STALE, clock=lambda: clock["now"])
    assert (await state.refresh()).pnl_status == "current"

    clock["now"] = NOW + timedelta(hours=3)
    later = await state.refresh()

    assert later.pnl_status == "unavailable" and later.pnl_as_of is None
    assert "older than 2 hours" in later.pnl_detail


@pytest.mark.asyncio
async def test_without_a_reader_the_pnl_stays_unavailable() -> None:
    refreshed = await operator_state(HealthyBroker()).refresh()
    assert refreshed.pnl_status == "unavailable"
    assert refreshed.pnl_detail == "valuation data is unavailable"


@pytest.mark.asyncio
async def test_a_broker_failure_still_refreshes_the_pnl(tmp_path) -> None:
    class Down:
        async def get_balances(self):
            raise RuntimeError("down")

    _, factory = database(tmp_path)
    snapshot(factory, at(0, 5), "10000")
    snapshot(factory, at(13, 30), "10010")
    state = operator_state(Down())
    state.day_pnl = reader(factory)

    refreshed = await state.refresh()

    assert refreshed.portfolio_status == "unavailable"
    assert refreshed.pnl_status == "current"


# The dashboard


def app_with_pnl(monkeypatch, tmp_path, *, rows=()):
    _, factory = database(tmp_path)
    for when, equity, partial in rows:
        snapshot(factory, when, equity, partial=partial)
    application = dashboard_app(monkeypatch, broker=HealthyBroker())
    application.state.operator_state.day_pnl = reader(factory)
    return application, factory


@pytest.mark.asyncio
async def test_the_dashboard_shows_the_figure_with_its_snapshot_time(monkeypatch, tmp_path) -> None:
    application, factory = app_with_pnl(
        monkeypatch, tmp_path, rows=[(at(0, 5), "10000", False), (at(13, 30), "10012.5", False)]
    )
    html = await page(application)
    pnl = card(html, "P/L")
    assert has_pill(pnl, "current", "neutral")
    assert "+12.50 USD" in pnl
    assert "Unavailable is not zero." not in pnl
    assert "2026-10-12 13:30:00 UTC" in pnl
    paragraph = re.search(r'<p class="pnl">.*?</p>', html, re.S)
    assert paragraph is not None
    assert has_pill(paragraph.group(0), "current", "neutral")
    assert "+12.50 USD" in paragraph.group(0)
    assert "No figure is shown until" not in paragraph.group(0)
    with factory() as session:  # rendering wrote nothing
        assert session.scalar(select(func.count()).select_from(EquitySnapshotRecord)) == 2


@pytest.mark.asyncio
async def test_the_dashboard_keeps_unavailable_with_its_reason(monkeypatch, tmp_path) -> None:
    application, _ = app_with_pnl(monkeypatch, tmp_path, rows=[(at(13, 30), "10000", False)])
    html = await page(application)
    pnl = card(html, "P/L")
    assert has_pill(pnl, "unavailable", "unknown")
    assert "only one equity snapshot" in pnl
    assert "Unavailable is not zero." in pnl
    assert "No figure" in pnl
    assert not re.search(r"\d", re.sub(r"<[^>]+>", "", pnl))


@pytest.mark.asyncio
async def test_the_status_payload_carries_the_snapshot_time(monkeypatch, tmp_path) -> None:
    import httpx

    application, _ = app_with_pnl(
        monkeypatch, tmp_path, rows=[(at(0, 5), "10000", False), (at(13, 30), "10010", False)]
    )
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/operator/state", headers=OPERATOR)
    assert response.status_code == 200, response.text
    assert response.json()["portfolio"]["pnl"] == {
        "status": "current",
        "detail": response.json()["portfolio"]["pnl"]["detail"],
        "as_of": at(13, 30).isoformat(),
    }


# Wiring


@pytest.mark.asyncio
async def test_the_service_gives_the_operator_state_a_reader_with_a_bounded_stale_limit(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.delenv("EQUITY_SAMPLE_INTERVAL_SECONDS", raising=False)
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
        wired = application.state.operator_state.day_pnl
        assert isinstance(wired, DayPnlReader)
        assert wired.stale_after == timedelta(hours=2) <= PNL_STALE_CAP
    finally:
        await stop()
