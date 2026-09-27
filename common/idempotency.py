"""Idempotency-key-bound-to-request-hash (ADR-008).

An idempotency key alone isn't enough: if a client reuses PAY-123 for two
*different* payloads (a bug, or a copy-pasted key), silently returning the
first result — or worse, processing the second payload — is wrong in both
directions. Binding the key to a hash of the semantically-relevant request
fields lets us tell "this is a safe retry" apart from "this is a different
request that happens to reuse a key" and return 409 for the latter.
"""
import hashlib
import json


def compute_request_hash(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()
