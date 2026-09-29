"""Presentation model for the server-drawn Markets page.

The read model owns candle aggregation and freshness. This module only turns its
JSON-shaped payload into safe text and SVG geometry. It never calls a provider or
the trading path, and all price calculations stay in ``Decimal``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any
from urllib.parse import urlencode

from api.markets import CandleQuery, parse_candles_query

MARKETS_PATH = "/operator/markets"
WATCHLIST_PATH = "/operator/watchlist"
INTRO = (
    "Read-only Coinbase candles from the saved watchlist. These charts never add a coin "
    "to the trading symbols or submit an order."
)
WINDOW_LABELS = {
    "24h": "Last 24 hours",
    "7d": "Last 7 days",
    "30d": "Last 30 days",
    "90d": "Last 90 days",
}
INTERVAL_LABELS = {
    "15m": "15-minute bars",
    "1h": "Hourly bars",
    "6h": "6-hour bars",
    "1d": "Daily bars",
}
STATE_LABELS = {
    "drawn": "Drawn",
    "stale": "Stale",
    "not_collected": "Not yet collected",
    "unavailable": "Unavailable",
}
_PAGE_STATE = {
    "fresh": "drawn",
    "stale": "stale",
    "not_collected": "not_collected",
    "unavailable": "unavailable",
}
STATE_MARKS = {
    "drawn": "◇",
    "stale": "△",
    "not_collected": "○",
    "unavailable": "□",
}
_SIGNIFICANT_DIGITS = 6
_PAGE_PARAMETERS = {"c", "window", "interval"}


def parse_markets_query(
    items: Iterable[tuple[str, str]],
    *,
    default_symbols: Sequence[str],
    now: datetime | None = None,
) -> CandleQuery:
    """Parse the page's shareable ``c`` parameter using the candle-read rules."""

    translated: list[tuple[str, str]] = []
    for key, value in items:
        if key not in _PAGE_PARAMETERS:
            # The page has its own vocabulary; the read endpoint remains separate.
            translated.append((key, value))
        else:
            translated.append(("symbols" if key == "c" else key, value))
    return parse_candles_query(translated, default_symbols=default_symbols, now=now)


def build_markets_view(
    payload: Mapping[str, Any] | None,
    *,
    form: Mapping[str, str],
    errors: Sequence[str] = (),
    unavailable: str | None = None,
) -> dict[str, Any]:
    """Build the complete page model without adding data to the read response."""

    selected_window = form.get("window") or "24h"
    selected_interval = form.get("interval") or ""
    view: dict[str, Any] = {
        "title": "Markets",
        "intro": INTRO,
        "path": MARKETS_PATH,
        "watchlist_path": WATCHLIST_PATH,
        "errors": list(errors),
        "unavailable": unavailable,
        "window": selected_window,
        "interval": selected_interval,
        "windows": [
            {"value": key, "label": label, "selected": key == selected_window}
            for key, label in WINDOW_LABELS.items()
        ],
        "intervals": [
            {"value": key, "label": label, "selected": key == selected_interval}
            for key, label in INTERVAL_LABELS.items()
        ],
        "tiles": [],
        "columns": 1,
        "share_href": None,
        "query": None,
    }
    if payload is None:
        return view

    query = dict(payload["query"])
    query["symbols"] = [str(item["symbol"]) for item in payload.get("symbols", ())]
    view["window"] = str(query["window"])
    view["interval"] = str(query["interval"])
    view["windows"] = [
        {"value": key, "label": label, "selected": key == view["window"]}
        for key, label in WINDOW_LABELS.items()
    ]
    view["intervals"] = [
        {"value": key, "label": label, "selected": key == view["interval"]}
        for key, label in INTERVAL_LABELS.items()
    ]
    tiles = [_tile(item, query) for item in payload.get("symbols", ())]
    view["tiles"] = tiles
    view["columns"] = 3 if len(tiles) > 4 else 2 if len(tiles) > 1 else 1
    view["query"] = query
    symbols = ",".join(str(item["symbol"]) for item in payload.get("symbols", ()))
    view["share_href"] = _href(
        MARKETS_PATH,
        {"c": symbols, "window": str(query["window"]), "interval": str(query["interval"])},
    )
    return view


def _tile(item: Mapping[str, Any], query: Mapping[str, Any]) -> dict[str, Any]:
    symbol = str(item["symbol"])
    freshness = item.get("freshness") or {}
    raw_state = str(freshness.get("state", "not_collected"))
    state = _PAGE_STATE.get(raw_state, "not_collected")
    bars = list(item.get("bars", ()))
    values = _values(bars)
    first = values[0][1] if values else None
    last = values[-1][1] if values else None
    highs = _decimals(bar.get("high") for bar in bars)
    lows = _decimals(bar.get("low") for bar in bars)
    change = last - first if first is not None and last is not None else None
    change_percent = _percent(change, first)
    source = str(item.get("source") or "none recorded")
    as_of = freshness.get("last_candle_at") or "none yet"
    has_chart = bool(values) and state in {"drawn", "stale"}
    chart_label = _chart_summary(symbol, state, first, last, change, highs, lows, as_of)
    return {
        "symbol": symbol,
        "state": state,
        "state_label": STATE_LABELS[state],
        "state_mark": STATE_MARKS[state],
        "detail": str(freshness.get("detail") or ""),
        "source": source,
        "as_of": _moment(as_of),
        "last": _price(last),
        "change": _signed_price(change),
        "change_percent": _signed_percent(change_percent),
        "high": _price(max(highs) if highs else None),
        "low": _price(min(lows) if lows else None),
        "window": WINDOW_LABELS.get(str(query["window"]), str(query["window"])),
        "interval": INTERVAL_LABELS.get(str(query["interval"]), str(query["interval"])),
        "summary": chart_label,
        "has_chart": has_chart,
        "segments": _segments(bars) if has_chart else (),
        "points": len(values),
        "table": _table_rows(
            last=last,
            change=change,
            change_percent=change_percent,
            high=max(highs) if highs else None,
            low=min(lows) if lows else None,
            as_of=as_of,
            source=source,
            state=STATE_LABELS[state],
        ),
        "tradingview": _tradingview(symbol),
    }


