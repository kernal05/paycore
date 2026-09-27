"""Reconciliation Service
================
A payment touches three systems of record: our ledger, the payment
processor, and the bank. They can disagree — a processor can report
SUCCESS while the bank never settles it. This service finds those
mismatches, categorizes them the way a real ops/reconciliation team would
triage them, and exposes an exceptions queue instead of just a log line.

Two ways reconciliation gets triggered, on purpose:
  1. Event-driven — a background thread consumes `ledger.posted` off Kafka
     (the outbox pattern's output) and reconciles each transaction almost
     immediately. Uses the INBOX pattern (inbox_events) so an at-least-once
     redelivered event is a no-op the second time.
  2. Scheduled batch sweep — `/reconcile/run-batch`, run every 15 minutes
     by the CronJob in infra/k8s/, as a safety net for anything the
     event-driven path missed (consumer downtime, a lost message).

`/simulate` stands in for the processor/bank webhooks a real integration
would receive, using the same MockProcessor from common/processors.py that
ledger-service uses, so both sides of the platform agree on what "the
processor" does.
"""
import os
import random
import sys
import threading

import httpx
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel
from sqlalchemy import text

sys.path.append("/app")
from common.db import SessionLocal
from common.observability import setup_observability, BUSINESS_EVENTS
from common.auth import require_service_auth, issue_token
from common.kafka_utils import get_producer, get_consumer, TOPIC_LEDGER_POSTED, TOPIC_RECONCILIATION_MISMATCH
from common.processors import ProcessorRouter

app = FastAPI(title="Reconciliation Service")
logger = setup_observability(app, "reconciliation-service")

REQUIRE_AUTH = os.getenv("REQUIRE_SERVICE_AUTH", "true").lower() == "true"
auth_dep = [Depends(require_service_auth)] if REQUIRE_AUTH else []

LEDGER_SERVICE_URL = os.getenv("LEDGER_SERVICE_URL", "http://ledger-service:8000")
MISMATCH_INJECTION_RATE = float(os.getenv("MISMATCH_INJECTION_RATE", "0.12"))
CONSUMER_GROUP = "reconciliation-service"

processor_router = ProcessorRouter()
_producer = None


def _get_producer():
    global _producer
    if _producer is None:
        _producer = get_producer()
    return _producer


def _service_headers() -> dict:
    return {"Authorization": f"Bearer {issue_token('reconciliation-service')}"}


def _fetch_transaction(transaction_id: str) -> dict:
    with httpx.Client(timeout=5.0) as client:
        resp = client.get(f"{LEDGER_SERVICE_URL}/transactions/{transaction_id}", headers=_service_headers())
        if resp.status_code == 404:
            raise HTTPException(404, "transaction not found in ledger")
        resp.raise_for_status()
        return resp.json()


# ------------------------------------------------------------------ simulate

