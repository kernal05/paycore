"""Ledger Service
================
The source of truth for money, and the only writer of `transactions`,
`journal_entries`, and account balances. Everything here is designed so
that a crash at any point leaves the ledger in a *correct* state, never a
partially-money-moved one:

  - postings are DEBIT+CREDIT pairs, DB-constrained to balance (see
    scripts/init_db.sql's trg_check_journal_balance trigger)
  - concurrent writers on the same account serialize via row locks taken
    in a fixed, sorted order (deadlock-free)
  - transaction status only moves through the state machine in
    common/state_machine.py — no endpoint can jump straight to SETTLED
  - the outbox pattern means "money moved" and "event exists to notify the
    rest of the platform" commit atomically in one DB transaction
  - refunds never mutate or delete the original journal entries; they post
    a new, linked, reversing transaction (ADR-005: immutable ledger)
"""
import os
import sys
import uuid

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text

sys.path.append("/app")
from common.db import SessionLocal
from common.observability import setup_observability, BUSINESS_EVENTS
from common.state_machine import validate_transition, IllegalTransitionError
from common.auth import require_service_auth
from common.processors import ProcessorRouter
import recovery
import invariants

app = FastAPI(title="Ledger Service")

from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # local demo only -- tighten for real deployments
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
processor_router = ProcessorRouter()
logger = setup_observability(app, "ledger-service")

REQUIRE_AUTH = os.getenv("REQUIRE_SERVICE_AUTH", "true").lower() == "true"
auth_dep = [Depends(require_service_auth)] if REQUIRE_AUTH else []


def _set_status(session, txn_id: str, current_status: str, new_status: str):
    validate_transition(current_status, new_status)
    session.execute(
        text("UPDATE transactions SET status = :s, updated_at = now() WHERE id = :id"),
        {"s": new_status, "id": txn_id},
    )


def _write_outbox(session, aggregate_id: str, event_type: str, topic: str, payload: dict):
    import json
    session.execute(
        text(
            "INSERT INTO outbox_events (aggregate_id, event_type, topic, payload) "
            "VALUES (:agg, :et, :topic, :payload)"
        ),
        {"agg": aggregate_id, "et": event_type, "topic": topic, "payload": json.dumps(payload)},
    )


def _audit(session, actor: str, action: str, transaction_id: str, before: dict | None, after: dict | None):
    import json
    session.execute(
        text(
            "INSERT INTO audit_log (actor, action, transaction_id, before_state, after_state) "
            "VALUES (:actor, :action, :txn, :before, :after)"
        ),
        {
            "actor": actor, "action": action, "txn": transaction_id,
            "before": json.dumps(before) if before else None,
            "after": json.dumps(after) if after else None,
        },
    )


# ---------------------------------------------------------------- register

class RegisterRequest(BaseModel):
    idempotency_key: str
    source_account_id: str
    dest_account_id: str
    amount_minor: int = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)


@app.post("/transactions/register", dependencies=auth_dep)
def register_transaction(req: RegisterRequest):
    """Step 1 of the state machine: CREATED. Idempotent on idempotency_key —
    a retried register call returns the same transaction_id rather than
    erroring, which matters because payment-api may itself be retried by
    its own caller before it got to call fraud/post.
    """
    session = SessionLocal()
    try:
        existing = session.execute(
            text("SELECT id, status FROM transactions WHERE idempotency_key = :k"),
            {"k": req.idempotency_key},
        ).fetchone()
        if existing:
            return {"transaction_id": str(existing.id), "status": existing.status, "idempotent_replay": True}

        txn_id = str(uuid.uuid4())
        session.rollback()  # close any autobegun read transaction before an explicit begin (SQLAlchemy 2.0 autobegin)
        with session.begin():
            session.execute(
                text(
                    "INSERT INTO transactions (id, idempotency_key, source_account_id, dest_account_id, "
                    "amount_minor, currency, status) VALUES (:id, :key, :src, :dst, :amt, :cur, 'CREATED')"
                ),
                {
                    "id": txn_id, "key": req.idempotency_key, "src": req.source_account_id,
                    "dst": req.dest_account_id, "amt": req.amount_minor, "cur": req.currency,
                },
            )
            _audit(session, "service/payment-api", "PAYMENT_CREATED", txn_id, None,
                   {"amount_minor": req.amount_minor, "currency": req.currency})
        return {"transaction_id": txn_id, "status": "CREATED", "idempotent_replay": False}
    finally:
        session.close()


# -------------------------------------------------------------- risk-result

class RiskResultRequest(BaseModel):
    decision: str  # APPROVE or BLOCK
    fraud_score: float
    fraud_reasons: list[str] = []


