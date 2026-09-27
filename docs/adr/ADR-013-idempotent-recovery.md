# ADR-013: Idempotent recovery keyed on actual persisted state, not assumed sequence

**Status:** Accepted (supersedes the original ADR-008 recovery behavior)

## Context

The original implementation re-drove the full register → fraud-score →
risk-result → post sequence on every retry, assuming the transaction was
always exactly where a fresh request would find it. If payment-api
crashed *after* the ledger had already progressed past CREATED (e.g. after
risk-result succeeded but before payment-api's own response was recorded),
a retry would call `risk-result` again on a transaction that was already
AUTHORIZED or BLOCKED — an illegal transition per `common/state_machine.py`
— surfacing as an unhandled 409/500 to the client instead of a clean
replay. This was flagged in review as the one remaining gap before calling
idempotency production-grade.

## Decision

Two changes, together:

1. **Ledger endpoints became idempotent w.r.t. persisted state.**
   `POST /transactions/{id}/risk-result` now checks the transaction's
   actual current status first: if it's already past `CREATED`, it returns
   the previously-persisted decision instead of attempting the transition
   again. `POST /transactions/{id}/post` does the same for
   `SETTLED`/`FAILED`/`UNKNOWN` — returns the existing result rather than
   re-attempting a posting that already happened. Calling either endpoint
   from a genuinely wrong state (e.g. `post` before `risk-result` ran) is
   still a real error and returns 409, not a silent guess.
2. **payment-api branches on the transaction's actual current status**
   after `register`, instead of assuming CREATED. If it's already past
   risk-check, payment-api fetches the persisted fraud score/decision/reasons
   (added `transactions.fraud_reasons` for this) rather than re-running
   fraud scoring — which matters because fraud scoring has side effects
   (Redis velocity counters, known-device set) that must not be
   double-counted on a replay.

## Consequences

- A crash at *any* point in the payment-api orchestration now recovers
  deterministically to the correct next step (or the correct final result,
  if it had already finished) on retry, with the same idempotency_key.
- Fraud scoring runs exactly once per transaction, even across retries —
  its side effects on Redis state are no longer at risk of double-counting.
- `RISK_CHECK` is still treated as a state that should never be observed as
  persisted (it's only ever committed atomically alongside its next state
  within a single ledger-service DB transaction — a mid-transaction crash
  rolls back to the prior state instead of leaving it stranded). If it's
  ever observed, payment-api raises a 500 explicitly asking for
  investigation rather than guessing how to proceed with someone's money —
  a deliberate fail-loud choice, not an oversight.
- `tests/integration/test_idempotency_recovery.py` reproduces the crash
  scenario directly (advancing a transaction via the ledger's own API,
  bypassing payment-api, then retrying through payment-api) to prove the
  fix rather than just asserting it in prose.

## Addendum: PROCESSING is no longer a state that "never happens"

A related review comment pointed out that `/transactions/{id}/post`
originally called the (mock) processor — a network dependency that can
take up to 5 seconds on TIMEOUT — from *inside* the same DB transaction
that held `SELECT ... FOR UPDATE` locks on both accounts. In production,
holding a row lock for the duration of an external network call is exactly
the kind of thing that turns one slow dependency into cascading lock
contention across every other transfer touching the same accounts.

**Fix:** `post_transaction` is now split into three phases: (1) commit
`AUTHORIZED -> PROCESSING` in its own short transaction with no account
locks taken; (2) call the processor with **no transaction open and no
locks held**; (3) apply the result in a second short transaction that
takes account locks only for the few statements needed to actually move
money (or record FAILED/UNKNOWN).

This means `PROCESSING` **can** now be legitimately observed as a
persisted state — if a crash happens between phases 1 and 3, that's
exactly where the transaction sits. This is intentional, not a regression:
`/post` treats `PROCESSING` as a valid resume point (phase 2/3 re-run
safely, since `MockProcessor`'s outcome is deterministic per
`transaction_id`), and payment-api treats `PROCESSING` the same as
`AUTHORIZED` — call `/post` again — rather than the 500-and-investigate
path reserved for `RISK_CHECK`.
