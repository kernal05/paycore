# ADR-005: Immutable, append-only journal; refunds are new transactions

**Status:** Accepted

## Context
A financial ledger must be auditable. If a refund could edit or delete the
original transaction's journal entries, there would be no way to prove
what actually happened at the time of the original payment.

## Decision
`journal_entries` is insert-only — no UPDATE or DELETE statement against it
exists anywhere in the codebase. A refund posts a brand-new transaction
(`transaction_type = 'REFUND'`) with reversed DEBIT/CREDIT legs, linked to
the original via `original_transaction_id`. The original transaction's
own journal entries are never touched.

## Consequences
- The audit trail is permanent and reconstructable at any point in time.
- Balances are always the sum of journal history, not a mutable snapshot
  — see `/ledger/consistency-check`, which proves this stays true.
- Storage grows monotonically; this is the accepted cost of auditability
  in every real accounting system.
