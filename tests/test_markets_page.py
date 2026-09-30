"""Server-rendered Markets page: bounded, readable, and read-only."""

from __future__ import annotations

import hashlib
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


def test_view_places_linked_buy_and_sell_markers_and_preserves_unavailable_activity():
    payload = {
        "query": {
            "window": "24h",
            "interval": "15m",
            "symbols": ["BTC-USD"],
        },
        "symbols": [
            {
                "symbol": "BTC-USD",
                "source": "coinbase_advanced_trade",
                "freshness": {"state": "fresh", "last_candle_at": "2026-09-27T14:15:00+00:00"},
                "bars": [
                    {
                        "opened_at": "2026-09-27T14:00:00+00:00",
                        "closed_at": "2026-09-27T14:15:00+00:00",
                        "close": "100",
                        "high": "101",
                        "low": "99",
                    },
                    {
                        "opened_at": "2026-09-27T14:15:00+00:00",
                        "closed_at": "2026-09-27T14:30:00+00:00",
                        "close": "102",
                        "high": "103",
                        "low": "101",
                    },
                ],
            }
        ],
        "activity": {
            "status": "available",
            "symbols": [
                {
                    "symbol": "BTC-USD",
                    "total": 2,
                    "shown": 2,
                    "truncated": False,
                    "rows": [
                        {
                            "id": "signal",
                            "kind": "signal",
                            "label": "Signal",
                            "at": "2026-09-27T14:05:00+00:00",
                            "side": "buy",
                            "price": None,
                            "href": "/operator/history/orders/buy",
                        },
                        {
                            "id": "fill",
                            "kind": "fill",
                            "label": "Fill",
                            "at": "2026-09-27T14:20:00+00:00",
                            "side": "sell",
                            "price": "102",
                            "href": "/operator/history/orders/sell",
                        },
                    ],
                }
            ],
        },
    }
    view = build_markets_view(payload, form={})
    tile = view["tiles"][0]
    assert [marker["side"] for marker in tile["markers"]] == ["buy", "sell"]
    assert [row["marker"] for row in tile["activity"]["rows"]] == ["▲", "▼"]
    assert all(
        row["href"].startswith("/operator/history/orders/") for row in tile["activity"]["rows"]
    )

    unavailable = dict(payload)
    unavailable["activity"] = {
        "status": "unavailable",
        "reason": "activity history could not be read",
        "symbols": [],
    }
    tile = build_markets_view(unavailable, form={})["tiles"][0]
    assert tile["activity"]["status"] == "unavailable"
    assert "could not be read" in tile["activity"]["reason"]


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
    assert '<script src="/operator/static/lightweight-charts.js" defer></script>' in body
    assert '<script src="/operator/static/markets.js" defer></script>' in body
    assert "<script>" not in body.lower()
    assert "<iframe" not in body.lower()
    assert "<img" not in body.lower()
    assert "https://" not in body.replace("https://www.tradingview.com/", "")


def test_vendored_lightweight_charts_build_has_the_recorded_checksum_and_notices():
    root = Path(__file__).resolve().parents[1] / "api" / "static" / "markets"
    library = root / "lightweight-charts.standalone.production.js"
    digest = hashlib.sha256(library.read_bytes().replace(b"\r\n", b"\n")).hexdigest().upper()
    metadata = (root / "README.md").read_text(encoding="utf-8")
    assert "Version: `5.2.1`" in metadata
    assert f"SHA-256: `{digest}`" in metadata
    assert "Apache License" in (root / "lightweight-charts.LICENSE").read_text(encoding="utf-8")
    assert "TradingView Lightweight Charts" in (root / "lightweight-charts.NOTICE").read_text(
        encoding="utf-8"
    )


@pytest.mark.asyncio
async def test_local_library_route_has_javascript_content_type_and_immutable_cache_headers(
    tmp_path,
):
    application = app_with_watchlist(tmp_path, "BTC-USD")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.get("/operator/static/lightweight-charts.js")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/javascript")
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert b"LightweightCharts" in response.content
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        module = await client.get("/operator/static/markets.js")
    assert module.status_code == 200
    assert module.headers["content-type"].startswith("application/javascript")
    assert b"data-market-tile" in module.content


@pytest.mark.asyncio
async def test_markets_page_has_same_origin_csp_and_keeps_the_svg_fallback(tmp_path):
    response = await get_page(app_with_watchlist(tmp_path, "BTC-USD"))
    assert response.headers["content-security-policy"] == (
        "default-src 'none'; script-src 'self'; connect-src 'self'; "
        "style-src 'unsafe-inline'; img-src 'self' data:; font-src 'self'; "
        "base-uri 'none'; form-action 'self'; frame-ancestors 'none'; object-src 'none'"
    )
    assert 'class="market-line-svg"' in response.text
    assert "data-market-enhancement" in response.text


@pytest.mark.asyncio
async def test_other_operator_pages_remain_javascript_free(tmp_path):
    application = app_with_watchlist(tmp_path, "BTC-USD")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.get("/operator", headers=HTML)
    assert response.status_code == 200
    assert "<script" not in response.text.lower()


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
