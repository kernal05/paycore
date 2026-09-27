from common.idempotency import compute_request_hash


def test_same_payload_same_hash():
    a = {"source_account_id": "1", "dest_account_id": "2", "amount_minor": 100, "currency": "INR"}
    b = {"currency": "INR", "amount_minor": 100, "dest_account_id": "2", "source_account_id": "1"}
    assert compute_request_hash(a) == compute_request_hash(b), "key order must not affect the hash"


def test_different_amount_different_hash():
    a = {"source_account_id": "1", "dest_account_id": "2", "amount_minor": 100, "currency": "INR"}
    b = {"source_account_id": "1", "dest_account_id": "2", "amount_minor": 200, "currency": "INR"}
    assert compute_request_hash(a) != compute_request_hash(b)


def test_different_destination_different_hash():
    a = {"source_account_id": "1", "dest_account_id": "2", "amount_minor": 100, "currency": "INR"}
    b = {"source_account_id": "1", "dest_account_id": "3", "amount_minor": 100, "currency": "INR"}
    assert compute_request_hash(a) != compute_request_hash(b)
