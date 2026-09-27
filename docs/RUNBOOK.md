# Runbook

## Payment API High Error Rate

**Alert:** `PaymentAPIHighErrorRate` — 5xx rate above 1% for 5m.

1. Check `/metrics` on payment-api, split by downstream: is fraud-engine or
   ledger-service returning errors? (Both calls are `httpx.Client` calls
   in `services/payment-api/main.py` — either will bubble up as a 5xx.)
2. `kubectl logs -n fintech -l app=payment-api --tail=200` for the actual
   exception.
3. If ledger-service is the cause: check its own error rate and
   `/journal/integrity-check` — if the DB is unreachable, this is a
   `LedgerServiceDown`-class incident, escalate immediately (money movement
   is halted).
4. If fraud-engine is the cause: check Redis connectivity
   (`kubectl exec` into a fraud-engine pod, `redis-cli -h redis ping`).
5. Mitigation: roll back the last deploy (`kubectl rollout undo
   deployment/payment-api -n fintech`) if the timing lines up with a release.

## Payment API High Latency

**Alert:** `PaymentAPIHighLatency` — p99 above 500ms for 5m.

1. Check whether latency is upstream (fraud-engine `/score` p99) or in the
   ledger posting (row lock contention on a hot account is the classic
   cause — check for a single account receiving unusually high transaction
   volume).
2. Check HPA status: `kubectl get hpa -n fintech` — if replicas are pinned
   at max and CPU is saturated, the fix is `node_max_size` in
   `infra/terraform/variables.tf`, not just replica count.

## Ledger Service Down

**Alert:** `LedgerServiceDown` — Sev1, pages immediately.

This means money movement is completely halted. Treat as a Sev1:

1. Confirm scope: is RDS itself down, or just the service pods?
   `aws rds describe-db-instances` / check the RDS console for a failover
   event (Multi-AZ should auto-fail over in under a minute — see
   `DISASTER_RECOVERY.md`).
2. If RDS failed over, ledger-service pods should reconnect automatically
   (SQLAlchemy `pool_pre_ping=True`); if they don't, restart the deployment.
3. Do **not** manually edit balances during an incident. If a customer-visible
   discrepancy occurred, it gets corrected via a new, auditable reversing
   transaction after the incident, never a direct UPDATE.
4. Postmortem required within 48 hours for any Sev1.

## Reconciliation Mismatch Spike

**Alert:** `ReconciliationMismatchSpike` — >20 mismatches in 15 min.

1. `GET /reconcile/report` on reconciliation-service for the pattern —
   is it one processor, one bank, or random? A single-source spike usually
   means that external system is degraded, not us.
2. Check whether this correlates with a real processor/bank incident
   (their status page) before assuming it's a platform bug.
3. This is a `ticket`-severity alert, not a page — investigate same-day,
   not immediately, unless mismatch count keeps climbing.

## Outbox Publisher Lagging

**Alert:** `OutboxPublishingLagging` — multiple failed publish batches in 10m.

1. Check `outbox-publisher` logs — almost always either Kafka unreachable
   or a DB connectivity blip.
2. This does **not** affect payments — the ledger keeps posting normally
   (ADR-003). It affects reconciliation's event-driven trigger, which
   falls back to the 15-minute batch sweep CronJob in the meantime.
3. Check `SELECT count(*) FROM outbox_events WHERE published_at IS NULL`
   directly if the metric itself seems wrong — this is the real queue
   depth the publisher is working through.
4. Once the publisher recovers, it drains the backlog automatically at
   `OUTBOX_BATCH_SIZE` (100) per poll — no manual intervention needed
   unless the backlog is in the hundreds of thousands.

## Transactions Stuck in UNKNOWN

**Alert:** `TransactionsStuckUnknown` — unusually many transactions in
30 minutes came back UNKNOWN from processor authorization (see ADR-011).

1. This means the (mock) processor is reporting TIMEOUT/UNKNOWN/DUPLICATE
   at an elevated rate — in a real deployment, check the processor's own
   status page before assuming it's a platform bug.
2. Query `SELECT id, created_at FROM transactions WHERE status = 'UNKNOWN'
   ORDER BY created_at ASC` — these are waiting on a webhook (see
   `/webhooks/processor`) to resolve them. If they're piling up without
   webhooks arriving, check webhook delivery from the processor side, not
   just this platform.
3. There's no automatic timeout on UNKNOWN in this platform yet — a
   production version would add a scheduled job that escalates anything
   still UNKNOWN after e.g. 24h to manual investigation rather than
   leaving it indefinitely pending.

## Webhook Signature Rejections

If `webhook_rejected` events spike in the `business_events_total` metric
(labeled `payment-api`), someone or something is sending malformed or
forged requests to `/webhooks/processor`. Check the source IP in access
logs — a burst from one IP is likely a scanner or misconfigured retry
loop, not an attack on this specific endpoint, since a real attacker
without the shared secret cannot forge a valid signature (see ADR-012).

## Fraud Block Rate Anomaly

**Alert:** `FraudBlockRateAnomaly` — >15% of transactions blocked over 10m.

1. Pull recent `fraud_events.reasons` — is one rule (e.g. `new_device`)
   dominating? A false-positive spike is usually a single rule misfiring
   after a client-side change (e.g. a mobile app update that changed how
   `device_id` is generated, making every user look "new").
2. If it's a genuine attack (velocity + new_device + large_amount
   clustering on a few accounts), leave blocking on and escalate to
   the fraud/security on-call instead of relaxing rules.
