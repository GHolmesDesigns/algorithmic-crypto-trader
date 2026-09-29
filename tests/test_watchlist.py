"""Watchlist and watch-only feed (#87): saved coins to chart, never to trade."""

from __future__ import annotations

import ast
import asyncio
import importlib.util
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from api.alerts import AlertRouter
from app import watch_feed as watch_feed_module
from app.main import attach_watchlist, create_app, start_watch_feed
from app.paper_runtime import PaperRuntimeConfig, parse_paper_symbols
from app.watch_feed import (
    FIRST_BACKOFF,
    INTERVAL,
    INTERVAL_NAME,
    WatchFeedConfig,
    WatchOnlyFeed,
    build_watch_client,
)
from core.guards import StartupGuardError
from core.models import Candle, KillSwitchState
from core.resilience import CircuitBreaker, TokenBucketRateLimiter
from data.coinbase import CoinbaseProduct, CoinbaseProductNotFound, CoinbaseRESTClient
from data.storage import SqlAlchemyCandleStore
from data.watchlist import (
    WATCHLIST_EVENT,
    WATCHLIST_LIMIT,
    SqlAlchemyWatchlist,
    WatchlistRefused,
    check_product,
    normalize_symbol,
)
from db.models import Base, MarketCandleRecord, SystemEventRecord, WatchlistRecord
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from tests.operator_support import history_app

OPERATOR_TOKEN = "operator-secret-watch-71c2"
ADMIN_TOKEN = "admin-secret-watch-5e90"
OPERATOR = {"x-operator-token": OPERATOR_TOKEN}
ADMIN = {"x-operator-token": ADMIN_TOKEN}
BROWSER = {**OPERATOR, "accept": "text/html,application/xhtml+xml"}
PATH = "/operator/watchlist"
NOW = datetime(2026, 9, 27, 12, 7, 30, tzinfo=UTC)
BUCKET = datetime(2026, 9, 27, 12, 5, tzinfo=UTC)
COINS = ["ETH-USD", "SOL-USD", "ADA-USD", "XRP-USD", "DOGE-USD", "LTC-USD", "LINK-USD", "AVAX-USD"]


