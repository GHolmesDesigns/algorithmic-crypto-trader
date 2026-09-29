"""Market candles: bounded, aggregated by the database, exact, and truthful about absence."""

import ast
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import api.markets as markets
import httpx
import pytest
from api.markets import (
    GAP,
    IN_PROGRESS,
    INTERVALS,
    MAX_POINTS,
    MAX_SYMBOLS,
    PARTIAL,
    WINDOWS,
    CandleQueryRefused,
    CandlesUnavailable,
    SqlAlchemyCandleReads,
    candle_query,
    parse_candles_query,
)
from api.markets import _text as decimal_text
from data.watchlist import SqlAlchemyWatchlist
from db.models import Base, MarketCandleRecord
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.orm import sessionmaker

from tests.operator_support import history_app, journaled_app

OPERATOR_TOKEN = "operator-secret-markets-7c31"
OPERATOR = {"x-operator-token": OPERATOR_TOKEN}
PATH = "/operator/markets/candles"
# Mid-bar for every interval up to six hours, so no bar edge falls on the clock.
NOW = datetime(2026, 9, 27, 14, 23, 45, tzinfo=UTC)
FIVE = timedelta(minutes=5)


@pytest.fixture(autouse=True)
def environment(monkeypatch, tmp_path):
    monkeypatch.setenv("OPERATOR_TOKEN", OPERATOR_TOKEN)
    monkeypatch.delenv("OPERATOR_ADMIN_TOKEN", raising=False)
    monkeypatch.setenv("KILL_SWITCH_FILE", str(tmp_path / "kill-switch.json"))
    monkeypatch.setattr(markets, "utc_now", lambda: NOW)


def at(hour: int, minute: int = 0, *, day: int = 27) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=UTC)


def candle(
    symbol: str,
    opened_at: datetime,
    open_: str,
    high: str,
    low: str,
    close: str,
    volume: str,
    *,
    interval: str = "FIVE_MINUTE",
    source: str = "coinbase_advanced_trade",
) -> MarketCandleRecord:
    return MarketCandleRecord(
        symbol=symbol,
        interval=interval,
        opened_at=opened_at,
        closed_at=opened_at + FIVE,
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal(volume),
        source=source,
        as_of=opened_at + FIVE,
        ingested_at=opened_at + FIVE,
    )


def flat(symbol: str, opened_at: datetime, price: str = "1") -> MarketCandleRecord:
    return candle(symbol, opened_at, price, price, price, price, "1")


def database(tmp_path: Path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'trader.db'}", future=True)
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def add(tmp_path: Path, *records: MarketCandleRecord) -> None:
    engine, session_factory = database(tmp_path)
    with session_factory() as session:
        session.add_all(records)
        session.commit()
    engine.dispose()


def drop_candles(tmp_path: Path) -> None:
    engine, _ = database(tmp_path)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE market_candles"))
    engine.dispose()


def reads(tmp_path: Path) -> SqlAlchemyCandleReads:
    _, session_factory = database(tmp_path)
    return SqlAlchemyCandleReads(session_factory)


def run(tmp_path: Path, symbols=("BTC-USD",), window="24h", interval="15m", feed=None):
    return reads(tmp_path).read(candle_query(symbols, window, interval, NOW), feed_states=feed)


def one(payload: dict, symbol: str = "BTC-USD") -> dict:
    return next(item for item in payload["symbols"] if item["symbol"] == symbol)


def bar_at(series: dict, moment: datetime) -> dict:
    return next(bar for bar in series["bars"] if bar["opened_at"] == moment.isoformat())


def client_for(application) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test")


async def get(application, *, headers=OPERATOR, params=None) -> httpx.Response:
    async with client_for(application) as client:
        return await client.get(PATH, headers=headers, params=params)


# --- The request: vocabulary and caps ---------------------------------------------------


def parse(*items: tuple[str, str], default=()):
    return parse_candles_query(items, default_symbols=default, now=NOW)


def test_each_window_defaults_to_the_finest_interval_within_the_cap():
    picked = {w: parse(("symbols", "BTC-USD"), ("window", w)).interval for w in WINDOWS}
    assert picked == {"24h": "15m", "7d": "1h", "30d": "6h", "90d": "1d"}
    assert parse(("symbols", "BTC-USD")).window == "24h"


