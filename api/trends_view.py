"""Presentation model for the Trends page.

``build_trends_view`` turns the trends payload, the same one the JSON route
returns, into chart geometry, table rows, and words. Like the other views, it adds
no data and calls nothing.

Every chart ends in exactly one of four states, and none looks like another:

- ``drawn``: bars in one neutral ink, an accessible summary, and a table;
- ``zero``: the window was read and holds no rows, written as a count of 0;
- ``unavailable``: the rows could not be read, so nothing is drawn or counted;
- ``not_started``: no trustworthy producer exists yet, with the reason.

Only ``drawn`` has an axis, so an empty axis never stands in for zero.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from api.dashboard import Stamp
from api.history_view import NAV, PATHS
from api.trends import DATA_CHARTS, DEFAULT_WINDOW, MAX_BUCKETS, MAX_WINDOW, WINDOWS, TrendsQuery

TRENDS_PATH = PATHS["trends"]
INTRO = (
    "Counts over time from the persisted records, so they survive restarts. Each chart "
    "names the table it counts and has a table of the same counts."
)
WINDOW_LABELS = {
    "24h": "Last 24 hours, hourly bars",
    "7d": "Last 7 days, 6-hour bars",
    "30d": "Last 30 days, daily bars",
}
# window: (bucket noun, table heading)
_BUCKETS = {
    "24h": ("hour", "Hour (UTC)"),
    "7d": ("6-hour period", "6 hours from (UTC)"),
    "30d": ("day", "Day (UTC)"),
}
# chart: (one, many)
_UNITS = {
    "reconciliation_runs": ("completed run", "completed runs"),
    "discrepancies": ("discrepancy", "discrepancies"),
    "refusals": ("refusal", "refusals"),
}
_KINDS = {"discrepancies": "types", "refusals": "gates"}
_DRILL = {
    "discrepancies": (PATHS["discrepancies"], {}),
    "refusals": (PATHS["risk_decisions"], {"outcome": "refused"}),
}
# One bar slot is 10 units wide in the SVG's own coordinates; the bar fills 8.
_SLOT = 10
_BAR = 8
_HEIGHT = 100
_MIN_BAR = 2
_JUST_BEFORE = timedelta(microseconds=1)


def build_trends_view(
    *,
    form: Mapping[str, str],
    query: TrendsQuery | None = None,
    charts: Sequence[Mapping[str, Any]] | None = None,
    not_started: Sequence[Mapping[str, Any]] = (),
    errors: Sequence[str] = (),
    unavailable: str | None = None,
) -> dict[str, Any]:
    """Everything the Trends page shows; ``charts`` is absent when nothing was read."""

    selected = form.get("window") or DEFAULT_WINDOW
    view: dict[str, Any] = {
        "kind": "trends",
        "title": "Trends",
        "intro": INTRO,
        "path": TRENDS_PATH,
        "nav": [
            {"href": PATHS[item], "label": label, "current": item == "trends"}
            for item, _, label in NAV
        ],
        "windows": [
            {"value": key, "label": WINDOW_LABELS[key], "selected": key == selected}
            for key in WINDOWS
        ],
        "limits": {"days": MAX_WINDOW.days, "bars": MAX_BUCKETS},
        "errors": list(errors),
        "unavailable": unavailable,
        "window": None,
        "bucket": None,
        "charts": [],
    }
    if errors:
        return view
    if query is not None:
        noun, _ = _BUCKETS[query.window]
        view["bucket"] = noun
        view["window"] = {
            "label": WINDOW_LABELS[query.window],
            "since": _moment(query.since),
            "until": _moment(query.until),
            "as_of": _moment(query.now),
            "current": _moment(query.edges[-2]),
        }
    if charts is None:
        view["charts"] = [
            {
                "key": key,
                "id": _anchor(key),
                "title": title,
                "state": "unavailable",
                "reason": unavailable or "the trends could not be read",
            }
            for key, title in DATA_CHARTS
        ]
    else:
        assert query is not None
        view["charts"] = [
            _chart(chart, query) for chart in charts if chart.get("status") == "available"
        ]
    view["charts"] += [_not_started(chart) for chart in not_started]
    return view


def _chart(chart: Mapping[str, Any], query: TrendsQuery) -> dict[str, Any]:
    key = str(chart["key"])
    noun, heading = _BUCKETS[query.window]
    series = list(chart.get("series", ()))
    shown = [row for row in series if row.get("total")]
    zero = [row for row in series if not row.get("total")]
    total = int(chart.get("total", 0))
    one, unit = _UNITS.get(key, ("record", "records"))
    view: dict[str, Any] = {
        "key": key,
        "id": _anchor(key),
        "title": str(chart.get("title", key)),
        "state": "drawn" if total else "zero",
        "unit": unit,
        "total": total,
        "total_unit": one if total == 1 else unit,
        "source": chart.get("source"),
        "note": str(chart.get("note", "")),
        "multiples": len(series) > 1,
        "zero_labels": [_label(row) for row in zero],
        "kinds": _KINDS.get(key, "series"),
    }
    if not total:
        return view
    peak = max(max(row["counts"]) for row in shown)
    scale = _scale(peak)
    buckets = query.buckets
    edges = query.edges
    view |= {
        "width": buckets * _SLOT,
        "open_x": (buckets - 1) * _SLOT,
        "scale": scale,
        "mid": scale // 2 if scale % 2 == 0 else None,
        "ticks": [
            _tick(edges[0], query),
            _tick(edges[buckets // 2], query),
            _tick(edges[-1], query),
        ],
        "rows": [_row(row, scale, query, (one, unit), len(series) > 1) for row in shown],
        "detail_href": _drill(key, edges[0], edges[-1]),
    }
    columns = [_label(row) for row in shown]
    totals = list(chart.get("totals", ()))
    view["table"] = {
        "caption": f"{view['title']}: {unit} per {noun}, {WINDOW_LABELS[query.window].lower()}",
        "first": heading,
        "columns": columns + (["Total"] if len(shown) > 1 else []),
        "rows": [
            {
                "label": _bucket_label(edges[index], edges[index + 1], query),
                "href": _drill(key, edges[index], edges[index + 1]),
                "in_progress": index == buckets - 1,
                "counts": [row["counts"][index] for row in shown]
                + ([totals[index]] if len(shown) > 1 else []),
            }
            for index in range(buckets)
        ],
        "totals": [row["total"] for row in shown] + ([total] if len(shown) > 1 else []),
    }
    return view


def _row(
    row: Mapping[str, Any],
    scale: int,
    query: TrendsQuery,
    units: tuple[str, str],
    multiple: bool,
) -> dict[str, Any]:
    counts = [int(value) for value in row["counts"]]
    noun, _ = _BUCKETS[query.window]
    peak = max(counts)
    at = query.edges[counts.index(peak)]
    bars = []
    for index, count in enumerate(counts):
        if not count:
            continue
        height = max(round(count / scale * _HEIGHT, 2), _MIN_BAR)
        bars.append(
            {
                "x": index * _SLOT + (_SLOT - _BAR) / 2,
                "y": round(_HEIGHT - height, 2),
                "width": _BAR,
                "height": height,
                "open": index == query.buckets - 1,
            }
        )
    window = WINDOW_LABELS[query.window].split(",")[0].lower()
    most = (
        f"the most in one {noun} is {peak}, in the {noun} from {at.strftime('%Y-%m-%d %H:%M')} UTC"
    )
    total = int(row["total"])
    if multiple:
        unit = units[0] if total == 1 else units[1]
        summary = f"{_label(row)}: {total} {unit} in the {window}; {most}."
    else:
        summary = (
            f"Bar chart of {units[1]} per {noun} for the {window}: {total} in total; "
            f"{most}. The last bar is the current {noun}, still in progress. "
            "The table that follows lists every bar."
        )
    return {
        "label": _label(row),
        "key": row.get("key"),
        "position": row.get("position"),
        "total": total,
        "bars": bars,
        "summary": summary,
    }


def _not_started(chart: Mapping[str, Any]) -> dict[str, Any]:
    key = str(chart["key"])
    return {
        "key": key,
        "id": _anchor(key),
        "title": str(chart.get("title", key)),
        "state": "not_started",
        "reason": str(chart.get("reason", "")),
        "blocked_by": list(chart.get("blocked_by", ())),
    }


def _scale(peak: int) -> int:
    """The smallest of 1, 2, 4, 6, 8, 10, 20, 40 ... that holds ``peak``."""

    magnitude = 1
    while True:
        for step in (1, 2, 4, 6, 8):
            if peak <= step * magnitude:
                return step * magnitude
        magnitude *= 10


def _label(row: Mapping[str, Any]) -> str:
    """A series name: gates keep their order, and an unrecognised key is shown as recorded."""

    position = row.get("position")
    label = str(row.get("label", ""))
    if position:
        return f"{position}. {label}"
    if row.get("key") and row.get("recognised") is False:
        return f"{label} ({row['key']})"
    return label


def _moment(value: datetime) -> Stamp:
    moment = value.astimezone(UTC)
    return Stamp(moment.isoformat(), moment.strftime("%Y-%m-%d %H:%M UTC"))


def _tick(value: datetime, query: TrendsQuery) -> str:
    return value.strftime("%b %d" if query.window == "30d" else "%b %d %H:%M")


def _bucket_label(start: datetime, end: datetime, query: TrendsQuery) -> str:
    if query.window == "30d":
        return start.strftime("%Y-%m-%d")
    return f"{start.strftime('%Y-%m-%d %H:%M')} to {end.strftime('%H:%M')}"


def _drill(key: str, start: datetime, end: datetime) -> str | None:
    """The history list for one bucket: history windows include their end, so stop just before."""

    if key not in _DRILL:
        return None
    path, extra = _DRILL[key]
    params = {"since": start.isoformat(), "until": (end - _JUST_BEFORE).isoformat()} | extra
    return f"{path}?{urlencode(params)}"


def _anchor(key: str) -> str:
    return "chart-" + key.replace("_", "-")
