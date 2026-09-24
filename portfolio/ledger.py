"""Project the expected portfolio from a broker baseline plus locally recorded fills."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal

from core.models import Balance, Fill, OrderSide, Position, utc_now

from portfolio.reconciliation import PortfolioState

ZERO = Decimal("0")


def apply_fills(
    baseline: PortfolioState, fills: Iterable[Fill], *, as_of: datetime | None = None
) -> PortfolioState:
    """Return the positions and balances the broker should report after ``fills``.

    Accounting follows the local simulator: a buy adds base asset at a
    size-weighted average price and spends quote asset plus fee; a sell does the
    reverse and keeps the average price. Holds are carried over unchanged. A
    projection that goes negative is a local accounting failure and raises.
    """

    now = as_of or utc_now()
    positions = {item.symbol: (item.quantity, item.average_price) for item in baseline.positions}
    available = {item.asset: item.available for item in baseline.balances}
    holds = {item.asset: item.hold for item in baseline.balances}
    for fill in sorted(fills, key=lambda item: (item.occurred_at, item.fill_id)):
        base, quote = fill.symbol.split("-", maxsplit=1)
        notional = fill.quantity * fill.price
        quantity, average = positions.get(fill.symbol, (ZERO, ZERO))
        sign = Decimal("1") if fill.side is OrderSide.BUY else Decimal("-1")
        available[base] = available.get(base, ZERO) + sign * fill.quantity
        available[quote] = available.get(quote, ZERO) - sign * notional
        available[fill.fee_asset] = available.get(fill.fee_asset, ZERO) - fill.fee
        if fill.side is OrderSide.BUY:
            total = quantity + fill.quantity
            average = (quantity * average + notional) / total
            quantity = total
        else:
            quantity -= fill.quantity
        positions[fill.symbol] = (quantity, average)
    for asset, amount in available.items():
        if amount < 0:
            raise ValueError(f"projected {asset} balance is negative")
    return PortfolioState(
        orders=baseline.orders,
        fills=baseline.fills,
        positions=tuple(
            Position(symbol=symbol, quantity=quantity, average_price=average, as_of=now)
            for symbol, (quantity, average) in sorted(positions.items())
            if quantity != 0
        ),
        balances=tuple(
            Balance(asset=asset, available=amount, hold=holds.get(asset, ZERO), as_of=now)
            for asset, amount in sorted(available.items())
            if amount != 0 or holds.get(asset, ZERO) != 0
        ),
    )
