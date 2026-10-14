"""The Watchlist page's on/off control for the watch-only feed (#166)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx
import pytest
from app import watch_feed as watch_feed_module
from app.main import attach_watchlist, start_watch_feed
from app.watch_feed import INTERVAL, INTERVAL_NAME
from core.models import Candle, KillSwitchState
from data.feed_setting import (
    FEED_EVENT,
    FEED_SETTING_KEY,
    FeedSettingError,
    SqlAlchemyFeedSetting,
)
from data.watchlist import SqlAlchemyWatchlist
from db.models import Base, MarketCandleRecord, OperatorSettingRecord, SystemEventRecord
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from tests.operator_support import history_app

OPERATOR_TOKEN = "operator-secret-feed-3a11"
ADMIN_TOKEN = "admin-secret-feed-9d42"
OPERATOR = {"x-operator-token": OPERATOR_TOKEN}
ADMIN = {"x-operator-token": ADMIN_TOKEN}
PATH = "/operator/watchlist"
FEED_PATH = f"{PATH}/feed"
HTML = {"accept": "text/html,application/xhtml+xml"}
ON_BUTTON = "Turn on the watch-only feed"
OFF_BUTTON = "Turn off the watch-only feed"


@pytest.fixture(autouse=True)
def environment(monkeypatch, tmp_path):
    monkeypatch.setenv("OPERATOR_TOKEN", OPERATOR_TOKEN)
    monkeypatch.setenv("OPERATOR_ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("KILL_SWITCH_FILE", str(tmp_path / "kill-switch.json"))
    monkeypatch.setattr(watch_feed_module, "BACKFILL_DAYS", 1)


class Source:
    """A public candle source that never touches the network."""

    def __init__(self) -> None:
        self.calls = 0

    async def get_candles(self, product_id, start, end, *, granularity):
        self.calls += 1
        count = int((end - start) / INTERVAL)
        return tuple(
            Candle(
                symbol=product_id,
                interval=INTERVAL_NAME,
                opened_at=start + INTERVAL * i,
                closed_at=start + INTERVAL * (i + 1),
                open=1,
                high=2,
                low=1,
                close=1,
                volume=1,
                source="test",
                as_of=start + INTERVAL * (i + 1),
                ingested_at=start + INTERVAL * (i + 1),
            )
            for i in range(count)
        )


class Setup:
    def __init__(self, tmp_path) -> None:
        self.application = history_app(tmp_path)
        self.source = Source()
        self.engine = create_engine(
            self.application.state.startup_settings.database_url, future=True
        )
        self.factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.close_watchlist = None
        self.stop_feed = None

    async def start(self, environ=None):
        if self.close_watchlist is None:
            self.close_watchlist = attach_watchlist(self.application)
            SqlAlchemyWatchlist(self.factory).add("ETH-USD", role="operator")
        self.application.state.watch_client = self.source
        self.stop_feed = start_watch_feed(self.application, environ=environ or {})
        return self.application.state.watch_feed_controller

    async def restart(self, environ=None):
        await self.stop_feed()
        return await self.start(environ)

    async def close(self) -> None:
        if self.stop_feed is not None:
            await self.stop_feed()
        if self.close_watchlist is not None:
            await self.close_watchlist()
        self.engine.dispose()

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.application), base_url="http://test"
        )

    def saved(self):
        with self.factory() as session:
            return session.get(OperatorSettingRecord, FEED_SETTING_KEY)

    def events(self) -> list[SystemEventRecord]:
        query = select(SystemEventRecord).where(SystemEventRecord.event_type == FEED_EVENT)
        with self.factory() as session:
            return list(session.scalars(query).all())

    def candles(self) -> int:
        with self.factory() as session:
            return session.scalar(select(func.count()).select_from(MarketCandleRecord)) or 0


@pytest.fixture
async def setup(tmp_path):
    handle = Setup(tmp_path)
    yield handle
    await handle.close()


async def collected(setup: Setup) -> None:
    for _ in range(300):
        if setup.candles():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the feed collected nothing")


# --- the saved choice ----------------------------------------------------------------


def test_the_choice_and_its_event_are_saved_together(tmp_path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'feed.db'}", future=True)
    Base.metadata.create_all(engine)
    store = SqlAlchemyFeedSetting(sessionmaker(bind=engine, expire_on_commit=False))
    assert store.read() is None
    moment = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
    saved = store.save(True, previous=False, role="admin", now=moment)
    choice = store.read()
    assert choice is not None and choice.enabled is True and choice.changed_by == "admin"
    assert saved.changed_at == moment
    with sessionmaker(bind=engine)() as session:
        event = session.scalars(select(SystemEventRecord)).one()
    assert event.event_type == FEED_EVENT
    assert event.payload == {
        "action": "enable",
        "previous": False,
        "enabled": True,
        "role": "admin",
    }
    store.save(False, previous=True, role="admin")
    again = store.read()
    assert again is not None and again.enabled is False
    engine.dispose()


def test_a_choice_that_cannot_be_read_is_an_error_not_an_answer(tmp_path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'empty.db'}", future=True)
    store = SqlAlchemyFeedSetting(sessionmaker(bind=engine, expire_on_commit=False))
    with pytest.raises(FeedSettingError):
        store.read()
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as session, session.begin():
        session.add(
            OperatorSettingRecord(
                key=FEED_SETTING_KEY,
                value="maybe",
                updated_at=datetime.now(UTC),
                updated_by="admin",
            )
        )
    with pytest.raises(FeedSettingError):
        store.read()
    engine.dispose()


# --- startup -------------------------------------------------------------------------


async def test_with_nothing_saved_the_environment_decides(setup) -> None:
    controller = await setup.start({})
    assert controller.running is False
    assert controller.origin.source == "environment"
    controller = await setup.restart({"WATCH_FEED_ENABLED": "1"})
    assert controller.running is True
    assert controller.origin.source == "environment"
    assert setup.application.state.watch_feed is controller.feed


async def test_a_saved_off_beats_the_environment(setup) -> None:
    await setup.start({})
    SqlAlchemyFeedSetting(setup.factory).save(False, previous=True, role="admin")
    controller = await setup.restart({"WATCH_FEED_ENABLED": "1"})
    assert controller.running is False
    assert controller.origin.source == "saved"
    assert setup.application.state.watch_feed is None


async def test_a_saved_on_beats_the_environment(setup) -> None:
    await setup.start({})
    SqlAlchemyFeedSetting(setup.factory).save(True, previous=False, role="admin")
    controller = await setup.restart({"WATCH_FEED_ENABLED": "0"})
    assert controller.running is True
    assert controller.origin.changed_by == "admin"


async def test_an_unreadable_choice_keeps_the_feed_off_even_when_the_environment_is_on(
    setup,
) -> None:
    await setup.start({})
    with setup.factory.begin() as session:
        session.add(
            OperatorSettingRecord(
                key=FEED_SETTING_KEY,
                value="maybe",
                updated_at=datetime.now(UTC),
                updated_by="admin",
            )
        )
    controller = await setup.restart({"WATCH_FEED_ENABLED": "1"})
    assert controller.running is False
    assert controller.origin.source == "unreadable"
    async with setup.client() as client:
        page = await client.get(PATH, headers={**ADMIN, **HTML})
    assert "not a valid value" in page.text and "The feed is off" in page.text
    assert setup.source.calls == 0


# --- the button ----------------------------------------------------------------------


async def test_an_administrator_turns_the_feed_on_and_off_without_a_restart(setup) -> None:
    controller = await setup.start({})
    async with setup.client() as client:
        on = await client.post(FEED_PATH, json={"enabled": True}, headers=ADMIN)
        assert on.status_code == 200 and on.json() == {"status": "turned_on", "enabled": True}
        assert controller.running is True
        await collected(setup)
        state = (await client.get("/operator/state", headers=ADMIN)).json()
        assert state["watch_feed_setting"]["enabled"] is True
        assert state["watch_feed_setting"]["changed_by"] == "admin"

        off = await client.post(FEED_PATH, data={"enabled": "0"}, headers=ADMIN)
        assert off.status_code == 200 and off.json()["status"] == "turned_off"
    assert controller.running is False
    assert setup.application.state.watch_feed is None
    kept, calls = setup.candles(), setup.source.calls
    assert kept > 0
    await asyncio.sleep(0.05)
    assert setup.source.calls == calls and setup.candles() == kept
    events = setup.events()
    assert sorted(e.payload["action"] for e in events) == ["disable", "enable"]
    enable = next(e for e in events if e.payload["action"] == "enable")
    assert enable.payload["previous"] is False and enable.payload["role"] == "admin"


async def test_the_choice_survives_a_restart(setup) -> None:
    await setup.start({})
    async with setup.client() as client:
        await client.post(FEED_PATH, json={"enabled": True}, headers=ADMIN)
    controller = await setup.restart({"WATCH_FEED_ENABLED": "0"})
    assert controller.running is True


async def test_pressing_the_same_button_twice_changes_nothing_more(setup) -> None:
    await setup.start({})
    async with setup.client() as client:
        statuses = []
        for enabled in (True, True, False, False):
            response = await client.post(FEED_PATH, json={"enabled": enabled}, headers=ADMIN)
            statuses.append(response.json()["status"])
    assert statuses == ["turned_on", "unchanged", "turned_off", "unchanged"]
    assert len(setup.events()) == 2


async def test_only_an_administrator_can_use_it(setup) -> None:
    controller = await setup.start({})
    async with setup.client() as client:
        signed_out = await client.post(FEED_PATH, json={"enabled": True})
        operator = await client.post(FEED_PATH, json={"enabled": True}, headers=OPERATOR)
        garbage = await client.post(FEED_PATH, json={"enabled": "maybe"}, headers=ADMIN)
    assert signed_out.status_code == 401
    assert operator.status_code == 403
    assert garbage.status_code == 422
    assert controller.running is False
    assert setup.saved() is None and setup.events() == []


async def test_a_failed_save_leaves_the_feed_as_it_was(setup) -> None:
    controller = await setup.start({})
    with setup.engine.begin() as connection:
        SystemEventRecord.__table__.drop(connection)
    async with setup.client() as client:
        response = await client.post(FEED_PATH, json={"enabled": True}, headers=ADMIN)
    assert response.status_code == 503
    assert controller.running is False
    assert setup.application.state.watch_feed is None
    assert setup.saved() is None
    assert setup.source.calls == 0


# --- the page ------------------------------------------------------------------------


async def test_the_page_offers_the_button_to_an_administrator_only(setup) -> None:
    await setup.start({})
    async with setup.client() as client:
        admin = await client.get(PATH, headers={**ADMIN, **HTML})
        operator = await client.get(PATH, headers={**OPERATOR, **HTML})
        assert ON_BUTTON in admin.text and OFF_BUTTON not in admin.text
        assert "no candles are collected" in admin.text
        assert ON_BUTTON not in operator.text and OFF_BUTTON not in operator.text
        assert "Only an administrator can turn the feed on or off." in operator.text
        data = (await client.get(PATH, headers=OPERATOR)).json()
        assert data["feed_setting"]["enabled"] is False

        await client.post(FEED_PATH, json={"enabled": True}, headers=ADMIN)
        after = await client.get(PATH, headers={**ADMIN, **HTML})
        assert OFF_BUTTON in after.text and ON_BUTTON not in after.text
        assert "Turned on by admin" in after.text


async def test_the_page_form_works_without_javascript(setup) -> None:
    controller = await setup.start({})
    async with setup.client() as client:
        response = await client.post(
            FEED_PATH, data={"enabled": "1"}, headers={**ADMIN, **HTML}, follow_redirects=False
        )
    assert response.status_code == 303 and response.headers["location"] == PATH
    assert controller.running is True


# --- the controller ------------------------------------------------------------------


async def test_starting_and_stopping_twice_does_nothing_more(setup) -> None:
    controller = await setup.start({})
    controller.start()
    feed = controller.feed
    controller.start()
    assert controller.feed is feed
    await controller.stop()
    await controller.stop()
    assert controller.running is False


# --- trading is not touched ----------------------------------------------------------


async def test_toggling_the_feed_changes_nothing_in_trading(setup, monkeypatch) -> None:
    from app.trading import TradingCycle
    from execution.engine import ExecutionEngine
    from risk import engine as risk_engine

    def forbidden(*args, **kwargs):
        raise AssertionError("the feed control reached the trading path")

    async def forbidden_async(*args, **kwargs):
        forbidden()

    monkeypatch.setattr(TradingCycle, "on_market_state", forbidden_async)
    monkeypatch.setattr(risk_engine, "evaluate", forbidden)
    monkeypatch.setattr(ExecutionEngine, "submit", forbidden_async)
    await setup.start({})
    operator = setup.application.state.operator_state
    operator.heartbeat("primary", status="healthy", detail="all good")
    before = operator.to_dict()
    frozen = ("runtime", "strategies", "alerts", "errors", "risk", "reconciliation")
    async with setup.client() as client:
        await client.post(FEED_PATH, json={"enabled": True}, headers=ADMIN)
        await collected(setup)
        await client.post(FEED_PATH, json={"enabled": False}, headers=ADMIN)
    after = operator.to_dict()
    assert {key: after[key] for key in frozen} == {key: before[key] for key in frozen}
    assert setup.application.state.kill_switch.state is KillSwitchState.RUNNING
