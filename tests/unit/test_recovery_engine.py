import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "services", "ledger-service"))

from recovery import backoff_seconds, MAX_RECOVERY_ATTEMPTS
from common.processors import MockProcessor


def test_backoff_increases_exponentially():
    assert backoff_seconds(0) == 30
    assert backoff_seconds(1) == 60
    assert backoff_seconds(2) == 120
    assert backoff_seconds(3) == 240


def test_backoff_is_capped():
    assert backoff_seconds(20) == 3600  # doesn't grow unbounded for a long-stuck transaction


def test_max_recovery_attempts_is_positive():
    assert MAX_RECOVERY_ATTEMPTS > 0


def test_get_status_reports_pending_before_resolving():
    p = MockProcessor()
    # find a transaction_id whose original outcome is one that needs recovery
    txn_id = None
    for i in range(200):
        candidate = f"recovery-test-{i}"
        if p._outcome_for(candidate) in ("TIMEOUT", "UNKNOWN", "DUPLICATE"):
            txn_id = candidate
            break
    assert txn_id is not None, "expected at least one TIMEOUT/UNKNOWN/DUPLICATE outcome in 200 samples"

    early = p.get_status(txn_id, attempt_number=0)
    assert early.status == "PENDING"

    later = p.get_status(txn_id, attempt_number=3)
    assert later.status in ("SUCCESS", "DECLINED")


def test_get_status_is_deterministic_at_a_given_attempt():
    p = MockProcessor()
    r1 = p.get_status("stable-recovery-txn", attempt_number=3)
    r2 = p.get_status("stable-recovery-txn", attempt_number=3)
    assert r1.status == r2.status


def test_get_status_resolves_immediately_for_non_ambiguous_outcomes():
    p = MockProcessor()
    # find a transaction_id whose original outcome was a clean SUCCESS
    for i in range(50):
        candidate = f"clean-success-{i}"
        if p._outcome_for(candidate) == "SUCCESS":
            result = p.get_status(candidate, attempt_number=0)
            assert result.status == "SUCCESS"
            return
    raise AssertionError("expected at least one SUCCESS outcome in 50 samples")
