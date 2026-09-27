"""A small, dependency-free circuit breaker.

CLOSED (normal) --[N consecutive failures]--> OPEN (fail fast, no call made)
OPEN --[cooldown elapses]--> HALF_OPEN (allow exactly one trial call)
HALF_OPEN --[success]--> CLOSED
HALF_OPEN --[failure]--> OPEN (reset cooldown)

Why this matters here: without it, if the fraud engine gets slow or
starts erroring, every payment-api request still waits out the full
httpx timeout before failing — under load that pins every worker thread
waiting on a dependency that's already known to be unhealthy. The breaker
turns that into an instant, cheap failure once the pattern is detected.
"""
import time
from enum import Enum
from threading import Lock


class CircuitState(str, Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitOpenError(Exception):
    pass


class CircuitBreaker:
    def __init__(self, name: str, failure_threshold: int = 5, cooldown_seconds: float = 30.0):
        self.name = name
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._opened_at: float | None = None
        self._lock = Lock()

    @property
    def state(self) -> CircuitState:
        with self._lock:
            if self._state == CircuitState.OPEN and self._opened_at is not None:
                if time.time() - self._opened_at >= self.cooldown_seconds:
                    self._state = CircuitState.HALF_OPEN
            return self._state

    def call(self, fn, *args, **kwargs):
        current_state = self.state
        if current_state == CircuitState.OPEN:
            raise CircuitOpenError(f"circuit '{self.name}' is OPEN — failing fast")

        try:
            result = fn(*args, **kwargs)
        except Exception:
            self._on_failure()
            raise
        else:
            self._on_success()
            return result

    def _on_success(self):
        with self._lock:
            self._failure_count = 0
            self._state = CircuitState.CLOSED
            self._opened_at = None

    def _on_failure(self):
        with self._lock:
            self._failure_count += 1
            if self._state == CircuitState.HALF_OPEN or self._failure_count >= self.failure_threshold:
                self._state = CircuitState.OPEN
                self._opened_at = time.time()
