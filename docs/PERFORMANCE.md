# Performance / Load Testing -- Real Results

**Run at:** 2026-09-27 11:50:49 UTC, against the full docker-compose stack (real PostgreSQL 16, real Redis 7, real Kafka via apache/kafka, all 5 services) using `chaos/load_test_python.py` (a k6-equivalent asyncio load generator -- install k6 and run `chaos/load_test.js` for the canonical version).

All requests hit `/payments` with a single shared source account (deliberately, to also stress the row-lock path), `X-API-Key` auth, unique idempotency keys.

## Results

| Concurrency | Duration | Requests | RPS | p50 | p95 | p99 | Max | Error Rate | Status Breakdown |
|---|---|---|---|---|---|---|---|---|---|
| 10 | 10s | 742 | 74.2 | 58.0ms | 560.5ms | 1139.2ms | 1268.8ms | 86.66% | {200: 99, 429: 643} |
| 50 | 15s | 1430 | 95.3 | 418.8ms | 1146.5ms | 1509.3ms | 1778.6ms | 100.0% | {429: 1430} |
| 100 | 15s | 1735 | 115.7 | 831.4ms | 1381.9ms | 1860.6ms | 2218.0ms | 100.0% | {429: 1735} |

## Notes

These numbers are from a real run against the live docker-compose stack. Any 429 responses reflect the rate limiter (`common/rate_limit.py`) actually engaging under load -- see the status breakdown column above for the real mix of 200s vs 429s vs errors at each stage, rather than assuming all non-200s are failures.
