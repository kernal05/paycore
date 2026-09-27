# Chaos Experiment Results

Run at: 2026-09-27 11:50:12 UTC

| Experiment | Hypothesis | Expected | Result | Recovery Time | Ledger Integrity |
|---|---|---|---|---|---|
| 01: Kill payment-api mid-traffic | With 3 replicas in prod (1 in this local compose stack), in-flight requests to the killed instance fail but the platform recovers with zero financial corruption. | Some requests fail during the outage window; ledger integrity check stays healthy after recovery. | RECOVERED | 5.3s | ✅ healthy |
| 02: Kill Kafka | Payments still complete (Kafka is not in the synchronous ledger-posting path — see ADR-002) but outbox events queue up undelivered until Kafka returns. | Ledger postings continue to succeed; outbox_events accumulate published_at=NULL rows while Kafka is down. | RECOVERED | 0.4s | ✅ healthy |
| 03: Kill Redis | Fraud scoring degrades (loses velocity/device history) but does not hard-fail the payment path, since fraud-engine calls are behind a circuit breaker. | Fraud-engine requests error or the circuit opens; payment-api returns 503 fast rather than hanging. | RECOVERED | 0.2s | ✅ healthy |
| 04: Kill PostgreSQL | The API fails safely — no partial ledger postings — because every money-moving statement is inside a single DB transaction. | Payments fail with 5xx; zero imbalanced transactions afterward (journal integrity check still healthy). | RECOVERED | 0.4s | ✅ healthy |
| 05: Kill ledger-service | This is the Sev1 case in RUNBOOK.md — money movement halts entirely until it recovers. | payment-api's ledger circuit breaker opens; requests fail fast with 503, not timeouts. | RECOVERED | 4.9s | ✅ healthy |
