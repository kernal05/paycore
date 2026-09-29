# ADR-018: Scoped local Kubernetes verification (hardware constraint)

## Context
The development machine has 8GB total RAM (7.21GB usable), with WSL2 capped
at 4.8GB. Running the full stack (Postgres, Redis, Kafka, and all 5 app
services) simultaneously in minikube on this machine caused systemic
instability: the API server itself became unresponsive (TLS handshake
timeouts on basic `kubectl` reads), and Postgres/Redis entered repeated
restart loops under CPU/memory pressure.

## Decision
The full 8-service stack, including the Kafka outbox -> reconciliation
path, is fully verified via Docker Compose (see chaos/RESULTS.md and
docs/PERFORMANCE.md — real chaos experiments and load tests, all healthy).

The Kubernetes deployment on this machine is scoped to the core
synchronous payment path — postgres, redis, fraud-engine, ledger-service,
payment-api (5 pods instead of 8+) — which fits comfortably within this
node's real capacity and still demonstrates genuine K8s literacy:
Deployments, Services, ConfigMaps, Secrets, image patching, and a live
payment moving through the cluster.

The Kafka, outbox-publisher, and reconciliation-service manifests remain
in infra/k8s/ unmodified and are deployable as-is on adequately-provisioned
hardware (or in the production AWS/EKS target these manifests are actually
written for — see infra/terraform/).

## Consequence
This is a deliberate, documented scope reduction for local verification
only, not a claim that the async/Kafka path doesn't work on Kubernetes —
it's proven on Docker Compose and the manifests are unchanged from the
production-intended design.