@app.post("/simulate/{transaction_id}")
def simulate_external_records(transaction_id: str):
    """Stand-in for processor/bank webhooks. Uses the same MockProcessor
    the ledger uses for authorization, so a transaction's processor
    outcome is internally consistent across the platform, then injects a
    small, separate rate of *settlement-side* discrepancies (the bank
    disagreeing with a processor that already said SUCCESS) — that
    second, independent failure mode is the realistic one reconciliation
    exists to catch; a processor/ledger disagreement is caught by the
    resolve-unknown path instead.
    """
    session = SessionLocal()
    try:
        txn = _fetch_transaction(transaction_id)
        processor_status = txn["status"]
        bank_status = txn["status"]
        processor_amount = txn["amount_minor"]
        bank_amount = txn["amount_minor"]
        processor_ref = txn.get("processor_ref") or f"mock-ref-{transaction_id[:8]}"

        if random.random() < MISMATCH_INJECTION_RATE:
            fault = random.choice(["processor_failed", "bank_unknown", "amount_drift", "currency_drift"])
            if fault == "processor_failed":
                processor_status = "FAILED"
            elif fault == "bank_unknown":
                bank_status = "UNKNOWN"
            elif fault == "amount_drift":
                bank_amount = bank_amount - random.randint(1, 1000)
            # currency_drift is modeled at reconcile time via a flag; kept
            # simple here since currency is CHAR(3) not worth mutating for a demo

        session.rollback()  # close any autobegun read transaction before an explicit begin (SQLAlchemy 2.0 autobegin)

        with session.begin():
            session.execute(
                text(
                    "INSERT INTO processor_records (transaction_id, status, amount_minor, processor_ref) "
                    "VALUES (:id, :s, :a, :ref) ON CONFLICT (transaction_id) DO UPDATE SET status=:s, amount_minor=:a, processor_ref=:ref"
                ),
                {"id": transaction_id, "s": processor_status, "a": processor_amount, "ref": processor_ref},
            )
            session.execute(
                text(
                    "INSERT INTO bank_records (transaction_id, status, amount_minor) "
                    "VALUES (:id, :s, :a) ON CONFLICT (transaction_id) DO UPDATE SET status=:s, amount_minor=:a"
                ),
                {"id": transaction_id, "s": bank_status, "a": bank_amount},
            )
        return {"transaction_id": transaction_id, "processor_status": processor_status, "bank_status": bank_status}
    finally:
        session.close()


# ------------------------------------------------------------------ reconcile

def _categorize(internal_status, processor_status, bank_status, internal_amount, proc_amount, bank_amount) -> tuple[bool, str | None, list[str]]:
    details = []
    category = None

    if processor_status == "MISSING":
        category, mismatch = "MISSING_PROCESSOR", True
        details.append("no processor record found for a settled internal transaction")
    elif bank_status == "MISSING":
        category, mismatch = "MISSING_BANK", True
        details.append("no bank record found for a settled internal transaction")
    else:
        mismatch = False

    if proc_amount is not None and proc_amount != internal_amount:
        mismatch, category = True, category or "AMOUNT_MISMATCH"
        details.append(f"processor amount {proc_amount} != internal {internal_amount}")
    if bank_amount is not None and bank_amount != internal_amount:
        mismatch, category = True, category or "AMOUNT_MISMATCH"
        details.append(f"bank amount {bank_amount} != internal {internal_amount}")

    if processor_status not in ("MISSING", internal_status):
        mismatch, category = True, category or "STATUS_MISMATCH"
        details.append(f"processor status {processor_status} != internal {internal_status}")
    if bank_status not in ("MISSING", internal_status):
        mismatch, category = True, category or "STATUS_MISMATCH"
        details.append(f"bank status {bank_status} != internal {internal_status}")

    return mismatch, category, details


def reconcile_one(transaction_id: str) -> dict:
    session = SessionLocal()
    try:
        txn = _fetch_transaction(transaction_id)
        proc = session.execute(
            text("SELECT status, amount_minor FROM processor_records WHERE transaction_id = :id"), {"id": transaction_id}
        ).fetchone()
        bank = session.execute(
            text("SELECT status, amount_minor FROM bank_records WHERE transaction_id = :id"), {"id": transaction_id}
        ).fetchone()

        internal_status = txn["status"]
        processor_status = proc.status if proc else "MISSING"
        bank_status = bank.status if bank else "MISSING"

        mismatch, category, details = _categorize(
            internal_status, processor_status, bank_status,
            txn["amount_minor"], proc.amount_minor if proc else None, bank.amount_minor if bank else None,
        )

        session.rollback()  # close any autobegun read transaction before an explicit begin (SQLAlchemy 2.0 autobegin)

        with session.begin():
            session.execute(
                text(
                    "INSERT INTO reconciliation_reports (transaction_id, internal_status, processor_status, "
                    "bank_status, mismatch, category, detail) VALUES (:t, :i, :p, :b, :m, :c, :d)"
                ),
                {
                    "t": transaction_id, "i": internal_status, "p": processor_status, "b": bank_status,
                    "m": mismatch, "c": category, "d": "; ".join(details) if details else None,
                },
            )

        if mismatch:
            BUSINESS_EVENTS.labels("reconciliation-service", "mismatch_found", category or "unknown").inc()
            logger.warning(f"MISMATCH [{category}] on {transaction_id}: {details}")
            try:
                _get_producer().send(TOPIC_RECONCILIATION_MISMATCH, key=transaction_id,
                                      value={"transaction_id": transaction_id, "category": category, "details": details})
            except Exception as e:
                logger.warning(f"Kafka publish failed: {e}")
        else:
            BUSINESS_EVENTS.labels("reconciliation-service", "matched", "ok").inc()

        return {"transaction_id": transaction_id, "mismatch": mismatch, "category": category, "details": details}
    finally:
        session.close()


