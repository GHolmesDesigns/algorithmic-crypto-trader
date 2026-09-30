"""Shared configuration for market-data reconnect health and storm reporting."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta

from core.guards import StartupGuardError

DEFAULT_RECONNECT_STORM_THRESHOLD = 3
DEFAULT_RECONNECT_STORM_WINDOW_SECONDS = 300.0
DEFAULT_RECONNECT_STORM_ALERT_INTERVAL_SECONDS = 900.0
DEFAULT_RECONNECT_HEALTHY_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class ReconnectSettings:
    """Bounded reconnect-storm and healthy-connection settings."""

    storm_threshold: int
    storm_window: timedelta
    storm_alert_interval: timedelta
    healthy_connection_period: timedelta

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> ReconnectSettings:
        values = os.environ if environ is None else environ
        return cls(
            storm_threshold=_integer(
                values,
                "PAPER_RECONNECT_STORM_THRESHOLD",
                DEFAULT_RECONNECT_STORM_THRESHOLD,
                minimum=2,
                maximum=100,
            ),
            storm_window=timedelta(
                seconds=_number(
                    values,
                    "PAPER_RECONNECT_STORM_WINDOW_SECONDS",
                    DEFAULT_RECONNECT_STORM_WINDOW_SECONDS,
                    minimum=1,
                    maximum=86_400,
                )
            ),
            storm_alert_interval=timedelta(
                seconds=_number(
                    values,
                    "PAPER_RECONNECT_STORM_ALERT_INTERVAL_SECONDS",
                    DEFAULT_RECONNECT_STORM_ALERT_INTERVAL_SECONDS,
                    minimum=1,
                    maximum=86_400,
                )
            ),
            healthy_connection_period=timedelta(
                seconds=_number(
                    values,
                    "PAPER_RECONNECT_HEALTHY_SECONDS",
                    DEFAULT_RECONNECT_HEALTHY_SECONDS,
                    minimum=1,
                    maximum=86_400,
                )
            ),
        )


def _integer(
    environ: Mapping[str, str], name: str, default: int, *, minimum: int, maximum: int
) -> int:
    try:
        value = int(environ.get(name, str(default)))
    except ValueError as exc:
        raise StartupGuardError(f"{name} must be an integer") from exc
    if value < minimum or value > maximum:
        raise StartupGuardError(f"{name} must be between {minimum} and {maximum}")
    return value


def _number(
    environ: Mapping[str, str], name: str, default: float, *, minimum: float, maximum: float
) -> float:
    try:
        value = float(environ.get(name, str(default)))
    except ValueError as exc:
        raise StartupGuardError(f"{name} must be a number") from exc
    if value != value or value in {float("inf"), float("-inf")}:
        raise StartupGuardError(f"{name} must be finite")
    if value < minimum or value > maximum:
        raise StartupGuardError(f"{name} must be between {minimum:g} and {maximum:g}")
    return value