@app.post("/transactions/{transaction_id}/risk-result", dependencies=auth_dep)
def apply_risk_result(transaction_id: str, req: RiskResultRequest):
    """CREATED -> RISK_CHECK -> (BLOCKED | AUTHORIZED).

    Idempotent w.r.t. the transaction's *actual persisted* status, not just
    its idempotency_key: if this is a retried call (payment-api crashed
    after this succeeded but before its own response committed), the
    transaction is already past CREATED and this returns the existing
    result instead of attempting an illegal RISK_CHECK transition from
    AUTHORIZED/BLOCKED. See ADR-013.
    """
    session = SessionLocal()
    try:
        row = session.execute(
            text("SELECT status, fraud_score, fraud_decision, fraud_reasons FROM transactions WHERE id = :id"),
            {"id": transaction_id},
        ).fetchone()
        if row is None:
            raise HTTPException(404, "transaction not found")

        if row.status != "CREATED":
            # Already scored on a prior attempt — replay what was actually
            # decided rather than re-deriving or erroring. Deliberately does
            # NOT re-run fraud scoring here; that decision was payment-api's
            # to make once, and re-scoring would double-count this
            # transaction against Redis velocity counters.
            return {
                "transaction_id": transaction_id, "status": row.status,
                "fraud_score": float(row.fraud_score) if row.fraud_score is not None else None,
                "fraud_decision": row.fraud_decision,
                "fraud_reasons": row.fraud_reasons or [],
                "idempotent_replay": True,
            }

        target = "BLOCKED" if req.decision == "BLOCK" else "AUTHORIZED"
        session.rollback()  # close any autobegun read transaction before an explicit begin (SQLAlchemy 2.0 autobegin)
        with session.begin():
            _set_status(session, transaction_id, row.status, "RISK_CHECK")
            _set_status(session, transaction_id, "RISK_CHECK", target)
            import json
            session.execute(
                text("UPDATE transactions SET fraud_score = :s, fraud_decision = :d, fraud_reasons = :r WHERE id = :id"),
                {"s": req.fraud_score, "d": req.decision, "r": json.dumps(req.fraud_reasons), "id": transaction_id},
            )
            _audit(session, "service/fraud-engine", "RISK_EVALUATED", transaction_id,
                   {"status": row.status}, {"status": target, "fraud_score": req.fraud_score})
        return {
            "transaction_id": transaction_id, "status": target,
            "fraud_score": req.fraud_score, "fraud_decision": req.decision,
            "fraud_reasons": req.fraud_reasons, "idempotent_replay": False,
        }
    except IllegalTransitionError as e:
        raise HTTPException(409, str(e))
    finally:
        session.close()


# -------------------------------------------------------------------- post

