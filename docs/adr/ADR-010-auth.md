# ADR-010: Minimal HMAC service auth + API keys, not full OAuth2

**Status:** Accepted

## Context
Every internal call needed to be authenticated for this to be a credible
"production-inspired" platform, but standing up a real identity provider
(Cognito/Auth0) for a portfolio project is disproportionate effort for
what it demonstrates.

## Decision
- Service-to-service calls carry a short-lived (5min) bearer token: an
  HMAC-SHA256 signature over `service_name.expiry`, signed with a shared
  secret (`common/auth.py`).
- The public-facing endpoint (payment-api) validates an `X-API-Key`
  header against an env-configured allowlist.
- Both are explicitly marked LOCAL ONLY where hardcoded defaults appear
  (docker-compose.yml, this file).

## What a production version would use instead
- Cognito/Auth0-issued short-lived JWTs per service identity, or mTLS via
  a service mesh (Istio/Linkerd) so identity is enforced at the network
  layer, not the application layer.
- Customer API keys looked up from a database with per-key rate limits,
  scopes, and revocation — not an env var allowlist.
- Secrets sourced from AWS Secrets Manager via the External Secrets
  Operator into Kubernetes Secrets, never committed or hardcoded — see
  `infra/k8s/00-namespace.yaml`'s placeholder secret and the warning on it.

## Consequences
Every call in this platform IS authenticated and a forged/expired token
IS rejected — the property that matters — implemented at a cost
appropriate to a demonstration project rather than a funded engineering
team's multi-quarter identity platform.
