"""Minimal service auth.
================
This is deliberately NOT a full OAuth2/JWT deployment — see
docs/adr/ADR-010-auth.md for why, and what a production version would use
(Cognito/Auth0 + short-lived JWTs, mTLS between services via a mesh). For
this platform, every internal call carries a bearer token that is an
HMAC-SHA256 signature over `service_name.expiry`, signed with a shared
secret injected via env var (Kubernetes Secret / local .env — never
committed). This is enough to demonstrate the real thing that matters:
*every service call is authenticated, and a stale or forged token is
rejected*, without standing up an identity provider for a portfolio project.

Client-facing auth (a real end user calling POST /payments) uses a
separate API-key scheme — see require_api_key below and
payment-api/main.py for how they compose.
"""
import hashlib
import hmac
import os
import time

from fastapi import Header, HTTPException

SHARED_SECRET = os.getenv("SERVICE_AUTH_SECRET", "local-dev-only-secret-DO-NOT-USE-IN-PROD")
TOKEN_TTL_SECONDS = 300

# Local-dev API keys for external clients. In production these are looked
# up from a database/Secrets Manager keyed by customer, not hardcoded —
# flagged clearly as LOCAL ONLY.
VALID_API_KEYS = set(os.getenv("VALID_API_KEYS", "demo-key-local-only").split(","))


def issue_token(service_name: str) -> str:
    expiry = int(time.time()) + TOKEN_TTL_SECONDS
    payload = f"{service_name}.{expiry}"
    signature = hmac.new(SHARED_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def verify_token(token: str) -> str:
    """Returns the calling service's name if valid, raises otherwise."""
    try:
        service_name, expiry_str, signature = token.split(".")
        expiry = int(expiry_str)
    except (ValueError, AttributeError):
        raise HTTPException(401, "malformed service token")

    expected_payload = f"{service_name}.{expiry}"
    expected_sig = hmac.new(SHARED_SECRET.encode(), expected_payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_sig, signature):
        raise HTTPException(401, "invalid service token signature")
    if time.time() > expiry:
        raise HTTPException(401, "expired service token")
    return service_name


def require_service_auth(authorization: str = Header(default=None)) -> str:
    """FastAPI dependency for endpoints that should only be called by other
    platform services, never directly by an external client.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token")
    return verify_token(authorization.removeprefix("Bearer "))


def require_api_key(x_api_key: str = Header(default=None)) -> str:
    """FastAPI dependency for the public-facing endpoint (payment-api). A
    real deployment validates against a database of issued customer keys
    with per-key rate limits and revocation; this validates against an
    env-configured allowlist, which is the honest local-dev equivalent.
    """
    if not x_api_key or x_api_key not in VALID_API_KEYS:
        raise HTTPException(401, "missing or invalid API key")
    return x_api_key
