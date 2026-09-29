"""The saved watchlist: up to nine coins to chart, never to trade.

A watchlist entry has no path into trading. The trading symbols come from
``PAPER_SYMBOLS`` alone, and nothing in this module imports strategy, risk,
execution, or broker code. Every change is one transaction that also writes a
``system_events`` row, so a change cannot exist without its audit record.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import uuid4

import httpx
from core.models import utc_now
from db.models import SystemEventRecord, WatchlistRecord
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from data.coinbase import CoinbaseProduct, CoinbaseProductNotFound

WATCHLIST_LIMIT = 9
WATCHLIST_EVENT = "watchlist_change"
SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]+-USD$")


class WatchlistRefused(ValueError):
    """A change the watchlist will not make; the message is safe to show the operator."""


class ProductLookup(Protocol):
    async def get_product(self, product_id: str) -> CoinbaseProduct: ...


@dataclass(frozen=True, slots=True)
class WatchlistEntry:
    symbol: str
    position: int
    added_at: datetime
    added_by: str


def normalize_symbol(raw: str) -> str:
    symbol = raw.strip().upper()
    if not SYMBOL_PATTERN.fullmatch(symbol) or len(symbol) > 32:
        raise WatchlistRefused("Enter a Coinbase USD product such as ETH-USD.")
    return symbol


class SqlAlchemyWatchlist:
    """Persisted, ordered watchlist; every mutation records one system event."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def entries(self) -> tuple[WatchlistEntry, ...]:
        with self.session_factory() as session:
            rows = session.scalars(select(WatchlistRecord).order_by(WatchlistRecord.position))
            return tuple(
                WatchlistEntry(row.symbol, row.position, row.added_at, row.added_by) for row in rows
            )

    def symbols(self) -> tuple[str, ...]:
        return tuple(entry.symbol for entry in self.entries())

    def add(self, symbol: str, *, role: str, now: datetime | None = None) -> WatchlistEntry:
        symbol = normalize_symbol(symbol)
        moment = now or utc_now()
        with self.session_factory.begin() as session:
            rows = list(session.scalars(select(WatchlistRecord).order_by(WatchlistRecord.position)))
            if any(row.symbol == symbol for row in rows):
                raise WatchlistRefused(f"{symbol} is already on the watchlist.")
            if len(rows) >= WATCHLIST_LIMIT:
                raise WatchlistRefused(
                    f"The watchlist holds at most {WATCHLIST_LIMIT} coins. Remove one first."
                )
            record = WatchlistRecord(
                symbol=symbol, position=len(rows), added_at=moment, added_by=role
            )
            session.add(record)
            _record_event(session, "add", symbol, role, [*(r.symbol for r in rows), symbol], moment)
            return WatchlistEntry(symbol, record.position, moment, role)

    def remove(self, symbol: str, *, role: str, now: datetime | None = None) -> None:
        symbol = normalize_symbol(symbol)
        moment = now or utc_now()
        with self.session_factory.begin() as session:
            rows = list(session.scalars(select(WatchlistRecord).order_by(WatchlistRecord.position)))
            target = next((row for row in rows if row.symbol == symbol), None)
            if target is None:
                raise WatchlistRefused(f"{symbol} is not on the watchlist.")
            remaining = [row for row in rows if row is not target]
            session.delete(target)
            session.flush()
            for index, row in enumerate(remaining):
                row.position = index
            _record_event(session, "remove", symbol, role, [r.symbol for r in remaining], moment)

    def reorder(
        self, symbols: Sequence[str], *, role: str, now: datetime | None = None
    ) -> tuple[str, ...]:
        wanted = [normalize_symbol(symbol) for symbol in symbols]
        moment = now or utc_now()
        with self.session_factory.begin() as session:
            rows = list(session.scalars(select(WatchlistRecord).order_by(WatchlistRecord.position)))
            if len(set(wanted)) != len(wanted) or sorted(wanted) != sorted(r.symbol for r in rows):
                raise WatchlistRefused("The new order must list every watched coin exactly once.")
            if wanted == [row.symbol for row in rows]:
                return tuple(wanted)
            kept = {row.symbol: (row.added_at, row.added_by) for row in rows}
            for row in rows:
                session.delete(row)
            session.flush()
            for index, symbol in enumerate(wanted):
                added_at, added_by = kept[symbol]
                session.add(
                    WatchlistRecord(
                        symbol=symbol, position=index, added_at=added_at, added_by=added_by
                    )
                )
            _record_event(session, "reorder", None, role, wanted, moment)
            return tuple(wanted)


def _record_event(
    session: Session,
    action: str,
    symbol: str | None,
    role: str,
    order: Sequence[str],
    moment: datetime,
) -> None:
    event_id = uuid4()
    session.add(
        SystemEventRecord(
            event_id=event_id,
            event_type=WATCHLIST_EVENT,
            correlation_id=event_id,
            payload={"action": action, "symbol": symbol, "role": role, "order": list(order)},
            created_at=moment,
        )
    )


async def check_product(lookup: ProductLookup, symbol: str) -> None:
    """Refuse a symbol Coinbase does not list as a trading spot product.

    A refusal names the reason; a lookup that cannot answer refuses too, because
    a coin that could not be confirmed must not be saved.
    """

    try:
        product = await lookup.get_product(symbol)
    except CoinbaseProductNotFound:
        raise WatchlistRefused(f"Coinbase has no product named {symbol}.") from None
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 429:
            raise WatchlistRefused(
                "Coinbase is rate limiting lookups right now. Try again shortly."
            ) from None
        raise WatchlistRefused(
            "Coinbase could not confirm that product. Try again shortly."
        ) from None
    except Exception:
        raise WatchlistRefused(
            "Coinbase could not confirm that product. Try again shortly."
        ) from None
    if not product.tradable:
        raise WatchlistRefused(
            f"{symbol} is delisted or trading is disabled on Coinbase, so it is not watched."
        )
