# ADR-009: Inbox pattern for Kafka consumers

**Status:** Accepted

## Context
Kafka delivers at-least-once. A consumer that isn't idempotent will
double-process a redelivered message (e.g. after a consumer restart before
committing an offset), which for reconciliation would mean double-counting
or re-alerting on the same mismatch.

## Decision
`inbox_events (event_id, consumer_name)` records that a specific consumer
group has handled a specific event before acting on it — the same
event_id, redelivered, becomes a no-op. reconciliation-service's
`ledger.posted` consumer is the reference implementation.

## Consequences
- Consumers must construct a stable `event_id` (this platform uses
  `topic-partition-offset`, which is unique per message by construction).
- The inbox table grows unboundedly per consumer without a retention
  policy, same caveat as ADR-008.