@app.post("/transactions/{transaction_id}/post", dependencies=auth_dep)
def post_transaction(transaction_id: str):
    """AUTHORIZED -> PROCESSING -> SETTLED | FAILED | UNKNOWN.

    Deliberately split into three phases so that a slow/hanging processor
    never holds a DB row lock open (ADR-013 addendum): (1) mark PROCESSING
    in its own short transaction — no account locks taken here; (2) call
    the processor with NO transaction open and NO locks held — this can
    take up to 5s (TIMEOUT) with today's MockProcessor and must never
    block other transfers on the same accounts while it does; (3) apply
    the processor's result in a second short transaction that takes
    account locks only for the few milliseconds needed to actually move
    money (or record FAILED/UNKNOWN).

    Idempotent w.r.t. persisted state: a retried call after the
    transaction already reached SETTLED/FAILED/UNKNOWN returns that result.
    A retry that lands while status is still PROCESSING (e.g. payment-api
    crashed or its HTTP client timed out waiting on phase 2) safely
    re-enters at phase 2 — MockProcessor's outcome is deterministic per
    transaction_id, so calling authorize() again for the same transaction
    is guaranteed to reach the same decision, not a different one.
    """
    session = SessionLocal()
    try:
        txn = session.execute(
            text("SELECT * FROM transactions WHERE id = :id"), {"id": transaction_id}
        ).fetchone()
        if txn is None:
            raise HTTPException(404, "transaction not found")

        if txn.status in ("SETTLED", "FAILED", "UNKNOWN"):
            return {"transaction_id": transaction_id, "status": txn.status, "idempotent_replay": True}

        if txn.status not in ("AUTHORIZED", "PROCESSING"):
            raise HTTPException(
                409,
                f"cannot post transaction {transaction_id}: expected AUTHORIZED or PROCESSING, found {txn.status}",
            )

        # ---- phase 1: mark PROCESSING (no account locks, tiny transaction) ----
        if txn.status == "AUTHORIZED":
            session.rollback()  # close any autobegun read transaction before an explicit begin (SQLAlchemy 2.0 autobegin)
            with session.begin():
                _set_status(session, transaction_id, "AUTHORIZED", "PROCESSING")

        # ---- phase 2: call the processor with NO transaction/locks held ----
        auth_result = processor_router.get("mock").authorize(transaction_id, txn.amount_minor, txn.currency)

        # ---- phase 3: apply the result — locks held only for this short transaction ----
        session.rollback()  # close any autobegun read transaction before an explicit begin (SQLAlchemy 2.0 autobegin)
        with session.begin():
            session.execute(
                text("UPDATE transactions SET processor_name = 'mock', processor_ref = :ref WHERE id = :id"),
                {"ref": auth_result.processor_reference, "id": transaction_id},
            )

            if auth_result.status == "DECLINED":
                _set_status(session, transaction_id, "PROCESSING", "FAILED")
                _write_outbox(session, transaction_id, "ledger.failed", "ledger.posted",
                              {"transaction_id": transaction_id, "reason": "processor_declined", "detail": auth_result.detail})
                BUSINESS_EVENTS.labels("ledger-service", "posting_failed", "processor_declined").inc()
                return {"transaction_id": transaction_id, "status": "FAILED"}

            if auth_result.status in ("TIMEOUT", "UNKNOWN", "DUPLICATE"):
                # Do NOT move money yet — we genuinely don't know the outcome.
                # A webhook (see payment-api's /webhooks/processor) will call
                # /transactions/{id}/resolve-unknown once the processor tells
                # us what actually happened.
                _set_status(session, transaction_id, "PROCESSING", "UNKNOWN")
                _write_outbox(session, transaction_id, "ledger.unknown", "ledger.posted",
                              {"transaction_id": transaction_id, "processor_status": auth_result.status, "detail": auth_result.detail})
                BUSINESS_EVENTS.labels("ledger-service", "posting_unknown", auth_result.status.lower()).inc()
                return {"transaction_id": transaction_id, "status": "UNKNOWN", "detail": auth_result.detail}

            # SUCCESS: only now do we take account locks, and only for the
            # few statements needed to actually move the money.
            ids_in_order = sorted([str(txn.source_account_id), str(txn.dest_account_id)])
            for acc_id in ids_in_order:
                session.execute(text("SELECT id FROM accounts WHERE id = :id FOR UPDATE"), {"id": acc_id})

            source = session.execute(
                text("SELECT balance_minor, currency FROM accounts WHERE id = :id"),
                {"id": txn.source_account_id},
            ).fetchone()
            if source is None:
                raise HTTPException(404, "source account not found")
            if source.currency != txn.currency:
                raise HTTPException(400, "currency mismatch on source account")

            if source.balance_minor < txn.amount_minor:
                # The processor said SUCCESS but our own ledger can't cover
                # it — trust our own books, not the processor, and fail.
                _set_status(session, transaction_id, "PROCESSING", "FAILED")
                _write_outbox(session, transaction_id, "ledger.failed", "ledger.posted",
                              {"transaction_id": transaction_id, "reason": "insufficient_funds"})
                BUSINESS_EVENTS.labels("ledger-service", "posting_failed", "insufficient_funds").inc()
                return {"transaction_id": transaction_id, "status": "FAILED"}

            session.execute(
                text("UPDATE accounts SET balance_minor = balance_minor - :amt, version = version + 1 WHERE id = :id"),
                {"amt": txn.amount_minor, "id": txn.source_account_id},
            )
            session.execute(
                text("UPDATE accounts SET balance_minor = balance_minor + :amt, version = version + 1 WHERE id = :id"),
                {"amt": txn.amount_minor, "id": txn.dest_account_id},
            )
            session.execute(
                text(
                    "INSERT INTO journal_entries (transaction_id, account_id, direction, amount_minor, currency) "
                    "VALUES (:txn, :acc, 'DEBIT', :amt, :cur)"
                ),
                {"txn": transaction_id, "acc": txn.source_account_id, "amt": txn.amount_minor, "cur": txn.currency},
            )
            session.execute(
                text(
                    "INSERT INTO journal_entries (transaction_id, account_id, direction, amount_minor, currency) "
                    "VALUES (:txn, :acc, 'CREDIT', :amt, :cur)"
                ),
                {"txn": transaction_id, "acc": txn.dest_account_id, "amt": txn.amount_minor, "cur": txn.currency},
            )
            _set_status(session, transaction_id, "PROCESSING", "SETTLED")
            _write_outbox(session, transaction_id, "ledger.posted", "ledger.posted",
                          {"transaction_id": transaction_id, "amount_minor": txn.amount_minor, "currency": txn.currency})
            _audit(session, "service/ledger-service", "PAYMENT_SETTLED", transaction_id,
                   {"status": txn.status}, {"status": "SETTLED"})

        BUSINESS_EVENTS.labels("ledger-service", "posting_success", "settled").inc()
        logger.info(f"Settled transaction {transaction_id}: {txn.amount_minor} {txn.currency}")
        return {"transaction_id": transaction_id, "status": "SETTLED"}
    except IllegalTransitionError as e:
        raise HTTPException(409, str(e))
    finally:
        session.close()


