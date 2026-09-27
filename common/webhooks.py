"""Webhook security (ADR-012).
================
A processor webhook is an unauthenticated HTTP POST from the internet
unless you make it otherwise. Three things make it safe to act on:

  1. **Signature verification** — HMAC-SHA256 over the raw request body
     with a shared secret, so a forged webhook (anyone can guess the URL)
     is rejected before we even look at the payload.
  2. **Timestamp validation** — a signed timestamp inside the payload,
     rejected if it's older than WEBHOOK_MAX_AGE_SECONDS, so a captured
     and replayed webhook can't be re-sent hours later.
  3. **Idempotency** — every webhook carries a processor-issued event_id;
     `webhook_events` (see scripts/init_db.sql) records it before acting,
     so at-least-once delivery from the processor's retry logic can't
     double-apply a state transition.

This mirrors exactly how Stripe/real processors expect you to validate
their webhooks — this is not a toy version of the pattern, just a mock
signer standing in for the processor's real signing key.
"""
import hashlib
import hmac
import os
import time

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "local-dev-only-webhook-secret-DO-NOT-USE-IN-PROD")
WEBHOOK_MAX_AGE_SECONDS = 300


def sign_webhook_payload(raw_body: bytes, timestamp: int) -> str:
    """What the processor (or our own test client) computes and sends as
    the `X-Webhook-Signature` header.
    """
    signed_payload = f"{timestamp}.{raw_body.decode()}"
    return hmac.new(WEBHOOK_SECRET.encode(), signed_payload.encode(), hashlib.sha256).hexdigest()


class WebhookVerificationError(Exception):
    pass


def verify_webhook(raw_body: bytes, timestamp: int, signature: str) -> None:
    if time.time() - timestamp > WEBHOOK_MAX_AGE_SECONDS:
        raise WebhookVerificationError(f"webhook timestamp too old (>{WEBHOOK_MAX_AGE_SECONDS}s) — possible replay")
    if time.time() - timestamp < -30:  # allow small clock skew, reject anything from the future
        raise WebhookVerificationError("webhook timestamp is in the future")

    expected = sign_webhook_payload(raw_body, timestamp)
    if not hmac.compare_digest(expected, signature):
        raise WebhookVerificationError("webhook signature does not match — rejecting")
