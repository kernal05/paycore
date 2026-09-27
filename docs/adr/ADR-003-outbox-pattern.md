# ADR-003: Transactional outbox for event publishing

**Status:** Accepted

## Context
Publishing to Kafka right after committing a DB transaction is two
separate systems: if the process crashes between the commit and the
publish, the ledger says POSTED but no event was ever published — a
classic dual-write bug that silently desyncs the rest of the platform
(reconciliation never gets triggered, notifications never fire).

## Decision
Write the event to an `outbox_events` row in the *same* DB transaction as
the ledger posting. A separate `outbox-publisher` worker polls
`WHERE published_at IS NULL` and publishes to Kafka, marking rows
published after a successful flush.

## Consequences
- "Money moved" and "an event exists describing it" are now atomic.
- Adds latency between posting and event delivery bounded by the
  publisher's poll interval (1s by default) — acceptable for this
  platform's use cases (reconciliation, notifications), not appropriate
  for anything needing sub-100ms event delivery.
- The publisher itself must be monitored (its own `/healthz`, `/metrics`)
  since a permanently-down publisher means events silently stop flowing
  even though the ledger keeps working correctly.