# --------------------------------------------------------- resolve-unknown

def _finalize_terminal_resolution(session, transaction_id: str, txn, resolved_status: str,
                                   processor_ref: str | None, actor: str, from_status: str):
    """Shared core for every path that resolves a parked transaction to a
    final SETTLED/FAILED outcome — a webhook, the recovery sweep, or a
    human via /manual-resolve. Exactly one of these ever actually moves
    money for a given transaction (idempotent-replay guards at each call
    site ensure this function only runs once per transaction).
    """
    if resolved_status == "FAILED":
        _set_status(session, transaction_id, from_status, "FAILED")
        _write_outbox(session, transaction_id, "ledger.failed", "ledger.posted",
                      {"transaction_id": transaction_id, "reason": f"resolved_failed_via_{actor}"})
        _audit(session, actor, f"{from_status}_RESOLVED_FAILED", transaction_id,
               {"status": from_status}, {"status": "FAILED"})
        BUSINESS_EVENTS.labels("ledger-service", "unknown_resolved", "failed").inc()
        return "FAILED"

    ids_in_order = sorted([str(txn.source_account_id), str(txn.dest_account_id)])
    for acc_id in ids_in_order:
        session.execute(text("SELECT id FROM accounts WHERE id = :id FOR UPDATE"), {"id": acc_id})

    session.execute(
        text("UPDATE accounts SET balance_minor = balance_minor - :amt, version = version + 1 WHERE id = :id"),
        {"amt": txn.amount_minor, "id": txn.source_account_id},
    )
    session.execute(
        text("UPDATE accounts SET balance_minor = balance_minor + :amt, version = version + 1 WHERE id = :id"),
        {"amt": txn.amount_minor, "id": txn.dest_account_id},
    )
    session.execute(
        text("INSERT INTO journal_entries (transaction_id, account_id, direction, amount_minor, currency) "
             "VALUES (:txn, :acc, 'DEBIT', :amt, :cur)"),
        {"txn": transaction_id, "acc": txn.source_account_id, "amt": txn.amount_minor, "cur": txn.currency},
    )
    session.execute(
        text("INSERT INTO journal_entries (transaction_id, account_id, direction, amount_minor, currency) "
             "VALUES (:txn, :acc, 'CREDIT', :amt, :cur)"),
        {"txn": transaction_id, "acc": txn.dest_account_id, "amt": txn.amount_minor, "cur": txn.currency},
    )
    _set_status(session, transaction_id, from_status, "SETTLED")
    if processor_ref:
        session.execute(text("UPDATE transactions SET processor_ref = :ref WHERE id = :id"), {"ref": processor_ref, "id": transaction_id})
    _write_outbox(session, transaction_id, "ledger.posted", "ledger.posted",
                  {"transaction_id": transaction_id, "amount_minor": txn.amount_minor, "currency": txn.currency})
    _audit(session, actor, f"{from_status}_RESOLVED_SETTLED", transaction_id,
           {"status": from_status}, {"status": "SETTLED"})
    BUSINESS_EVENTS.labels("ledger-service", "unknown_resolved", "settled").inc()
    return "SETTLED"


class ResolveUnknownRequest(BaseModel):
    resolved_status: str  # "SETTLED" or "FAILED" — what the processor's webhook actually reported
    processor_ref: str | None = None


@app.post("/transactions/{transaction_id}/resolve-unknown", dependencies=auth_dep)
def resolve_unknown_transaction(transaction_id: str, req: ResolveUnknownRequest):
    """UNKNOWN -> RECONCILIATION -> (SETTLED | FAILED). Called from
    payment-api's /webhooks/processor once the processor tells us, out of
    band, what actually happened to an authorization we couldn't get a
    clear answer on synchronously. If the resolution is SETTLED, this is
    where the money actually moves — it was never posted while UNKNOWN.
    """
    if req.resolved_status not in ("SETTLED", "FAILED"):
        raise HTTPException(400, "resolved_status must be SETTLED or FAILED")

    session = SessionLocal()
    try:
        txn = session.execute(text("SELECT * FROM transactions WHERE id = :id"), {"id": transaction_id}).fetchone()
        if txn is None:
            raise HTTPException(404, "transaction not found")
        if txn.status != "UNKNOWN":
            # Already resolved (webhook redelivered, or the recovery sweep got there first) — idempotent no-op.
            return {"transaction_id": transaction_id, "status": txn.status, "idempotent_replay": True}

        session.rollback()  # close any autobegun read transaction before an explicit begin (SQLAlchemy 2.0 autobegin)

        with session.begin():
            _set_status(session, transaction_id, "UNKNOWN", "RECONCILIATION")
            result_status = _finalize_terminal_resolution(
                session, transaction_id, txn, req.resolved_status, req.processor_ref,
                actor="service/payment-api-webhook", from_status="RECONCILIATION",
            )

        return {"transaction_id": transaction_id, "status": result_status}
    except IllegalTransitionError as e:
        raise HTTPException(409, str(e))
    finally:
        session.close()


