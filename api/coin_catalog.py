"""In-memory cache and local search for the public, watch-only Coinbase catalog."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from brokers.coin_catalog import read_coinbase_catalog
from brokers.coinbase_catalog import CoinbaseCatalogProduct

FRESH_FOR = timedelta(minutes=15)
REFRESH_DEADLINE_SECONDS = 10.0
MAX_SEARCH_RESULTS = 20
MAX_QUERY_LENGTH = 80
SOURCE = "Coinbase public market data"


class CoinbaseCatalog:
    """Cache successful complete refreshes and share one concurrent refresh task."""

    def __init__(self, reader: Any) -> None:
        self._reader = reader
        self._products: tuple[CoinbaseCatalogProduct, ...] | None = None
        self._loaded_at: datetime | None = None
        self._refresh_error: str | None = None
        self._refresh_task: asyncio.Task[None] | None = None

    def view(self, query: str = "", *, now: datetime | None = None) -> dict[str, Any]:
        """Return display-safe state and at most 20 locally matched products."""

        moment = now or datetime.now(UTC)
        loaded_at = self._loaded_at
        products = self._products
        if loaded_at is None:
            status = "unavailable" if self._refresh_error else "not_loaded"
            as_of = None
            detail = self._refresh_error or "Name search has not been loaded."
        else:
            as_of = loaded_at.strftime("%Y-%m-%d %H:%M:%S UTC")
            stale = moment - loaded_at >= FRESH_FOR
            status = "stale" if stale else "available"
            detail = self._refresh_error or ""
        query_text = query.strip()[:MAX_QUERY_LENGTH]
        matches = _search(products or (), query_text) if query_text else ()
        return {
            "status": status,
            "source": SOURCE,
            "as_of": as_of,
            "detail": detail,
            "query": query_text,
            "matches": [
                {
                    "product_id": item.product_id,
                    "base_symbol": item.base_symbol,
                    "display_name": item.display_name,
                    "quote_currency": item.quote_currency,
                    "availability": item.availability,
                    "source": SOURCE,
                    "as_of": as_of,
                }
                for item in matches
            ],
            "match_limit": MAX_SEARCH_RESULTS,
        }

    async def refresh(self) -> None:
        """Load only when the cache is stale; overlapping callers share one task."""

        if self._is_fresh(datetime.now(UTC)):
            return
        task = self._refresh_task
        if task is None:
            task = asyncio.create_task(self._load())
            self._refresh_task = task
        try:
            await asyncio.shield(task)
        finally:
            if task.done() and self._refresh_task is task:
                self._refresh_task = None

    def _is_fresh(self, now: datetime) -> bool:
        return self._loaded_at is not None and now - self._loaded_at < FRESH_FOR

    async def _load(self) -> None:
        try:
            async with asyncio.timeout(REFRESH_DEADLINE_SECONDS):
                products = await read_coinbase_catalog(self._reader)
        except TimeoutError:
            self._refresh_error = "The 10-second catalog refresh limit expired."
            return
        except httpx.HTTPStatusError as exc:
            self._refresh_error = (
                "Coinbase is rate limiting the public catalog."
                if exc.response.status_code == 429
                else "Coinbase's public catalog is unavailable."
            )
            return
        except Exception:
            self._refresh_error = (
                "Coinbase returned an unavailable, malformed or incomplete catalog."
            )
            return
        self._products = products
        self._loaded_at = datetime.now(UTC)
        self._refresh_error = None


def _search(
    products: tuple[CoinbaseCatalogProduct, ...], query: str
) -> tuple[CoinbaseCatalogProduct, ...]:
    needle = query.casefold()
    ranked: list[tuple[int, str, CoinbaseCatalogProduct]] = []
    for product in products:
        fields = (
            product.product_id.casefold(),
            product.base_symbol.casefold(),
            product.display_name.casefold(),
        )
        if not any(needle in field for field in fields):
            continue
        score = 0 if needle in fields else 1
        ranked.append((score, product.product_id, product))
    ranked.sort(key=lambda item: (item[0], item[1]))
    return tuple(item[2] for item in ranked[:MAX_SEARCH_RESULTS])
