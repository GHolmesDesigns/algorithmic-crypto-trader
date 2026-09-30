"""Record the account's equity on a schedule, so every UTC day has an end-of-day value.

The value uses the definition the loss-limit gate uses (``portfolio.valuation``):
quote-asset dollars, free and held, plus each held coin at the venue's bid.

A holding is valued in one of three ways:

- ``fresh``: the venue answered a quote now; that bid is used and stored as the coin's
  last-known price, with the quote's own observed time.
- ``last_known``: the quote failed, but an earlier price is stored; that price is used
  and the snapshot records the holding and the price's age, measured from its observed
  time. A failed quote never overwrites a stored price.
- ``unpriced``: no quote and no stored price. The snapshot is partial and carries no
  equity value: a coin that cannot be priced is never counted as zero.

Sampling only reads from the broker and only writes the equity tables. It never raises:
a failure leaves the day's field missing, and cannot halt or start trading.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol
from uuid import uuid4

from brokers.interface import BrokerInterface
from core.models import Balance, Position, utc_now
from db.models import EquityHoldingRecord, EquitySnapshotRecord, LastPriceRecord
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from portfolio.reconciliation import PortfolioState
from portfolio.valuation import (
    equity_value,
    held_positions,
    mark_from_quote,
    position_value,
    quote_cash,
)

logger = logging.getLogger(__name__)

FRESH = "fresh"
LAST_KNOWN = "last_known"
UNPRICED = "unpriced"


@dataclass(frozen=True, slots=True)
class LastPrice:
    symbol: str
    bid: Decimal
    observed_at: datetime
    source: str


@dataclass(frozen=True, slots=True)
class ValuedHolding:
    symbol: str
    quantity: Decimal
    basis: str
    price: Decimal | None = None
    observed_at: datetime | None = None
    age_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class EquitySample:
    as_of: datetime
    equity: Decimal | None
    holdings: tuple[ValuedHolding, ...]

    @property
    def partial(self) -> bool:
        return self.equity is None


class LastPriceStore(Protocol):
    def record(self, price: LastPrice) -> None: ...

    def latest(self, symbols: tuple[str, ...]) -> dict[str, LastPrice]: ...


class EquityStore(Protocol):
    def save(self, sample: EquitySample, *, source: str) -> None: ...


class SqlAlchemyEquityStore:
    """Persists the latest price per symbol and each equity snapshot."""

    def __init__(self, session_factory) -> None:
        self.session_factory = session_factory

    def record(self, price: LastPrice) -> None:
        with self.session_factory() as session:
            row = session.get(LastPriceRecord, price.symbol)
            if row is None:
                session.add(
                    LastPriceRecord(
                        symbol=price.symbol,
                        bid=price.bid,
                        observed_at=price.observed_at,
                        source=price.source,
                    )
                )
            elif _aware(row.observed_at) <= price.observed_at:
                row.bid, row.observed_at, row.source = price.bid, price.observed_at, price.source
            session.commit()

    def latest(self, symbols: tuple[str, ...]) -> dict[str, LastPrice]:
        if not symbols:
            return {}
        with self.session_factory() as session:
            rows = session.scalars(
                select(LastPriceRecord).where(LastPriceRecord.symbol.in_(symbols))
            ).all()
            return {
                row.symbol: LastPrice(row.symbol, row.bid, _aware(row.observed_at), row.source)
                for row in rows
            }

    def save(self, sample: EquitySample, *, source: str) -> None:
        snapshot_id = uuid4()
        with self.session_factory() as session:
            session.add(
                EquitySnapshotRecord(
                    snapshot_id=snapshot_id,
                    equity=sample.equity,
                    as_of=sample.as_of,
                    source=source,
                    partial=sample.partial,
                )
            )
            session.flush()  # the snapshot row exists before its holdings reference it
            session.add_all(
                [
                    EquityHoldingRecord(
                        holding_id=uuid4(),
                        snapshot_id=snapshot_id,
                        symbol=holding.symbol,
                        basis=holding.basis,
                        quantity=holding.quantity,
                        price=holding.price,
                        price_observed_at=holding.observed_at,
                        price_age_seconds=holding.age_seconds,
                    )
                    for holding in sample.holdings
                    if holding.basis != FRESH
                ]
            )
            session.commit()


class EquitySampler:
    def __init__(
        self,
        broker: BrokerInterface,
        prices: LastPriceStore,
        store: EquityStore,
        *,
        quote_asset: str = "USD",
        source: str = "broker",
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.broker = broker
        self.prices = prices
        self.store = store
        self.quote_asset = quote_asset
        self.source = source
        self.clock = clock

    async def sample(self, state: PortfolioState | None = None) -> EquitySample | None:
        """Value the account and record it; ``None`` when nothing could be recorded.

        ``state`` is a portfolio the caller already read consistently (the reconciler's
        authoritative state); without it the broker is read directly.
        """

        try:
            if state is None:
                balances = await self.broker.get_balances()
                positions = await self.broker.get_positions()
            else:
                balances, positions = state.balances, state.positions
            sample = await self._value(balances, positions)
        except Exception:
            logger.exception("equity could not be valued; the day's equity stays unrecorded")
            return None
        try:
            self.store.save(sample, source=self.source)
        except (SQLAlchemyError, OSError):
            logger.exception("equity snapshot could not be saved")
            return None
        return sample

    async def _value(
        self, balances: tuple[Balance, ...], positions: tuple[Position, ...]
    ) -> EquitySample:
        held = held_positions(positions)
        _, total_cash = quote_cash(balances, self.quote_asset)
        stored = self._stored_prices(tuple(held))
        now = self.clock()
        exposure = Decimal("0")
        unpriced = False
        holdings: list[ValuedHolding] = []
        for symbol, position in held.items():
            mark = await self._fresh_mark(symbol)
            if mark is not None:
                holdings.append(ValuedHolding(symbol, position.quantity, FRESH, mark))
            elif symbol in stored:
                last = stored[symbol]
                age = max(0, int((now - last.observed_at).total_seconds()))
                mark = last.bid
                holdings.append(
                    ValuedHolding(
                        symbol, position.quantity, LAST_KNOWN, mark, last.observed_at, age
                    )
                )
            else:
                unpriced = True
                holdings.append(ValuedHolding(symbol, position.quantity, UNPRICED))
                continue
            exposure += position_value(position, mark)
        equity = None if unpriced else equity_value(total_cash, exposure)
        return EquitySample(as_of=now, equity=equity, holdings=tuple(holdings))

    async def _fresh_mark(self, symbol: str) -> Decimal | None:
        try:
            quote = await self.broker.get_quote(symbol)
        except Exception:
            logger.warning("no quote to value the %s holding", symbol)
            return None
        mark = mark_from_quote(quote, symbol)
        if mark is None or mark <= 0:
            return None
        try:
            self.prices.record(LastPrice(symbol, mark, _aware(quote.as_of), quote.source[:64]))
        except (SQLAlchemyError, OSError):
            logger.warning("the %s price could not be stored", symbol)
        return mark

    def _stored_prices(self, symbols: tuple[str, ...]) -> dict[str, LastPrice]:
        try:
            return self.prices.latest(symbols)
        except (SQLAlchemyError, OSError):
            logger.warning("stored prices could not be read")
            return {}

    async def run(
        self, stop: asyncio.Event, interval_seconds: float, lock: asyncio.Lock | None = None
    ) -> None:
        """Sample every interval until ``stop`` is set, reading under the trading lock."""

        if interval_seconds <= 0:
            raise ValueError("equity sampling interval must be positive")
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
            except TimeoutError:
                if lock is None:
                    await self.sample()
                else:
                    async with lock:
                        await self.sample()


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)
