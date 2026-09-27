# ADR-015: Automated Payment Recovery Engine

**Status:** Accepted

## Context
A transaction that comes back TIMEOUT/UNKNOWN from processor authorization
parks in UNKNOWN until something resolves it. The webhook path (ADR-012)
handles the case where the processor proactively tells us. It doesn't
handle the case where it doesn't — a dropped webhook, a processor with no
webhook support for a given event, or a webhook that never fires because
the ambiguous state was never truly resolved processor-side until asked.

## Decision
A scheduled sweep (`POST /recovery/run-sweep`) actively queries the
processor's status for every UNKNOWN transaction whose exponential backoff
window has elapsed (30s, 60s, 120s... capped at 1h), records every attempt
in `recovery_attempts` (full audit trail — what was tried, when, what came
back), and escalates to a new `MANUAL_REVIEW` state after
`MAX_RECOVERY_ATTEMPTS` (default 4) rather than polling forever. Both the
webhook path and the recovery sweep funnel through the same
`_finalize_terminal_resolution` function, so there is exactly one code
path that ever moves money for a previously-UNKNOWN transaction, regardless
of which mechanism resolved it.

## Consequences
- Bounded, auditable automated recovery instead of an indefinite silent
  retry loop or a transaction that only ever resolves if a webhook happens
  to arrive.
- `MANUAL_REVIEW` requires a named human resolver and a note
  (`POST /transactions/{id}/manual-resolve`) — this is deliberate friction;
  a financial decision of last resort should never be anonymous.
- The backoff schedule is computed at query time from `recovery_attempts`
  history, not stored as a `next_attempt_at` column, so changing
  `RECOVERY_BACKOFF_BASE_SECONDS` doesn't need a migration.
