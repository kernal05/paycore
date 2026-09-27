# Fintech Payment, Fraud & Ledger Platform

A production-inspired payments platform — not a demo that happens to use
the right buzzwords. **Payment API → Fraud Engine → Double-Entry Ledger →
Reconciliation**, with a real payment state machine, the outbox/inbox
patterns for reliable eventing, two-layer idempotency, signed webhooks,
refunds that never mutate the original ledger entry, and a processor
abstraction that models real-world timeout/unknown outcomes instead of
pretending every authorization succeeds or fails cleanly.

Built to demonstrate DevOps/SRE + fintech-domain engineering together,
modeled on the problem Stripe/Wise/Airwallex/a bank's digital-payments arm
actually solve: move money correctly, catch fraud before it settles,
never lose track of a rupee, and stay up.

## What's actually in here

| Area | What's implemented |
|---|---|
| **Payment state machine** | `CREATED → RISK_CHECK → {BLOCKED\|AUTHORIZED} → PROCESSING → {SETTLED\|FAILED\|UNKNOWN} → RECONCILIATION → ...`, enforced centrally (`common/state_machine.py`) — no endpoint can jump straight to SETTLED |
| **Idempotency (two layers)** | Client-facing: key bound to a request-body hash, 409 on reuse with a different payload. Ledger-level: hard uniqueness constraint as the money-safety backstop |
| **Ledger** | Real double-entry bookkeeping: matched DEBIT+CREDIT, row-locked in a deadlock-safe order, **DB-enforced** balance via a deferred constraint trigger (not just app-level checks), plus a separate balance-vs-journal consistency check |
| **Outbox pattern** | Ledger writes the domain event in the *same* DB transaction as the posting; a dedicated `outbox-publisher` worker polls and publishes to Kafka — "posted but event never published" is structurally impossible |
| **Inbox pattern** | Reconciliation's Kafka consumer dedupes by `(event_id, consumer_name)` — an at-least-once redelivery is a no-op |
| **Refunds/reversals** | Post as a brand-new, linked transaction with reversed legs — the original transaction's journal entries are never touched or deleted |
| **Processor abstraction** | `PaymentProcessor` interface + `MockProcessor` with a realistic outcome distribution (SUCCESS/DECLINED/TIMEOUT/UNKNOWN/DUPLICATE/SLOW_RESPONSE) wired into the actual posting flow — TIMEOUT/UNKNOWN park the transaction with **no money moved** until resolved |
| **Webhooks** | `/webhooks/processor` resolves parked UNKNOWN transactions, with HMAC signature verification, timestamp-freshness replay protection, and event-id idempotency — all three required |
| **Fraud** | Explainable rule engine — amount, Redis sliding-window velocity, new-device detection — every decision returns machine-readable `reasons` |
| **Reconciliation** | Categorized exceptions (MISSING_PROCESSOR/BANK, AMOUNT_MISMATCH, STATUS_MISMATCH...), an exceptions queue with resolve/replay, both event-driven and scheduled-batch triggers |
| **Auth** | API-key + rate limiting at the edge; short-lived HMAC service tokens between internal services; all LOCAL-ONLY defaults clearly marked (see ADR-010) |
| **Resilience** | Circuit breakers around every inter-service call (fraud, ledger) |
| **Observability** | Shared Prometheus metrics + structured JSON logs from one `common/` module across all 5 services |
| **SLOs** | Per-service targets with **multi-window burn-rate alerts** (fast 5m/1h + slow 2h/24h), not just a single threshold |
| **Infra as code** | Terraform: multi-AZ VPC, EKS, Multi-AZ RDS + read replica, MSK Kafka, ElastiCache Redis |
| **Kubernetes** | Zero-downtime rolling deploys, readiness/liveness probes, HPA, AZ topology spread, CronJob for scheduled reconciliation |
| **CI/CD** | Lint → unit tests → **real docker-compose integration tests** → Trivy image scan → Terraform validate → build/push → manual-approval-gated deploy |
| **Testing** | 23 unit tests (state machine, idempotency, circuit breaker, mock processor, webhook verification) + integration tests that fire 20 concurrent transfers and assert the ledger balance is exact and stays in balance |
| **Reliability engineering** | Runbook (10 incident types), DR plan (RTO/RPO, cross-region failover), 5 named chaos experiments — **executed, with real results below** |
| **Architecture Decision Records** | 17 ADRs in `docs/adr/` — the *why* behind Postgres, Kafka, outbox, immutable ledger, auth tradeoffs, processor abstraction, webhook security, the recovery engine, invariant monitor, forensic timeline, and the real bugs found by running it |
| **Financial Invariant Monitor** | `GET /financial-health` — 11 correctness properties re-derived across the *whole* ledger on demand (ledger balances to zero, no orphan/duplicate legs, balance matches journal history, refund amount never exceeds original, terminal non-settled transactions moved no money, settlements reconcile within 15 min, UNKNOWN resolves at most once, webhook events never reused), not just checked once at transaction time |
| **Payment Recovery Engine** | `POST /recovery/run-sweep` — automated, backoff-scheduled processor status queries for transactions parked in UNKNOWN, escalating to a `MANUAL_REVIEW` state with a named-human resolution endpoint after exhausting retries, full audit trail in `recovery_attempts` |
| **Payment Forensic Timeline** | `GET /transactions/{id}/timeline` — every recorded event touching a transaction (state transitions, recovery attempts, reconciliation, webhooks, outbox events) merged into one ordered sequence — the answer to "this payment says UNKNOWN, what happened?" |

