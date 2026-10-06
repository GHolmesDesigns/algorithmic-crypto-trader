"""Itemize why a projected balance did or did not equal the broker's.

Reconciliation compares two numbers and says only that they differ. Given the broker's balances
at the start of a window, the fills recorded inside it, and the broker's balances at its end,
this lays each asset's ``available`` out as the pieces the ledger added up: the starting balance,
every fill's quantity or ``quantity × price`` and fee, and the rounding to the venue's balance
unit. It calls ``apply_fills`` for the projection, so the rounding shown is the rounding the
reconciler actually applied, never a second implementation that could drift from it.

Pure arithmetic over values already in hand: it reads no broker or store and writes nothing.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal

from core.logging import fill_reference
from core.models import Fill, OrderSide

from portfolio.ledger import apply_fills
from portfolio.reconciliation import PortfolioState

ZERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class Item:
    """One signed contribution to an asset's ``available``."""

    label: str
    amount: Decimal


@dataclass(frozen=True, slots=True)
class AssetExplanation:
    asset: str
    before: Decimal
    items: tuple[Item, ...]
    # ``before`` plus every item, at full precision.
    exact: Decimal
    # What rounding to the venue's unit added to ``exact``; zero when the asset is not rounded.
    rounding: Decimal
    projected: Decimal
    broker: Decimal
    # Broker minus projected. Zero means the window reconciled for this asset.
    difference: Decimal


def explain_window(
    before: PortfolioState,
    after: PortfolioState,
    fills: Iterable[Fill],
    *,
    balance_increments: Mapping[str, Decimal] | None = None,
) -> tuple[AssetExplanation, ...]:
    """Explain each asset the ``fills`` moved, from ``before`` to the broker's ``after``.

    Raises ``ValueError`` when the projection goes negative, as ``apply_fills`` does.
    """

    ordered = sorted(fills, key=lambda item: (item.occurred_at, item.fill_id))
    projected = _available(apply_fills(before, ordered, balance_increments=balance_increments))
    starting = _available(before)
    reported = _available(after)

    items: dict[str, list[Item]] = {}
    for fill in ordered:
        base, quote = fill.symbol.split("-", maxsplit=1)
        reference = fill_reference(fill.fill_id)
        sign = Decimal("1") if fill.side is OrderSide.BUY else Decimal("-1")
        items.setdefault(base, []).append(Item(f"fill {reference} quantity", sign * fill.quantity))
        items.setdefault(quote, []).append(
            Item(f"fill {reference} quantity × price", -sign * fill.quantity * fill.price)
        )
        items.setdefault(fill.fee_asset, []).append(Item(f"fill {reference} fee", -fill.fee))

    explanations = []
    for asset in sorted(items):
        start = starting.get(asset, ZERO)
        exact = start + sum((item.amount for item in items[asset]), ZERO)
        final = projected.get(asset, ZERO)
        broker = reported.get(asset, ZERO)
        explanations.append(
            AssetExplanation(
                asset=asset,
                before=start,
                items=tuple(items[asset]),
                exact=exact,
                rounding=final - exact,
                projected=final,
                broker=broker,
                difference=broker - final,
            )
        )
    return tuple(explanations)


def _available(state: PortfolioState) -> dict[str, Decimal]:
    return {balance.asset: balance.available for balance in state.balances}
