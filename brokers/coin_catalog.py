"""Bounded, complete-only reader for Coinbase's public spot product pages."""

from __future__ import annotations

from typing import Any, Protocol

from brokers.coinbase_catalog import (
    CoinbaseCatalogError,
    CoinbaseCatalogProduct,
    sanitize_coinbase_product,
)

PAGE_SIZE = 500
MAX_REQUESTS = 10
MAX_PRODUCTS = PAGE_SIZE * MAX_REQUESTS


class PublicProductReader(Protocol):
    async def list_public_products_page(self, *, limit: int, offset: int) -> dict[str, Any]: ...


async def read_coinbase_catalog(reader: PublicProductReader) -> tuple[CoinbaseCatalogProduct, ...]:
    """Read at most 5000 products; reject pagination that cannot be proven complete."""

    products: dict[str, CoinbaseCatalogProduct] = {}
    expected_total: int | None = None
    for page_number in range(MAX_REQUESTS):
        offset = page_number * PAGE_SIZE
        payload = await reader.list_public_products_page(limit=PAGE_SIZE, offset=offset)
        rows = payload.get("products")
        pagination = payload.get("pagination")
        if not isinstance(rows, list) or not isinstance(pagination, dict):
            raise CoinbaseCatalogError("Coinbase product page omitted products or pagination")
        has_next = pagination.get("has_next")
        if not isinstance(has_next, bool):
            raise CoinbaseCatalogError("Coinbase product page omitted pagination status")
        total = payload.get("num_products")
        if total is not None:
            if isinstance(total, bool) or not isinstance(total, int) or total < 0:
                raise CoinbaseCatalogError("Coinbase product count is malformed")
            if expected_total is not None and total != expected_total:
                raise CoinbaseCatalogError("Coinbase product count changed during refresh")
            expected_total = total
            if total > MAX_PRODUCTS:
                raise CoinbaseCatalogError("Coinbase product catalog exceeds the 5000 product cap")
        if has_next and len(rows) != PAGE_SIZE:
            raise CoinbaseCatalogError("Coinbase product pagination skipped a partial page")
        if offset + len(rows) > MAX_PRODUCTS:
            raise CoinbaseCatalogError("Coinbase product catalog exceeds the 5000 product cap")
        for raw in rows:
            product = sanitize_coinbase_product(raw)
            if product is not None:
                if product.product_id in products:
                    raise CoinbaseCatalogError("Coinbase product page repeated a product")
                products[product.product_id] = product
        if not has_next:
            if expected_total is not None and offset + len(rows) < expected_total:
                raise CoinbaseCatalogError("Coinbase product pagination ended before its total")
            return tuple(sorted(products.values(), key=lambda item: item.product_id))
    raise CoinbaseCatalogError("Coinbase product pagination exceeded the 10 request cap")
