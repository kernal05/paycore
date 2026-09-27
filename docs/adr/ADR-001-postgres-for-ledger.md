# ADR-001: PostgreSQL for the ledger

**Status:** Accepted

## Context
The ledger needs ACID transactions, row-level locking, and the ability to
enforce invariants (debit==credit) at the database level, not just in
application code.

## Decision
Use PostgreSQL, not a NoSQL store, for `accounts`, `transactions`, and
`journal_entries`. Use `SELECT ... FOR UPDATE` for pessimistic locking on
account rows during a transfer, and a deferred constraint trigger to
enforce journal balance at commit time (see `scripts/init_db.sql`).

## Consequences
- Strong consistency by default; no need to hand-roll distributed
  consensus for a single ledger write.
- Vertical scaling limits apply eventually — the read-replica in
  `infra/terraform/rds.tf` offloads reconciliation/analytics reads so they
  never compete with the payment write path for connections.
- Multi-region requires cross-region replication with a promotion step on
  failover (see `docs/DISASTER_RECOVERY.md`), not multi-master writes.
