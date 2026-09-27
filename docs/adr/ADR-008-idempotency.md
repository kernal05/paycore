# ADR-008: Two-layer idempotency, keyed on request hash

**Status:** Accepted

## Context
Clients retry on timeout. Without protection, a retried payment request
could double-charge. A bare idempotency key isn't quite enough either: a
client bug that reuses a key for a genuinely different request should be
rejected, not silently actioned or silently ignored.

## Decision
Two layers:
1. `payment_requests` (payment-api): keyed on `idempotency_key`, bound to
   `compute_request_hash()` of the semantically-relevant fields. Same key
   + same hash -> replay the stored response. Same key + different hash ->
   409 Conflict.
2. `transactions.idempotency_key` (ledger-service): a hard uniqueness
   constraint, so even a bug in layer 1 cannot cause a double-post — this
   is the money-safety backstop, not just a UX nicety.

## Consequences
- A client can safely retry any payment request without changing its
  idempotency key.
- Idempotency records grow unboundedly without a retention policy — a
  production version would TTL/archive rows older than e.g. 30 days.
