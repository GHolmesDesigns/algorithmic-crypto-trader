"""Presentation model for the research workspace."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def build_research_view(
    *,
    catalog: Sequence[Mapping[str, Any]],
    strategies: Sequence[Mapping[str, str]],
    jobs: Sequence[Mapping[str, Any]],
    compare: Sequence[Mapping[str, Any]] = (),
    selected: Sequence[str] = (),
    job: Mapping[str, Any] | None = None,
    errors: Sequence[str] = (),
) -> dict[str, Any]:
    """Build all words and rows the template displays; it performs no reads."""

    return {
        "title": "Research and replay",
        "intro": (
            "Run bounded backtests and deterministic replays against approved historical "
            "datasets. Past results are not a promise of profit."
        ),
        "catalog": list(catalog),
        "strategies": list(strategies),
        "jobs": list(jobs),
        "compare": list(compare),
        "selected": list(selected),
        "job": job,
        "errors": list(errors),
        "limits": {"candles": 5000, "active_jobs": 2, "windows": 24},
    }
