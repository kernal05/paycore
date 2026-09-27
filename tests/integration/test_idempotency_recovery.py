"""Reproduces the exact crash-recovery scenario flagged in review (see
ADR-013): a payment_requests row is left PENDING because payment-api
"crashed" after the ledger already progressed past CREATED, and a retry
through payment-api must recover cleanly instead of hitting an illegal
state transition.

We simulate the crash by advancing the ledger transaction directly via
ledger-service's own API (bypassing payment-api entirely) to put it in
AUTHORIZED, then hand the *same* idempotency_key + payload to payment-api
as if the client were retrying after a timeout.
"""
import uuid

from .conftest import requires_stack, CUSTOMER_ACCOUNT, MERCHANT_ACCOUNT


@requires_stack
def test_recovery_after_crash_between_risk_check_and_post(api_client, ledger_client):
    """payment-api never got to call /post — simulate that by stopping
    right after risk-result, then retry through payment-api and confirm
    it resumes (calls /post itself) rather than erroring.
    """
    idem_key = f"crash-sim-{uuid.uuid4()}"

    registered = ledger_client.post("/transactions/register", json={
        "idempotency_key": idem_key,
        "source_account_id": CUSTOMER_ACCOUNT,
        "dest_account_id": MERCHANT_ACCOUNT,
        "amount_minor": 500,
        "currency": "INR",
    }).json()
    txn_id = registered["transaction_id"]
    assert registered["status"] == "CREATED"

    risk = ledger_client.post(f"/transactions/{txn_id}/risk-result", json={
        "decision": "APPROVE", "fraud_score": 0.1, "fraud_reasons": [],
    }).json()
    assert risk["status"] == "AUTHORIZED"
    # <-- this is where we pretend payment-api crashed: risk-result done,
    #     /post never called, and no payment_requests row exists at all
    #     (payment-api never even started this attempt in our simulation).

    resp = api_client.post("/payments", json={
        "idempotency_key": idem_key,
        "source_account_id": CUSTOMER_ACCOUNT,
        "dest_account_id": MERCHANT_ACCOUNT,
        "amount_minor": 500,
        "currency": "INR",
    })

    assert resp.status_code == 200, f"expected clean recovery, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body["transaction_id"] == txn_id, "must resume the SAME transaction, not create a new one"
    assert body["status"] in ("APPROVED_AND_SETTLED", "FAILED", "UNKNOWN"), \
        "must reach a final state by calling /post itself, not get stuck"

    final = ledger_client.get(f"/transactions/{txn_id}").json()
    assert final["status"] in ("SETTLED", "FAILED", "UNKNOWN")


@requires_stack
def test_retry_after_full_completion_replays_cleanly(api_client, ledger_client, idem_key):
    """The simpler case: payment-api itself completed and recorded the
    response, and the client retries anyway (e.g. it never received the
    response due to a network blip). Must replay, not error or re-process.
    """
    from .conftest import make_payment

    first = make_payment(api_client, idem_key, amount_minor=750)
    assert first.status_code == 200

    second = make_payment(api_client, idem_key, amount_minor=750)
    assert second.status_code == 200
    assert second.json()["transaction_id"] == first.json()["transaction_id"]
    assert second.json()["status"] == first.json()["status"]
    assert second.json()["idempotent_replay"] is True


@requires_stack
def test_risk_result_endpoint_itself_is_idempotent(ledger_client):
    """Direct test of the ledger-level fix: calling risk-result twice on
    the same transaction must not raise an IllegalTransitionError.
    """
    idem_key = f"risk-idem-{uuid.uuid4()}"
    registered = ledger_client.post("/transactions/register", json={
        "idempotency_key": idem_key, "source_account_id": CUSTOMER_ACCOUNT,
        "dest_account_id": MERCHANT_ACCOUNT, "amount_minor": 200, "currency": "INR",
    }).json()
    txn_id = registered["transaction_id"]

    first = ledger_client.post(f"/transactions/{txn_id}/risk-result", json={
        "decision": "APPROVE", "fraud_score": 0.05, "fraud_reasons": [],
    })
    assert first.status_code == 200
    assert first.json()["status"] == "AUTHORIZED"

    second = ledger_client.post(f"/transactions/{txn_id}/risk-result", json={
        "decision": "APPROVE", "fraud_score": 0.05, "fraud_reasons": [],
    })
    assert second.status_code == 200, f"expected idempotent 200, got {second.status_code}: {second.text}"
    assert second.json()["status"] == "AUTHORIZED"
    assert second.json()["idempotent_replay"] is True


@requires_stack
def test_post_endpoint_itself_is_idempotent(ledger_client):
    """Direct test of the ledger-level fix: calling /post twice must not
    attempt to re-lock accounts or re-insert journal entries.
    """
    idem_key = f"post-idem-{uuid.uuid4()}"
    registered = ledger_client.post("/transactions/register", json={
        "idempotency_key": idem_key, "source_account_id": CUSTOMER_ACCOUNT,
        "dest_account_id": MERCHANT_ACCOUNT, "amount_minor": 300, "currency": "INR",
    }).json()
    txn_id = registered["transaction_id"]
    ledger_client.post(f"/transactions/{txn_id}/risk-result", json={
        "decision": "APPROVE", "fraud_score": 0.05, "fraud_reasons": [],
    })

    first = ledger_client.post(f"/transactions/{txn_id}/post")
    assert first.status_code == 200
    first_status = first.json()["status"]

    second = ledger_client.post(f"/transactions/{txn_id}/post")
    assert second.status_code == 200, f"expected idempotent 200, got {second.status_code}: {second.text}"
    assert second.json()["status"] == first_status
    assert second.json().get("idempotent_replay") is True

    integrity = ledger_client.get("/journal/integrity-check").json()
    assert integrity["healthy"] is True, "a double /post call must never create duplicate journal legs"