# ------------------------------------------------------------ recovery engine

@app.post("/recovery/run-sweep", dependencies=auth_dep)
def run_recovery_sweep(limit: int = 50):
    """Queries the (mock) processor's status for every UNKNOWN transaction
    whose exponential-backoff window has elapsed, and either resolves it
    (same finalize path as a webhook), records another PENDING attempt, or
    — past MAX_RECOVERY_ATTEMPTS — escalates to MANUAL_REVIEW. Intended to
    run on a schedule (see infra/k8s's CronJob pattern, same idea as
    reconciliation's batch sweep) rather than being triggered per-request.
    """
    session = SessionLocal()
    try:
        due = recovery.find_due_transactions(session, limit=limit)
    finally:
        session.close()

    results = []
    for transaction_id, attempt_number in due:
        session = SessionLocal()
        try:
            txn = session.execute(text("SELECT * FROM transactions WHERE id = :id"), {"id": transaction_id}).fetchone()
            if txn is None or txn.status != "UNKNOWN":
                continue  # resolved by something else since the sweep started (webhook race) — nothing to do

            status_result = processor_router.get("mock").get_status(transaction_id, attempt_number)
            session.rollback()

            if status_result.status in ("SUCCESS", "DECLINED"):
                with session.begin():
                    recovery.record_attempt(session, transaction_id, attempt_number, status_result.status, status_result.detail)
                    _set_status(session, transaction_id, "UNKNOWN", "RECONCILIATION")
                    resolved = "SETTLED" if status_result.status == "SUCCESS" else "FAILED"
                    result_status = _finalize_terminal_resolution(
                        session, transaction_id, txn, resolved, status_result.processor_reference,
                        actor="service/recovery-engine", from_status="RECONCILIATION",
                    )
                results.append({"transaction_id": transaction_id, "attempt": attempt_number, "outcome": result_status})

            elif attempt_number >= recovery.MAX_RECOVERY_ATTEMPTS - 1:
                # Still no answer, and we're out of budget — escalate to a human rather than poll forever.
                with session.begin():
                    recovery.record_attempt(session, transaction_id, attempt_number, "ESCALATED_TO_MANUAL_REVIEW",
                                             f"exhausted {recovery.MAX_RECOVERY_ATTEMPTS} recovery attempts")
                    _set_status(session, transaction_id, "UNKNOWN", "RECONCILIATION")
                    _set_status(session, transaction_id, "RECONCILIATION", "MANUAL_REVIEW")
                    _audit(session, "service/recovery-engine", "ESCALATED_TO_MANUAL_REVIEW", transaction_id,
                           {"status": "UNKNOWN"}, {"status": "MANUAL_REVIEW"})
                    BUSINESS_EVENTS.labels("ledger-service", "recovery_escalated", "manual_review").inc()
                results.append({"transaction_id": transaction_id, "attempt": attempt_number, "outcome": "ESCALATED_TO_MANUAL_REVIEW"})

            else:
                # Still pending, budget remains — record the attempt and wait for the next backoff window.
                with session.begin():
                    recovery.record_attempt(session, transaction_id, attempt_number, "PENDING", status_result.detail)
                results.append({"transaction_id": transaction_id, "attempt": attempt_number, "outcome": "PENDING"})

        except IllegalTransitionError as e:
            results.append({"transaction_id": transaction_id, "attempt": attempt_number, "outcome": "ERROR", "detail": str(e)})
        finally:
            session.close()

    return {"checked": len(due), "results": results}


class ManualResolveRequest(BaseModel):
    resolved_status: str  # "SETTLED" or "FAILED"
    resolver: str  # e.g. an ops engineer's name/id — goes straight into the audit trail
    note: str


