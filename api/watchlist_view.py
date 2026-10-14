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
_FEED_ON = Status("on", "ok")
_FEED_OFF = Status("off", "neutral")
_BUDGET = (
    "Public Coinbase market data only: no credentials, and it never trades. Turning it on "
    "backfills 30 days for each watched coin (about 30 requests per coin), then makes one "
    "request per coin every five minutes."
)


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
        "feed_status": _FEED_OFF,
        "feed_origin": "",
        "feed_budget": _BUDGET,
        "can_toggle": False,
        "full": False,
    }
    if payload is None:
        return view
    symbols = payload["symbols"]
    view["limit"] = payload["limit"]
    view["feed_enabled"] = payload["feed_enabled"]
    view["feed_status"] = _FEED_ON if payload["feed_enabled"] else _FEED_OFF
    view["feed_origin"] = _origin_text(payload.get("feed_setting"), payload["feed_enabled"])
    view["can_toggle"] = role == "admin" and payload.get("feed_setting") is not None
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


def _origin_text(setting: Mapping[str, Any] | None, enabled: bool) -> str:
    """Say why the feed is in its state, in the operator's words."""

    word = "on" if enabled else "off"
    if setting is None:
        return f"The feed is {word}. This process has no on/off control."
    source = setting["source"]
    if source == "saved" and setting["changed_at"]:
        when = datetime.fromisoformat(setting["changed_at"]).astimezone(UTC)
        stamp = when.strftime("%Y-%m-%d %H:%M:%S UTC")
        return f"Turned {word} by {setting['changed_by']}, {stamp}."
    if source == "unreadable":
        return setting["detail"] or "The saved on/off choice could not be read, so the feed is off."
    return (
        f"{word.capitalize()} by the deployment setting (WATCH_FEED_ENABLED); "
        "no choice has been saved from this page."
    )
