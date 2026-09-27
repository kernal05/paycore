# Performance / Load Testing — Real Results

**Run at:** 2026-09-26, against the real services (uvicorn + real PostgreSQL 16
+ real Redis 7) directly in the test sandbox — no Docker/Kafka available there
(locked-down network), so this substitutes a Python asyncio load generator
(`chaos/load_test_python.py`) for k6. `chaos/load_test.js` (real k6) is still
the canonical script — run it yourself for the docker-compose stack; these
numbers are from the Python substitute, honestly labeled as such.

All requests hit `/payments` with a single shared source account (deliberately,
to also stress the row-lock path), `X-API-Key` auth, unique idempotency keys.

## Results

| Concurrency | Duration | Requests | RPS | p50 | p95 | p99 | Max | Errors |
|---|---|---|---|---|---|---|---|---|
| 10 | 10s | 115 | 11.5 | 862ms | 1367ms | 1597ms | 1609ms | 0% |
| 50 | 15s | 220 | 14.7 | 3878ms | 5346ms | 5478ms | 5815ms | 0% |
| 100 | 15s | 201 | 13.4 | 8987ms | 9899ms | 9923ms | 9931ms | 0% |

**Post-run ledger check:** `/journal/integrity-check` → healthy: true.
`/ledger/consistency-check` → healthy: true, zero inconsistent accounts.
Zero errors across all 517 requests fired.

## What this actually shows

- **Zero errors at every concurrency level** — every request got a clean
  response and the ledger stayed correct throughout, including under 100
  concurrent writers hammering the same account. That's the property that
  matters most for a payments platform: it does not corrupt under load, it
  just gets slower.
- **Throughput plateaus around 12–15 RPS regardless of concurrency** — more
  concurrent clients did not increase completed requests/sec, only queued
  more work and pushed latency up linearly. This is exactly ADR-001's
  predicted bottleneck: every request in this test hits the *same* source
  account, so `SELECT ... FOR UPDATE` serializes all of them onto one lock.
  This is a single-hot-account worst case by construction (chosen to prove
  the lock is real), not the platform's aggregate throughput across many
  accounts.
- **Latency grows roughly linearly with queued concurrency** — p50 going
  862ms → 3878ms → 8987ms as concurrency goes 10 → 50 → 100 is consistent
  with a single serialized queue: more waiters, proportionally longer wait,
  not a sign of a slow individual request (a single unloaded request
  completes in well under 200ms, per the integration test suite's timing).

## Honest limitations of this run

- Single shared account by design (see above) — this measures the lock
  contention ceiling, not realistic multi-account throughput. A follow-up
  worth running: repeat with N distinct source accounts and confirm RPS
  scales with N until some other resource (DB connections, fraud-engine
  Redis round-trips) becomes the limit instead.
- Python asyncio generator, not k6 — fine for correctness-under-load
  validation (which is what mattered most here), but k6's HTTP engine is
  more efficient and closer to a fair production benchmark if the number
  itself needs to go in front of stakeholders.
- Single process, no Kafka/outbox in this run (same sandbox limitation
  noted in `chaos/RESULTS.md`) — the ledger-posting path measured here
  doesn't include outbox-publisher overhead, which is decoupled from the
  request path by design (ADR-003) and shouldn't affect these numbers
  anyway.

## Reproducing with real k6 against the full docker-compose stack

```bash
docker compose up --build -d
k6 run chaos/load_test.js
```
