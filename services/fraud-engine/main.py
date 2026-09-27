"""Fraud Engine
================
Real-time risk scoring in the payment path. Deliberately rule-based and
explainable (each rule contributes a weighted score + a human-readable
reason) rather than a black-box model — this is what you'd actually put
in production first, with a slot left to swap in / blend an ML model
later without changing the API contract.

Score in [0,1]:
  0.00 - 0.30  -> APPROVE
  0.30 - 0.70  -> REVIEW
  0.70 - 1.00  -> BLOCK
"""
import os
import sys
import time

import redis
from fastapi import Depends, FastAPI
from pydantic import BaseModel

sys.path.append("/app")
from common.observability import setup_observability, BUSINESS_EVENTS
from common.auth import require_service_auth

app = FastAPI(title="Fraud Engine")
logger = setup_observability(app, "fraud-engine")

REQUIRE_AUTH = os.getenv("REQUIRE_SERVICE_AUTH", "true").lower() == "true"
auth_dep = [Depends(require_service_auth)] if REQUIRE_AUTH else []

r = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/0"), decode_responses=True)

VELOCITY_WINDOW_SECONDS = 300  # 5 minutes
LARGE_AMOUNT_MINOR = 5_000_000  # e.g. ₹50,000 in paise


class ScoreRequest(BaseModel):
    transaction_id: str
    account_id: str
    amount_minor: int
    currency: str
    device_id: str | None = None
    ip_address: str | None = None
    location: str | None = None


class ScoreResult(BaseModel):
    transaction_id: str
    score: float
    decision: str
    reasons: list[str]


def _velocity_count(account_id: str) -> int:
    key = f"velocity:{account_id}"
    now = time.time()
    pipe = r.pipeline()
    pipe.zadd(key, {str(now): now})
    pipe.zremrangebyscore(key, 0, now - VELOCITY_WINDOW_SECONDS)
    pipe.zcard(key)
    pipe.expire(key, VELOCITY_WINDOW_SECONDS)
    _, _, count, _ = pipe.execute()
    return count


def _is_known_device(account_id: str, device_id: str | None) -> bool:
    if not device_id:
        return False
    key = f"known_devices:{account_id}"
    known = r.sismember(key, device_id)
    r.sadd(key, device_id)  # first time seen, remember it for next time
    return bool(known)


@app.post("/score", response_model=ScoreResult, dependencies=auth_dep)
def score_transaction(req: ScoreRequest):
    score = 0.0
    reasons: list[str] = []

    if req.amount_minor >= LARGE_AMOUNT_MINOR:
        score += 0.35
        reasons.append(f"large_amount:{req.amount_minor}")

    velocity = _velocity_count(req.account_id)
    if velocity >= 5:
        score += 0.4
        reasons.append(f"high_velocity:{velocity}_txns_in_5min")
    elif velocity >= 3:
        score += 0.2
        reasons.append(f"elevated_velocity:{velocity}_txns_in_5min")

    if req.device_id and not _is_known_device(req.account_id, req.device_id):
        score += 0.25
        reasons.append("new_device")

    if not req.ip_address:
        score += 0.05
        reasons.append("missing_ip")

    score = min(score, 1.0)

    if score >= 0.70:
        decision = "BLOCK"
    elif score >= 0.30:
        decision = "REVIEW"
    else:
        decision = "APPROVE"

    BUSINESS_EVENTS.labels("fraud-engine", "transaction_scored", decision).inc()
    logger.info(f"Scored {req.transaction_id}: {score:.2f} -> {decision} ({reasons})")

    return ScoreResult(transaction_id=req.transaction_id, score=round(score, 3), decision=decision, reasons=reasons)
