"""Project the expected portfolio from a broker baseline plus locally recorded fills."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from decimal import ROUND_DOWN, Decimal

from core.models import Balance, Fill, OrderSide, Position, utc_now

from portfolio.reconciliation import PortfolioState

ZERO = Decimal("0")


def apply_fills(
    baseline: PortfolioState,
    fills: Iterable[Fill],
    *,
    as_of: datetime | None = None,
    balance_increments: Mapping[str, Decimal] | None = None,
) -> PortfolioState:
    """Return the positions and balances the broker should report after ``fills``.

    Accounting follows the local simulator: a buy adds base asset at a
    size-weighted average price and spends quote asset plus fee; a sell does the
    reverse and keeps the average price. A position whose cost basis is unknown
    (``None``) stays unknown after a buy rather than averaging against a
    placeholder. Holds are carried over unchanged. A projection that goes
    negative is a local accounting failure and raises.

    A venue settles a balance in a fixed unit, and a fill's notional can carry more
    decimals than that. ``balance_increments`` names the unit per asset. Every amount
    a fill moves in a listed asset (its quantity × price, its fee) is rounded toward
    zero to that unit, fill by fill, as the Gemini Sandbox settles it (#118, then the
    split sell of 2026-10-06 20:55 UTC). The exact comparison against the venue is then
    between two numbers of the same precision. An asset not listed, holds and positions
    are not rounded.
    """

    now = as_of or utc_now()
    increments = balance_increments or {}
    positions: dict[str, tuple[Decimal, Decimal | None]] = {
        item.symbol: (item.quantity, item.average_price) for item in baseline.positions
    }
    available = {item.asset: item.available for item in baseline.balances}
    # The same balances unrounded, so rounding a debit down never hides a shortfall.
    exact = dict(available)
    holds = {item.asset: item.hold for item in baseline.balances}
    for fill in sorted(fills, key=lambda item: (item.occurred_at, item.fill_id)):
        base, quote = fill.symbol.split("-", maxsplit=1)
        notional = fill.quantity * fill.price
        quantity, average = positions.get(fill.symbol, (ZERO, None))
        sign = Decimal("1") if fill.side is OrderSide.BUY else Decimal("-1")
        for asset, amount in (
            (base, sign * fill.quantity),
            (quote, -sign * notional),
            (fill.fee_asset, -fill.fee),
        ):
            increment = increments.get(asset)
            settled = amount if increment is None else _toward_zero(amount, increment)
            exact[asset] = exact.get(asset, ZERO) + amount
            available[asset] = available.get(asset, ZERO) + settled
        if fill.side is OrderSide.BUY:
            total = quantity + fill.quantity
            if quantity == 0:
                average = notional / total
            elif average is not None:
                average = (quantity * average + notional) / total
            # else: the cost of what was already held is unknown, so the blend is too.
            quantity = total
        else:
            quantity -= fill.quantity
        positions[fill.symbol] = (quantity, average)
    for asset, amount in available.items():
        if amount < 0 or exact[asset] < 0:
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


def _toward_zero(amount: Decimal, increment: Decimal) -> Decimal:
    """``amount`` in whole ``increment`` units, dropping the remainder as the venue does."""

    return (amount / increment).to_integral_value(rounding=ROUND_DOWN) * increment