@pytest.fixture(autouse=True)
def environment(monkeypatch, tmp_path):
    monkeypatch.setenv("OPERATOR_TOKEN", OPERATOR_TOKEN)
    monkeypatch.setenv("OPERATOR_ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("KILL_SWITCH_FILE", str(tmp_path / "kill-switch.json"))


@pytest.fixture(autouse=True)
def short_backfill(monkeypatch):
    """One day of backfill keeps the SQLite-backed tests quick; one test restores 30."""

    monkeypatch.setattr(watch_feed_module, "BACKFILL_DAYS", 1)


@pytest.fixture
def database(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'watch.db'}", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    engine.dispose()


def make_candle(symbol: str, opened_at: datetime) -> Candle:
    return Candle(
        symbol=symbol,
        interval=INTERVAL_NAME,
        opened_at=opened_at,
        closed_at=opened_at + INTERVAL,
        open=Decimal("10"),
        high=Decimal("11"),
        low=Decimal("9"),
        close=Decimal("10.5"),
        volume=Decimal("3"),
        source="test",
        as_of=opened_at + INTERVAL,
        ingested_at=opened_at + INTERVAL,
    )


class FakeSource:
    """A public candle source that never touches the network."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, datetime, datetime]] = []
        self.failures: dict[str, Exception] = {}
        self.dense = True

    async def get_candles(self, product_id, start, end, *, granularity):
        assert granularity == INTERVAL_NAME
        self.calls.append((product_id, start, end))
        failure = self.failures.get(product_id)
        if failure is not None:
            raise failure
        if not self.dense:
            return ()
        count = int((end - start) / INTERVAL)
        return tuple(make_candle(product_id, start + INTERVAL * i) for i in range(count))

    def symbols_requested(self) -> set[str]:
        return {symbol for symbol, _, _ in self.calls}


class Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://example.test/candles")
    return httpx.HTTPStatusError(
        "boom", request=request, response=httpx.Response(code, request=request)
    )


def build_feed(database, *, trading=("BTC-USD",), retention_days=30, clock=None, source=None):
    watchlist = SqlAlchemyWatchlist(database)
    store = SqlAlchemyCandleStore(database)
    source = source or FakeSource()
    clock = clock or Clock()
    feed = WatchOnlyFeed(
        watchlist=watchlist,
        trading_symbols=tuple(trading),
        store=store,
        source=source,
        config=WatchFeedConfig(enabled=True, retention=timedelta(days=retention_days)),
        clock=clock,
    )
    return feed, watchlist, store, source, clock


def events(database) -> list[SystemEventRecord]:
    with database() as session:
        rows = session.scalars(
            select(SystemEventRecord)
            .where(SystemEventRecord.event_type == WATCHLIST_EVENT)
            .order_by(SystemEventRecord.created_at)
        )
        return list(rows)


# --- the saved watchlist -------------------------------------------------------------


def test_add_lists_in_order_and_writes_one_event_per_change(database) -> None:
    watchlist = SqlAlchemyWatchlist(database)
    watchlist.add("eth-usd", role="operator", now=NOW)
    watchlist.add("SOL-USD", role="admin", now=NOW + timedelta(seconds=1))
    assert watchlist.symbols() == ("ETH-USD", "SOL-USD")
    entries = watchlist.entries()
    assert [(e.position, e.added_by) for e in entries] == [(0, "operator"), (1, "admin")]
    rows = events(database)
    assert [row.payload["action"] for row in rows] == ["add", "add"]
    assert rows[1].payload == {
        "action": "add",
        "symbol": "SOL-USD",
        "role": "admin",
        "order": ["ETH-USD", "SOL-USD"],
    }


def test_a_tenth_symbol_and_a_duplicate_are_refused_without_writing(database) -> None:
    watchlist = SqlAlchemyWatchlist(database)
    for coin in COINS:
        watchlist.add(coin, role="operator")
    watchlist.add("ATOM-USD", role="operator")
    assert len(watchlist.symbols()) == WATCHLIST_LIMIT == 9
    before = len(events(database))
    with pytest.raises(WatchlistRefused, match="at most 9"):
        watchlist.add("DOT-USD", role="operator")
    with pytest.raises(WatchlistRefused, match="already"):
        watchlist.add("ETH-USD", role="operator")
    assert len(watchlist.symbols()) == 9
    assert len(events(database)) == before


@pytest.mark.parametrize(
    "raw", ["", "ETH", "ETH-EUR", "eth usd", "ETH-USD;DROP", "-USD", "ETH--USD"]
)
def test_a_malformed_symbol_is_refused(raw) -> None:
    with pytest.raises(WatchlistRefused):
        normalize_symbol(raw)


def test_remove_closes_the_gap_and_reorder_keeps_who_and_when(database) -> None:
    watchlist = SqlAlchemyWatchlist(database)
    for index, coin in enumerate(COINS[:4]):
        watchlist.add(
            coin, role="admin" if index == 0 else "operator", now=NOW + timedelta(minutes=index)
        )
    watchlist.remove("SOL-USD", role="operator")
    assert [(e.symbol, e.position) for e in watchlist.entries()] == [
        ("ETH-USD", 0),
        ("ADA-USD", 1),
        ("XRP-USD", 2),
    ]
    assert watchlist.reorder(["XRP-USD", "ETH-USD", "ADA-USD"], role="operator") == (
        "XRP-USD",
        "ETH-USD",
        "ADA-USD",
    )
    eth = next(e for e in watchlist.entries() if e.symbol == "ETH-USD")
    assert (eth.position, eth.added_by, eth.added_at.replace(tzinfo=UTC)) == (1, "admin", NOW)
    assert [row.payload["action"] for row in events(database)][-2:] == ["remove", "reorder"]
    with pytest.raises(WatchlistRefused, match="not on the watchlist"):
        watchlist.remove("SOL-USD", role="operator")


def test_reorder_must_name_every_coin_once(database) -> None:
    watchlist = SqlAlchemyWatchlist(database)
    watchlist.add("ETH-USD", role="operator")
    watchlist.add("SOL-USD", role="operator")
    for wanted in (["ETH-USD"], ["ETH-USD", "ETH-USD"], ["ETH-USD", "ADA-USD"], []):
        with pytest.raises(WatchlistRefused):
            watchlist.reorder(wanted, role="operator")
    assert watchlist.symbols() == ("ETH-USD", "SOL-USD")


def test_the_watchlist_survives_a_restart(tmp_path) -> None:
    url = f"sqlite+pysqlite:///{tmp_path / 'restart.db'}"
    engine = create_engine(url, future=True)
    Base.metadata.create_all(engine)
    SqlAlchemyWatchlist(sessionmaker(bind=engine, expire_on_commit=False)).add(
        "ETH-USD", role="operator"
    )
    engine.dispose()
    reopened = create_engine(url, future=True)
    assert SqlAlchemyWatchlist(sessionmaker(bind=reopened)).symbols() == ("ETH-USD",)
    reopened.dispose()


def test_the_table_itself_caps_positions_at_nine(database) -> None:
    with database() as session:
        for index in range(9):
            session.add(
                WatchlistRecord(symbol=f"C{index}-USD", position=index, added_at=NOW, added_by="x")
            )
        session.commit()
        session.add(WatchlistRecord(symbol="TEN-USD", position=9, added_at=NOW, added_by="x"))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
        session.add(WatchlistRecord(symbol="DUP-USD", position=0, added_at=NOW, added_by="x"))
        with pytest.raises(IntegrityError):
            session.commit()


# --- the public product lookup -------------------------------------------------------


class StubLookup:
    def __init__(self, result=None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.asked: list[str] = []

    async def get_product(self, product_id):
        self.asked.append(product_id)
        if self.error is not None:
            raise self.error
        return self.result


def product(**overrides) -> CoinbaseProduct:
    values = {
        "product_id": "ETH-USD",
        "status": "online",
        "trading_disabled": False,
        "is_disabled": False,
        "product_type": "SPOT",
    }
    return CoinbaseProduct(**{**values, **overrides})


async def test_check_product_accepts_a_trading_spot_product() -> None:
    await check_product(StubLookup(product()), "ETH-USD")


@pytest.mark.parametrize(
    ("lookup", "message"),
    [
        (StubLookup(error=CoinbaseProductNotFound("X")), "no product named"),
        (StubLookup(product(status="offline")), "delisted or trading is disabled"),
        (StubLookup(product(trading_disabled=True)), "delisted or trading is disabled"),
        (StubLookup(product(is_disabled=True)), "delisted or trading is disabled"),
        (StubLookup(product(product_type="FUTURE")), "delisted or trading is disabled"),
        (StubLookup(error=status_error(429)), "rate limiting"),
        (StubLookup(error=status_error(500)), "could not confirm"),
        (StubLookup(error=httpx.ConnectTimeout("slow")), "could not confirm"),
    ],
)
async def test_check_product_refuses_with_the_reason(lookup, message) -> None:
    with pytest.raises(WatchlistRefused, match=message):
        await check_product(lookup, "ETH-USD")


def coinbase_client(handler) -> CoinbaseRESTClient:
    return CoinbaseRESTClient(
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        rate_limiter=TokenBucketRateLimiter(100, 100),
        circuit_breaker=CircuitBreaker(failure_threshold=2),
    )


async def test_the_rest_client_reads_a_public_product_without_credentials() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "product_id": "ETH-USD",
                "status": "online",
                "trading_disabled": False,
                "is_disabled": False,
                "product_type": "SPOT",
            },
        )

    result = await coinbase_client(handler).get_product("ETH-USD")
    assert result.tradable
    assert seen[0].url.path.endswith("/market/products/ETH-USD")
    assert "authorization" not in seen[0].headers


async def test_the_rest_client_maps_404_to_not_found_and_keeps_the_breaker_closed() -> None:
    client = coinbase_client(lambda request: httpx.Response(404, json={}))
    for _ in range(3):
        with pytest.raises(CoinbaseProductNotFound):
            await client.get_product("NOPE-USD")
    assert client._circuit_breaker.allow_request()


async def test_the_rest_client_counts_server_errors_against_the_breaker() -> None:
    client = coinbase_client(lambda request: httpx.Response(500, json={}))
    for _ in range(2):
        with pytest.raises(httpx.HTTPStatusError):
            await client.get_product("ETH-USD")
    assert not client._circuit_breaker.allow_request()


async def test_a_product_response_for_another_id_is_refused() -> None:
    client = coinbase_client(lambda request: httpx.Response(200, json={"product_id": "OTHER-USD"}))
    with pytest.raises(Exception, match="did not match"):
        await client.get_product("ETH-USD")


# --- the watch-only feed -------------------------------------------------------------


async def test_adding_a_coin_backfills_thirty_days_in_29_bounded_requests(
    database, monkeypatch
) -> None:
    monkeypatch.setattr(watch_feed_module, "BACKFILL_DAYS", 30)
    feed, watchlist, store, source, _ = build_feed(database)
    watchlist.add("ETH-USD", role="operator")
    await feed.run_cycle()
    assert len(source.calls) == 29 == feed.requests
    assert all(end - start <= INTERVAL * 300 for _, start, end in source.calls)
    assert source.calls[0][1] == BUCKET - timedelta(days=30)
    assert source.calls[-1][2] == BUCKET
    assert feed._latest("ETH-USD") == BUCKET - INTERVAL
    with database() as session:
        assert len(list(session.scalars(select(MarketCandleRecord)))) == 30 * 288
    snapshot = feed.to_dict()
    assert snapshot["symbols"][0]["state"] == "fresh"


async def test_the_next_bucket_needs_one_request_and_stores_closed_candles_only(database) -> None:
    feed, watchlist, store, source, clock = build_feed(database)
    watchlist.add("ETH-USD", role="operator")
    await feed.run_cycle()
    source.calls.clear()
    clock.now = NOW + INTERVAL
    await feed.run_cycle()
    assert len(source.calls) == 1
    assert source.calls[0][1:] == (BUCKET, BUCKET + INTERVAL)
    assert feed._latest("ETH-USD") == BUCKET
    # An unchanged clock asks for nothing more: the in-progress bucket is never fetched.
    source.calls.clear()
    await feed.run_cycle()
    assert source.calls == []


async def test_a_symbol_in_both_lists_is_collected_once_by_the_trading_feed(database) -> None:
    feed, watchlist, store, source, _ = build_feed(database, trading=("BTC-USD", "ETH-USD"))
    watchlist.add("ETH-USD", role="operator")
    watchlist.add("SOL-USD", role="operator")
    await feed.run_cycle()
    assert source.symbols_requested() == {"SOL-USD"}
    assert store.latest_opened_at("ETH-USD", INTERVAL_NAME) is None
    assert [item["symbol"] for item in feed.to_dict()["symbols"]] == ["SOL-USD"]


async def test_removing_a_symbol_stops_its_collection(database) -> None:
    feed, watchlist, _, source, clock = build_feed(database)
    watchlist.add("ETH-USD", role="operator")
    watchlist.add("SOL-USD", role="operator")
    await feed.run_cycle()
    watchlist.remove("ETH-USD", role="operator")
    source.calls.clear()
    clock.now = NOW + INTERVAL
    await feed.run_cycle()
    assert source.symbols_requested() == {"SOL-USD"}
    assert [item["symbol"] for item in feed.to_dict()["symbols"]] == ["SOL-USD"]


async def test_a_quiet_coin_with_sparse_candles_is_kept_and_a_young_coin_backfills(
    database,
) -> None:
    feed, watchlist, store, source, _ = build_feed(database)
    source.dense = False
    watchlist.add("NEW-USD", role="operator")
    await feed.run_cycle()
    assert store.latest_opened_at("NEW-USD", INTERVAL_NAME) is None
    state = feed.to_dict()["symbols"][0]
    assert state["state"] == "not_collected"


async def test_a_429_marks_only_that_symbol_unavailable_and_backs_off(database) -> None:
    feed, watchlist, store, source, clock = build_feed(database)
    watchlist.add("ETH-USD", role="operator")
    watchlist.add("SOL-USD", role="operator")
    source.failures["ETH-USD"] = status_error(429)
    await feed.run_cycle()
    states = {item["symbol"]: item for item in feed.to_dict()["symbols"]}
    assert states["ETH-USD"]["state"] == "unavailable"
    assert "429" in states["ETH-USD"]["detail"]
    assert states["SOL-USD"]["state"] == "fresh"
    # It backs off: the next cycle inside the delay makes no request for that symbol.
    source.calls.clear()
    await feed.run_cycle()
    assert "ETH-USD" not in source.symbols_requested()
    # The delay doubles on each failure and is capped.
    source.calls.clear()
    clock.now = NOW + FIRST_BACKOFF + timedelta(seconds=1)
    await feed.run_cycle()
    assert "ETH-USD" in source.symbols_requested()
    assert feed.statuses["ETH-USD"].failures == 2
    assert feed.statuses["ETH-USD"].next_attempt_at == clock.now + FIRST_BACKOFF * 2
    # Recovery clears the status.
    source.failures.clear()
    clock.now = clock.now + timedelta(minutes=10)
    await feed.run_cycle()
    assert feed.statuses["ETH-USD"].failures == 0
    assert feed.to_dict()["symbols"][0]["state"] == "fresh"


@pytest.mark.parametrize(
    "error",
    [httpx.ReadTimeout("slow"), httpx.ConnectError("down"), status_error(503), RuntimeError("x")],
)
async def test_any_failure_is_contained_in_the_symbol_status(database, error) -> None:
    feed, watchlist, _, source, _ = build_feed(database)
    watchlist.add("ETH-USD", role="operator")
    source.failures["ETH-USD"] = error
    await feed.run_cycle()
    item = feed.to_dict()["symbols"][0]
    assert item["state"] == "unavailable"
    assert "Traceback" not in item["detail"]


async def test_stale_and_not_yet_collected_states(database) -> None:
    feed, watchlist, store, _, clock = build_feed(database)
    watchlist.add("ETH-USD", role="operator")
    watchlist.add("SOL-USD", role="operator")
    await feed.run_cycle()
    assert {i["state"] for i in feed.to_dict()["symbols"]} == {"fresh"}
    assert feed.to_dict(now=clock.now + timedelta(minutes=30))["symbols"][0]["state"] == "stale"
    quiet, quiet_list, *_ = build_feed(database, trading=("ETH-USD", "SOL-USD", "BTC-USD"))
    assert quiet.to_dict()["symbols"] == []


async def test_pruning_touches_only_watch_only_symbols_past_retention(database) -> None:
    feed, watchlist, store, _, clock = build_feed(database, trading=("BTC-USD", "ETH-USD"))
    watchlist.add("ETH-USD", role="operator")  # in both lists
    watchlist.add("SOL-USD", role="operator")  # watch-only
    old = clock.now - timedelta(days=40)
    recent = clock.now - timedelta(days=1)
    store.upsert_many(
        tuple(
            make_candle(symbol, moment)
            for symbol in ("BTC-USD", "ETH-USD", "SOL-USD", "GONE-USD")
            for moment in (old, recent)
        )
    )
    feed.store = store
    await feed.run_cycle()
    with database() as session:
        remaining = {
            (row.symbol, row.opened_at.replace(tzinfo=UTC) >= clock.now - timedelta(days=30))
            for row in session.scalars(select(MarketCandleRecord))
            if row.opened_at.replace(tzinfo=UTC) in (old, recent)
        }
    assert ("SOL-USD", False) not in remaining
    assert ("SOL-USD", True) in remaining
    for untouched in ("BTC-USD", "ETH-USD", "GONE-USD"):
        assert (untouched, False) in remaining and (untouched, True) in remaining


async def test_pruning_runs_at_most_hourly(database) -> None:
    feed, watchlist, store, _, clock = build_feed(database)
    watchlist.add("SOL-USD", role="operator")
    await feed.run_cycle()
    store.upsert_many((make_candle("SOL-USD", clock.now - timedelta(days=40)),))
    clock.now = NOW + timedelta(minutes=10)
    await feed.run_cycle()
    assert store.latest_opened_at("SOL-USD", INTERVAL_NAME) is not None
    with database() as session:
        old_rows = [
            row
            for row in session.scalars(select(MarketCandleRecord))
            if row.opened_at.replace(tzinfo=UTC) < NOW - timedelta(days=39)
        ]
    assert len(old_rows) == 1
    clock.now = NOW + timedelta(hours=1, minutes=1)
    await feed.run_cycle()
    with database() as session:
        assert not [
            row
            for row in session.scalars(select(MarketCandleRecord))
            if row.opened_at.replace(tzinfo=UTC) < NOW - timedelta(days=39)
        ]


async def test_the_run_loop_collects_wakes_on_add_and_stops(database) -> None:
    feed, watchlist, _, source, _ = build_feed(database)
    stop = asyncio.Event()
    task = asyncio.create_task(feed.run(stop))
    await asyncio.sleep(0.05)
    assert source.calls == []
    watchlist.add("ETH-USD", role="operator")
    feed.wake()
    for _ in range(100):
        if feed.statuses.get("ETH-USD") and source.calls:
            break
        await asyncio.sleep(0.02)
    assert "ETH-USD" in source.symbols_requested()
    stop.set()
    await asyncio.wait_for(task, timeout=2)


def test_the_feed_has_its_own_budgeted_client() -> None:
    client = build_watch_client()
    assert client._rate_limiter.capacity == 2 and client._rate_limiter.refill_per_second == 1.0
    assert client is not build_watch_client()


# --- isolation from trading ----------------------------------------------------------

FORBIDDEN_PREFIXES = ("app.trading", "strategy", "risk", "execution", "brokers", "portfolio")


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


@pytest.mark.parametrize("module", ["app/watch_feed.py", "data/watchlist.py"])
def test_watch_modules_import_nothing_from_the_trading_path(module) -> None:
    root = Path(__file__).parents[1]
    offenders = {
        name
        for name in imported_modules(root / module)
        if name.split(".")[0] in {"strategy", "risk", "execution", "brokers", "portfolio"}
        or name.startswith(FORBIDDEN_PREFIXES[0])
    }
    assert offenders == set()


async def test_watch_candles_never_reach_the_trading_path(database, monkeypatch) -> None:
    from app.trading import TradingCycle
    from execution.engine import ExecutionEngine
    from risk import engine as risk_engine
    from strategy.reference import AlwaysBuyStrategy, MovingAverageCrossStrategy

    def forbidden(*args, **kwargs):
        raise AssertionError("a watch-only candle reached the trading path")

    async def forbidden_async(*args, **kwargs):
        forbidden()

    monkeypatch.setattr(TradingCycle, "on_market_state", forbidden_async)
    monkeypatch.setattr(MovingAverageCrossStrategy, "on_market_state", forbidden)
    monkeypatch.setattr(AlwaysBuyStrategy, "on_market_state", forbidden)
    monkeypatch.setattr(risk_engine, "evaluate", forbidden)
    monkeypatch.setattr(ExecutionEngine, "submit", forbidden_async)
    feed, watchlist, store, source, clock = build_feed(database)
    watchlist.add("ETH-USD", role="operator")
    await feed.run_cycle()
    source.failures["ETH-USD"] = status_error(429)
    clock.now = NOW + INTERVAL
    await feed.run_cycle()
    assert store.latest_opened_at("ETH-USD", INTERVAL_NAME) is not None


async def test_a_feed_failure_changes_only_that_symbol_status(database, tmp_path) -> None:
    application = history_app(tmp_path)
    operator = application.state.operator_state
    feed, watchlist, _, source, clock = build_feed(database)
    operator.set_runtime("running", "waiting for the next closed live candle")
    operator.heartbeat("primary", status="healthy", detail="all good")
    before = operator.to_dict()
    operator.watch_feed = feed
    frozen = {
        key: before[key]
        for key in ("runtime", "strategies", "alerts", "errors", "risk", "reconciliation")
    }
    watchlist.add("ETH-USD", role="operator")
    for error in (status_error(429), httpx.ReadTimeout("slow"), RuntimeError("bad")):
        source.failures["ETH-USD"] = error
        clock.now += timedelta(hours=1)
        await feed.run_cycle()
    after = operator.to_dict()
    assert {key: after[key] for key in frozen} == frozen
    assert application.state.kill_switch.state is KillSwitchState.RUNNING
    assert after["watch_feed"]["symbols"][0]["state"] == "unavailable"
    assert before["watch_feed"] == {"enabled": False, "symbols": []}


async def test_the_operator_state_keeps_watch_status_apart_from_runtime(tmp_path) -> None:
    application = history_app(tmp_path)
    state = application.state.operator_state.to_dict()
    assert state["watch_feed"] == {"enabled": False, "symbols": []}
    assert "watch_feed" not in state["runtime"]


def test_watching_a_coin_never_changes_the_trading_symbols(database) -> None:
    environ = {"PAPER_SYMBOLS": "BTC-USD, eth-usd"}
    before = PaperRuntimeConfig.from_env(environ).symbols
    watchlist = SqlAlchemyWatchlist(database)
    watchlist.add("SOL-USD", role="operator")
    assert PaperRuntimeConfig.from_env(environ).symbols == before == ("BTC-USD", "ETH-USD")
    assert parse_paper_symbols({}) == ("BTC-USD",)
    with pytest.raises(StartupGuardError):
        parse_paper_symbols({"PAPER_SYMBOLS": "BTC-EUR"})


# --- configuration and startup -------------------------------------------------------


def test_the_feed_is_off_by_default_and_validates_its_settings() -> None:
    assert WatchFeedConfig.from_env({}).enabled is False
    assert WatchFeedConfig.from_env({"WATCH_FEED_ENABLED": "true"}).retention == timedelta(days=30)
    assert WatchFeedConfig.from_env(
        {"WATCH_FEED_ENABLED": "1", "WATCH_FEED_RETENTION_DAYS": "90"}
    ).retention == timedelta(days=90)
    for bad in (
        {"WATCH_FEED_ENABLED": "yes"},
        {"WATCH_FEED_RETENTION_DAYS": "abc"},
        {"WATCH_FEED_RETENTION_DAYS": "7"},
        {"WATCH_FEED_RETENTION_DAYS": "400"},
    ):
        with pytest.raises(StartupGuardError):
            WatchFeedConfig.from_env(bad)


async def test_start_watch_feed_is_a_no_op_unless_enabled(tmp_path) -> None:
    application = history_app(tmp_path)
    close = attach_watchlist(application)
    try:
        assert start_watch_feed(application, environ={}) is None
        assert getattr(application.state, "watch_feed", None) is None
        assert application.state.operator_state.watch_feed is None
    finally:
        await close()


async def test_start_watch_feed_runs_and_stops_when_enabled(tmp_path) -> None:
    application = history_app(tmp_path)
    close = attach_watchlist(application)
    source = FakeSource()
    application.state.watch_client = source
    try:
        stop = start_watch_feed(
            application, environ={"WATCH_FEED_ENABLED": "1", "PAPER_SYMBOLS": "BTC-USD"}
        )
        assert stop is not None
        feed = application.state.watch_feed
        assert application.state.operator_state.watch_feed is feed
        assert feed.trading_symbols == frozenset({"BTC-USD"})
        await stop()
    finally:
        await close()


# --- the operator routes -------------------------------------------------------------


class RouteLookup:
    def __init__(self) -> None:
        self.results: dict[str, CoinbaseProduct | Exception] = {}
        self.asked: list[str] = []

    async def get_product(self, product_id):
        self.asked.append(product_id)
        result = self.results.get(product_id, product(product_id=product_id))
        if isinstance(result, Exception):
            raise result
        return result


def watch_app(tmp_path):
    application = history_app(tmp_path)
    engine = create_engine(application.state.startup_settings.database_url, future=True)
    application.state.watchlist = SqlAlchemyWatchlist(
        sessionmaker(bind=engine, expire_on_commit=False)
    )
    application.state.watch_client = RouteLookup()
    return application


def client_for(application) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test")


async def test_the_routes_need_a_signed_in_operator(tmp_path) -> None:
    application = watch_app(tmp_path)
    async with client_for(application) as client:
        assert (await client.get(PATH)).status_code == 401
        assert (await client.post(f"{PATH}/add", json={"symbol": "ETH-USD"})).status_code == 401
        assert (await client.post(f"{PATH}/remove", json={"symbol": "ETH-USD"})).status_code == 401
        assert (await client.post(f"{PATH}/reorder", json={"order": []})).status_code == 401
        assert (await client.get(f"{PATH}?token=x", headers=OPERATOR)).status_code == 400
    assert application.state.watch_client.asked == []


async def test_add_remove_and_reorder_as_json(tmp_path) -> None:
    application = watch_app(tmp_path)
    async with client_for(application) as client:
        for symbol in ("eth-usd", "SOL-USD", "ADA-USD"):
            response = await client.post(f"{PATH}/add", json={"symbol": symbol}, headers=OPERATOR)
            assert response.status_code == 200
            assert response.json()["status"] == "added"
        listing = (await client.get(PATH, headers=OPERATOR)).json()
        assert [s["symbol"] for s in listing["symbols"]] == ["ETH-USD", "SOL-USD", "ADA-USD"]
        assert listing["limit"] == 9 and listing["feed_enabled"] is False
        assert listing["symbols"][0]["added_by"] == "operator"
        moved = await client.post(
            f"{PATH}/reorder", json={"order": ["ADA-USD", "ETH-USD", "SOL-USD"]}, headers=ADMIN
        )
        assert moved.json() == {"status": "reordered", "order": ["ADA-USD", "ETH-USD", "SOL-USD"]}
        removed = await client.post(f"{PATH}/remove", json={"symbol": "ETH-USD"}, headers=OPERATOR)
        assert removed.json() == {"status": "removed", "symbol": "ETH-USD"}
        final = (await client.get(PATH, headers=OPERATOR)).json()
        assert [s["symbol"] for s in final["symbols"]] == ["ADA-USD", "SOL-USD"]
    actions = [row.payload["action"] for row in events(application.state.watchlist.session_factory)]
    assert actions == ["add", "add", "add", "reorder", "remove"]


async def test_the_form_adds_moves_and_removes_and_redirects(tmp_path) -> None:
    application = watch_app(tmp_path)
    async with client_for(application) as client:
        added = await client.post(f"{PATH}/add", data={"symbol": "ETH-USD"}, headers=BROWSER)
        assert (added.status_code, added.headers["location"]) == (303, PATH)
        await client.post(f"{PATH}/add", data={"symbol": "SOL-USD"}, headers=BROWSER)
        moved = await client.post(
            f"{PATH}/reorder", data={"symbol": "SOL-USD", "direction": "up"}, headers=BROWSER
        )
        assert moved.status_code == 303
        page = await client.get(PATH, headers=BROWSER)
        assert page.status_code == 200
        body = page.text
        assert body.index("SOL-USD") < body.index("ETH-USD")
        assert "The watch-only feed is off" in body
        assert "Watched coins (2 of 9)" in body
        removed = await client.post(f"{PATH}/remove", data={"symbol": "ETH-USD"}, headers=BROWSER)
        assert removed.status_code == 303
    assert application.state.watchlist.symbols() == ("SOL-USD",)


@pytest.mark.parametrize(
    ("symbol", "lookup_result", "message"),
    [
        ("NOPE-USD", CoinbaseProductNotFound("NOPE-USD"), "no product named"),
        ("OLD-USD", product(product_id="OLD-USD", status="offline"), "delisted or trading"),
        ("HALT-USD", product(product_id="HALT-USD", trading_disabled=True), "delisted or trading"),
        ("SLOW-USD", httpx.ReadTimeout("slow"), "could not confirm"),
    ],
)
async def test_a_refused_add_names_the_reason_and_saves_nothing(
    tmp_path, symbol, lookup_result, message
) -> None:
    application = watch_app(tmp_path)
    application.state.watch_client.results[symbol] = lookup_result
    async with client_for(application) as client:
        response = await client.post(f"{PATH}/add", json={"symbol": symbol}, headers=OPERATOR)
        assert response.status_code == 422
        assert message in response.json()["detail"]["errors"][0]
        html = await client.post(f"{PATH}/add", data={"symbol": symbol}, headers=BROWSER)
        assert html.status_code == 422
        assert message in html.text and "unchanged" in html.text
        assert f'value="{symbol}"' in html.text
    assert application.state.watchlist.symbols() == ()
    assert events(application.state.watchlist.session_factory) == []


async def test_duplicates_a_tenth_symbol_and_bad_input_never_reach_the_lookup(tmp_path) -> None:
    application = watch_app(tmp_path)
    lookup = application.state.watch_client
    async with client_for(application) as client:
        for coin in [*COINS, "ATOM-USD"]:
            assert (
                await client.post(f"{PATH}/add", json={"symbol": coin}, headers=OPERATOR)
            ).status_code == 200
        asked = len(lookup.asked)
        duplicate = await client.post(f"{PATH}/add", json={"symbol": "ETH-USD"}, headers=OPERATOR)
        tenth = await client.post(f"{PATH}/add", json={"symbol": "DOT-USD"}, headers=OPERATOR)
        bad = await client.post(f"{PATH}/add", json={"symbol": "ETH-EUR"}, headers=OPERATOR)
        empty = await client.post(
            f"{PATH}/add",
            content=b"not json",
            headers={**OPERATOR, "content-type": "application/json"},
        )
        assert [r.status_code for r in (duplicate, tenth, bad, empty)] == [422] * 4
        assert "already" in duplicate.json()["detail"]["errors"][0]
        assert "at most 9" in tenth.json()["detail"]["errors"][0]
        page = await client.get(PATH, headers=BROWSER)
        assert "watchlist is full" in page.text
    assert len(lookup.asked) == asked
    assert len(application.state.watchlist.symbols()) == 9


async def test_remove_and_reorder_refuse_unknown_input(tmp_path) -> None:
    application = watch_app(tmp_path)
    application.state.watchlist.add("ETH-USD", role="operator")
    async with client_for(application) as client:
        assert (
            await client.post(f"{PATH}/remove", json={"symbol": "SOL-USD"}, headers=OPERATOR)
        ).status_code == 422
        assert (
            await client.post(f"{PATH}/reorder", json={"order": ["SOL-USD"]}, headers=OPERATOR)
        ).status_code == 422
        assert (
            await client.post(
                f"{PATH}/reorder",
                json={"symbol": "ETH-USD", "direction": "sideways"},
                headers=OPERATOR,
            )
        ).status_code == 422
        edge = await client.post(
            f"{PATH}/reorder", json={"symbol": "ETH-USD", "direction": "up"}, headers=OPERATOR
        )
        assert edge.status_code == 200
    assert application.state.watchlist.symbols() == ("ETH-USD",)


async def test_an_unconfigured_watchlist_answers_503_not_an_empty_list(tmp_path) -> None:
    application = history_app(tmp_path)
    async with client_for(application) as client:
        json_read = await client.get(PATH, headers=OPERATOR)
        assert json_read.status_code == 503
        assert json_read.json()["detail"]["status"] == "unavailable"
        html_read = await client.get(PATH, headers=BROWSER)
        assert html_read.status_code == 503 and "Not available" in html_read.text
        write = await client.post(f"{PATH}/add", json={"symbol": "ETH-USD"}, headers=OPERATOR)
        assert write.status_code == 503


async def test_adding_wakes_the_running_feed(tmp_path) -> None:
    application = watch_app(tmp_path)

    class Feed:
        woken = 0
        trading_symbols = frozenset({"BTC-USD"})

        def wake(self) -> None:
            self.woken += 1

        def to_dict(self) -> dict:
            return {
                "enabled": True,
                "symbols": [
                    {
                        "symbol": "ETH-USD",
                        "state": "unavailable",
                        "detail": "Coinbase is rate limiting this feed (429)",
                        "last_candle_at": None,
                    }
                ],
            }

    application.state.watch_feed = Feed()
    async with client_for(application) as client:
        await client.post(f"{PATH}/add", json={"symbol": "ETH-USD"}, headers=OPERATOR)
        page = await client.get(PATH, headers=BROWSER)
        listing = (await client.get(PATH, headers=OPERATOR)).json()
    assert application.state.watch_feed.woken == 1
    assert "unavailable" in page.text and "429" in page.text
    assert listing["feed_enabled"] is True and listing["trading_symbols"] == ["BTC-USD"]
    assert listing["symbols"][0]["collected_by"] == "watch-only feed"


async def test_a_coin_the_trading_feed_collects_says_so(tmp_path) -> None:
    application = watch_app(tmp_path)
    application.state.watchlist.add("BTC-USD", role="operator")

    class Feed:
        trading_symbols = frozenset({"BTC-USD"})

        def to_dict(self) -> dict:
            return {"enabled": True, "symbols": []}

    application.state.watch_feed = Feed()
    async with client_for(application) as client:
        page = await client.get(PATH, headers=BROWSER)
    assert "collected by the trading feed" in page.text


def test_alert_router_is_untouched_by_watchlist_routes() -> None:
    assert AlertRouter().configured_destinations == ()


# --- migration -----------------------------------------------------------------------


def _load_migration():
    path = Path(__file__).parents[1] / "alembic" / "versions" / "0009_watchlist.py"
    spec = importlib.util.spec_from_file_location("revision_0009", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_watchlist_migration_upgrades_downgrades_and_matches_the_model(
    tmp_path, monkeypatch
) -> None:
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import inspect, text

    url = f"sqlite+pysqlite:///{tmp_path / 'migrated.db'}"
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parents[1] / "alembic"))
    monkeypatch.setenv("DATABASE_URL", url)
    engine = create_engine(url, future=True)
    assert _load_migration().down_revision == "0008_incident_records"

    command.upgrade(config, "0008_incident_records")
    assert "watchlist" not in inspect(engine).get_table_names()
    command.upgrade(config, "head")
    columns = {c["name"]: c for c in inspect(engine).get_columns("watchlist")}
    assert set(columns) == {"symbol", "position", "added_at", "added_by"}
    assert inspect(engine).get_pk_constraint("watchlist")["constrained_columns"] == ["symbol"]
    assert {u["name"] for u in inspect(engine).get_unique_constraints("watchlist")} == {
        "uq_watchlist_position"
    }
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO watchlist (symbol, position, added_at, added_by) "
                "VALUES ('ETH-USD', 0, '2026-09-27 12:00:00', 'operator')"
            )
        )
        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "INSERT INTO watchlist (symbol, position, added_at, added_by) "
                    "VALUES ('SOL-USD', 9, '2026-09-27 12:00:00', 'operator')"
                )
            )
    model_columns = {column.name for column in WatchlistRecord.__table__.columns}
    assert model_columns == set(columns)

    command.downgrade(config, "0008_incident_records")
    assert "watchlist" not in inspect(engine).get_table_names()
    with engine.connect() as connection:
        version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    assert version == "0008_incident_records"
    command.upgrade(config, "head")
    assert "watchlist" in inspect(engine).get_table_names()
    engine.dispose()


def test_the_feed_module_uses_the_shared_resilience_primitives() -> None:
    names = imported_modules(Path(watch_feed_module.__file__))
    assert "core.resilience" in names and "data.coinbase" in names


def test_the_app_registers_the_watchlist_routes(tmp_path) -> None:
    application = create_app(history_app(tmp_path).state.startup_settings)
    paths = {route.path for route in application.routes}
    assert {PATH, f"{PATH}/add", f"{PATH}/remove", f"{PATH}/reorder"} <= paths
