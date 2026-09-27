# ADR-006: Kubernetes for orchestration

**Status:** Accepted

## Decision
Deploy all services on EKS rather than e.g. plain EC2 or ECS, for
declarative rolling deploys, HPA-driven autoscaling, and a portable
manifest format (`infra/k8s/`) that doesn't lock the platform to AWS
specifically (a GKE/AKS migration would mostly touch `infra/terraform/`,
not the app-level manifests).

## Consequences
- Operational complexity (cluster upgrades, node group management) that a
  simpler platform wouldn't have — justified here because it's also the
  platform this project exists to demonstrate competency in.
- Requires the hardening in `infra/k8s/` (PDBs, resource limits, non-root
  containers — see ADR-013) to actually be production-appropriate rather
  than a toy deployment.
