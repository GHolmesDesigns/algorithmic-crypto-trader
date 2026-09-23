import pytest
from core.resilience import CircuitBreaker, CircuitOpen, TokenBucketRateLimiter


def test_rate_limiter_enforces_capacity() -> None:
    limiter = TokenBucketRateLimiter(capacity=1, refill_per_second=1)
    assert limiter.try_acquire()
    assert not limiter.try_acquire()


def test_circuit_breaker_fails_closed_after_threshold() -> None:
    breaker = CircuitBreaker(failure_threshold=2, recovery_timeout=60)
    breaker.record_failure()
    assert breaker.allow_request()
    breaker.record_failure()
    with pytest.raises(CircuitOpen):
        breaker.before_request()
