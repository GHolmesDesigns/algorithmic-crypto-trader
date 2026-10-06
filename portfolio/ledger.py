"""Project the expected portfolio from a broker baseline plus locally recorded fills."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

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

    A venue reports a balance in a fixed unit, and a fill's notional can carry more
    decimals than that. ``balance_increments`` names the unit per asset, and the
    projected ``available`` of a listed asset that a fill moved is rounded half up to
    it, so the exact comparison against the venue is between two numbers of the same
    precision (#118). An asset no fill moved, or one not listed, is not rounded, and
    neither are holds or positions.
    """

    now = as_of or utc_now()
    positions: dict[str, tuple[Decimal, Decimal | None]] = {
        item.symbol: (item.quantity, item.average_price) for item in baseline.positions
    }
    available = {item.asset: item.available for item in baseline.balances}
    holds = {item.asset: item.hold for item in baseline.balances}
    moved: set[str] = set()
    for fill in sorted(fills, key=lambda item: (item.occurred_at, item.fill_id)):
        base, quote = fill.symbol.split("-", maxsplit=1)
        moved.update((base, quote, fill.fee_asset))
        notional = fill.quantity * fill.price
        quantity, average = positions.get(fill.symbol, (ZERO, None))
        sign = Decimal("1") if fill.side is OrderSide.BUY else Decimal("-1")
        available[base] = available.get(base, ZERO) + sign * fill.quantity
        available[quote] = available.get(quote, ZERO) - sign * notional
        available[fill.fee_asset] = available.get(fill.fee_asset, ZERO) - fill.fee
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
        if amount < 0:
            raise ValueError(f"projected {asset} balance is negative")
    # After the check, so a projection that is negative at full precision still raises.
    for asset, increment in (balance_increments or {}).items():
        if asset in moved:
            units = (available[asset] / increment).to_integral_value(rounding=ROUND_HALF_UP)
            available[asset] = units * increment
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
