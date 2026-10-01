"""Payment Processor Abstraction (ADR-011)
================
The real integration point in any payments company (Visa/Mastercard, a
local bank rail, a card network) sits behind this interface. We don't
integrate a real processor here — that needs a merchant account and money
— but the interface and a MockProcessor with *realistic failure behavior*
let the rest of the platform (state machine, webhook handling,
reconciliation) be built and tested against real-world failure modes
instead of an idealized always-succeeds stub.
"""
import hashlib
import random
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class ProcessorResult:
    status: str  # SUCCESS, DECLINED, TIMEOUT, UNKNOWN, DUPLICATE, SLOW_RESPONSE
    processor_reference: str
    latency_ms: int
    detail: str = ""


class PaymentProcessor(ABC):
    @abstractmethod
    def authorize(self, transaction_id: str, amount_minor: int, currency: str) -> ProcessorResult: ...

    @abstractmethod
    def capture(self, transaction_id: str) -> ProcessorResult: ...

    @abstractmethod
    def refund(self, transaction_id: str, amount_minor: int) -> ProcessorResult: ...

    @abstractmethod
    def get_status(self, transaction_id: str, attempt_number: int = 0) -> ProcessorResult: ...


class MockProcessor(PaymentProcessor):
    """Deterministic-but-varied mock: outcome is derived from a hash of the
    transaction_id (not pure random) so the *same* transaction always gets
    the *same* simulated outcome — reproducible for tests and demos,
    without an in-memory dict of state to manage across services.
    """

    # Weighted outcome distribution, tuned to look like a real card
    # network: mostly fine, with the failure modes you actually have to
    # handle in production at realistic-ish rates.
    OUTCOME_WEIGHTS = [
        ("SUCCESS", 85),
        ("DECLINED", 8),
        ("TIMEOUT", 3),
        ("SLOW_RESPONSE", 2),
        ("UNKNOWN", 1),
        ("DUPLICATE", 1),
    ]

    def _outcome_for(self, transaction_id: str) -> str:
        import os
        forced = os.getenv("MOCK_OUTCOME_OVERRIDE", "").strip().upper()
        if forced:
            return forced  # demo/testing switch: force every outcome
        digest = hashlib.sha256(transaction_id.encode()).hexdigest()
        bucket = int(digest[:8], 16) % 100
        cumulative = 0
        for outcome, weight in self.OUTCOME_WEIGHTS:
            cumulative += weight
            if bucket < cumulative:
                return outcome
        return "SUCCESS"

    def authorize(self, transaction_id: str, amount_minor: int, currency: str) -> ProcessorResult:
        start = time.time()
        outcome = self._outcome_for(transaction_id)

        if outcome == "SLOW_RESPONSE":
            time.sleep(random.uniform(1.5, 3.0))  # exercises client timeouts/circuit breaker for real
        elif outcome == "TIMEOUT":
            time.sleep(5.0)

        latency_ms = int((time.time() - start) * 1000)
        detail = {
            "SUCCESS": "authorized",
            "DECLINED": "insufficient funds (simulated)",
            "TIMEOUT": "processor did not respond in time",
            "UNKNOWN": "ambiguous response — requires reconciliation",
            "DUPLICATE": "processor reports this reference already used",
            "SLOW_RESPONSE": "authorized, but slowly",
        }[outcome if outcome != "SLOW_RESPONSE" else "SLOW_RESPONSE"]

        status = "SUCCESS" if outcome == "SLOW_RESPONSE" else outcome
        return ProcessorResult(
            status=status,
            processor_reference=f"mock-ref-{transaction_id[:8]}",
            latency_ms=latency_ms,
            detail=detail,
        )

    def capture(self, transaction_id: str) -> ProcessorResult:
        return ProcessorResult(status="SUCCESS", processor_reference=f"mock-cap-{transaction_id[:8]}", latency_ms=10)

    def refund(self, transaction_id: str, amount_minor: int) -> ProcessorResult:
        return ProcessorResult(status="SUCCESS", processor_reference=f"mock-refund-{transaction_id[:8]}", latency_ms=15)

    def get_status(self, transaction_id: str, attempt_number: int = 0) -> ProcessorResult:
        """Used by the recovery engine to poll a processor for the true
        outcome of an authorization that came back TIMEOUT/UNKNOWN/DUPLICATE.
        Realistically, a processor doesn't necessarily have an answer on the
        first query either — this simulates that: the first two queries
        report PENDING, and only from the third attempt does it resolve
        (deterministically, so re-querying the same transaction_id at the
        same attempt_number always gives the same answer).
        """
        original_outcome = self._outcome_for(transaction_id)
        if original_outcome not in ("TIMEOUT", "UNKNOWN", "DUPLICATE"):
            status = "SUCCESS" if original_outcome == "SLOW_RESPONSE" else original_outcome
            return ProcessorResult(status=status, processor_reference=f"mock-ref-{transaction_id[:8]}", latency_ms=5)

        if attempt_number < 2:
            return ProcessorResult(
                status="PENDING", processor_reference=f"mock-ref-{transaction_id[:8]}", latency_ms=5,
                detail="processor has no final answer yet",
            )

        digest = hashlib.sha256(f"{transaction_id}:final".encode()).hexdigest()
        bucket = int(digest[:8], 16) % 100
        final_status = "SUCCESS" if bucket < 80 else "DECLINED"
        return ProcessorResult(
            status=final_status, processor_reference=f"mock-ref-{transaction_id[:8]}", latency_ms=5,
            detail=f"resolved on recovery attempt {attempt_number}",
        )


class ProcessorRouter:
    """Picks a processor by name; a real router would also do
    availability/latency/cost-based routing (see the original project doc's
    "Payment Routing Engine" idea) — kept simple here since we only have
    one real implementation to route to.
    """

    def __init__(self):
        self._processors: dict[str, PaymentProcessor] = {"mock": MockProcessor()}

    def get(self, name: str = "mock") -> PaymentProcessor:
        return self._processors[name]
