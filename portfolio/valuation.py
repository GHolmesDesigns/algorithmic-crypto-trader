"""The one definition of account equity, shared by the risk gates and the daily record.

Equity is the quote asset's dollars, free and held (a resting buy reserves dollars
without losing them), plus every held coin at the venue's current bid. The loss-limit
gate in ``app.trading`` and the equity sampler in ``portfolio.equity`` both build on
these functions, so the two cannot drift apart.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from core.models import Balance, Position, Quote

# A last-known price older than this keeps a day from passing the soak digest.
LAST_PRICE_MAX_AGE = timedelta(hours=24)


def quote_cash(balances: tuple[Balance, ...], quote_asset: str) -> tuple[Decimal, Decimal]:
    """Quote-asset dollars free to spend, and in total with those a resting buy holds."""

    cash = next((item for item in balances if item.asset == quote_asset), None)
    if cash is None:
        return Decimal("0"), Decimal("0")
    return cash.available, cash.available + cash.hold


def held_positions(positions: tuple[Position, ...]) -> dict[str, Position]:
    return {item.symbol: item for item in positions if item.quantity != 0}


def mark_from_quote(quote: Quote, symbol: str) -> Decimal | None:
    """The bid a holding is valued at; ``None`` when the quote is for another symbol."""

    return quote.bid if quote.symbol == symbol else None


def position_value(position: Position, mark: Decimal) -> Decimal:
    return abs(position.quantity) * mark


def equity_value(total_cash: Decimal, exposure: Decimal) -> Decimal:
    return total_cash + exposure