@app.post("/transactions/{transaction_id}/manual-resolve", dependencies=auth_dep)
def manual_resolve_transaction(transaction_id: str, req: ManualResolveRequest):
    """The human-in-the-loop escape hatch for a transaction the recovery
    engine gave up automating on. Requires a named resolver and a note —
    both go into audit_log — because this is exactly the kind of action
    that must never be anonymous or unexplained in a financial system.
    """
    if req.resolved_status not in ("SETTLED", "FAILED"):
        raise HTTPException(400, "resolved_status must be SETTLED or FAILED")
    if not req.resolver.strip() or not req.note.strip():
        raise HTTPException(400, "resolver and note are required for a manual financial decision")

    session = SessionLocal()
    try:
        txn = session.execute(text("SELECT * FROM transactions WHERE id = :id"), {"id": transaction_id}).fetchone()
        if txn is None:
            raise HTTPException(404, "transaction not found")
        if txn.status != "MANUAL_REVIEW":
            return {"transaction_id": transaction_id, "status": txn.status, "idempotent_replay": True}

        session.rollback()
        with session.begin():
            result_status = _finalize_terminal_resolution(
                session, transaction_id, txn, req.resolved_status, None,
                actor=f"human/{req.resolver}", from_status="MANUAL_REVIEW",
            )
            _audit(session, f"human/{req.resolver}", "MANUAL_RESOLUTION_NOTE", transaction_id, None, {"note": req.note})

        return {"transaction_id": transaction_id, "status": result_status}
    except IllegalTransitionError as e:
        raise HTTPException(409, str(e))
    finally:
        session.close()


@app.get("/recovery/pending", dependencies=auth_dep)
def list_recovery_pending():
    """Everything currently in UNKNOWN or MANUAL_REVIEW, with its attempt
    history — the queue an ops dashboard would actually show.
    """
    session = SessionLocal()
    try:
        rows = session.execute(
            text(
                """
                SELECT t.id, t.status, t.amount_minor, t.currency, t.created_at,
                       COALESCE(MAX(r.attempt_number), -1) AS attempts_made
                FROM transactions t
                LEFT JOIN recovery_attempts r ON r.transaction_id = t.id
                WHERE t.status IN ('UNKNOWN', 'MANUAL_REVIEW')
                GROUP BY t.id, t.status, t.amount_minor, t.currency, t.created_at
                ORDER BY t.created_at ASC
                """
            )
        ).fetchall()
        return {"pending": [dict(r._mapping) for r in rows]}
    finally:
        session.close()


# ------------------------------------------------------------------ refund

class RefundRequest(BaseModel):
    reason: str = "customer_requested"


@app.post("/transactions/{transaction_id}/refund", dependencies=auth_dep)
def refund_transaction(transaction_id: str, req: RefundRequest):
    """SETTLED -> REFUND_REQUESTED -> REFUNDED. Never touches the original
    journal entries — posts a brand-new transaction with swapped
    DEBIT/CREDIT accounts, linked via original_transaction_id, so the
    original transfer remains a permanent, unaltered part of the record.
    """
    session = SessionLocal()
    try:
        original = session.execute(
            text("SELECT * FROM transactions WHERE id = :id"), {"id": transaction_id}
        ).fetchone()
        if original is None:
            raise HTTPException(404, "transaction not found")
        if original.transaction_type == "REFUND":
            raise HTTPException(400, "cannot refund a refund")

        refund_id = str(uuid.uuid4())
        refund_idempotency_key = f"refund:{transaction_id}"

        existing_refund = session.execute(
            text("SELECT id, status FROM transactions WHERE idempotency_key = :k"),
            {"k": refund_idempotency_key},
        ).fetchone()
        if existing_refund:
            return {"refund_transaction_id": str(existing_refund.id), "status": existing_refund.status, "idempotent_replay": True}

        session.rollback()  # close any autobegun read transaction before an explicit begin (SQLAlchemy 2.0 autobegin)

        with session.begin():
            _set_status(session, transaction_id, original.status, "REFUND_REQUESTED")

            session.execute(
                text(
                    "INSERT INTO transactions (id, idempotency_key, source_account_id, dest_account_id, "
                    "amount_minor, currency, status, transaction_type, original_transaction_id) "
                    "VALUES (:id, :key, :src, :dst, :amt, :cur, 'AUTHORIZED', 'REFUND', :orig)"
                ),
                {
                    "id": refund_id, "key": refund_idempotency_key,
                    "src": original.dest_account_id, "dst": original.source_account_id,  # reversed direction
                    "amt": original.amount_minor, "cur": original.currency, "orig": transaction_id,
                },
            )

            ids_in_order = sorted([str(original.source_account_id), str(original.dest_account_id)])
            for acc_id in ids_in_order:
                session.execute(text("SELECT id FROM accounts WHERE id = :id FOR UPDATE"), {"id": acc_id})

            session.execute(
                text("UPDATE accounts SET balance_minor = balance_minor - :amt, version = version + 1 WHERE id = :id"),
                {"amt": original.amount_minor, "id": original.dest_account_id},
            )
            session.execute(
                text("UPDATE accounts SET balance_minor = balance_minor + :amt, version = version + 1 WHERE id = :id"),
                {"amt": original.amount_minor, "id": original.source_account_id},
            )
            session.execute(
                text(
                    "INSERT INTO journal_entries (transaction_id, account_id, direction, amount_minor, currency) "
                    "VALUES (:txn, :acc, 'DEBIT', :amt, :cur)"
                ),
                {"txn": refund_id, "acc": original.dest_account_id, "amt": original.amount_minor, "cur": original.currency},
            )
            session.execute(
                text(
                    "INSERT INTO journal_entries (transaction_id, account_id, direction, amount_minor, currency) "
                    "VALUES (:txn, :acc, 'CREDIT', :amt, :cur)"
                ),
                {"txn": refund_id, "acc": original.source_account_id, "amt": original.amount_minor, "cur": original.currency},
            )

            _set_status(session, refund_id, "AUTHORIZED", "PROCESSING")
            _set_status(session, refund_id, "PROCESSING", "SETTLED")
            _set_status(session, transaction_id, "REFUND_REQUESTED", "REFUNDED")

            _write_outbox(session, refund_id, "ledger.refunded", "ledger.posted",
                          {"transaction_id": refund_id, "original_transaction_id": transaction_id,
                           "amount_minor": original.amount_minor, "currency": original.currency})
            _audit(session, "service/ledger-service", "REFUND_ISSUED", transaction_id,
                   {"status": "SETTLED"}, {"status": "REFUNDED", "refund_transaction_id": refund_id, "reason": req.reason})

        BUSINESS_EVENTS.labels("ledger-service", "refund_issued", "settled").inc()
        return {"refund_transaction_id": refund_id, "original_transaction_id": transaction_id, "status": "SETTLED"}
    except IllegalTransitionError as e:
        raise HTTPException(409, str(e))
    finally:
        session.close()