def test_every_accepted_window_and_interval_stays_within_the_point_cap():
    for window in WINDOWS:
        for interval in INTERVALS:
            points = WINDOWS[window] // INTERVALS[interval]
            items = (("symbols", "BTC-USD"), ("window", window), ("interval", interval))
            if points > MAX_POINTS:
                with pytest.raises(CandleQueryRefused):
                    parse(*items)
            else:
                query = parse(*items)
                assert query.points == points <= MAX_POINTS


@pytest.mark.parametrize(
    ("window", "interval"), [("7d", "15m"), ("30d", "1h"), ("30d", "15m"), ("90d", "6h")]
)
def test_an_interval_that_would_exceed_the_cap_is_refused_with_the_reason(window, interval):
    with pytest.raises(CandleQueryRefused) as refused:
        parse(("symbols", "BTC-USD"), ("window", window), ("interval", interval))
    assert str(MAX_POINTS) in refused.value.errors[0]


def test_more_than_nine_symbols_are_refused():
    names = ",".join(f"C{index}-USD" for index in range(MAX_SYMBOLS + 1))
    with pytest.raises(CandleQueryRefused) as refused:
        parse(("symbols", names))
    assert "at most 9" in refused.value.errors[0]
    assert len(parse(("symbols", names.rsplit(",", 1)[0])).symbols) == MAX_SYMBOLS


@pytest.mark.parametrize(
    "items",
    [
        (("symbols", "BTC-USD"), ("window", "1y")),
        (("symbols", "BTC-USD"), ("interval", "5m")),
        (("symbols", "BTC-USD"), ("limit", "10")),
        (("symbols", "BTC-USD"), ("window", "24h"), ("window", "7d")),
        (("symbols", "BTC-USD"), ("symbols", "ETH-USD")),
        (("symbols", "BTC-EUR"),),
        (("symbols", "BTC-USD,BTC-USD"),),
        (("symbols", " , "),),
        (),
    ],
)
def test_anything_outside_the_vocabulary_is_refused(items):
    with pytest.raises(CandleQueryRefused):
        parse(*items)


def test_every_problem_is_reported_together():
    with pytest.raises(CandleQueryRefused) as refused:
        parse(("symbols", "BTC-EUR"), ("window", "1y"), ("extra", "x"))
    assert len(refused.value.errors) == 3


def test_the_watchlist_stands_in_only_when_no_symbols_are_named():
    assert parse(default=("BTC-USD", "ETH-USD")).symbols == ("BTC-USD", "ETH-USD")
    assert parse(("symbols", "xlm-usd"), default=("BTC-USD",)).symbols == ("XLM-USD",)


def test_the_caps_hold_even_when_the_parser_is_skipped():
    with pytest.raises(CandleQueryRefused):
        candle_query(("BTC-USD",), "90d", "15m", NOW)
    with pytest.raises(CandleQueryRefused):
        candle_query(tuple(f"C{i}-USD" for i in range(10)), "24h", "15m", NOW)
    with pytest.raises(CandleQueryRefused):
        candle_query((), "24h", "15m", NOW)


def test_bars_are_aligned_to_utc_and_end_with_the_bar_running_now():
    query = candle_query(("BTC-USD",), "24h", "15m", NOW)
    assert query.until == at(14, 30)
    assert query.since == at(14, 30, day=26)
    assert query.edges[-2] == at(14, 15)
    daily = candle_query(("BTC-USD",), "90d", "1d", NOW)
    assert daily.until == at(0, day=28)
    six = candle_query(("BTC-USD",), "30d", "6h", NOW)
    assert six.edges[-2] == at(12) and six.points == 120


def test_the_stale_limit_and_interval_match_the_watch_feed():
    from app.watch_feed import INTERVAL_NAME, STALE_AFTER

    assert markets.STALE_AFTER == STALE_AFTER
    assert markets.SOURCE_INTERVAL == INTERVAL_NAME


# --- Aggregation -----------------------------------------------------------------------


def test_fifteen_minute_bars_take_the_first_open_last_close_extremes_and_volume_sum(tmp_path):
    add(
        tmp_path,
        # Out of time order on purpose: first and last are by time, not by insert order.
        candle("BTC-USD", at(14, 5), "101", "110", "100", "108", "2.25"),
        candle("BTC-USD", at(14, 10), "108", "109", "95", "96", "0.5"),
        candle("BTC-USD", at(14, 0), "100", "105", "99", "101", "1.5"),
    )
    bar = bar_at(one(run(tmp_path)), at(14, 0))
    assert bar == {
        "opened_at": at(14, 0).isoformat(),
        "closed_at": at(14, 15).isoformat(),
        "state": "complete",
        "candles": 3,
        "expected": 3,
        "open": "100",
        "high": "110",
        "low": "95",
        "close": "96",
        "volume": "4.25",
    }


