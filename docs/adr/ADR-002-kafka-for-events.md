# ADR-002: Kafka as the event backbone

**Status:** Accepted

## Context
Multiple services (reconciliation, future notifications/settlement) need
to react to "a transaction was posted" without being in the synchronous
request path of the payment itself.

## Decision
Kafka carries domain events (`ledger.posted`, `reconciliation.mismatch`)
published via the outbox pattern (ADR-003). The ledger posting itself
never waits on Kafka.

## Consequences
- A slow or down consumer cannot block money movement.
- Consumers must handle at-least-once delivery — see ADR-009 (inbox
  pattern).
- Adds an operational dependency (MSK in production) with its own
  failure modes, sized for in `infra/terraform/msk_redis.tf`.