# ------------------------------------------------------------------- reads

@app.get("/accounts/{account_id}/balance", dependencies=auth_dep)
def get_balance(account_id: str):
    session = SessionLocal()
    try:
        row = session.execute(
            text("SELECT balance_minor, currency, version FROM accounts WHERE id = :id"), {"id": account_id}
        ).fetchone()
        if row is None:
            raise HTTPException(404, "account not found")
        return {"account_id": account_id, "balance_minor": row.balance_minor, "currency": row.currency, "version": row.version}
    finally:
        session.close()


@app.get("/transactions/{transaction_id}", dependencies=auth_dep)
def get_transaction(transaction_id: str):
    session = SessionLocal()
    try:
        row = session.execute(text("SELECT * FROM transactions WHERE id = :id"), {"id": transaction_id}).fetchone()
        if row is None:
            raise HTTPException(404, "transaction not found")
        return dict(row._mapping)
    finally:
        session.close()


# -------------------------------------------------------------- integrity

@app.get("/journal/integrity-check")
def journal_integrity_check():
    """Debit==credit per transaction. The DB trigger already makes this
    impossible to violate going forward; this endpoint is the scheduled
    proof (see the CronJob in infra/k8s/) that it's actually true, and the
    thing PromQL/alerting watches via the imbalance count.
    """
    session = SessionLocal()
    try:
        rows = session.execute(
            text(
                """
                SELECT transaction_id,
                       SUM(CASE WHEN direction = 'DEBIT' THEN amount_minor ELSE -amount_minor END) AS imbalance
                FROM journal_entries GROUP BY transaction_id
                HAVING SUM(CASE WHEN direction = 'DEBIT' THEN amount_minor ELSE -amount_minor END) != 0
                """
            )
        ).fetchall()
        healthy = len(rows) == 0
        BUSINESS_EVENTS.labels("ledger-service", "integrity_check", "healthy" if healthy else "violated").inc()
        return {"imbalanced_transactions": [dict(r._mapping) for r in rows], "healthy": healthy}
    finally:
        session.close()


@app.get("/ledger/consistency-check")
def balance_vs_journal_consistency():
    """A different, complementary check from integrity-check: does each
    account's current balance actually equal what its journal history says
    it should be? Catches a bug in the balance UPDATE path even if every
    individual posting is internally balanced.
    """
    session = SessionLocal()
    try:
        rows = session.execute(
            text(
                """
                SELECT a.id, a.balance_minor,
                       COALESCE(SUM(CASE WHEN j.direction = 'CREDIT' THEN j.amount_minor
                                          WHEN j.direction = 'DEBIT' THEN -j.amount_minor
                                          ELSE 0 END), 0) AS computed_balance
                FROM accounts a
                LEFT JOIN journal_entries j ON j.account_id = a.id
                GROUP BY a.id, a.balance_minor
                HAVING a.balance_minor != COALESCE(SUM(CASE WHEN j.direction = 'CREDIT' THEN j.amount_minor
                                          WHEN j.direction = 'DEBIT' THEN -j.amount_minor
                                          ELSE 0 END), 0)
                """
            )
        ).fetchall()
        healthy = len(rows) == 0
        BUSINESS_EVENTS.labels("ledger-service", "consistency_check", "healthy" if healthy else "violated").inc()
        return {"inconsistent_accounts": [dict(r._mapping) for r in rows], "healthy": healthy}
    finally:
        session.close()