def test_hourly_six_hourly_and_daily_bars_group_on_utc_edges(tmp_path):
    rows = [
        candle("BTC-USD", at(13) + FIVE * i, "10", str(20 + i), "5", str(i + 1), "1")
        for i in range(12)
    ]
    rows += [
        candle("BTC-USD", at(5, 55), "7", "8", "6", "7.5", "2"),
        candle("BTC-USD", at(6), "8", "9", "7", "8.5", "3"),
        candle("BTC-USD", at(0, 0, day=26), "1", "2", "0.5", "1.5", "4"),
        candle("BTC-USD", at(23, 55, day=25), "9", "9", "9", "9", "5"),
    ]
    add(tmp_path, *rows)

    hour = bar_at(one(run(tmp_path, window="7d", interval="1h")), at(13))
    assert (hour["state"], hour["candles"], hour["expected"]) == ("complete", 12, 12)
    assert (hour["open"], hour["high"], hour["low"], hour["close"], hour["volume"]) == (
        "10",
        "31",
        "5",
        "12",
        "12",
    )

    six = one(run(tmp_path, window="30d", interval="6h"))
    early, late = bar_at(six, at(0)), bar_at(six, at(6))
    # 05:55 belongs to the 00:00 bar and 06:00 to the 06:00 bar, exactly on the edge.
    assert (early["candles"], early["close"]) == (1, "7.5")
    assert (late["candles"], late["open"], late["volume"]) == (1, "8", "3")
    assert late["state"] == PARTIAL

    daily = one(run(tmp_path, window="90d", interval="1d"))
    assert bar_at(daily, at(0, day=26))["open"] == "1"
    assert bar_at(daily, at(0, day=25))["close"] == "9"
    assert bar_at(daily, at(0, day=27))["candles"] == 14


def test_the_window_starts_at_its_first_edge_and_ends_at_the_bar_running_now(tmp_path):
    since = candle_query(("BTC-USD",), "24h", "15m", NOW).since
    add(
        tmp_path,
        flat("BTC-USD", since - FIVE, "50"),
        flat("BTC-USD", since, "60"),
        flat("BTC-USD", at(14, 30), "70"),
    )
    series = one(run(tmp_path))
    assert series["in_window"]["candles"] == 1
    assert series["bars"][0]["open"] == "60"
    assert series["bars"][0]["opened_at"] == since.isoformat()
    assert {bar["open"] for bar in series["bars"]} == {"60", None}


def test_only_five_minute_rows_are_read(tmp_path):
    add(
        tmp_path,
        flat("BTC-USD", at(14, 0)),
        candle("BTC-USD", at(14, 0), "999", "999", "999", "999", "999", interval="ONE_MINUTE"),
    )
    bar = bar_at(one(run(tmp_path)), at(14, 0))
    assert (bar["candles"], bar["high"], bar["volume"]) == (1, "1", "1")


def test_symbols_are_kept_apart_and_come_back_in_the_order_asked(tmp_path):
    add(
        tmp_path,
        candle("ETH-USD", at(14, 0), "20", "21", "19", "20.5", "3"),
        candle("BTC-USD", at(14, 0), "10", "11", "9", "10.5", "1"),
    )
    payload = run(tmp_path, symbols=("ETH-USD", "BTC-USD", "ADA-USD"))
    assert [item["symbol"] for item in payload["symbols"]] == ["ETH-USD", "BTC-USD", "ADA-USD"]
    assert bar_at(one(payload, "ETH-USD"), at(14, 0))["open"] == "20"
    assert bar_at(one(payload, "BTC-USD"), at(14, 0))["open"] == "10"
    assert one(payload, "ADA-USD")["in_window"]["candles"] == 0


def test_a_coin_priced_below_a_hundredth_of_a_cent_keeps_every_digit(tmp_path):
    add(
        tmp_path,
        candle("SHIB-USD", at(14, 0), "0.00000123", "0.000001234", "0.00000121", "0.00000122", "1"),
        candle(
            "SHIB-USD", at(14, 5), "0.00000122", "0.00000125", "0.000001215", "0.000001225", "2"
        ),
    )
    payload = run(tmp_path, symbols=("SHIB-USD",))
    bar = bar_at(one(payload, "SHIB-USD"), at(14, 0))
    assert (bar["open"], bar["high"], bar["low"], bar["close"]) == (
        "0.00000123",
        "0.00000125",
        "0.00000121",
        "0.000001225",
    )


