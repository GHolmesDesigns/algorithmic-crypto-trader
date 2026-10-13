"""A position's average price from our own saved fills (#162), for display only.

Gemini and Coinbase build positions from balances and report no cost basis, so a position
arrives with ``average_price=None``. When the fills this system saved account for the whole
held quantity, their size-weighted average is a real price, and this reader reports it. The
dashboard labels it "from our fills" so it is not mistaken for a venue-reported price.

The walk follows ``portfolio.ledger.apply_fills``: fills in ``(occurred_at, fill_id)`` order,
a buy sets the size-weighted average (``notional / quantity``, fees excluded), a sell reduces
the quantity and keeps the average. A sell that takes the quantity below zero means earlier
units came from outside our fills: the quantity is clamped to zero and the walk restarts
after that fill.

The average is reported only when the walk ends on exactly the broker's quantity. Anything
else (no fills, units our fills do not cover, units they record that the broker no longer
holds) is unknown with a plain reason, never a partly computed price and never zero.

The ``fills`` table does not store the asset a fee was charged in. A fee of zero cannot
matter. For a positive fee the walk is tried with the fee charged in the quote asset (nothing
leaves the held quantity) and charged in the base asset (the fee is netted out of the held
quantity); the reading counts only when its quantity matches the broker's exactly. That check
is what establishes the fee asset, not an assumption: the two readings differ by the fees
until a clamp restarts both together, so they cannot both match with different prices.

Reading is side-effect free: no broker call and no write. The result is never given to
reconciliation, equity, or the risk inputs, which keep the broker's own positions.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from core.models import OrderSide, Position
from db.models import FillRecord, OrderRecord
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

ZERO = Decimal("0")

NO_FILLS = "this system has no saved fills for it"
NOT_COVERED = "the position includes units our saved fills do not cover"
EXCESS = "our saved fills record more units than the broker holds"
UNREADABLE = "our saved fills could not be read"


@dataclass(frozen=True, slots=True)
class FillCost:
    """The derived average for one position, or the reason there is none.

    ``quantity`` is the broker quantity the figure was checked against, so a consumer can
    tell when it describes a position that has since changed.
    """

    symbol: str
    quantity: Decimal
    average_price: Decimal | None = None
    reason: str | None = None

    @classmethod
    def unknown(cls, symbol: str, quantity: Decimal, reason: str) -> FillCost:
        return cls(symbol, quantity, None, reason)


@dataclass(frozen=True, slots=True)
class _SavedFill:
    fill_id: str
    occurred_at: datetime
    side: OrderSide
    quantity: Decimal
    price: Decimal
    fee: Decimal


class FillCostReader:
    """Reads the saved fills for the positions that lack a venue price."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self.session_factory = session_factory

    def read(self, positions: Iterable[Position]) -> dict[str, FillCost]:
        """A ``FillCost`` per position that has no venue-reported average price."""

        wanted = {item.symbol: item.quantity for item in positions if item.average_price is None}
        if not wanted:
            return {}
        try:
            saved = self._saved_fills(sorted(wanted))
        except (SQLAlchemyError, ValueError):
            return {
                symbol: FillCost.unknown(symbol, quantity, UNREADABLE)
                for symbol, quantity in wanted.items()
            }
        return {
            symbol: cost_from_fills(symbol, quantity, saved.get(symbol, ()))
            for symbol, quantity in wanted.items()
        }

    def _saved_fills(self, symbols: Sequence[str]) -> dict[str, list[_SavedFill]]:
        with self.session_factory() as session:
            rows = session.execute(
                select(FillRecord, OrderRecord.symbol, OrderRecord.side)
                .join(OrderRecord, OrderRecord.order_id == FillRecord.order_id)
                .where(OrderRecord.symbol.in_(symbols), OrderRecord.side.is_not(None))
            ).all()
            grouped: dict[str, list[_SavedFill]] = {}
            for record, symbol, side in rows:
                if symbol is None or side is None:  # the query already leaves these out
                    continue
                grouped.setdefault(symbol, []).append(
                    _SavedFill(
                        fill_id=record.broker_fill_id,
                        occurred_at=record.occurred_at,
                        side=OrderSide(side),
                        quantity=record.quantity,
                        price=record.price,
                        fee=record.fee,
                    )
                )
        return grouped


def cost_from_fills(symbol: str, held: Decimal, fills: Sequence[_SavedFill]) -> FillCost:
    """The average price when ``fills`` account for exactly ``held`` units, else the reason."""

    if not fills:
        return FillCost.unknown(symbol, held, NO_FILLS)
    ordered = sorted(fills, key=lambda item: (item.occurred_at, item.fill_id))
    readings = [_walk(ordered, fee_in_base=False)]
    if any(item.fee > 0 for item in ordered):
        readings.append(_walk(ordered, fee_in_base=True))
    for quantity, average in readings:
        if quantity == held and average is not None:
            return FillCost(symbol, held, average)
    ours = readings[0][0]
    return FillCost.unknown(symbol, held, EXCESS if ours > held else NOT_COVERED)


def _walk(fills: Sequence[_SavedFill], *, fee_in_base: bool) -> tuple[Decimal, Decimal | None]:
    """The running quantity and average after every fill, in the order given."""

    quantity = ZERO
    average: Decimal | None = None
    for fill in fills:
        base_fee = fill.fee if fee_in_base else ZERO
        if fill.side is OrderSide.BUY:
            total = quantity + fill.quantity
            notional = fill.quantity * fill.price
            if quantity == 0 or average is None:
                average = notional / fill.quantity
            else:
                average = (quantity * average + notional) / total
            quantity = total - base_fee
        else:
            quantity -= fill.quantity + base_fee
        if quantity < 0:
            quantity, average = ZERO, None
    return quantity, average if quantity > 0 else None
