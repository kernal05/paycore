"""Financial Invariant Monitor (ADR-016)
================
Everything else in this platform checks correctness *at the moment a
transaction happens* (the DB trigger, the idempotency layer, the state
machine). This module is different: it re-derives a set of properties that
should hold across the *entire* ledger at any point in time, so drift
introduced by a future bug, a bad migration, or manual intervention gets
caught even though no single transaction did anything wrong in isolation.

Exposed as GET /financial-health — cheap enough to run on a schedule
(a Prometheus scrape, a health-check cron) rather than only on demand.
"""
from sqlalchemy import text


def _check(session, name: str, query: str, params: dict | None = None) -> dict:
    """Runs a query that returns violating rows (empty result = PASS).
    Every invariant below follows this same shape on purpose: a single
    query whose result set IS the list of violations, so "PASS" always
    means "we looked and found zero", never "we didn't check."
    """
    rows = session.execute(text(query), params or {}).fetchall()
    violations = [dict(r._mapping) for r in rows]
    return {"name": name, "status": "PASS" if not violations else "FAIL", "violation_count": len(violations), "violations": violations[:10]}


def run_all_checks(session) -> dict:
    checks = [
        _check(session, "ledger_balances_to_zero", """
            SELECT transaction_id, SUM(CASE WHEN direction='DEBIT' THEN amount_minor ELSE -amount_minor END) AS imbalance
            FROM journal_entries GROUP BY transaction_id
            HAVING SUM(CASE WHEN direction='DEBIT' THEN amount_minor ELSE -amount_minor END) != 0
        """),
        _check(session, "settled_transactions_have_exactly_two_journal_legs", """
            SELECT t.id AS transaction_id, count(j.id) AS leg_count
            FROM transactions t LEFT JOIN journal_entries j ON j.transaction_id = t.id
            WHERE t.status = 'SETTLED' GROUP BY t.id HAVING count(j.id) != 2
        """),
        _check(session, "no_duplicate_journal_legs", """
            SELECT transaction_id, account_id, direction, count(*) AS n
            FROM journal_entries GROUP BY transaction_id, account_id, direction HAVING count(*) > 1
        """),
        _check(session, "account_balance_matches_journal_history", """
            SELECT a.id AS account_id, a.balance_minor, COALESCE(SUM(
                CASE WHEN j.direction = 'CREDIT' THEN j.amount_minor ELSE -j.amount_minor END), 0) AS computed_balance
            FROM accounts a LEFT JOIN journal_entries j ON j.account_id = a.id
            GROUP BY a.id, a.balance_minor
            HAVING a.balance_minor != COALESCE(SUM(
                CASE WHEN j.direction = 'CREDIT' THEN j.amount_minor ELSE -j.amount_minor END), 0)
        """),
        _check(session, "refunds_reference_exactly_one_original_transaction", """
            SELECT id AS transaction_id FROM transactions
            WHERE transaction_type = 'REFUND' AND original_transaction_id IS NULL
        """),
        _check(session, "refund_amount_never_exceeds_original", """
            SELECT r.id AS refund_transaction_id, r.amount_minor AS refund_amount, o.amount_minor AS original_amount
            FROM transactions r JOIN transactions o ON o.id = r.original_transaction_id
            WHERE r.transaction_type = 'REFUND' AND r.amount_minor > o.amount_minor
        """),
        _check(session, "terminal_non_settled_transactions_moved_no_money", """
            SELECT t.id AS transaction_id, t.status, count(j.id) AS journal_legs
            FROM transactions t JOIN journal_entries j ON j.transaction_id = t.id
            WHERE t.status IN ('BLOCKED', 'FAILED') GROUP BY t.id, t.status
        """),
        _check(session, "no_timestamp_moved_backwards", """
            SELECT id AS transaction_id, created_at, updated_at FROM transactions
            WHERE updated_at < created_at
        """),
        _check(session, "settled_transactions_reconciled_within_15_minutes", """
            SELECT t.id AS transaction_id, t.created_at
            FROM transactions t
            LEFT JOIN reconciliation_reports r ON r.transaction_id = t.id
            WHERE t.status = 'SETTLED' AND t.transaction_type <> 'GENESIS'
              AND t.created_at < now() - interval '15 minutes' AND r.transaction_id IS NULL
        """),
        _check(session, "unknown_resolution_happens_at_most_once_per_transaction", """
            SELECT transaction_id, count(*) AS resolution_count FROM audit_log
            WHERE action IN ('UNKNOWN_RESOLVED_SETTLED', 'UNKNOWN_RESOLVED_FAILED',
                              'RECONCILIATION_RESOLVED_SETTLED', 'RECONCILIATION_RESOLVED_FAILED')
            GROUP BY transaction_id HAVING count(*) > 1
        """),
        _check(session, "webhook_events_are_never_reused_across_transactions", """
            SELECT event_id, count(DISTINCT payload->>'transaction_id') AS distinct_transactions
            FROM webhook_events GROUP BY event_id HAVING count(DISTINCT payload->>'transaction_id') > 1
        """),
    ]
    overall = "PASS" if all(c["status"] == "PASS" for c in checks) else "FAIL"
    total_violations = sum(c["violation_count"] for c in checks)
    return {"overall_status": overall, "total_violations": total_violations, "checks": checks}
