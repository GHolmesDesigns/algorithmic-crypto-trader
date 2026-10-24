"""Sanitize Coinbase public spot products for the operator's watch-only picker."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


class CoinbaseCatalogError(ValueError):
    """A product page is malformed or incomplete and cannot be cached as complete."""


@dataclass(frozen=True, slots=True)
class CoinbaseCatalogProduct:
    product_id: str
    base_symbol: str
    display_name: str
    quote_currency: str
    availability: str


_PRODUCT_PAIR = re.compile(r"^([A-Z0-9]+)-([A-Z0-9]+)$")


def sanitize_coinbase_product(raw: Any) -> CoinbaseCatalogProduct | None:
    """Return only eligible USD spot fields; malformed records fail the full refresh."""

    if not isinstance(raw, dict):
        raise CoinbaseCatalogError("product row is not an object")
    product_id = _required_text(raw, "product_id").upper()
    base_symbol = _required_text(raw, "base_display_symbol").upper()
    display_name = str(raw.get("base_name") or raw.get("display_name") or "").strip()
    quote_currency = _required_text(raw, "quote_currency_id").upper()
    status = _required_text(raw, "status").lower()
    product_type = _required_text(raw, "product_type").upper()
    trading_disabled = _required_bool(raw, "trading_disabled")
    is_disabled = _required_bool(raw, "is_disabled")

    product_match = _PRODUCT_PAIR.fullmatch(product_id)
    if not display_name:
        raise CoinbaseCatalogError("product row omitted its public display name")
    if (
        not base_symbol
        or product_match is None
        or product_match.group(1) != base_symbol
        or product_match.group(2) != quote_currency
    ):
        raise CoinbaseCatalogError("product id, base symbol and quote currency did not match")
    if (
        quote_currency != "USD"
        or product_type != "SPOT"
        or status != "online"
        or trading_disabled
        or is_disabled
    ):
        return None
    return CoinbaseCatalogProduct(
        product_id=product_id,
        base_symbol=base_symbol,
        display_name=display_name,
        quote_currency=quote_currency,
        availability=status,
    )


def _required_text(raw: dict[str, Any], field: str) -> str:
    value = raw.get(field)
    if not isinstance(value, str) or not value.strip():
        raise CoinbaseCatalogError(f"product row omitted {field}")
    return value.strip()


def _required_bool(raw: dict[str, Any], field: str) -> bool:
    value = raw.get(field)
    if not isinstance(value, bool):
        raise CoinbaseCatalogError(f"product row omitted {field}")
    return value
