# ADR-012: Signed, timestamped, deduplicated webhooks

**Status:** Accepted

## Context
`/webhooks/processor` resolves UNKNOWN transactions and can move money
(via `resolve-unknown`'s SETTLED path). An unauthenticated or replayable
webhook endpoint that can trigger money movement is a serious
vulnerability.

## Decision
Three independent checks, all required (`common/webhooks.py`):
1. HMAC-SHA256 signature over the raw body + timestamp, shared secret.
2. Timestamp freshness (reject anything older than 5 minutes, or from the
   future beyond clock-skew tolerance).
3. `webhook_events(event_id)` idempotency — a redelivered webhook with the
   same processor-issued event_id is a no-op.

## Consequences
This mirrors how real processors (Stripe et al.) expect their webhooks to
be verified — the pattern transfers directly to a real integration, only
the signing secret's origin (a mock secret vs. a processor-issued one)
would change.
