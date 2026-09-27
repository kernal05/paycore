# Disaster Recovery

## Targets

- **RTO (Recovery Time Objective): 15 minutes** — time to restore service
  in the secondary region after a full primary-region outage.
- **RPO (Recovery Point Objective): < 1 minute** — max acceptable data loss,
  bounded by RDS cross-region read replica lag (typically sub-second to a
  few seconds under normal load).

## Topology

```
        Route 53 (health-checked failover routing)
              │
   ┌──────────┴──────────┐
   ▼                     ▼
ap-south-1 (primary)   ap-southeast-1 (standby)
   │                     │
EKS cluster           EKS cluster (warm: min replicas running,
   │                       scaled to full size on failover)
RDS Postgres  ───────▶ RDS cross-region read replica
(Multi-AZ)             (promotable to primary)
   │                     │
MSK Kafka             MSK Kafka (independent cluster; events
                       reprocessed from last committed offset)
```

`infra/terraform/` as written provisions the primary region. The standby
region is the same module set applied a second time with
`aws_region = "ap-southeast-1"` and the RDS resource swapped for
`aws_db_instance` with `replicate_source_db` pointing cross-region — this
repo ships the primary-region config as the reference implementation; the
standby is a `terraform workspace`/second `tfvars` file away, deliberately
not duplicated here to keep one source of truth for the module definitions.

## Failover procedure

1. **Detect**: Route 53 health checks against `payment-api`'s `/healthz` in
   the primary region fail for the configured threshold (3 consecutive
   checks).
2. **Promote**: promote the RDS read replica in `ap-southeast-1` to a
   standalone primary (`aws rds promote-read-replica`). This is the
   long-pole step and is what the 15-minute RTO is budgeted around.
3. **Repoint**: update the standby region's `fintech-db-secret` to the
   newly-promoted instance endpoint; scale its EKS node group and
   deployments from warm-standby size to full production size
   (`kubectl scale` or let the HPA do it once traffic arrives).
4. **Cut over**: Route 53 failover routing automatically shifts traffic
   once the standby's health check passes.
5. **Reconcile**: run `/reconcile/run-batch` against the full recent
   transaction window as soon as the standby is serving traffic, to catch
   anything that was in flight at the moment of failover.

## What this protects against, and what it doesn't

- Protects against: a full AWS region outage, an AZ-correlated failure that
  somehow takes out Multi-AZ RDS (rare, but the reason a *second region*
  exists rather than relying on Multi-AZ alone).
- Does **not** protect against: application-level bugs or bad deploys
  replicated to both regions — that's what canary deployments and the
  rollback step in `RUNBOOK.md` are for, not DR.

## Testing this

DR plans that are never tested don't work when needed. Run a scheduled
"game day" quarterly: fail traffic over to the standby region deliberately
during a low-traffic window, time the actual RTO, and update this document
with the real number, not the target.
