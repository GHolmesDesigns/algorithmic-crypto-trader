"""Provider-neutral rate limiting and circuit breaking, enabled in every mode."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from enum import StrEnum
from threading import Lock


class RateLimitExceeded(RuntimeError):
    """Raised when a request cannot be admitted within the configured budget."""


class CircuitOpen(RuntimeError):
    """Raised when the provider circuit is open after repeated failures."""


@dataclass
class TokenBucketRateLimiter:
    capacity: float
    refill_per_second: float

    def __post_init__(self) -> None:
        if self.capacity <= 0 or self.refill_per_second <= 0:
            raise ValueError("rate limiter capacity and refill rate must be positive")
        self._tokens = self.capacity
        self._last_refill = time.monotonic()
        self._lock = Lock()

    def try_acquire(self, tokens: float = 1.0) -> bool:
        if tokens <= 0 or tokens > self.capacity:
            raise ValueError("requested tokens must be greater than zero and fit the bucket")
        with self._lock:
            now = time.monotonic()
            self._tokens = min(
                self.capacity, self._tokens + (now - self._last_refill) * self.refill_per_second
            )
            self._last_refill = now
            if self._tokens < tokens:
                return False
            self._tokens -= tokens
            return True

    async def acquire(self, tokens: float = 1.0, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while not self.try_acquire(tokens):
            if time.monotonic() >= deadline:
                raise RateLimitExceeded("request budget was not available before timeout")
            await asyncio.sleep(min(0.05, max(0.001, deadline - time.monotonic())))


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    def __init__(self, failure_threshold: int = 3, recovery_timeout: float = 30.0) -> None:
        if failure_threshold < 1 or recovery_timeout <= 0:
            raise ValueError("circuit breaker settings must be positive")
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.state = CircuitState.CLOSED
        self.failures = 0
        self._opened_at: float | None = None
        self._lock = Lock()

    def allow_request(self) -> bool:
        with self._lock:
            if self.state is CircuitState.CLOSED:
                return True
            if self.state is CircuitState.OPEN and self._opened_at is not None:
                if time.monotonic() - self._opened_at >= self.recovery_timeout:
                    self.state = CircuitState.HALF_OPEN
                    return True
            return self.state is CircuitState.HALF_OPEN

    def before_request(self) -> None:
        if not self.allow_request():
            raise CircuitOpen("provider circuit is open")

    def record_success(self) -> None:
        with self._lock:
            self.state = CircuitState.CLOSED
            self.failures = 0
            self._opened_at = None

    def record_failure(self) -> None:
        with self._lock:
            self.failures += 1
            if self.failures >= self.failure_threshold:
                self.state = CircuitState.OPEN
                self._opened_at = time.monotonic()
