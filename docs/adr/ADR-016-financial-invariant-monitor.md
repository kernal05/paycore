# ADR-016: Financial Invariant Monitor

**Status:** Accepted

## Context
Every other correctness mechanism in this platform (the DB trigger, the
idempotency layer, the state machine) checks a property *at the moment a
transaction happens*. None of them would catch drift introduced later —
a bad migration, a manual DB fix, a future bug in a code path that doesn't
touch the trigger — because by definition nothing is "happening" for them
to check at that moment.

## Decision
`GET /financial-health` re-derives 11 correctness properties across the
*entire* ledger on every call: journal balance, orphan/duplicate legs,
balance-vs-journal consistency, refund invariants (references exactly one
original, never exceeds it), terminal non-settled transactions moved no
money, timestamps never go backwards, settlements reconcile within 15
minutes, UNKNOWN resolves at most once, webhook events are never reused
across transactions. Each check is a single SQL query whose result set IS
the violation list — "PASS" always means "zero found," never "not checked."

## Consequences
- Cheap enough to run on a schedule (a Prometheus scrape interval) rather
  than only during an incident — correctness becomes a continuously
  monitored property, not a one-time-checked one.
- Deliberately overlaps with `/journal/integrity-check` and
  `/ledger/consistency-check` (kept as separate endpoints too, since
  they're referenced by name in existing alerting rules) — the invariant
  monitor is the superset, not a replacement.
- Verified live: reports 0 violations against a freshly-seeded database
  and again after real payments settle — see the README's empirical
  validation section.
