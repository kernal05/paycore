# Architecture

## Why this shape

This platform models the core problem every payments company (Stripe,
Wise, Airwallex, a bank's digital-payments arm) actually has to solve:
move money correctly, catch fraud before it settles, never lose track of
a rupee, stay up, and handle the reality that a payment is a *process*,
not a single request/response. It's built as small, independently
deployable services with very different change rates and blast radii —
you want to redeploy the fraud engine's rules ten times a day without
ever touching the ledger.

Every non-obvious decision below has a corresponding ADR in `docs/adr/`
with the fuller reasoning and tradeoffs — this doc is the map, the ADRs
are the "why."

```
                              Client
                                │  X-API-Key + idempotency_key
                                ▼
                        ┌───────────────┐        ┌──────────────────┐
                        │  Payment API  │◀──────▶│ payment_requests │ (client-facing idempotency)
                        └───────┬───────┘        │ webhook_events   │ (webhook replay protection)
                                │                 └──────────────────┘
              ┌─────────────────┼──────────────────────┐
              │ register        │ risk-result           │ post / refund / resolve-unknown
              ▼                 ▼                       ▼
        ┌───────────────────────────────────────────────────┐
        │                 Ledger Service                    │
        │  state machine │ row-locked postings │ outbox      │──▶ outbox_events (same DB txn)
        └──────────────────────┬──────────────────────────┬─┘
                                │ authorize()               │
                                ▼                           │
                     MockProcessor (common/processors.py)   │
                     SUCCESS / DECLINED / TIMEOUT / UNKNOWN  │
                                                             │
        ┌────────────────────────────────────────────────────┘
        ▼
  Fraud Engine ──▶ Redis (velocity, known devices)
        │
        ▼
  score + reasons, back to Payment API's risk-result call

                        Outbox Publisher (worker)
                        polls outbox_events → Kafka
                                │
                                ▼
                             Kafka
                          (ledger.posted, reconciliation.mismatch)
                                │
                   ┌────────────┴─────────────┐
                   ▼ (inbox pattern)           ▼ (scheduled CronJob)
           Reconciliation Service ───▶ processor_records / bank_records
           categorized exceptions          reconciliation_reports
```

Asynchronous resolution path (for processor TIMEOUT/UNKNOWN):

```
processor authorize() → UNKNOWN → transaction parked, no money moved
                                        │
                    (minutes later) processor sends a webhook
                                        ▼
                   POST /webhooks/processor (signature + timestamp + event_id checked)
                                        │
                                        ▼
                 ledger-service: /transactions/{id}/resolve-unknown
                        → SETTLED (money moves now) or FAILED
```

## Service responsibilities

- **payment-api** — the only public entry point. Enforces the client-facing
  idempotency contract (`payment_requests`, ADR-008), orchestrates the
  ledger + fraud calls through circuit breakers (ADR-004), rate-limits by
  API key, and terminates asynchronous processor webhooks (ADR-012).
- **fraud-engine** — explainable, rule-based real-time risk scoring
  (amount, Redis sliding-window velocity, new-device detection). Every
  decision carries machine-readable `reasons`.
- **ledger-service** — the only writer of account balances and the owner
  of the payment state machine (`common/state_machine.py`). Enforces
  double-entry bookkeeping with a DB-level deferred constraint trigger
  (not just app-level checks — see `scripts/init_db.sql`), row-level
  locking in a fixed order (deadlock-free), calls the processor
  abstraction before moving money (ADR-011), and writes the outbox event
  in the same transaction as the posting (ADR-003).
- **outbox-publisher** — a standalone worker (not a FastAPI service) that
  polls `outbox_events` and publishes to Kafka, closing the dual-write
  gap between "money moved" and "an event exists describing it."
- **reconciliation-service** — categorizes mismatches
  (MISSING_PROCESSOR/BANK, AMOUNT_MISMATCH, STATUS_MISMATCH), exposes an
  exceptions queue (`/reconciliation/exceptions`, `/resolve`, `/replay`),
  and triggers both event-driven (Kafka consumer with inbox-pattern
  dedup, ADR-009) and scheduled-batch reconciliation.

## Two layers of idempotency, by design

1. **Client-facing** (`payment_requests` in payment-api): keyed on
   `idempotency_key`, bound to a hash of the request body. Same key + same
   body → replay the stored response. Same key + different body → 409.
2. **Money-safety backstop** (`transactions.idempotency_key` in
   ledger-service): a hard uniqueness constraint, so even a bug in layer 1
   cannot cause a double-post.

## Auth boundary

- Public: payment-api validates `X-API-Key` and applies a Redis-backed
  rate limit before anything else runs.
- Internal: every other service call carries a short-lived HMAC bearer
  token (`common/auth.py`) — see ADR-010 for exactly what this does and
  doesn't protect against, and what a funded production version would use
  instead (JWTs from a real IdP, or mTLS via a service mesh).
- Webhooks: signature + timestamp + idempotency, all three required
  (ADR-012) — this is the one inbound path that can move money
  asynchronously, so it gets the most scrutiny.

## Data model

`accounts` → `transactions` (state machine, ADR-005's immutable journal) →
`journal_entries` (append-only audit trail, DB-enforced balance) →
`outbox_events` / `inbox_events` (ADR-003/ADR-009) → `fraud_events` →
`processor_records` / `bank_records` / `webhook_events` →
`reconciliation_reports` (categorized, resolvable, replayable) →
`audit_log` (WHO/WHAT/WHEN/BEFORE/AFTER, append-only). Full schema and
constraints in `scripts/init_db.sql`.

## Testing

- `tests/unit/` — state machine transitions, idempotency hashing, circuit
  breaker state transitions, mock processor determinism, webhook signature
  verification. No infra required; run on every commit.
- `tests/integration/` — against the live `docker compose` stack: idempotent
  replay, 409 on key reuse with a different payload, refund correctness
  without mutating the original transaction, ledger integrity after
  activity, and 20 concurrent transfers with an assertion that the balance
  is exact and the journal stays balanced. This is where "the ledger is
  correct" stops being a claim and becomes something actually checked.

## What's intentionally simplified (and why)

- **MockProcessor** stands in for a real processor integration — building
  one needs a merchant account and real money. The interface
  (`PaymentProcessor`) is real; only the implementation behind it is mocked.
- **Auth is HMAC + API-key allowlist, not a real IdP** — see ADR-010 for
  the honest tradeoff and what production would use instead.
- **DLQ/retry is a primitive, not fully wired everywhere** — see
  `common/kafka_utils.py`'s `send_to_dlq`; reconciliation-service's
  consumer relies on its batch-sweep safety net instead, which is a
  simpler and equally correct design for that specific consumer.
- **Multi-currency/FX is not implemented** — every account is single-currency
  and a transfer requires matching currencies; this is the natural next
  addition (see the README's "what's next" section).
