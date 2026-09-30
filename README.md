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

The platform has actually been run — repeatedly, on real infrastructure,
with real failures found and fixed along the way.

- **32/32 unit tests pass**, **11/11 integration tests pass**, including
  20 concurrent transfers with an exact-balance assertion
- **Real Kafka, verified end-to-end** — `apache/kafka` (KRaft mode) running
  in the docker-compose stack. A live payment's outbox event shows
  `"published": true"`; the reconciliation-service's Kafka consumer
  independently matched real events (`total_checked: 2, match_rate: 1.0`)
  with zero manual intervention. This was the one gap flagged across every
  prior session as untested — it now genuinely works.
- **5/5 chaos experiments RECOVERED with healthy ledger integrity**:
  payment-api kill (5.3s), Kafka kill (0.4s), Redis kill (0.2s), PostgreSQL
  kill (0.4s), ledger-service kill (4.9s) — full results in
  `chaos/RESULTS.md`
- **Real load test against the live stack**, including the rate limiter
  genuinely engaging under concurrent load (visible as 429s in the status
  breakdown) — see `docs/PERFORMANCE.md` for the honest per-stage numbers
- **Deployed and verified on Kubernetes** (minikube) — the production
  manifests in `infra/k8s/` applied as-is, patched only for local image
  refs, with a live payment created, settled, and reconciled through the
  cluster (`financial-health`: 0 violations before and after). Scoped to
  the core synchronous payment path (postgres, redis, fraud-engine,
  ledger-service, payment-api) due to an 8GB dev-machine RAM ceiling — see
  `docs/adr/ADR-018-local-k8s-scope.md` for the honest reasoning. The full
  8-service stack including Kafka is proven separately via Docker Compose.
- **6 real bugs found and fixed by actually running the stack**, each
  documented rather than quietly patched:
  - SQLAlchemy autobegin conflict (`ADR-014`)
  - Missing service-auth header on one internal call (`ADR-014`)
  - Unseeded genesis journal entries (`ADR-014`)
  - A FastAPI route-ordering bug: `/reconcile/run-batch` was being
    swallowed by `/reconcile/{transaction_id}`, causing silent 500s,
    until the routes were reordered
  - A chaos-harness false negative: the post-recovery integrity check
    only waited 2s after a Postgres kill before judging health, which is
    shorter than Postgres sometimes needs to finish accepting connections
    — fixed with a retry loop instead of a single premature check
  - The load-test script computed real results but never wrote them to
    `docs/PERFORMANCE.md` — it only printed to stdout; fixed to actually
    persist the report

**What's explicitly out of scope for this dev machine, not the design**:
running the full 8-service stack (including Kafka) simultaneously inside
Kubernetes locally — the 8GB machine's control plane became unresponsive
under that load. The manifests for the full stack are unmodified and
deployable as-is on adequately-provisioned hardware or the AWS/EKS target
they're actually written for. Terraform itself remains unexercised (no
AWS account used) — IaC as design, not a claimed deployment.

## Truth table

| Capability | Implemented | Tested live | Evidence |
|---|---|---|---|
| Idempotency (2-layer) | ✅ | ✅ | Integration tests |
| State machine | ✅ | ✅ | Unit + integration |
| Double-entry ledger | ✅ | ✅ | Integrity + consistency checks |
| Outbox | ✅ | ✅ | DB-transactional + real Kafka publish confirmed |
| Inbox | ✅ | ✅ | Real Kafka consumer, deduped, reconciled live |
| Webhooks | ✅ | ✅ | Unit tests (signature/replay) |
| Refunds | ✅ | ✅ | Integration test |
| Reconciliation | ✅ | ✅ | Live Kafka-consumer path + route-bug fixed |
| Processor abstraction | ✅ | ✅ | Unit + live recovery sweep |
| Circuit breaker | ✅ | ✅ | Unit tests |
| Rate limiting | ✅ | ✅ | Live: 429s observed under real load test |
| Financial Invariant Monitor | ✅ | ✅ | Live: PASS on clean + post-activity ledger |
| Payment Recovery Engine | ✅ | ✅ | Live: found, queried, settled a parked transaction |
| Forensic Timeline | ✅ | ✅ | Live: real transaction timeline reconstructed |
| payment-api failure recovery | ✅ | ✅ | Chaos experiment 01 — 5.3s |
| Kafka failure recovery | ✅ | ✅ | Chaos experiment 02 — 0.4s |
| Redis failure recovery | ✅ | ✅ | Chaos experiment 03 — 0.2s |
| PostgreSQL failure recovery | ✅ | ✅ | Chaos experiment 04 — 0.4s |
| Ledger-service failure recovery | ✅ | ✅ | Chaos experiment 05 — 4.9s |
| Load testing | ✅ | ✅ | Real run, rate limiter engaging under load |
| Kubernetes deployment | ✅ | ✅ (scoped) | Live payment on minikube — `ADR-018` |
| AWS deployment | IaC only | ❌ | Not claimed — see `docs/ARCHITECTURE.md` |
| Multi-region DR | design only | ❌ | Simulation/design in `docs/DISASTER_RECOVERY.md`, not executed |

## Honesty about what's not fully built

This project follows its own rule: don't claim things that aren't real.
- **Full 8-service stack on Kubernetes** is unverified on this specific
  8GB dev machine — the control plane became unresponsive under that much
  simultaneous load. The manifests are unmodified and correct; this is a
  hardware ceiling for local verification, not a design gap. Full parity
  is proven on Docker Compose instead. See `ADR-018`.
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
