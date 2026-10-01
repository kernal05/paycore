"""Payment API
================
The public entry point. Orchestrates the payment state machine across
services:

    CREATED (ledger) -> RISK_CHECK (fraud-engine) -> AUTHORIZED -> PROCESSING -> SETTLED (ledger)

Two layers of idempotency exist on purpose:
  1. `payment_requests` here, keyed on idempotency_key + bound to a hash of
     the request body — the client-facing contract (409 on key reuse with
     a different payload).
  2. `transactions.idempotency_key` in the ledger — the money-safety
     backstop, so even a bug in this layer can't double-post.

Calls to fraud-engine and ledger-service go through a circuit breaker each,
so a struggling dependency fails fast instead of stacking up timeouts.
"""
import os
import sys

import httpx
import redis
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text

sys.path.append("/app")
from common.db import SessionLocal
from common.observability import setup_observability, BUSINESS_EVENTS
from common.idempotency import compute_request_hash
from common.auth import require_api_key
from common.rate_limit import rate_limit_dependency
from common.circuit_breaker import CircuitBreaker, CircuitOpenError
from common.auth import issue_token
from common.webhooks import verify_webhook, WebhookVerificationError

app = FastAPI(title="Payment API")

from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # local demo only -- tighten for real deployments
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
logger = setup_observability(app, "payment-api")

FRAUD_ENGINE_URL = os.getenv("FRAUD_ENGINE_URL", "http://fraud-engine:8000")
LEDGER_SERVICE_URL = os.getenv("LEDGER_SERVICE_URL", "http://ledger-service:8000")
REQUIRE_AUTH = os.getenv("REQUIRE_API_KEY", "true").lower() == "true"

redis_client = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/0"), decode_responses=True)
rate_limiter = rate_limit_dependency(redis_client, limit=int(os.getenv("RATE_LIMIT_PER_MIN", "100")))

fraud_breaker = CircuitBreaker("fraud-engine", failure_threshold=5, cooldown_seconds=30)
ledger_breaker = CircuitBreaker("ledger-service", failure_threshold=5, cooldown_seconds=30)

REQUEST_FIELDS_FOR_HASH = ["source_account_id", "dest_account_id", "amount_minor", "currency"]

# Maps a ledger-persisted terminal status to the client-facing status string.
# A transaction that has moved on past SETTLED (RECONCILIATION en route to a
# webhook resolution, or refunded afterward) still reports as settled here —
# the payment itself did settle; what happened to it afterward is a separate
# concern (see /webhooks/processor and /payments/{id}/refund).
TERMINAL_STATUS_MAP = {
    "SETTLED": "APPROVED_AND_SETTLED",
    "FAILED": "FAILED",
    "UNKNOWN": "UNKNOWN",
    "RECONCILIATION": "UNKNOWN",
    "REFUND_REQUESTED": "APPROVED_AND_SETTLED",
    "REFUNDED": "APPROVED_AND_SETTLED",
}


class PaymentRequest(BaseModel):
    idempotency_key: str
    source_account_id: str
    dest_account_id: str
    amount_minor: int = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)
    device_id: str | None = None
    ip_address: str | None = None
    location: str | None = None


class PaymentResponse(BaseModel):
    transaction_id: str
    status: str
    fraud_score: float | None = None
    fraud_decision: str | None = None
    reasons: list[str] = []
    idempotent_replay: bool = False


def _service_headers(service_name: str) -> dict:
    return {"Authorization": f"Bearer {issue_token(service_name)}"}


def _call_ledger(method: str, path: str, json_body: dict | None = None) -> dict:
    def _do():
        with httpx.Client(timeout=5.0) as client:
            resp = client.request(
                method, f"{LEDGER_SERVICE_URL}{path}", json=json_body,
                headers=_service_headers("payment-api"),
            )
            resp.raise_for_status()
            return resp.json()
    return ledger_breaker.call(_do)


