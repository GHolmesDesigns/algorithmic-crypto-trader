"""Fail-closed validation for normalized market data."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from core.models import Candle, Quote


class MarketDataValidationError(ValueError):
    """Raised when market data cannot safely enter the canonical data set."""


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str
    opened_at: datetime | None = None


class MarketDataValidator:
    """Validate provider-normalized candles and quotes before persistence or use."""

    def __init__(
        self,
        *,
        max_price_change_ratio: Decimal = Decimal("0.25"),
        max_quote_age_seconds: int = 60,
    ) -> None:
        if max_price_change_ratio <= 0:
            raise ValueError("max_price_change_ratio must be positive")
        if max_quote_age_seconds <= 0:
            raise ValueError("max_quote_age_seconds must be positive")
        self.max_price_change_ratio = max_price_change_ratio
        self.max_quote_age_seconds = max_quote_age_seconds

    def validate_candles(
        self,
        candles: tuple[Candle, ...] | list[Candle],
        *,
        expected_interval: timedelta,
    ) -> tuple[Candle, ...]:
        ordered = tuple(candles)
        issues: list[ValidationIssue] = []
        previous: Candle | None = None
        seen: set[tuple[str, str, datetime]] = set()
        for candle in ordered:
            key = (candle.symbol, candle.interval, candle.opened_at)
            if key in seen:
                issues.append(ValidationIssue("duplicate", "duplicate candle", candle.opened_at))
            seen.add(key)
            if candle.closed_at - candle.opened_at != expected_interval:
                issues.append(
                    ValidationIssue(
                        "duration",
                        "candle duration does not match the requested interval",
                        candle.opened_at,
                    )
                )
            if previous is not None:
                if candle.opened_at <= previous.opened_at:
                    issues.append(
                        ValidationIssue("ordering", "candle timestamps are not increasing")
                    )
                if candle.opened_at - previous.opened_at != expected_interval:
                    issues.append(
                        ValidationIssue("gap", "candle series contains an unexplained gap")
                    )
                if previous.close > 0:
                    change = abs(candle.open - previous.close) / previous.close
                    if change > self.max_price_change_ratio:
                        issues.append(
                            ValidationIssue(
                                "outlier",
                                "candle open moved beyond the configured "
                                "suspicious-outlier threshold",
                                candle.opened_at,
                            )
                        )
            previous = candle
        if issues:
            details = "; ".join(f"{issue.code}: {issue.message}" for issue in issues)
            raise MarketDataValidationError(details)
        return ordered

    def validate_quote(self, quote: Quote, *, now: datetime) -> Quote:
        age = now - quote.received_at
        if age > timedelta(seconds=self.max_quote_age_seconds):
            raise MarketDataValidationError("stale: quote exceeds the maximum allowed age")
        return quote