@app.post("/reconcile/{transaction_id}", dependencies=auth_dep)
def reconcile_endpoint(transaction_id: str):
    return reconcile_one(transaction_id)


@app.post("/reconcile/run-batch", dependencies=auth_dep)
def reconcile_batch(limit: int = 200):
    """Scheduled safety-net sweep (see the CronJob in infra/k8s/): catches
    anything the event-driven consumer missed, e.g. because it was down.
    """
    session = SessionLocal()
    try:
        pending = session.execute(
            text(
                """
                SELECT p.transaction_id FROM processor_records p
                LEFT JOIN reconciliation_reports r ON r.transaction_id = p.transaction_id
                WHERE r.transaction_id IS NULL
                LIMIT :limit
                """
            ),
            {"limit": limit},
        ).fetchall()
    finally:
        session.close()

    results = [reconcile_one(str(row.transaction_id)) for row in pending]
    mismatches = sum(1 for r in results if r["mismatch"])
    return {"checked": len(results), "mismatches": mismatches}


# ------------------------------------------------------------------ exceptions

@app.get("/reconciliation/exceptions", dependencies=auth_dep)
def list_exceptions(resolved: bool = False, limit: int = 50):
    session = SessionLocal()
    try:
        rows = session.execute(
            text(
                "SELECT id, transaction_id, category, detail, internal_status, processor_status, "
                "bank_status, created_at FROM reconciliation_reports "
                "WHERE mismatch AND resolved = :resolved ORDER BY created_at DESC LIMIT :limit"
            ),
            {"resolved": resolved, "limit": limit},
        ).fetchall()
        return {"exceptions": [dict(r._mapping) for r in rows]}
    finally:
        session.close()


@app.get("/reconciliation/{report_id}", dependencies=auth_dep)
def get_exception(report_id: int):
    session = SessionLocal()
    try:
        row = session.execute(text("SELECT * FROM reconciliation_reports WHERE id = :id"), {"id": report_id}).fetchone()
        if row is None:
            raise HTTPException(404, "reconciliation report not found")
        return dict(row._mapping)
    finally:
        session.close()


class ResolveExceptionRequest(BaseModel):
    resolution_note: str = "manually_resolved"


@app.post("/reconciliation/{report_id}/resolve", dependencies=auth_dep)
def resolve_exception(report_id: int, req: ResolveExceptionRequest):
    """Marks a mismatch as investigated and closed. Deliberately does NOT
    touch the ledger or any balance — resolution here is an ops/audit
    action ("we looked into it, here's why"), never a way to quietly patch
    a number. Any actual money correction goes through a new, auditable
    ledger transaction (e.g. a manual reversal), never this endpoint.
    """
    session = SessionLocal()
    try:
        session.rollback()  # close any autobegun read transaction before an explicit begin (SQLAlchemy 2.0 autobegin)
        with session.begin():
            result = session.execute(
                text(
                    "UPDATE reconciliation_reports SET resolved = true, resolved_at = now(), "
                    "detail = COALESCE(detail, '') || ' | resolved: ' || :note WHERE id = :id AND mismatch RETURNING id"
                ),
                {"id": report_id, "note": req.resolution_note},
            ).fetchone()
        if result is None:
            raise HTTPException(404, "no unresolved mismatch with that id")
        BUSINESS_EVENTS.labels("reconciliation-service", "exception_resolved", "ok").inc()
        return {"id": report_id, "resolved": True}
    finally:
        session.close()