def _call_fraud(json_body: dict) -> dict:
    def _do():
        with httpx.Client(timeout=5.0) as client:
            resp = client.post(f"{FRAUD_ENGINE_URL}/score", json=json_body, headers=_service_headers("payment-api"))
            resp.raise_for_status()
            return resp.json()
    return fraud_breaker.call(_do)


auth_deps = [Depends(require_api_key), Depends(rate_limiter)] if REQUIRE_AUTH else [Depends(rate_limiter)]


@app.post("/payments", response_model=PaymentResponse, dependencies=auth_deps)
def create_payment(req: PaymentRequest, request: Request):
    session = SessionLocal()
    request_body = req.model_dump()
    hash_input = {k: request_body[k] for k in REQUEST_FIELDS_FOR_HASH}
    request_hash = compute_request_hash(hash_input)

    try:
        # ---- Layer 1 idempotency: is this key already in flight or done? ----
        existing = session.execute(
            text("SELECT idempotency_key, request_hash, transaction_id, status, response_body "
                 "FROM payment_requests WHERE idempotency_key = :k"),
            {"k": req.idempotency_key},
        ).fetchone()

        if existing:
            if existing.request_hash != request_hash:
                raise HTTPException(
                    409,
                    f"idempotency_key '{req.idempotency_key}' was already used with a different request payload",
                )
            if existing.status == "COMPLETE" and existing.response_body:
                import json
                body = json.loads(existing.response_body) if isinstance(existing.response_body, str) else existing.response_body
                body["idempotent_replay"] = True
                return PaymentResponse(**body)
            # PENDING replay (original attempt crashed mid-flight): fall through
            # and re-drive the same transaction_id from wherever it left off.

        session.rollback()  # close any autobegun read transaction before an explicit begin (SQLAlchemy 2.0 autobegin)

        with session.begin():
            session.execute(
                text(
                    "INSERT INTO payment_requests (idempotency_key, request_hash, status) "
                    "VALUES (:k, :h, 'PENDING') ON CONFLICT (idempotency_key) DO NOTHING"
                ),
                {"k": req.idempotency_key, "h": request_hash},
            )

        # ---- Step 1: register with the ledger (CREATED, or resume) ----
        registered = _call_ledger("POST", "/transactions/register", {
            "idempotency_key": req.idempotency_key,
            "source_account_id": req.source_account_id,
            "dest_account_id": req.dest_account_id,
            "amount_minor": req.amount_minor,
            "currency": req.currency,
        })
        transaction_id = registered["transaction_id"]
        ledger_status = registered["status"]

        # ---- Step 2: fraud score + risk-result, UNLESS this is a resumed
        # retry that already got past this step on a prior attempt. Never
        # re-run fraud scoring on a resume: it has side effects (Redis
        # velocity counters, known-device set) that must not be double-counted,
        # and the decision was already made and persisted (ADR-013).
        if ledger_status == "CREATED":
            fraud = _call_fraud({
                "transaction_id": transaction_id,
                "account_id": req.source_account_id,
                "amount_minor": req.amount_minor,
                "currency": req.currency,
                "device_id": req.device_id,
                "ip_address": req.ip_address,
                "location": req.location,
            })
            risk = _call_ledger("POST", f"/transactions/{transaction_id}/risk-result", {
                "decision": fraud["decision"],
                "fraud_score": fraud["score"],
                "fraud_reasons": fraud["reasons"],
            })
            ledger_status = risk["status"]
            fraud_score, fraud_decision, reasons = fraud["score"], fraud["decision"], fraud["reasons"]
        else:
            txn = _call_ledger("GET", f"/transactions/{transaction_id}")
            fraud_score = float(txn["fraud_score"]) if txn.get("fraud_score") is not None else None
            fraud_decision = txn.get("fraud_decision")
            reasons = txn.get("fraud_reasons") or []

        # ---- Step 3: resolve to a final client-facing result from wherever
        # the transaction actually is, rather than assuming it's exactly
        # where a fresh request would be.
        if ledger_status in TERMINAL_STATUS_MAP:
            result = PaymentResponse(
                transaction_id=transaction_id, status=TERMINAL_STATUS_MAP[ledger_status],
                fraud_score=fraud_score, fraud_decision=fraud_decision, reasons=reasons,
            )
            BUSINESS_EVENTS.labels("payment-api", "payment_completed", result.status.lower()).inc()

        elif ledger_status == "BLOCKED":
            result = PaymentResponse(
                transaction_id=transaction_id, status="BLOCKED",
                fraud_score=fraud_score, fraud_decision=fraud_decision, reasons=reasons,
            )
            BUSINESS_EVENTS.labels("payment-api", "payment_blocked", "fraud").inc()

        elif ledger_status == "AUTHORIZED" and fraud_decision == "REVIEW":
            # Held at AUTHORIZED, not posted — a real system queues this for
            # manual review; here we surface it and stop, deliberately not
            # auto-approving just because it wasn't an outright BLOCK. A
            # retry of this same idempotency_key will land right back here
            # (fraud_decision is persisted) until a reviewer acts.
            result = PaymentResponse(
                transaction_id=transaction_id, status="REVIEW",
                fraud_score=fraud_score, fraud_decision=fraud_decision, reasons=reasons,
            )
            BUSINESS_EVENTS.labels("payment-api", "payment_held_for_review", "fraud").inc()

        elif ledger_status in ("AUTHORIZED", "PROCESSING"):
            # PROCESSING is now a state that CAN be legitimately observed
            # here (not just AUTHORIZED): post_transaction splits into a
            # phase-1 PROCESSING commit, an un-transacted processor call,
            # and a phase-3 result commit, specifically so the processor
            # call never holds a DB lock (see ADR-013 addendum). A crash
            # between phases 1 and 3 leaves the transaction sitting at
            # PROCESSING, and calling /post again safely resumes from
            # exactly there — the processor's outcome is deterministic per
            # transaction_id, so re-calling it lands on the same decision.
            posted = _call_ledger("POST", f"/transactions/{transaction_id}/post")
            final_status = TERMINAL_STATUS_MAP.get(posted["status"], posted["status"])
            result = PaymentResponse(
                transaction_id=transaction_id, status=final_status,
                fraud_score=fraud_score, fraud_decision=fraud_decision, reasons=reasons,
            )
            BUSINESS_EVENTS.labels("payment-api", "payment_completed", final_status.lower()).inc()

        else:
            # RISK_CHECK should never be observable here: it's only ever set
            # and committed atomically alongside its next state within a
            # single ledger-service DB transaction, so a crash mid-transaction
            # rolls back to the prior state instead of leaving it stranded.
            # If this ever fires, that invariant broke and needs
            # investigating — not a reason to guess at how to proceed with
            # someone's money.
            raise HTTPException(
                500, f"transaction {transaction_id} is in unexpected state '{ledger_status}' — needs manual investigation",
            )

        _finalize_request(session, req.idempotency_key, transaction_id, result)
        return result

    except CircuitOpenError as e:
        raise HTTPException(503, f"downstream unavailable: {e}")
    except httpx.HTTPStatusError as e:
        # A downstream service rejected the request outright (4xx/5xx) in a
        # way the circuit breaker hasn't tripped on yet. Surface it as a
        # clean 502 with the downstream body rather than leaking a bare,
        # unstructured 500 — this is exactly the class of bug a missing
        # service-auth header on an internal call produces (caught in
        # practice: fraud-engine's /score requires the same bearer token
        # ledger calls already carried, and payment-api originally forgot
        # to attach it — a real integration bug only a live end-to-end run
        # surfaced, not a hypothetical).
        raise HTTPException(502, f"downstream call failed: {e.response.status_code} {e.response.text}")
    finally:
        session.close()