def test_decimal_text_is_plain_and_never_rounds():
    assert decimal_text(Decimal("1.230000000000000000")) == "1.23"
    assert decimal_text(Decimal("0E-18")) == "0"
    assert decimal_text(Decimal("1E+2")) == "100"
    assert decimal_text(Decimal("1E-7")) == "0.0000001"
    wide = Decimal("12345678901234567890.123456789012345678")
    assert decimal_text(wide) == "12345678901234567890.123456789012345678"


def test_no_float_or_decimal_object_reaches_the_payload(tmp_path):
    add(tmp_path, candle("BTC-USD", at(14, 0), "0.1", "0.3", "0.05", "0.2", "0.25"))

    def leaves(value):
        if isinstance(value, dict):
            for item in value.values():
                yield from leaves(item)
        elif isinstance(value, list):
            for item in value:
                yield from leaves(item)
        else:
            yield value

    kinds = {type(leaf) for leaf in leaves(run(tmp_path))}
    assert float not in kinds and Decimal not in kinds


# --- Gaps, partial and in-progress bars ---------------------------------------------------


def test_missing_bars_are_gaps_and_never_zero_or_carried_forward(tmp_path):
    add(
        tmp_path,
        flat("BTC-USD", at(10, 0), "10"),
        flat("BTC-USD", at(13, 45), "20"),
        flat("BTC-USD", at(13, 50), "20"),
        flat("BTC-USD", at(14, 0), "30"),
        flat("BTC-USD", at(14, 5), "30"),
        flat("BTC-USD", at(14, 10), "30"),
    )
    series = one(run(tmp_path))
    quiet = bar_at(series, at(12, 0))
    assert quiet["state"] == GAP and quiet["candles"] == 0
    assert [quiet[k] for k in ("open", "high", "low", "close", "volume")] == [None] * 5
    assert bar_at(series, at(13, 45))["state"] == PARTIAL
    assert bar_at(series, at(14, 0))["state"] == "complete"
    since = candle_query(("BTC-USD",), "24h", "15m", NOW).since
    assert series["gaps"] == [
        {"from": since.isoformat(), "to": at(10, 0).isoformat(), "bars": 78},
        {"from": at(10, 15).isoformat(), "to": at(13, 45).isoformat(), "bars": 14},
    ]
    assert len(series["bars"]) == 96


def test_the_running_bar_is_in_progress_whether_or_not_it_has_candles(tmp_path):
    add(tmp_path, candle("BTC-USD", at(14, 15), "5", "6", "4", "5.5", "1"))
    running = bar_at(one(run(tmp_path)), at(14, 15))
    assert running["state"] == IN_PROGRESS
    assert (running["candles"], running["open"], running["close"]) == (1, "5", "5.5")

    quiet = tmp_path / "quiet"
    quiet.mkdir()
    add(quiet, flat("BTC-USD", at(9, 0)))
    series = one(run(quiet))
    last = series["bars"][-1]
    assert last["state"] == IN_PROGRESS and last["candles"] == 0 and last["open"] is None
    # The running bar is not counted as a gap.
    assert series["gaps"][-1]["to"] == at(14, 15).isoformat()


def test_an_empty_window_is_a_full_set_of_gaps_not_an_error(tmp_path):
    add(tmp_path, flat("BTC-USD", at(9, 0, day=1)))
    series = one(run(tmp_path))
    assert series["in_window"] == {"candles": 0, "expected": 288, "empty": True}
    assert [bar["state"] for bar in series["bars"]] == [GAP] * 95 + [IN_PROGRESS]
    assert series["gaps"][0]["bars"] == 95


# --- Freshness -----------------------------------------------------------------------------


def test_fresh_stale_not_collected_and_unavailable_are_four_different_states(tmp_path):
    add(
        tmp_path,
        flat("BTC-USD", at(14, 15)),
        flat("ETH-USD", at(13, 0)),
        flat("XLM-USD", at(14, 15)),
    )
    feed = {"XLM-USD": {"symbol": "XLM-USD", "state": "unavailable", "detail": "HTTP 429"}}
    payload = run(tmp_path, symbols=("BTC-USD", "ETH-USD", "ADA-USD", "XLM-USD"), feed=feed)
    states = {item["symbol"]: item["freshness"] for item in payload["symbols"]}
    assert states["BTC-USD"]["state"] == "fresh"
    assert states["ETH-USD"]["state"] == "stale"
    assert states["ADA-USD"]["state"] == "not_collected"
    assert states["ADA-USD"]["last_candle_at"] is None
    assert states["XLM-USD"]["state"] == "unavailable"
    assert states["XLM-USD"]["detail"] == "HTTP 429"
    assert states["BTC-USD"]["last_candle_at"] == at(14, 15).isoformat()


