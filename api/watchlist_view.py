"""Presentation model for the Watchlist page; it adds no data and calls nothing."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from api.dashboard import Status, _stamp

WATCHLIST_PATH = "/operator/watchlist"
INTRO = (
    "Coins you chart but never trade. When the watch-only feed is on, adding one starts "
    "collecting its five-minute Coinbase candles. A watched coin is never added to the "
    "trading symbols, and the strategy, risk gates, and execution never see these candles."
)
_STATES = {
    "fresh": Status("fresh", "ok"),
    "stale": Status("stale", "warn"),
    "not_collected": Status("not yet collected", "unknown"),
    "unavailable": Status("unavailable", "crit"),
}
_TRADING = Status("collected by the trading feed", "neutral")


def build_watchlist_view(
    payload: Mapping[str, Any] | None,
    *,
    role: str = "operator",
    errors: Sequence[str] = (),
    unavailable: str | None = None,
    add_form: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    moment = now or datetime.now(UTC)
    view: dict[str, Any] = {
        "title": "Watchlist",
        "intro": INTRO,
        "path": WATCHLIST_PATH,
        "errors": list(errors),
        "unavailable": unavailable,
        "role": role,
        "add_symbol": (add_form or {}).get("symbol", ""),
        "rows": [],
        "limit": 0,
        "feed_enabled": False,
        "full": False,
    }
    if payload is None:
        return view
    symbols = payload["symbols"]
    view["limit"] = payload["limit"]
    view["feed_enabled"] = payload["feed_enabled"]
    view["full"] = len(symbols) >= payload["limit"]
    for item in symbols:
        feed = item["feed"]
        last: str | None = None
        if item["collected_by"] == "trading feed":
            status, detail = _TRADING, ""
        elif not payload["feed_enabled"]:
            status, detail = _STATES["not_collected"], "The watch-only feed is off."
        elif feed is None:
            status, detail = _STATES["not_collected"], "Waiting for the next collection."
        else:
            status = _STATES.get(feed["state"], _STATES["not_collected"])
            detail, last = feed["detail"] or "", feed["last_candle_at"]
        view["rows"].append(
            {
                "symbol": item["symbol"],
                "first": item["position"] == 0,
                "last": item["position"] == len(symbols) - 1,
                "status": status,
                "detail": detail,
                "last_candle": _stamp(last, moment, missing="none yet"),
                "added": _stamp(item["added_at"], moment),
                "added_by": item["added_by"],
            }
        )
    return view
