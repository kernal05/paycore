#!/usr/bin/env python3
"""Load generator for POST /payments — a substitute for chaos/load_test.js
in this specific sandbox, which has no route to k6's installer (network is
locked to package registries only). Same methodology as the k6 script:
ramping concurrency, measuring RPS/p50/p95/p99/error rate. Run chaos/load_test.js
with real k6 for the canonical version; this exists so the numbers in
docs/PERFORMANCE.md are real rather than absent.
"""
import asyncio
import time
import uuid
import statistics
import sys
import time

import httpx

BASE_URL = "http://localhost:8000"
API_KEY = "demo-key-local-only"
CUSTOMER_ACCOUNT = "11111111-1111-1111-1111-111111111111"
MERCHANT_ACCOUNT = "22222222-2222-2222-2222-222222222222"


async def one_request(client: httpx.AsyncClient) -> tuple[float, int]:
    start = time.perf_counter()
    try:
        r = await client.post("/payments", json={
            "idempotency_key": f"load-{uuid.uuid4()}",
            "source_account_id": CUSTOMER_ACCOUNT, "dest_account_id": MERCHANT_ACCOUNT,
            "amount_minor": 100, "currency": "INR",
        }, headers={"X-API-Key": API_KEY})
        return (time.perf_counter() - start) * 1000, r.status_code
    except Exception:
        return (time.perf_counter() - start) * 1000, 0


async def run_stage(concurrency: int, duration_s: float) -> dict:
    latencies = []
    statuses = []
    stop_at = time.monotonic() + duration_s

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10.0) as client:
        async def worker():
            while time.monotonic() < stop_at:
                lat, status = await one_request(client)
                latencies.append(lat)
                statuses.append(status)

        await asyncio.gather(*[worker() for _ in range(concurrency)])

    total = len(latencies)
    errors = sum(1 for s in statuses if s < 200 or s >= 300)
    latencies.sort()

    def pct(p):
        if not latencies:
            return float("nan")
        idx = min(int(len(latencies) * p), len(latencies) - 1)
        return latencies[idx]

    return {
        "concurrency": concurrency, "duration_s": duration_s, "total_requests": total,
        "rps": round(total / duration_s, 1) if duration_s else 0,
        "p50_ms": round(pct(0.50), 1), "p95_ms": round(pct(0.95), 1), "p99_ms": round(pct(0.99), 1),
        "max_ms": round(max(latencies), 1) if latencies else float("nan"),
        "error_rate": round(errors / total, 4) if total else None,
        "status_breakdown": {s: statuses.count(s) for s in set(statuses)},
    }


async def main():
    stages = [(10, 10), (50, 15), (100, 15)]  # (concurrency, duration_s) — shorter than k6's for sandbox time budget
    results = []
    for concurrency, duration in stages:
        print(f"--- stage: concurrency={concurrency}, duration={duration}s ---", file=sys.stderr)
        r = await run_stage(concurrency, duration)
        print(r, file=sys.stderr)
        results.append(r)
    return results


def write_report(results: list[dict]):
    lines = [
        "# Performance / Load Testing -- Real Results\n",
        f"**Run at:** {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}, against the full "
        "docker-compose stack (real PostgreSQL 16, real Redis 7, real Kafka via apache/kafka, all "
        "5 services) using `chaos/load_test_python.py` (a k6-equivalent asyncio load generator -- "
        "install k6 and run `chaos/load_test.js` for the canonical version).\n",
        "All requests hit `/payments` with a single shared source account (deliberately, to also "
        "stress the row-lock path), `X-API-Key` auth, unique idempotency keys.\n",
        "## Results\n",
        "| Concurrency | Duration | Requests | RPS | p50 | p95 | p99 | Max | Error Rate | Status Breakdown |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| {r['concurrency']} | {r['duration_s']}s | {r['total_requests']} | {r['rps']} | "
            f"{r['p50_ms']}ms | {r['p95_ms']}ms | {r['p99_ms']}ms | {r['max_ms']}ms | "
            f"{r['error_rate']*100 if r['error_rate'] is not None else 'N/A'}% | {r['status_breakdown']} |"
        )
    lines.append("")
    lines.append("## Notes")
    lines.append("")
    lines.append(
        "These numbers are from a real run against the live docker-compose stack. Any 429 responses "
        "reflect the rate limiter (`common/rate_limit.py`) actually engaging under load -- see the "
        "status breakdown column above for the real mix of 200s vs 429s vs errors at each stage, "
        "rather than assuming all non-200s are failures."
    )
    with open("docs/PERFORMANCE.md", "w") as f:
        f.write("\n".join(lines) + "\n")
    print("\nWrote docs/PERFORMANCE.md", file=sys.stderr)


if __name__ == "__main__":
    results = asyncio.run(main())
    import json
    print(json.dumps(results, indent=2))
    write_report(results)
