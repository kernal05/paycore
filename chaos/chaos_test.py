#!/usr/bin/env python3
"""Chaos Experiment Harness
================
Named experiments in the standard chaos-engineering format — hypothesis,
method, expected result — run against the local docker-compose stack.
Produces `chaos/RESULTS.md` with what actually happened, not invented
numbers: if you haven't run this against a live stack, RESULTS.md should
say so, not show fabricated timings.

Usage:
    docker compose up --build -d
    python3 chaos/chaos_test.py
"""
import json
import subprocess
import time
import uuid
from dataclasses import dataclass, field

import httpx

PAYMENT_API = "http://localhost:8000"
API_KEY = "demo-key-local-only"
CUSTOMER_ACCOUNT = "11111111-1111-1111-1111-111111111111"
MERCHANT_ACCOUNT = "22222222-2222-2222-2222-222222222222"

PORT_MAP = {
    "payment-api": 8000, "ledger-service": 8001,
    "fraud-engine": 8002, "reconciliation-service": 8003,
}


@dataclass
class Experiment:
    name: str
    hypothesis: str
    target_service: str  # docker-compose service name to kill
    expected: str
    result: str = "NOT RUN"
    recovery_time_s: float | None = None
    integrity_ok: bool | None = None
    notes: str = ""


EXPERIMENTS: list[Experiment] = [
    Experiment(
        name="01: Kill payment-api mid-traffic",
        hypothesis="With 3 replicas in prod (1 in this local compose stack), in-flight requests to the killed "
                   "instance fail but the platform recovers with zero financial corruption.",
        target_service="payment-api",
        expected="Some requests fail during the outage window; ledger integrity check stays healthy after recovery.",
    ),
    Experiment(
        name="02: Kill Kafka",
        hypothesis="Payments still complete (Kafka is not in the synchronous ledger-posting path — see ADR-002) "
                   "but outbox events queue up undelivered until Kafka returns.",
        target_service="kafka",
        expected="Ledger postings continue to succeed; outbox_events accumulate published_at=NULL rows while Kafka is down.",
    ),
    Experiment(
        name="03: Kill Redis",
        hypothesis="Fraud scoring degrades (loses velocity/device history) but does not hard-fail the payment path, "
                   "since fraud-engine calls are behind a circuit breaker.",
        target_service="redis",
        expected="Fraud-engine requests error or the circuit opens; payment-api returns 503 fast rather than hanging.",
    ),
    Experiment(
        name="04: Kill PostgreSQL",
        hypothesis="The API fails safely — no partial ledger postings — because every money-moving statement is "
                   "inside a single DB transaction.",
        target_service="postgres",
        expected="Payments fail with 5xx; zero imbalanced transactions afterward (journal integrity check still healthy).",
    ),
    Experiment(
        name="05: Kill ledger-service",
        hypothesis="This is the Sev1 case in RUNBOOK.md — money movement halts entirely until it recovers.",
        target_service="ledger-service",
        expected="payment-api's ledger circuit breaker opens; requests fail fast with 503, not timeouts.",
    ),
]


def docker_kill_and_restart(service: str):
    subprocess.run(["docker", "compose", "kill", service], check=False)
    subprocess.run(["docker", "compose", "up", "-d", service], check=False)


def is_healthy(service: str) -> bool:
    port = PORT_MAP.get(service)
    if port is None:
        return True  # postgres/redis/kafka have no /healthz; caller checks via payment-api instead
    try:
        return httpx.get(f"http://localhost:{port}/healthz", timeout=2.0).status_code == 200
    except Exception:
        return False


def fire_test_payment() -> bool:
    try:
        r = httpx.post(f"{PAYMENT_API}/payments", headers={"X-API-Key": API_KEY}, json={
            "idempotency_key": f"chaos-{uuid.uuid4()}",
            "source_account_id": CUSTOMER_ACCOUNT, "dest_account_id": MERCHANT_ACCOUNT,
            "amount_minor": 100, "currency": "INR",
        }, timeout=5.0)
        return r.status_code < 500
    except Exception:
        return False


def check_ledger_integrity() -> bool:
    try:
        r = httpx.get("http://localhost:8001/journal/integrity-check", timeout=5.0)
        return r.json().get("healthy", False)
    except Exception:
        return False  # can't confirm healthy if we can't reach it — don't claim success


def run_experiment(exp: Experiment) -> Experiment:
    print(f"\n=== {exp.name} ===\nHypothesis: {exp.hypothesis}")
    docker_kill_and_restart(exp.target_service)

    start = time.time()
    recovered = False
    for _ in range(30):
        if is_healthy(exp.target_service) or exp.target_service in ("postgres", "redis", "kafka"):
            # for infra deps without /healthz, use payment-api reachability as the recovery signal
            if fire_test_payment() or is_healthy("payment-api"):
                recovered = True
                break
        time.sleep(1)
    exp.recovery_time_s = round(time.time() - start, 1) if recovered else None

    time.sleep(2)
    exp.integrity_ok = check_ledger_integrity()
    exp.result = "RECOVERED" if recovered else "DID NOT RECOVER WITHIN 30s"
    print(f"Result: {exp.result} (recovery_time_s={exp.recovery_time_s}, integrity_ok={exp.integrity_ok})")
    return exp


def write_report(experiments: list[Experiment]):
    lines = ["# Chaos Experiment Results\n",
             f"Run at: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}\n",
             "| Experiment | Hypothesis | Expected | Result | Recovery Time | Ledger Integrity |",
             "|---|---|---|---|---|---|"]
    for e in experiments:
        lines.append(f"| {e.name} | {e.hypothesis} | {e.expected} | {e.result} | "
                      f"{e.recovery_time_s if e.recovery_time_s is not None else 'N/A'}s | "
                      f"{'✅ healthy' if e.integrity_ok else '❌ NOT CONFIRMED HEALTHY'} |")
    with open("chaos/RESULTS.md", "w") as f:
        f.write("\n".join(lines) + "\n")
    print("\nWrote chaos/RESULTS.md")


def main():
    results = [run_experiment(exp) for exp in EXPERIMENTS]
    write_report(results)
    failed = [r for r in results if r.result != "RECOVERED" or not r.integrity_ok]
    if failed:
        print(f"\n{len(failed)}/{len(results)} experiments did not fully pass — see chaos/RESULTS.md")
    else:
        print(f"\nAll {len(results)} experiments recovered with healthy ledger integrity.")


if __name__ == "__main__":
    main()