def _values(bars: Sequence[Mapping[str, Any]]) -> list[tuple[int, Decimal]]:
    values: list[tuple[int, Decimal]] = []
    for index, bar in enumerate(bars):
        value = _decimal(bar.get("close"))
        if value is not None:
            values.append((index, value))
    return values


def _segments(bars: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    values = _values(bars)
    if not values:
        return ()
    low_values = _decimals(bar.get("low") for bar in bars)
    high_values = _decimals(bar.get("high") for bar in bars)
    if not low_values or not high_values:
        return ()
    low, high = min(low_values), max(high_values)
    segments: list[list[tuple[int, Decimal]]] = []
    current: list[tuple[int, Decimal]] = []
    previous = -2
    for index, value in values:
        if index != previous + 1 and current:
            segments.append(current)
            current = []
        current.append((index, value))
        previous = index
    if current:
        segments.append(current)
    return tuple(_path(segment, len(bars), low, high) for segment in segments)


def _path(points: Sequence[tuple[int, Decimal]], count: int, low: Decimal, high: Decimal) -> str:
    parts: list[str] = []
    for point_index, (index, value) in enumerate(points):
        x = Decimal(50) if count == 1 else Decimal(index * 100) / Decimal(count - 1)
        if high == low:
            y = Decimal(50)
        else:
            y = Decimal(100) - ((value - low) * Decimal(100) / (high - low))
        command = "M" if point_index == 0 else "L"
        parts.append(f"{command} {_coordinate(x)} {_coordinate(y)}")
    return " ".join(parts)


def _table_rows(
    *,
    last: Decimal | None,
    change: Decimal | None,
    change_percent: Decimal | None,
    high: Decimal | None,
    low: Decimal | None,
    as_of: Any,
    source: str,
    state: str,
) -> tuple[dict[str, str], ...]:
    return (
        {"label": "Last price", "value": _price(last)},
        {
            "label": "Change",
            "value": f"{_signed_price(change)} ({_signed_percent(change_percent)})",
        },
        {"label": "High", "value": _price(high)},
        {"label": "Low", "value": _price(low)},
        {"label": "As of", "value": _moment(as_of)},
        {"label": "Source", "value": source},
        {"label": "State", "value": state},
    )


def _chart_summary(
    symbol: str,
    state: str,
    first: Decimal | None,
    last: Decimal | None,
    change: Decimal | None,
    highs: Sequence[Decimal],
    lows: Sequence[Decimal],
    as_of: Any,
) -> str:
    if state == "unavailable":
        return f"{symbol}: unavailable. {str(as_of)}. No price line is drawn."
    if state == "not_collected":
        return f"{symbol}: not yet collected. No price line is drawn."
    if last is None:
        return f"{symbol}: no candles in this window. No price line is drawn."
    direction = (
        "up"
        if change is not None and change > 0
        else "down"
        if change is not None and change < 0
        else "unchanged"
    )
    return (
        f"{symbol}: price line, last {_price(last)}, {direction} by "
        f"{_price(abs(change or Decimal(0)))}; "
        f"high {_price(max(highs) if highs else None)}, low {_price(min(lows) if lows else None)}."
    )


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _decimals(values: Iterable[Any]) -> list[Decimal]:
    result: list[Decimal] = []
    for value in values:
        parsed = _decimal(value)
        if parsed is not None:
            result.append(parsed)
    return result


def _price(value: Decimal | None) -> str:
    return _significant(value) if value is not None else "none"


def _signed_price(value: Decimal | None) -> str:
    if value is None:
        return "none"
    prefix = "+" if value > 0 else ""
    return prefix + _significant(value)


def _signed_percent(value: Decimal | None) -> str:
    if value is None:
        return "none"
    prefix = "+" if value > 0 else ""
    return prefix + _significant(value) + "%"


def _percent(change: Decimal | None, first: Decimal | None) -> Decimal | None:
    if change is None or first is None or first == Decimal(0):
        return None
    with localcontext() as context:
        context.prec = 40
        return change / first * Decimal(100)


def _significant(value: Decimal) -> str:
    if value == 0:
        return "0"
    places = _SIGNIFICANT_DIGITS - value.copy_abs().adjusted() - 1
    quantum = Decimal(1).scaleb(-places)
    with localcontext() as context:
        context.prec = max(40, len(value.as_tuple().digits) + abs(places) + 4)
        rounded = value.quantize(quantum)
    return format(rounded, "f").rstrip("0").rstrip(".") or "0"


def _coordinate(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01")), "f").rstrip("0").rstrip(".") or "0"


def _moment(value: Any) -> str:
    if not value or value == "none yet":
        return "none yet"
    try:
        moment = datetime.fromisoformat(str(value)).astimezone(UTC)
    except (TypeError, ValueError):
        return str(value)
    return moment.strftime("%Y-%m-%d %H:%M UTC")


def _tradingview(symbol: str) -> str:
    base = symbol.removesuffix("-USD")
    return f"https://www.tradingview.com/chart/?symbol=COINBASE:{base}USD"


def _href(path: str, params: Mapping[str, str]) -> str:
    return f"{path}?{urlencode(params)}"
