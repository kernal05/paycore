# ADR-017: Payment Forensic Timeline

**Status:** Accepted

## Context
"This payment says UNKNOWN, what happened?" previously meant grep-ing
audit_log, recovery_attempts, reconciliation_reports, webhook_events, and
outbox_events across services by hand, correlating on transaction_id and
timestamp manually.

## Decision
`GET /transactions/{id}/timeline` does that correlation once, server-side:
merges every recorded event touching a transaction from all five tables
into one chronologically-ordered sequence, each entry labeled with what
happened and by which actor.

## Consequences
- No new data is recorded to support this — it's a read-side aggregation
  over infrastructure (audit_log, outbox_events, etc.) that already exists
  for other reasons (ADR-003, ADR-009, ADR-015). Adding a new event source
  later (e.g., a future notifications service) is one more UNION, not a
  new subsystem.
- Verified live: correctly reconstructed a 4-event timeline (creation,
  recovery attempt, resolution, outbox event) for a real parked-then-recovered
  transaction — see the README's empirical validation section.
