"""Integration tests for the fraud review queue and refund/batch reconciliation.

Run against the live stack (see test_financial_correctness.py). They must pass whether or not the stack
forces every processor outcome to SUCCESS, so they only assert on states reachable either way.
"""
import time
import uuid

import httpx
import pytest

from common.auth import issue_token

from .conftest import requires_stack, make_payment

RECON_SERVICE = "http://localhost:8003"
FINAL_STATES = {"SETTLED", "FAILED", "UNKNOWN"}


def _held_payment(api_client, max_attempts=20):
    """Fire small payments until the velocity rule holds one for review. Returns its id, or None."""
    for _ in range(max_attempts):
        r = make_payment(api_client, f"review-{uuid.uuid4()}", amount_minor=100)
        if r.status_code == 200 and r.json()["status"] == "REVIEW":
            return r.json()["transaction_id"]
    return None


def _settled_payment(api_client, amount_minor=500):
    r = make_payment(api_client, f"settled-{uuid.uuid4()}", amount_minor=amount_minor)
    if r.status_code != 200 or r.json()["status"] != "APPROVED_AND_SETTLED":
        pytest.skip("payment was held or blocked by the fraud engine this run")
    return r.json()["transaction_id"]


def _decision(reviewer="pytest", note="automated test"):
    return {"reviewer": reviewer, "note": note}


@requires_stack
def test_review_decision_requires_reviewer_and_note(ledger_client):
    tid = str(uuid.uuid4())
    for body in (_decision(reviewer=""), _decision(note=""), _decision(reviewer="  ", note="x")):
        assert ledger_client.post(f"/transactions/{tid}/review-reject", json=body).status_code == 400
        assert ledger_client.post(f"/transactions/{tid}/review-approve", json=body).status_code == 400


@requires_stack
def test_held_payment_appears_in_review_queue(api_client, ledger_client):
    tid = _held_payment(api_client)
    if tid is None:
        pytest.skip("fraud engine did not hold a payment for review this run")
    pending = ledger_client.get("/review/pending").json()["pending"]
    assert tid in {p["id"] for p in pending}


@requires_stack
def test_reject_fails_payment_moves_no_money_and_is_idempotent(api_client, ledger_client):
    tid = _held_payment(api_client)
    if tid is None:
        pytest.skip("fraud engine did not hold a payment for review this run")
    before = ledger_client.get("/accounts/11111111-1111-1111-1111-111111111111/balance").json()["balance_minor"]

    r1 = ledger_client.post(f"/transactions/{tid}/review-reject", json=_decision(note="rejected by test"))
    assert r1.status_code == 200
    assert r1.json()["status"] == "FAILED"
    assert ledger_client.get(f"/transactions/{tid}").json()["status"] == "FAILED"

    r2 = ledger_client.post(f"/transactions/{tid}/review-reject", json=_decision())
    assert r2.status_code == 200
    assert r2.json()["status"] == "FAILED"
    assert r2.json().get("idempotent_replay") is True

    after = ledger_client.get("/accounts/11111111-1111-1111-1111-111111111111/balance").json()["balance_minor"]
    assert after == before, "rejecting a held payment must not move money"

    pending = ledger_client.get("/review/pending").json()["pending"]
    assert tid not in {p["id"] for p in pending}


@requires_stack
def test_approve_posts_the_payment_to_a_final_state(api_client, ledger_client):
    tid = _held_payment(api_client)
    if tid is None:
        pytest.skip("fraud engine did not hold a payment for review this run")
    r = ledger_client.post(f"/transactions/{tid}/review-approve", json=_decision(note="approved by test"))
    assert r.status_code == 200
    assert r.json()["status"] in FINAL_STATES

    replay = ledger_client.post(f"/transactions/{tid}/review-approve", json=_decision())
    assert replay.status_code == 200
    assert replay.json().get("idempotent_replay") is True

    assert ledger_client.get("/journal/integrity-check").json()["healthy"] is True
    assert ledger_client.get("/ledger/consistency-check").json()["healthy"] is True


@requires_stack
def test_review_endpoints_ignore_payments_that_are_not_held(api_client, ledger_client):
    tid = _settled_payment(api_client)
    r = ledger_client.post(f"/transactions/{tid}/review-reject", json=_decision())
    assert r.status_code == 200
    assert r.json()["status"] == "SETTLED"
    assert r.json().get("idempotent_replay") is True
    assert ledger_client.get(f"/transactions/{tid}").json()["status"] == "SETTLED"


def _has_reconciliation(ledger_client, tid):
    timeline = ledger_client.get(f"/transactions/{tid}/timeline").json()["timeline"]
    return any(e["event"].startswith("RECONCILIATION") for e in timeline)


def _wait_for_reconciliation(ledger_client, ids, seconds):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if all(_has_reconciliation(ledger_client, i) for i in ids):
            return True
        time.sleep(2)
    return False


@requires_stack
def test_payment_and_refund_both_get_reconciled(api_client, ledger_client):
    """Via the Kafka consumer if it is running, otherwise via the batch sweep (which must also cover refunds)."""
    pay_id = _settled_payment(api_client, amount_minor=700)
    refund = api_client.post(f"/payments/{pay_id}/refund")
    assert refund.status_code == 200
    refund_id = refund.json()["refund_transaction_id"]
    ids = [pay_id, refund_id]

    if _wait_for_reconciliation(ledger_client, ids, seconds=20):
        return

    headers = {"Authorization": f"Bearer {issue_token('test-suite')}"}
    try:
        batch = httpx.post(f"{RECON_SERVICE}/reconcile/run-batch", headers=headers, timeout=30.0)
    except httpx.HTTPError:
        pytest.skip("reconciliation service is not reachable on :8003")
    assert batch.status_code == 200
    assert _wait_for_reconciliation(ledger_client, ids, seconds=10), \
        "batch sweep must reconcile settled payments and refunds even when the event was missed"


@requires_stack
def test_financial_health_has_no_violations_after_activity():
    health = httpx.get("http://localhost:8001/financial-health", timeout=10.0).json()
    failing = [c for c in health["checks"] if c["status"] != "PASS"]
    assert health["overall_status"] == "PASS", f"failing invariants: {failing}"