def test_no_rows_in_the_window_is_distinct_from_not_yet_collected(tmp_path):
    # Candles exist, all newer than a window read for five days ago.
    add(tmp_path, flat("BTC-USD", at(14, 15)))
    query = candle_query(("BTC-USD", "ADA-USD"), "24h", "15m", NOW - timedelta(days=5))
    payload = reads(tmp_path).read(query)
    btc, ada = payload["symbols"]
    assert btc["in_window"]["empty"] and ada["in_window"]["empty"]
    assert btc["freshness"]["state"] != "not_collected" and btc["freshness"]["last_candle_at"]
    assert ada["freshness"]["state"] == "not_collected"


def test_a_symbol_can_be_fresh_with_a_window_that_has_rows_or_stale_with_a_full_window(tmp_path):
    add(tmp_path, *[flat("BTC-USD", at(6) + FIVE * i) for i in range(12)])
    series = one(run(tmp_path))
    assert series["freshness"]["state"] == "stale"
    assert series["in_window"]["candles"] == 12 and not series["in_window"]["empty"]


def test_the_boundary_between_fresh_and_stale_is_three_intervals_after_a_candle_closes(tmp_path):
    edge = NOW - timedelta(minutes=20)
    add(
        tmp_path,
        flat("BTC-USD", edge - timedelta(seconds=1)),
        flat("ETH-USD", edge + timedelta(seconds=1)),
    )
    payload = run(tmp_path, symbols=("BTC-USD", "ETH-USD"))
    assert one(payload, "BTC-USD")["freshness"]["state"] == "stale"
    assert one(payload, "ETH-USD")["freshness"]["state"] == "fresh"


def test_source_is_the_newest_candles_source(tmp_path):
    add(
        tmp_path,
        candle("BTC-USD", at(14, 0), "1", "1", "1", "1", "1", source="old_source"),
        candle("BTC-USD", at(14, 5), "1", "1", "1", "1", "1", source="coinbase_advanced_trade"),
    )
    payload = run(tmp_path, symbols=("BTC-USD", "ADA-USD"))
    assert one(payload)["source"] == "coinbase_advanced_trade"
    assert one(payload, "ADA-USD")["source"] is None
    assert payload["query"]["source_interval"] == "FIVE_MINUTE"


# --- Read-only and bounded -----------------------------------------------------------------


def test_a_read_only_selects_and_aggregates_in_the_database(tmp_path):
    add(tmp_path, *[flat("BTC-USD", at(0) + FIVE * index) for index in range(200)])
    engine, session_factory = database(tmp_path)
    statements: list[str] = []
    event.listen(engine, "before_cursor_execute", lambda *args: statements.append(args[2]))
    SqlAlchemyCandleReads(session_factory).read(candle_query(("BTC-USD",), "7d", "1h", NOW))
    assert len(statements) == 3
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in statements)
    grouped = next(
        s.lower() for s in statements if "group by" in s.lower() and "count(" in s.lower()
    )
    assert "sum(" in grouped and "max(" in grouped and "min(" in grouped


def test_an_unreadable_table_is_reported_not_shown_as_empty(tmp_path):
    source = reads(tmp_path)
    drop_candles(tmp_path)
    with pytest.raises(CandlesUnavailable):
        source.read(candle_query(("BTC-USD",), "24h", "15m", NOW))