See `docs/ARCHITECTURE.md` for the full design and diagrams.

## Empirical validation

The platform has actually been run, not just written. In a sandbox with no
Docker daemon and no Kafka broker (network locked to package registries
only), the services were run directly — real PostgreSQL 16, real Redis 7,
real uvicorn processes — and exercised for real:

- **32/32 unit tests pass** (state machine, idempotency, circuit breaker,
  mock processor, webhook verification, recovery backoff)
- **11/11 integration tests pass**, including 20 concurrent transfers with
  an exact-balance assertion
- **517 load-test requests, 0% HTTP errors**, including 100 concurrent
  writers hitting the same account — ledger integrity and balance-vs-journal
  consistency stayed healthy throughout (full results, including the
  identified lock-contention bottleneck, in `docs/PERFORMANCE.md`)
- **4 chaos experiments executed** — payment-api, Redis, PostgreSQL, and
  ledger-service kills, each with a measured recovery time and a
  post-experiment integrity check (`chaos/RESULTS.md`)
- **3 real bugs found and fixed** by actually running the stack — a
  SQLAlchemy autobegin conflict, a missing service-auth header on one
  internal call, and unseeded genesis journal entries — documented
  honestly in `docs/adr/ADR-014-bugs-found-by-running-it.md` rather than
  quietly patched and forgotten
- **Financial Invariant Monitor, Recovery Engine, and Forensic Timeline
  verified live** against a freshly-recreated database: `financial-health`
  reports 0 violations on a clean ledger and after real payments; the
  recovery sweep found a parked transaction, queried the processor, and
  settled it end-to-end; the timeline correctly reconstructed all 4 events

**What's explicitly NOT verified**: the Kafka/outbox event-flow path
(no broker available in this sandbox), and anything cloud-specific in
Terraform (no AWS account exercised — the IaC is a design, not a claimed
deployment). See the truth table below for the exact line between
"implemented" and "tested live."

## Truth table

| Capability | Implemented | Tested live | Evidence |
|---|---|---|---|
| Idempotency (2-layer) | ✅ | ✅ | Integration tests |
| State machine | ✅ | ✅ | Unit + integration |
| Double-entry ledger | ✅ | ✅ | Integrity + consistency checks |
| Outbox | ✅ | ⚠️ | DB-transactional part tested; Kafka publish NOT RUN (no broker) |
| Inbox | ✅ | ⚠️ | Logic unit-tested; Kafka consumer NOT RUN (no broker) |
| Webhooks | ✅ | ✅ | Unit tests (signature/replay) |
| Refunds | ✅ | ✅ | Integration test |
| Reconciliation | ✅ | ✅ | Direct service calls |
| Processor abstraction | ✅ | ✅ | Unit + live recovery sweep |
| Circuit breaker | ✅ | ✅ | Unit tests |
| Rate limiting | ✅ | ✅ | Present in payment-api |
| Financial Invariant Monitor | ✅ | ✅ | Live: PASS on clean + post-activity ledger |
| Payment Recovery Engine | ✅ | ✅ | Live: found, queried, settled a parked transaction |
| Forensic Timeline | ✅ | ✅ | Live: 4-event timeline reconstructed correctly |
| Redis failure recovery | ✅ | ✅ | Chaos experiment 03 |
| PostgreSQL failure recovery | ✅ | ✅ | Chaos experiment 04 |
| Ledger-service failure recovery | ✅ | ✅ | Chaos experiment 05 |
| Kafka failure recovery | design only | ❌ | NOT RUN — no broker in this sandbox |
| Load testing | ✅ | ✅ | 517 requests, 0% errors |
| AWS deployment | IaC only | ❌ | Not claimed — see `docs/ARCHITECTURE.md` |
| Multi-region DR | design only | ❌ | Simulation/design in `docs/DISASTER_RECOVERY.md`, not executed |

## Honesty about what's not fully built

This project follows its own rule: don't claim things that aren't real.
- **Kafka/outbox event delivery** genuinely hasn't been exercised — no
  broker was available in the environment used to validate this. The
  transactional-write half of the outbox pattern (the part that matters
  most — no dual-write) is real and DB-tested; the publish-to-Kafka half
  is unverified here.
- **DLQ/retry** is a primitive (`common/kafka_utils.py`) used by design in
  one place, not blanket-applied everywhere — see `docs/ARCHITECTURE.md`'s
  "intentionally simplified" section for why.
- **Auth is HMAC + API-key allowlist**, explicitly not a production IdP —
  ADR-010 says exactly what a funded production version would use instead.
