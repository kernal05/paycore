import time

import pytest

from common.circuit_breaker import CircuitBreaker, CircuitOpenError, CircuitState


def test_starts_closed():
    cb = CircuitBreaker("test", failure_threshold=3, cooldown_seconds=1)
    assert cb.state == CircuitState.CLOSED


def test_opens_after_threshold_failures():
    cb = CircuitBreaker("test", failure_threshold=3, cooldown_seconds=10)

    def failing():
        raise ValueError("boom")

    for _ in range(3):
        with pytest.raises(ValueError):
            cb.call(failing)

    assert cb.state == CircuitState.OPEN
    with pytest.raises(CircuitOpenError):
        cb.call(failing)  # fails fast now, doesn't even invoke failing()


def test_half_open_after_cooldown_then_closes_on_success():
    cb = CircuitBreaker("test", failure_threshold=1, cooldown_seconds=0.2)

    with pytest.raises(ValueError):
        cb.call(lambda: (_ for _ in ()).throw(ValueError()))
    assert cb.state == CircuitState.OPEN

    time.sleep(0.3)
    assert cb.state == CircuitState.HALF_OPEN

    result = cb.call(lambda: "ok")
    assert result == "ok"
    assert cb.state == CircuitState.CLOSED


def test_half_open_failure_reopens():
    cb = CircuitBreaker("test", failure_threshold=1, cooldown_seconds=0.1)
    with pytest.raises(ValueError):
        cb.call(lambda: (_ for _ in ()).throw(ValueError()))
    time.sleep(0.15)
    assert cb.state == CircuitState.HALF_OPEN

    with pytest.raises(ValueError):
        cb.call(lambda: (_ for _ in ()).throw(ValueError()))
    assert cb.state == CircuitState.OPEN