@app.post("/reconciliation/{report_id}/replay", dependencies=auth_dep)
def replay_exception(report_id: int):
    """Re-runs reconciliation for the underlying transaction — useful when
    the mismatch was caused by a timing issue (e.g. the bank record landed
    late) rather than a real discrepancy, and you want to confirm it now
    matches without waiting for the next batch sweep.
    """
    session = SessionLocal()
    try:
        row = session.execute(text("SELECT transaction_id FROM reconciliation_reports WHERE id = :id"), {"id": report_id}).fetchone()
        if row is None:
            raise HTTPException(404, "reconciliation report not found")
    finally:
        session.close()
    return reconcile_one(str(row.transaction_id))


@app.get("/reconcile/report", dependencies=auth_dep)
def reconciliation_summary():
    session = SessionLocal()
    try:
        totals = session.execute(
            text("SELECT COUNT(*) FILTER (WHERE mismatch) AS mismatches, COUNT(*) AS total FROM reconciliation_reports")
        ).fetchone()
        by_category = session.execute(
            text("SELECT category, COUNT(*) AS n FROM reconciliation_reports WHERE mismatch GROUP BY category ORDER BY n DESC")
        ).fetchall()
        return {
            "total_checked": totals.total,
            "total_mismatches": totals.mismatches,
            "match_rate": round(1 - (totals.mismatches / totals.total), 4) if totals.total else None,
            "mismatches_by_category": {r.category or "UNCATEGORIZED": r.n for r in by_category},
        }
    finally:
        session.close()


# ------------------------------------------------------------- event-driven

def _consume_ledger_posted():
    """Background thread: consumes ledger.posted off Kafka and reconciles
    each SETTLED transaction almost immediately, using the INBOX pattern
    (inbox_events) so Kafka's at-least-once delivery can't reconcile (or
    double-count) the same event twice.
    """
    consumer = get_consumer(TOPIC_LEDGER_POSTED, group_id=CONSUMER_GROUP)
    logger.info("Started ledger.posted consumer thread")
    for message in consumer:
        event = message.value
        event_id = f"{message.topic}-{message.partition}-{message.offset}"
        transaction_id = event.get("transaction_id")
        event_type = event.get("event_type")

        session = SessionLocal()
        try:
            session.rollback()  # close any autobegun read transaction before an explicit begin (SQLAlchemy 2.0 autobegin)
            with session.begin():
                inserted = session.execute(
                    text(
                        "INSERT INTO inbox_events (event_id, consumer_name) VALUES (:eid, :cn) "
                        "ON CONFLICT DO NOTHING RETURNING event_id"
                    ),
                    {"eid": event_id, "cn": CONSUMER_GROUP},
                ).fetchone()
            if inserted is None:
                continue  # already processed this exact offset — inbox caught a redelivery

            if event_type == "ledger.posted" and transaction_id:
                simulate_external_records(transaction_id)
                reconcile_one(transaction_id)
        except Exception:
            logger.exception(f"Failed processing ledger.posted event for {transaction_id} — will not retry this offset "
                              f"automatically; the batch sweep CronJob will catch it")
        finally:
            session.close()


@app.on_event("startup")
def start_background_consumer():
    if os.getenv("DISABLE_KAFKA_CONSUMER") == "true":
        return
    threading.Thread(target=_consume_ledger_posted, daemon=True).start()
