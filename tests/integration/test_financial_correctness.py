"""Integration tests against the live docker-compose stack.

Run with:
    docker compose up --build -d
    pytest tests/integration -v

These are the tests that make the platform "engineering evidence, not a
collection of YAML files" — they exercise real HTTP calls across real
services against a real Postgres, and assert the properties that actually
matter for a payments platform: no double-posting, no lost money, no
imbalanced ledger, ever — even under concurrency.
"""
import concurrent.futures
import uuid

import httpx

from .conftest import requires_stack, make_payment, CUSTOMER_ACCOUNT, MERCHANT_ACCOUNT


@requires_stack
def test_payment_settles_and_moves_money(api_client, ledger_client, idem_key):
    before = ledger_client.get(f"/accounts/{CUSTOMER_ACCOUNT}/balance").json()["balance_minor"]

    resp = make_payment(api_client, idem_key, amount_minor=1500)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] in ("APPROVED_AND_SETTLED", "REVIEW", "BLOCKED")

    if body["status"] == "APPROVED_AND_SETTLED":
        after = ledger_client.get(f"/accounts/{CUSTOMER_ACCOUNT}/balance").json()["balance_minor"]
        assert after == before - 1500


@requires_stack
def test_idempotent_replay_returns_same_transaction(api_client, idem_key):
    r1 = make_payment(api_client, idem_key, amount_minor=2000)
    r2 = make_payment(api_client, idem_key, amount_minor=2000)  # exact same payload, retried

    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["transaction_id"] == r2.json()["transaction_id"]
    assert r2.json()["idempotent_replay"] is True


@requires_stack
def test_idempotency_key_reuse_with_different_payload_is_rejected(api_client, idem_key):
    r1 = make_payment(api_client, idem_key, amount_minor=1000)
    assert r1.status_code == 200

    r2 = make_payment(api_client, idem_key, amount_minor=9999)  # same key, different amount
    assert r2.status_code == 409


@requires_stack
def test_missing_api_key_is_rejected():
    with httpx.Client(base_url="http://localhost:8000") as c:  # no X-API-Key header
        resp = c.post("/payments", json={
            "idempotency_key": str(uuid.uuid4()), "source_account_id": CUSTOMER_ACCOUNT,
            "dest_account_id": MERCHANT_ACCOUNT, "amount_minor": 100, "currency": "INR",
        })
    assert resp.status_code == 401


@requires_stack
def test_refund_reverses_balance_without_mutating_original(api_client, ledger_client, idem_key):
    before = ledger_client.get(f"/accounts/{CUSTOMER_ACCOUNT}/balance").json()["balance_minor"]

    pay = make_payment(api_client, idem_key, amount_minor=3000)
    if pay.json()["status"] != "APPROVED_AND_SETTLED":
        return  # fraud engine held it for review/blocked this run — not what this test is checking

    txn_id = pay.json()["transaction_id"]
    original_before_refund = ledger_client.get(f"/transactions/{txn_id}").json()
    assert original_before_refund["status"] == "SETTLED"

    refund = api_client.post(f"/payments/{txn_id}/refund")
    assert refund.status_code == 200

    after = ledger_client.get(f"/accounts/{CUSTOMER_ACCOUNT}/balance").json()["balance_minor"]
    assert after == before, "refund must fully restore the original balance"

    original_after_refund = ledger_client.get(f"/transactions/{txn_id}").json()
    assert original_after_refund["status"] == "REFUNDED"
    assert original_after_refund["amount_minor"] == original_before_refund["amount_minor"], \
        "the original transaction's own record must never be mutated by a refund"


@requires_stack
def test_ledger_integrity_check_stays_healthy_after_activity(ledger_client):
    result = ledger_client.get("/journal/integrity-check").json()
    assert result["healthy"] is True, f"found imbalanced transactions: {result['imbalanced_transactions']}"

    consistency = ledger_client.get("/ledger/consistency-check").json()
    assert consistency["healthy"] is True, f"found accounts whose balance disagrees with their journal: {consistency['inconsistent_accounts']}"


@requires_stack
def test_concurrent_transfers_never_corrupt_balance(api_client, ledger_client):
    """The core financial-correctness test: fire 20 concurrent payments at
    the same source account and verify afterward that (a) the balance
    dropped by exactly the sum of whatever actually settled, (b) the
    ledger never went negative, and (c) the journal is still balanced.
    This is what the row-locking in ledger-service's /transactions/{id}/post
    exists to guarantee.
    """
    before = ledger_client.get(f"/accounts/{CUSTOMER_ACCOUNT}/balance").json()["balance_minor"]
    n = 20
    amount = 100

    def fire(i):
        return make_payment(api_client, f"concurrent-{uuid.uuid4()}", amount_minor=amount)

    with concurrent.futures.ThreadPoolExecutor(max_workers=n) as pool:
        results = list(pool.map(fire, range(n)))

    settled_count = sum(1 for r in results if r.status_code == 200 and r.json()["status"] == "APPROVED_AND_SETTLED")

    after = ledger_client.get(f"/accounts/{CUSTOMER_ACCOUNT}/balance").json()["balance_minor"]
    assert after == before - (settled_count * amount)
    assert after >= 0, "balance must never go negative"

    integrity = ledger_client.get("/journal/integrity-check").json()
    assert integrity["healthy"] is True