@app.get("/financial-health")
def financial_health():
    """The Financial Invariant Monitor (ADR-016): 11 correctness properties
    re-derived across the whole ledger, not just checked at transaction
    time. Cheap enough to scrape on a schedule — wire a Prometheus alert
    on `overall_status != PASS` the same way the other integrity checks
    are wired in observability/alerting-rules.yml.
    """
    session = SessionLocal()
    try:
        result = invariants.run_all_checks(session)
        for check in result["checks"]:
            BUSINESS_EVENTS.labels("ledger-service", f"invariant_{check['name']}", check["status"].lower()).inc()
        return result
    finally:
        session.close()


# ------------------------------------------------------------------ timeline

@app.get("/transactions/{transaction_id}/timeline")
def payment_timeline(transaction_id: str):
    """Payment Forensic Timeline (ADR-017): every recorded event touching
    this transaction, from every table that has a say in its life —
    audit_log (state transitions), recovery_attempts (automated recovery
    tries), reconciliation_reports (settlement matching), webhook_events
    (async processor callbacks) — merged into one ordered sequence.

    This is the answer to "payment X says UNKNOWN, what happened?": read
    this endpoint instead of grep-ing four services' logs.
    """
    session = SessionLocal()
    try:
        txn = session.execute(text("SELECT * FROM transactions WHERE id = :id"), {"id": transaction_id}).fetchone()
        if txn is None:
            raise HTTPException(404, "transaction not found")

        events = []

        events.append({"ts": txn.created_at.isoformat(), "event": "TRANSACTION_CREATED",
                        "detail": {"amount_minor": txn.amount_minor, "currency": txn.currency, "idempotency_key": txn.idempotency_key}})

        for row in session.execute(
            text("SELECT actor, action, before_state, after_state, created_at FROM audit_log "
                 "WHERE transaction_id = :id ORDER BY created_at"),
            {"id": transaction_id},
        ).fetchall():
            events.append({"ts": row.created_at.isoformat(), "event": row.action,
                            "detail": {"actor": row.actor, "before": row.before_state, "after": row.after_state}})

        for row in session.execute(
            text("SELECT attempt_number, method, outcome, detail, attempted_at FROM recovery_attempts "
                 "WHERE transaction_id = :id ORDER BY attempted_at"),
            {"id": transaction_id},
        ).fetchall():
            events.append({"ts": row.attempted_at.isoformat(), "event": f"RECOVERY_ATTEMPT_{row.attempt_number}",
                            "detail": {"method": row.method, "outcome": row.outcome, "detail": row.detail}})

        for row in session.execute(
            text("SELECT id, mismatch, category, detail, created_at FROM reconciliation_reports "
                 "WHERE transaction_id = :id ORDER BY created_at"),
            {"id": transaction_id},
        ).fetchall():
            events.append({"ts": row.created_at.isoformat(), "event": "RECONCILIATION_MATCH" if not row.mismatch else "RECONCILIATION_MISMATCH",
                            "detail": {"report_id": row.id, "category": row.category, "detail": row.detail}})

        for row in session.execute(
            text("SELECT event_id, processor, received_at, processed_at FROM webhook_events "
                 "WHERE payload->>'transaction_id' = :id ORDER BY received_at"),
            {"id": transaction_id},
        ).fetchall():
            events.append({"ts": row.received_at.isoformat(), "event": "WEBHOOK_RECEIVED",
                            "detail": {"event_id": row.event_id, "processor": row.processor, "processed": row.processed_at is not None}})

        for row in session.execute(
            text("SELECT event_type, topic, published_at, created_at, payload FROM outbox_events WHERE aggregate_id = :id ORDER BY created_at"),
            {"id": transaction_id},
        ).fetchall():
            events.append({"ts": row.created_at.isoformat(), "event": f"OUTBOX_EVENT_CREATED:{row.event_type}",
                            "detail": _outbox_detail(row)})

        events.sort(key=lambda e: e["ts"])

        return {
            "transaction_id": transaction_id,
            "current_status": txn.status,
            "amount_minor": txn.amount_minor,
            "currency": txn.currency,
            "event_count": len(events),
            "timeline": events,
        }
    finally:
        session.close()


def _outbox_detail(row):
    """Timeline detail for an outbox event: delivery state plus the business reason, if any."""
    import json as _json
    detail = {"topic": row.topic, "published": row.published_at is not None}
    payload = row.payload
    if isinstance(payload, str):
        try:
            payload = _json.loads(payload)
        except Exception:
            payload = None
    if isinstance(payload, dict):
        for k in ("reason", "detail", "processor_status"):
            if k in payload:
                detail[k] = payload[k]
    return detail
