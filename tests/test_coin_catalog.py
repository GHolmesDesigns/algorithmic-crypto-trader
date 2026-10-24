"""Bounded, read-only watch-coin catalog behavior."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from api.coin_catalog import MAX_SEARCH_RESULTS, CoinbaseCatalog
from brokers.coin_catalog import MAX_PRODUCTS, PAGE_SIZE, read_coinbase_catalog
from brokers.coinbase_catalog import CoinbaseCatalogError, sanitize_coinbase_product


def raw_product(**overrides):
    return {
        "product_id": "BTC-USD",
        "base_display_symbol": "BTC",
        "base_name": "Bitcoin",
        "quote_currency_id": "USD",
        "product_type": "SPOT",
        "status": "online",
        "trading_disabled": False,
        "is_disabled": False,
        "price": "65000.00",
        "base_increment": "0.00000001",
        **overrides,
    }


class ProductReader:
    def __init__(self, pages, error=None, *, delay=0):
        self.pages = pages
        self.error = error
        self.delay = delay
        self.calls: list[tuple[int, int]] = []

    async def list_public_products_page(self, *, limit, offset):
        self.calls.append((limit, offset))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.pages[offset // PAGE_SIZE]


def page(rows, *, has_next=False, total=None):
    result = {"products": rows, "pagination": {"has_next": has_next}}
    if total is not None:
        result["num_products"] = total
    return result


async def test_catalog_resolves_name_symbol_and_canonical_id_from_sanitized_fields():
    reader = ProductReader(
        [
            page(
                [
                    raw_product(),
                    raw_product(
                        product_id="WBTC-USD",
                        base_display_symbol="WBTC",
                        base_name="Wrapped Bitcoin",
                    ),
                ],
                total=2,
            )
        ]
    )
    cache = CoinbaseCatalog(reader)
    await cache.refresh()

    for query in ("bitcoin", "BTC", "btc", "bTc-uSd"):
        result = cache.view(query)
        assert result["source"] == "Coinbase public market data"
        assert result["as_of"]
        assert result["matches"][0]["product_id"] == "BTC-USD"
        assert set(result["matches"][0]) == {
            "product_id",
            "base_symbol",
            "display_name",
            "quote_currency",
            "availability",
            "source",
            "as_of",
        }
    assert [item["product_id"] for item in cache.view("bitcoin")["matches"]] == [
        "BTC-USD",
        "WBTC-USD",
    ]


@pytest.mark.parametrize(
    "overrides",
    [
        {"product_type": "FUTURE"},
        {"product_id": "BTC-USDC", "quote_currency_id": "USDC"},
        {"status": "offline"},
        {"trading_disabled": True},
        {"is_disabled": True},
    ],
)
def test_catalog_excludes_unsupported_non_usd_and_ineligible_products(overrides):
    assert sanitize_coinbase_product(raw_product(**overrides)) is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"base_name": ""},
        {"product_id": "OTHER-USD"},
        {"base_display_symbol": ""},
        {"trading_disabled": "false"},
        {"status": None},
    ],
)
async def test_malformed_catalog_rows_refuse_the_whole_refresh(overrides):
    reader = ProductReader([page([raw_product(**overrides)])])
    with pytest.raises(CoinbaseCatalogError):
        await read_coinbase_catalog(reader)


async def test_catalog_paginates_within_ten_requests_and_refuses_truncation():
    rows = [
        raw_product(
            product_id=f"C{index}-USD",
            base_display_symbol=f"C{index}",
            base_name=f"Coin {index}",
        )
        for index in range(PAGE_SIZE)
    ]
    pages = [page(rows, has_next=True, total=MAX_PRODUCTS + 1)]
    cache = CoinbaseCatalog(ProductReader(pages))
    await cache.refresh()
    assert cache.view()["status"] == "unavailable"
    assert cache.view()["matches"] == []
    assert len(cache._reader.calls) == 1


async def test_catalog_ten_full_pages_without_end_marker_are_unavailable():
    pages = []
    for page_number in range(10):
        offset = page_number * PAGE_SIZE
        rows = [
            raw_product(
                product_id=f"C{index}-USD",
                base_display_symbol=f"C{index}",
                base_name=f"Coin {index}",
            )
            for index in range(offset, offset + PAGE_SIZE)
        ]
        pages.append(page(rows, has_next=True))
    reader = ProductReader(pages)
    with pytest.raises(CoinbaseCatalogError):
        await read_coinbase_catalog(reader)
    assert len(reader.calls) == 10
    assert reader.calls[-1] == (PAGE_SIZE, MAX_PRODUCTS - PAGE_SIZE)


async def test_catalog_shares_one_concurrent_refresh_and_reuses_fresh_cache():
    reader = ProductReader([page([raw_product()], total=1)], delay=0.02)
    cache = CoinbaseCatalog(reader)
    await asyncio.gather(*(cache.refresh() for _ in range(5)))
    await cache.refresh()
    assert reader.calls == [(PAGE_SIZE, 0)]


async def test_catalog_timeout_and_rate_limit_keep_stale_successful_data():
    reader = ProductReader([page([raw_product()], total=1)])
    cache = CoinbaseCatalog(reader)
    await cache.refresh()
    cache._loaded_at = datetime.now(UTC) - timedelta(minutes=16)
    reader.error = httpx.HTTPStatusError(
        "rate limited",
        request=httpx.Request("GET", "https://api.coinbase.com"),
        response=httpx.Response(429),
    )

    await cache.refresh()
    result = cache.view("BTC")
    assert result["status"] == "stale"
    assert result["detail"] == "Coinbase is rate limiting the public catalog."
    assert result["matches"][0]["product_id"] == "BTC-USD"


async def test_malformed_refresh_keeps_last_success_and_marks_it_stale():
    reader = ProductReader([page([raw_product()], total=1)])
    cache = CoinbaseCatalog(reader)
    await cache.refresh()
    cache._loaded_at = datetime.now(UTC) - timedelta(minutes=16)
    reader.error = ValueError("malformed provider response")

    await cache.refresh()
    result = cache.view("BTC")
    assert result["status"] == "stale"
    assert "malformed or incomplete" in result["detail"]
    assert result["matches"][0]["product_id"] == "BTC-USD"


async def test_catalog_deadline_is_explicit_and_empty_cache_stays_unavailable(monkeypatch):
    monkeypatch.setattr("api.coin_catalog.REFRESH_DEADLINE_SECONDS", 0.01)
    reader = ProductReader([page([raw_product()], total=1)], delay=0.05)
    cache = CoinbaseCatalog(reader)
    await cache.refresh()
    assert cache.view()["status"] == "unavailable"
    assert "10-second" in cache.view()["detail"]


async def test_search_caps_matches_and_does_not_fetch():
    rows = [
        raw_product(
            product_id=f"C{index}-USD",
            base_display_symbol=f"C{index}",
            base_name=f"Coin {index}",
        )
        for index in range(30)
    ]
    reader = ProductReader([page(rows, total=30)])
    cache = CoinbaseCatalog(reader)
    result = cache.view("coin")
    assert result["status"] == "not_loaded"
    assert result["matches"] == []
    await cache.refresh()
    result = cache.view("coin")
    assert len(result["matches"]) == MAX_SEARCH_RESULTS == 20
    assert reader.calls == [(PAGE_SIZE, 0)]


async def test_expired_cache_is_labeled_stale_before_refresh():
    reader = ProductReader([page([raw_product()], total=1)])
    cache = CoinbaseCatalog(reader)
    await cache.refresh()
    assert cache.view(now=datetime.now(UTC) + timedelta(minutes=16))["status"] == "stale"
