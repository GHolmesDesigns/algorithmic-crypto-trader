"""Presentation model for the research workspace."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

FORM_DEFAULTS = {
    "dataset_id": "btc-usd-calm-trend-v1",
    "strategy_version": "always-buy-v1",
    "run_type": "backtest",
    "initial_cash": "100000",
    "quantity": "1",
    "maker_fee_rate": "0",
    "taker_fee_rate": "0",
    "spread_bps": "0",
    "slippage_bps": "0",
    "fee_asset": "USD",
    "partial_fill_ratio": "1",
    "train_bars": "16",
    "test_bars": "16",
    "step_bars": "",
    "holdout_bars": "0",
}


def build_research_view(
    *,
    catalog: Sequence[Mapping[str, Any]],
    strategies: Sequence[Mapping[str, str]],
    jobs: Sequence[Mapping[str, Any]],
    compare: Sequence[Mapping[str, Any]] = (),
    selected: Sequence[str] = (),
    job: Mapping[str, Any] | None = None,
    errors: Sequence[str] = (),
    form_values: Mapping[str, Any] | None = None,
    review: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build all words and rows the template displays; it performs no reads."""

    values = dict(FORM_DEFAULTS)
    if form_values is not None:
        values.update(
            {key: str(value) for key, value in form_values.items() if key in FORM_DEFAULTS}
        )
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
        "form": values,
        "review": review,
        "limits": {"candles": 5000, "active_jobs": 2, "windows": 24},
    }
