"""Server-rendered Markets page: bounded, readable, and read-only."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import api.markets as markets
import httpx
import pytest
from api.markets_view import build_markets_view, parse_markets_query
from data.watchlist import SqlAlchemyWatchlist
from db.models import Base, MarketCandleRecord
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from tests.operator_support import history_app

OPERATOR = {"x-operator-token": "operator-markets-page"}
HTML = {**OPERATOR, "accept": "text/html,application/xhtml+xml"}
NOW = datetime(2026, 9, 27, 14, 23, 45, tzinfo=UTC)
FIVE = timedelta(minutes=5)


@pytest.fixture(autouse=True)
def environment(monkeypatch, tmp_path):
    monkeypatch.setenv("OPERATOR_TOKEN", OPERATOR["x-operator-token"])
    monkeypatch.delenv("OPERATOR_ADMIN_TOKEN", raising=False)
    monkeypatch.setenv("KILL_SWITCH_FILE", str(tmp_path / "kill-switch.json"))
    monkeypatch.setattr(markets, "utc_now", lambda: NOW)


def record(symbol: str, opened_at: datetime, price: str) -> MarketCandleRecord:
    value = Decimal(price)
    return MarketCandleRecord(
        symbol=symbol,
        interval="FIVE_MINUTE",
        opened_at=opened_at,
        closed_at=opened_at + FIVE,
        open=value,
        high=value + Decimal("1"),
        low=value - Decimal("1"),
        close=value,
        volume=Decimal("1"),
        source="coinbase_advanced_trade",
        as_of=opened_at + FIVE,
        ingested_at=opened_at + FIVE,
    )


def database(tmp_path: Path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'trader.db'}", future=True)
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def app_with_watchlist(tmp_path: Path, *symbols: str):
    engine, factory = database(tmp_path)
    with factory() as session:
        session.add_all(
            [
                record("BTC-USD", datetime(2026, 9, 27, 14, 0, tzinfo=UTC), "100"),
                record("BTC-USD", datetime(2026, 9, 27, 14, 5, tzinfo=UTC), "102"),
            ]
        )
        session.commit()
    engine.dispose()
    application = history_app(tmp_path)
    application.state.watchlist = SqlAlchemyWatchlist(factory)
    for symbol in symbols:
        application.state.watchlist.add(symbol, role="operator")
    return application


async def get_page(application, *, params=None, headers=HTML) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        return await client.get("/operator/markets", headers=headers, params=params)


def test_shareable_query_uses_the_same_caps_and_symbol_rules_as_candle_reads():
    with pytest.raises(ValueError) as refused:
        parse_markets_query((("c", "BTC-USD,BTC-USD"),), default_symbols=(), now=NOW)
    assert "named twice" in str(refused.value)

    query = parse_markets_query(
        (("c", "btc-usd,eth-usd"), ("window", "7d"), ("interval", "1h")),
        default_symbols=(),
        now=NOW,
    )
    assert query.symbols == ("BTC-USD", "ETH-USD")
    assert query.window == "7d" and query.interval == "1h"


def test_view_breaks_the_svg_line_at_gaps_and_keeps_state_words_distinct():
    payload = {
        "query": {
            "window": "24h",
            "interval": "15m",
            "symbols": ["BTC-USD", "ETH-USD", "ADA-USD", "XLM-USD"],
        },
        "symbols": [
            {
                "symbol": "BTC-USD",
                "source": "coinbase_advanced_trade",
                "freshness": {"state": "fresh", "last_candle_at": "2026-09-27T14:15:00+00:00"},
                "bars": [
                    {"close": "100", "high": "101", "low": "99"},
                    {"close": None, "high": None, "low": None},
                    {"close": "102", "high": "103", "low": "101"},
                ],
            },
            *[
                {
                    "symbol": symbol,
                    "source": None,
                    "freshness": {"state": state, "last_candle_at": None},
                    "bars": [],
                }
                for symbol, state in (
                    ("ETH-USD", "stale"),
                    ("ADA-USD", "not_collected"),
                    ("XLM-USD", "unavailable"),
                )
            ],
        ],
    }
    view = build_markets_view(payload, form={})
    assert view["columns"] == 2
    assert [item["state"] for item in view["tiles"]] == [
        "drawn",
        "stale",
        "not_collected",
        "unavailable",
    ]
    assert len(view["tiles"][0]["segments"]) == 2
    assert view["tiles"][0]["last"] == "102"
    assert view["tiles"][0]["tradingview"] == (
        "https://www.tradingview.com/chart/?symbol=COINBASE:BTCUSD"
    )


@pytest.mark.asyncio
async def test_page_is_server_rendered_with_metrics_table_and_only_tradingview_external_links(
    tmp_path,
):
    application = app_with_watchlist(tmp_path, "BTC-USD")
    response = await get_page(application)
    assert response.status_code == 200
    body = response.text
    assert 'class="market-grid"' in body
    assert 'class="market-line-svg"' in body
    assert "Accessible data table for BTC-USD" in body
    assert 'href="https://www.tradingview.com/chart/?symbol=COINBASE:BTCUSD"' in body
    assert 'target="_blank"' in body and 'rel="noopener noreferrer"' in body
    assert "<script" not in body.lower()
    assert "<iframe" not in body.lower()
    assert "<img" not in body.lower()
    assert "https://" not in body.replace(
        "https://www.tradingview.com/chart/?symbol=COINBASE:BTCUSD", ""
    )


@pytest.mark.asyncio
async def test_invalid_shared_layout_is_refused_with_a_reason(tmp_path):
    application = app_with_watchlist(tmp_path, "BTC-USD")
    response = await get_page(
        application,
        params={"c": "BTC-USD,BTC-USD"},
    )
    assert response.status_code == 400
    assert "refused" in response.text.lower()
    assert "named twice" in response.text