def _finalize_request(session, idempotency_key: str, transaction_id: str, result: PaymentResponse):
    import json
    session.rollback()  # close any autobegun read transaction before an explicit begin (SQLAlchemy 2.0 autobegin)
    with session.begin():
        session.execute(
            text(
                "UPDATE payment_requests SET status = 'COMPLETE', transaction_id = :txn, "
                "response_body = :body, updated_at = now() WHERE idempotency_key = :k"
            ),
            {"txn": transaction_id, "body": json.dumps(result.model_dump()), "k": idempotency_key},
        )


@app.get("/payments/{transaction_id}")
def get_payment(transaction_id: str):
    return _call_ledger("GET", f"/transactions/{transaction_id}")


@app.post("/payments/{transaction_id}/refund", dependencies=auth_deps)
def refund_payment(transaction_id: str):
    return _call_ledger("POST", f"/transactions/{transaction_id}/refund", {})


# --------------------------------------------------------------- webhooks

@app.post("/webhooks/processor")
async def processor_webhook(
    request: Request,
    x_webhook_signature: str = Header(default=None),
    x_webhook_timestamp: int = Header(default=None),
):
    """Receives asynchronous outcome updates from the (mock) processor for
    transactions that came back UNKNOWN from the synchronous authorize
    call — see ledger-service's /transactions/{id}/resolve-unknown.

    Security properties, in order:
      1. signature verification (rejects forged callers)
      2. timestamp freshness (rejects replayed-later captures)
      3. event_id idempotency via webhook_events (rejects redelivery)
    None of these are optional — this endpoint moves money.
    """
    raw_body = await request.body()

    if not x_webhook_signature or x_webhook_timestamp is None:
        raise HTTPException(400, "missing X-Webhook-Signature / X-Webhook-Timestamp headers")

    try:
        verify_webhook(raw_body, x_webhook_timestamp, x_webhook_signature)
    except WebhookVerificationError as e:
        BUSINESS_EVENTS.labels("payment-api", "webhook_rejected", "invalid_signature_or_replay").inc()
        raise HTTPException(401, str(e))

    import json
    body = json.loads(raw_body)
    event_id = body.get("event_id")
    transaction_id = body.get("transaction_id")
    resolved_status = body.get("resolved_status")
    processor_ref = body.get("processor_ref")
    if not event_id or not transaction_id or resolved_status not in ("SETTLED", "FAILED"):
        raise HTTPException(400, "webhook payload must include event_id, transaction_id, resolved_status")

    session = SessionLocal()
    try:
        session.rollback()  # close any autobegun read transaction before an explicit begin (SQLAlchemy 2.0 autobegin)
        with session.begin():
            inserted = session.execute(
                text(
                    "INSERT INTO webhook_events (event_id, processor, payload, signature_valid) "
                    "VALUES (:eid, :proc, :payload, true) ON CONFLICT (event_id) DO NOTHING RETURNING event_id"
                ),
                {"eid": event_id, "proc": body.get("processor", "mock"), "payload": json.dumps(body)},
            ).fetchone()

        if inserted is None:
            # Already seen this exact event_id — at-least-once delivery, handled.
            BUSINESS_EVENTS.labels("payment-api", "webhook_deduplicated", "replay").inc()
            return {"status": "already_processed", "event_id": event_id}

        result = _call_ledger("POST", f"/transactions/{transaction_id}/resolve-unknown", {
            "resolved_status": resolved_status, "processor_ref": processor_ref,
        })

        session.rollback()  # close any autobegun read transaction before an explicit begin (SQLAlchemy 2.0 autobegin)

        with session.begin():
            session.execute(
                text("UPDATE webhook_events SET processed_at = now() WHERE event_id = :eid"),
                {"eid": event_id},
            )

        BUSINESS_EVENTS.labels("payment-api", "webhook_processed", resolved_status.lower()).inc()
        return {"status": "processed", "event_id": event_id, "resolution": result}
    finally:
        session.close()
