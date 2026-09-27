# ADR-007: Warm-standby multi-region, not multi-master

**Status:** Accepted

## Context
A single-region deployment (even Multi-AZ) doesn't survive a full region
outage. Full active-active multi-master would eliminate failover time
entirely but requires solving multi-region write conflicts for the ledger
— a much harder and riskier problem than this platform needs to take on.

## Decision
Warm standby: a second region runs a scaled-down EKS cluster and an RDS
cross-region read replica, promoted on failover. See
`docs/DISASTER_RECOVERY.md` for the full procedure and stated RTO/RPO.

## Consequences
- Simpler and safer than multi-master, at the cost of a real (though
  bounded, ~15min target) RTO during a full regional failure.
- The promotion step is the long pole and the reason DR game days
  (rehearsing this for real) matter more than the paper plan.
