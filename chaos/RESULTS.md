# Chaos Experiment Results

Run at: 2026-09-26 12:01:00 UTC

**Environment note:** this sandbox has no Docker daemon and no Kafka broker (network is locked to package registries only). Experiments ran against the real services directly — actual uvicorn processes, real PostgreSQL 16, real Redis 7 — using `kill -9` on the actual process PID + restart, in place of `docker compose kill/up`. Kafka-dependent paths (outbox-publisher, the reconciliation event consumer) were not exercised for that reason and are marked NOT RUN rather than guessed at.

| # | Experiment | Result | Recovery Time | Ledger Integrity After |
|---|---|---|---|---|
| 01 | Kill payment-api (SIGKILL) mid-traffic, new pid=588 | recovered=YES; payment_during_outage=000; payment_after_recovery=200 | 1s | True |
| 02 | Kill Kafka | NOT RUN — no Kafka broker available in this sandbox | N/A | N/A |
| 03 | Kill Redis (service stop/start) | redis_recovered=YES; payment_during_outage=500; payment_after_recovery= | 0s | True |
| 04 | Kill PostgreSQL (service stop/start) | pg_recovered=YES; payment_during_outage=500; payment_after_recovery=200 | 0s | True |
| 05 | Kill ledger-service (SIGKILL) — the Sev1 case | recovered=YES; payment_during_outage=500 (expect failure — money movement halted); payment_after_recovery=200 | 1s | True |

Raw transaction count after all experiments: 6

## Note on experiment 03's blank `payment_after_recovery` value

The measuring script's own 3-second client-side timeout on that specific
probe was hit before curl printed the response code — but `payment.log`
confirms the request actually completed successfully server-side
(`POST /payments HTTP/1.1" 200 OK`) a moment later. In other words: this
is not a failure, it's the measurement being stricter than the system —
the request right after a fresh Redis restart took a bit over 3s
(presumably a reconnect cost on fraud-engine's Redis client), which the
test harness's own timeout didn't accommodate. Recorded as-is rather than
re-run and quietly replaced, since a slower-than-expected first request
after a dependency recovers is itself a real, useful observation: a
production version of this platform would want a Redis connection warm-up
or a slightly more generous client timeout immediately after a dependency
recovery event, not just when steady-state.
