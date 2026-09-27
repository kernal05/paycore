import time
from unittest.mock import patch

import pytest

from common.processors import MockProcessor
from common.webhooks import sign_webhook_payload, verify_webhook, WebhookVerificationError


def test_mock_processor_deterministic_per_transaction():
    p = MockProcessor()
    with patch("common.processors.time.sleep"):
        r1 = p.authorize("txn-fixed-id-aaaa", 1000, "INR")
        r2 = p.authorize("txn-fixed-id-aaaa", 1000, "INR")
    assert r1.status == r2.status, "same transaction_id must always produce the same simulated outcome"


def test_mock_processor_distribution_is_mostly_success():
    p = MockProcessor()
    with patch("common.processors.time.sleep"):  # don't actually wait out TIMEOUT/SLOW_RESPONSE delays in a unit test
        outcomes = [p.authorize(f"txn-{i}", 1000, "INR").status for i in range(500)]
    success_rate = outcomes.count("SUCCESS") / len(outcomes)
    assert success_rate > 0.7, f"expected mostly-success distribution, got {success_rate:.2%}"


def test_mock_processor_covers_failure_modes():
    p = MockProcessor()
    with patch("common.processors.time.sleep"):
        outcomes = {p.authorize(f"txn-{i}", 1000, "INR").status for i in range(500)}
    # Over 500 distinct hashes we should see at least the common failure modes
    assert "DECLINED" in outcomes
    assert "SUCCESS" in outcomes


def test_webhook_signature_round_trip():
    body = b'{"event_id": "evt_1", "transaction_id": "txn_1", "resolved_status": "SETTLED"}'
    ts = int(time.time())
    sig = sign_webhook_payload(body, ts)
    verify_webhook(body, ts, sig)  # should not raise


def test_webhook_rejects_bad_signature():
    body = b'{"event_id": "evt_1"}'
    ts = int(time.time())
    with pytest.raises(WebhookVerificationError):
        verify_webhook(body, ts, "not-the-real-signature")


def test_webhook_rejects_stale_timestamp():
    body = b'{"event_id": "evt_1"}'
    old_ts = int(time.time()) - 3600  # an hour old
    sig = sign_webhook_payload(body, old_ts)
    with pytest.raises(WebhookVerificationError):
        verify_webhook(body, old_ts, sig)


def test_webhook_rejects_tampered_body():
    ts = int(time.time())
    original_body = b'{"amount_minor": 100}'
    sig = sign_webhook_payload(original_body, ts)
    tampered_body = b'{"amount_minor": 999999}'
    with pytest.raises(WebhookVerificationError):
        verify_webhook(tampered_body, ts, sig)
