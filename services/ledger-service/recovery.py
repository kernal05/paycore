"""Payment Recovery Engine (ADR-015)
================
What happens to a transaction that comes back TIMEOUT/UNKNOWN from
authorization today is: it sits in UNKNOWN until a webhook happens to
arrive. That's fine when the processor sends one, but real processors
don't always send a timely webhook for an ambiguous authorization — you
also need to be able to *ask*. This module is that: a scheduled sweep that
queries the processor's status for every UNKNOWN transaction whose backoff
window has elapsed, records every attempt (recovery_attempts), and — after
a bounded number of attempts — escalates to MANUAL_REVIEW rather than
polling forever.

    UNKNOWN --(webhook arrives)--> resolve-unknown (existing path)
    UNKNOWN --(sweep, backoff elapsed)--> query processor --> still pending: wait longer
                                                            --> resolved: settle/fail, same as a webhook
                                                            --> attempts exhausted: MANUAL_REVIEW

Both resolution paths (webhook, recovery sweep) funnel through the same
`_finalize_unknown` core logic as resolve-unknown, so there is exactly one
place that ever moves money for a previously-UNKNOWN transaction.
"""
import os

from sqlalchemy import text

MAX_RECOVERY_ATTEMPTS = int(os.getenv("MAX_RECOVERY_ATTEMPTS", "4"))
BACKOFF_BASE_SECONDS = int(os.getenv("RECOVERY_BACKOFF_BASE_SECONDS", "30"))


def backoff_seconds(attempt_number: int) -> int:
    """Exponential backoff: 30s, 60s, 120s, 240s, ... capped at 1 hour so a
    transaction stuck for days doesn't get queried at absurd intervals.
    """
    return min(BACKOFF_BASE_SECONDS * (2 ** attempt_number), 3600)


def find_due_transactions(session, limit: int = 50):
    """UNKNOWN transactions whose next backoff window has elapsed, or that
    have never been attempted yet. Computed from recovery_attempts rather
    than stored as a separate "next_attempt_at" column, so the backoff
    schedule can change (env var) without a migration.
    """
    rows = session.execute(
        text(
            """
            SELECT t.id, t.created_at, COALESCE(MAX(r.attempt_number), -1) AS last_attempt,
                   MAX(r.attempted_at) AS last_attempted_at
            FROM transactions t
            LEFT JOIN recovery_attempts r ON r.transaction_id = t.id
            WHERE t.status = 'UNKNOWN'
            GROUP BY t.id, t.created_at
            LIMIT :limit
            """
        ),
        {"limit": limit},
    ).fetchall()

    due = []
    for row in rows:
        next_attempt = row.last_attempt + 1
        if row.last_attempted_at is None:
            due.append((str(row.id), next_attempt))
            continue
        elapsed = session.execute(
            text("SELECT EXTRACT(EPOCH FROM (now() - :last_at)) AS secs"),
            {"last_at": row.last_attempted_at},
        ).scalar()
        if elapsed >= backoff_seconds(row.last_attempt):
            due.append((str(row.id), next_attempt))
    return due


def record_attempt(session, transaction_id: str, attempt_number: int, outcome: str, detail: str):
    session.execute(
        text(
            "INSERT INTO recovery_attempts (transaction_id, attempt_number, outcome, detail) "
            "VALUES (:t, :a, :o, :d)"
        ),
        {"t": transaction_id, "a": attempt_number, "o": outcome, "d": detail},
    )