def test_the_module_reaches_no_broker_strategy_risk_or_execution_code():
    source = Path(markets.__file__).read_text(encoding="utf-8")
    imported = {
        node.module.split(".")[0]
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not imported & {"brokers", "strategy", "risk", "execution", "portfolio", "app"}
    assert "httpx" not in source


# --- The route -----------------------------------------------------------------------------


async def test_the_route_answers_json_with_bars_gaps_freshness_and_source(tmp_path):
    add(
        tmp_path,
        candle("BTC-USD", at(14, 0), "100", "110", "95", "96", "4"),
        candle("BTC-USD", at(14, 5), "96", "97", "94", "95", "1"),
    )
    response = await get(history_app(tmp_path), params={"symbols": "BTC-USD,ETH-USD"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert body["status"] == "available"
    assert body["query"]["window"] == "24h" and body["query"]["interval"] == "15m"
    assert body["query"]["points"] == 96 and body["query"]["max_points"] == 300
    btc, eth = body["symbols"]
    assert btc["freshness"]["state"] == "fresh" and btc["source"] == "coinbase_advanced_trade"
    assert bar_at(btc, at(14, 0))["open"] == "100"
    assert eth["freshness"]["state"] == "not_collected" and eth["in_window"]["empty"]
    assert json.loads(response.text) == body


async def test_the_route_takes_a_window_and_interval(tmp_path):
    response = await get(
        history_app(tmp_path), params={"symbols": "BTC-USD", "window": "30d", "interval": "1d"}
    )
    assert response.status_code == 200
    assert response.json()["query"]["points"] == 30
    assert len(response.json()["symbols"][0]["bars"]) == 30


async def test_the_route_refuses_requests_beyond_its_caps_with_400_and_a_reason(tmp_path):
    application = history_app(tmp_path)
    too_many = ",".join(f"C{index}-USD" for index in range(10))
    for params in (
        {"symbols": too_many},
        {"symbols": "BTC-USD", "window": "90d", "interval": "15m"},
        {"symbols": "BTC-USD", "window": "1y"},
        {"symbols": "BTC-USD", "unknown": "1"},
        {},
    ):
        response = await get(application, params=params)
        assert response.status_code == 400, params
        detail = response.json()["detail"]
        assert detail["status"] == "refused" and detail["errors"]


async def test_unauthenticated_requests_are_refused_and_query_string_tokens_get_400(tmp_path):
    application = history_app(tmp_path)
    params = {"symbols": "BTC-USD"}
    assert (await get(application, headers={}, params=params)).status_code == 401
    wrong = {"x-operator-token": "wrong"}
    assert (await get(application, headers=wrong, params=params)).status_code == 401
    leaked = await get(application, headers={}, params={**params, "token": OPERATOR_TOKEN})
    assert leaked.status_code == 400
    assert OPERATOR_TOKEN not in leaked.text
    signed_in = await get(application, headers=OPERATOR, params={**params, "token": "x"})
    assert signed_in.status_code == 400


async def test_the_route_only_reads(tmp_path):
    add(tmp_path, flat("BTC-USD", at(14, 0)))
    application = history_app(tmp_path)
    async with client_for(application) as client:
        for method in ("post", "put", "patch", "delete"):
            response = await getattr(client, method)(PATH, headers=OPERATOR)
            assert response.status_code == 405
        await client.get(PATH, headers=OPERATOR, params={"symbols": "BTC-USD"})
    _, session_factory = database(tmp_path)
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(MarketCandleRecord)) == 1


async def test_without_the_candle_database_the_route_answers_503(tmp_path):
    response = await get(journaled_app(tmp_path), params={"symbols": "BTC-USD"})
    assert response.status_code == 503
    assert response.json()["detail"]["status"] == "unavailable"


async def test_an_unreadable_table_answers_503_never_an_empty_chart(tmp_path):
    application = history_app(tmp_path)
    drop_candles(tmp_path)
    response = await get(application, params={"symbols": "BTC-USD"})
    assert response.status_code == 503
    assert "bars" not in response.text


async def test_the_saved_watchlist_is_the_default_and_the_feeds_failure_shows(tmp_path):
    add(tmp_path, flat("ETH-USD", at(14, 15)))
    application = history_app(tmp_path)
    _, session_factory = database(tmp_path)
    watchlist = SqlAlchemyWatchlist(session_factory)
    watchlist.add("ETH-USD", role="operator")
    watchlist.add("ADA-USD", role="operator")
    application.state.watchlist = watchlist

    class Feed:
        def to_dict(self):
            return {
                "enabled": True,
                "symbols": [{"symbol": "ADA-USD", "state": "unavailable", "detail": "HTTP 429"}],
            }

    application.state.watch_feed = Feed()
    body = (await get(application)).json()
    assert [item["symbol"] for item in body["symbols"]] == ["ETH-USD", "ADA-USD"]
    assert body["symbols"][0]["freshness"]["state"] == "fresh"
    assert body["symbols"][1]["freshness"]["state"] == "unavailable"
    named = (await get(application, params={"symbols": "BTC-USD"})).json()
    assert [item["symbol"] for item in named["symbols"]] == ["BTC-USD"]