- **Multi-processor intelligent routing was considered and deliberately
  not built** — the single-processor abstraction already demonstrates the
  interface pattern; adding a second mock processor and routing logic
  would be surface area without new correctness lessons.

## Quickstart (local, docker-compose)

```bash
docker compose up --build
```

Brings up Postgres, Redis, Kafka, all 5 services, Prometheus (`:9090`),
and Grafana (`:3000`, admin/admin — LOCAL ONLY, see ADR-010).

**Send a payment** (note the required `X-API-Key`):

```bash
curl -X POST http://localhost:8000/payments \
  -H "Content-Type: application/json" \
  -H "X-API-Key: demo-key-local-only" \
  -d '{
    "idempotency_key": "demo-txn-001",
    "source_account_id": "11111111-1111-1111-1111-111111111111",
    "dest_account_id": "22222222-2222-2222-2222-222222222222",
    "amount_minor": 250000,
    "currency": "INR",
    "device_id": "device-abc",
    "ip_address": "203.0.113.5"
  }'
```

Depending on `MockProcessor`'s deterministic-per-transaction outcome, the
response is `APPROVED_AND_SETTLED`, `FAILED`, `REVIEW`, `BLOCKED`, or
`UNKNOWN` (parked, pending a webhook — see below).

**Check the ledger and its integrity:**

```bash
curl http://localhost:8001/accounts/11111111-1111-1111-1111-111111111111/balance
curl http://localhost:8001/journal/integrity-check
curl http://localhost:8001/ledger/consistency-check
```

**Refund a settled payment:**

```bash
curl -X POST http://localhost:8000/payments/<transaction_id>/refund \
  -H "X-API-Key: demo-key-local-only"
```

**Resolve a parked UNKNOWN transaction via webhook** (in real life the
processor sends this; here's how to simulate one):

```python
import hashlib, hmac, json, time, requests

secret = "local-dev-only-webhook-secret-DO-NOT-USE-IN-PROD"
body = json.dumps({"event_id": "evt_1", "transaction_id": "<txn_id>", "resolved_status": "SETTLED"}).encode()
ts = int(time.time())
sig = hmac.new(secret.encode(), f"{ts}.{body.decode()}".encode(), hashlib.sha256).hexdigest()
requests.post("http://localhost:8000/webhooks/processor", data=body,
              headers={"X-Webhook-Signature": sig, "X-Webhook-Timestamp": str(ts), "Content-Type": "application/json"})
```

**Check reconciliation exceptions:**

```bash
curl http://localhost:8003/reconcile/report
curl http://localhost:8003/reconciliation/exceptions
```

**Check financial health** (11 correctness invariants, re-derived live):

```bash
curl http://localhost:8001/financial-health
```

**Run the recovery sweep** (resolves transactions parked in UNKNOWN by
querying the processor, on a backoff schedule):

```bash
curl -X POST http://localhost:8001/recovery/run-sweep
curl http://localhost:8001/recovery/pending
```

**Get a payment's forensic timeline** (every event, one ordered sequence):

```bash
curl http://localhost:8001/transactions/<transaction_id>/timeline
```

## Testing

```bash
pip install -r requirements.txt -r requirements-dev.txt

pytest tests/unit -v          # 23 tests, no infra needed, ~0.5s

docker compose up --build -d
pytest tests/integration -v   # idempotency, refunds, ledger integrity, 20 concurrent transfers
```

## Chaos and load testing

```bash
python3 chaos/chaos_test.py       # 5 named experiments -> chaos/RESULTS.md
k6 run chaos/load_test.js         # -> paste real output into docs/PERFORMANCE.md
```

## Deploying to AWS

```bash
cd infra/terraform
terraform init
terraform apply   # VPC, EKS, RDS (Multi-AZ), MSK, ElastiCache

kubectl apply -f ../k8s/
```

`.github/workflows/ci-cd.yml` runs lint → unit tests → full docker-compose
integration tests → Trivy scan → `terraform validate` → build/push to
GHCR → a manually-approved production deploy.

## Documentation map

- `docs/ARCHITECTURE.md` — full design, diagrams, service responsibilities
- `docs/adr/` — 12 ADRs explaining every non-obvious decision
- `docs/SLO.md` — per-service SLOs and multi-window burn-rate alerting
- `docs/RUNBOOK.md` — 10 incident types with concrete triage steps
- `docs/DISASTER_RECOVERY.md` — RTO/RPO targets and the failover procedure
- `docs/PERFORMANCE.md` — load-testing methodology (template; run it yourself)
- `chaos/RESULTS.md` — chaos experiment results (template; run it yourself)

## What I'd build next

- Multi-currency/FX quotes between accounts of different currencies
- A real second processor implementation behind `ProcessorRouter`
- mTLS between services via a service mesh, replacing the HMAC token scheme
- A scheduled job to escalate transactions stuck in UNKNOWN past a timeout
- Terraform environments (`dev`/`staging`/`prod`) instead of a single config
