"""The authenticated, read-only market-candle endpoint: JSON for the charts.

One GET reads stored five-minute candles, aggregated to bars, for one to nine
symbols over one bounded window. A request beyond the caps or the vocabulary is
refused with 400 and the reasons before anything is read. Candles that cannot be
read answer 503, never an empty chart. The handler is a plain function, so
FastAPI runs the database read in its thread pool rather than on the event loop
the trading runtime shares.
"""

from __future__ import annotations

from typing import Any

from data.watchlist import SqlAlchemyWatchlist
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.exc import SQLAlchemyError

from api.markets import (
    CandleQueryRefused,
    CandlesUnavailable,
    parse_candles_query,
)
from api.routes import _authorize

router = APIRouter()

CANDLES_PATH = "/operator/markets/candles"


@router.get(CANDLES_PATH)
def market_candles(request: Request) -> Response:
    """Bars per symbol, with gaps, freshness, and source. Read-only."""

    _authorize(request)
    reads = getattr(request.app.state, "market_candles", None)
    if reads is None:
        return _unavailable("the candle database is not configured in this process")
    params = request.query_params.multi_items()
    try:
        # The saved watchlist stands in only when no symbols are named.
        default = () if any(key == "symbols" for key, _ in params) else _watchlist(request)
        query = parse_candles_query(params, default_symbols=default)
    except CandleQueryRefused as refused:
        return JSONResponse({"detail": {"status": "refused", "errors": list(refused.errors)}}, 400)
    except SQLAlchemyError:
        return _unavailable("the watchlist could not be read")
    try:
        payload = reads.read(query, feed_states=_feed_states(request))
    except CandlesUnavailable as exc:
        return _unavailable(str(exc))
    return JSONResponse(payload)


def _watchlist(request: Request) -> tuple[str, ...]:
    store: SqlAlchemyWatchlist | None = getattr(request.app.state, "watchlist", None)
    return store.symbols() if store is not None else ()


def _feed_states(request: Request) -> dict[str, dict[str, Any]]:
    """The watch-only feed's own per-symbol report, when the feed is running."""

    feed = getattr(request.app.state, "watch_feed", None)
    if feed is None:
        return {}
    return {item["symbol"]: item for item in feed.to_dict()["symbols"]}


def _unavailable(reason: str) -> Response:
    return JSONResponse({"detail": {"status": "unavailable", "reason": reason}}, 503)
