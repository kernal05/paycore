-- Fintech Platform - Core Schema (v2)
-- Payment state machine + double-entry ledger + outbox/inbox + idempotency
-- + refunds + reconciliation exceptions + audit trail.
-- This file is the single source of truth for table shapes; every service
-- writes SQL against exactly what's defined here (grep services/*/main.py
-- for "INSERT INTO" / "FROM" if you change something and want to find
-- every caller).

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS accounts (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT NOT NULL,
    currency        CHAR(3) NOT NULL,
    balance_minor   BIGINT NOT NULL DEFAULT 0,
    version         BIGINT NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Client-facing idempotency layer (payment-api). Binds an idempotency_key
-- to a hash of the request so a replayed key with a *different* body is
-- rejected (409) rather than silently actioned. See common/idempotency.py
-- and docs/adr/ADR-008-idempotency.md.
CREATE TABLE IF NOT EXISTS payment_requests (
    idempotency_key TEXT PRIMARY KEY,
    request_hash    TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'PENDING',  -- PENDING, COMPLETE
    transaction_id  UUID,
    response_body   JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Money-safety idempotency layer (ledger-service). Even if payment-api's
-- layer above had a bug, this unique key makes a duplicate POST a no-op
-- rather than a double-post.
--
-- status is driven by common/state_machine.py:
--   CREATED -> RISK_CHECK -> {BLOCKED | AUTHORIZED} -> PROCESSING ->
--   {SETTLED | FAILED | UNKNOWN} ; SETTLED -> REFUND_REQUESTED -> REFUNDED
CREATE TABLE IF NOT EXISTS transactions (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    idempotency_key         TEXT UNIQUE NOT NULL,
    source_account_id       UUID REFERENCES accounts(id),
    dest_account_id         UUID REFERENCES accounts(id),
    amount_minor            BIGINT NOT NULL,
    currency                CHAR(3) NOT NULL,
    status                  TEXT NOT NULL DEFAULT 'CREATED',
    transaction_type        TEXT NOT NULL DEFAULT 'PAYMENT' CHECK (transaction_type IN ('PAYMENT','REFUND','REVERSAL','GENESIS')),
    original_transaction_id UUID REFERENCES transactions(id),
    fraud_score             NUMERIC(4,3),
    fraud_decision          TEXT,
    fraud_reasons           JSONB DEFAULT '[]',
    processor_name          TEXT,
    processor_ref           TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS journal_entries (
    id              BIGSERIAL PRIMARY KEY,
    transaction_id  UUID NOT NULL REFERENCES transactions(id),
    account_id      UUID NOT NULL REFERENCES accounts(id),
    direction       TEXT NOT NULL CHECK (direction IN ('DEBIT','CREDIT')),
    amount_minor    BIGINT NOT NULL CHECK (amount_minor > 0),
    currency        CHAR(3) NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (transaction_id, account_id, direction)
);

-- DB-ENFORCED LEDGER INTEGRITY: a deferred constraint trigger firing at
-- COMMIT that rejects any transaction whose journal legs don't sum to
-- exactly zero, or that has fewer than 2 legs. This is stronger than an
-- application-level check: even a future bug in service code cannot
-- commit an imbalanced posting.
CREATE OR REPLACE FUNCTION check_journal_balance() RETURNS TRIGGER AS $$
DECLARE
    imbalance BIGINT;
    leg_count INT;
BEGIN
    SELECT
        COALESCE(SUM(CASE WHEN direction = 'DEBIT' THEN amount_minor ELSE -amount_minor END), 0),
        COUNT(*)
    INTO imbalance, leg_count
    FROM journal_entries
    WHERE transaction_id = NEW.transaction_id;

    IF leg_count < 2 THEN
        RAISE EXCEPTION 'transaction % has fewer than 2 journal legs at commit', NEW.transaction_id;
    END IF;

    IF imbalance != 0 THEN
        RAISE EXCEPTION 'transaction % is imbalanced by % minor units', NEW.transaction_id, imbalance;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_check_journal_balance ON journal_entries;
CREATE CONSTRAINT TRIGGER trg_check_journal_balance
    AFTER INSERT ON journal_entries
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION check_journal_balance();

-- OUTBOX PATTERN (ADR-003): written in the SAME DB transaction as the
-- ledger posting it describes. outbox-publisher polls WHERE published_at
-- IS NULL and publishes to `topic`, so "posted but event never published"
-- (a dual-write) is impossible.
CREATE TABLE IF NOT EXISTS outbox_events (
    id              BIGSERIAL PRIMARY KEY,
    aggregate_id    TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    topic           TEXT NOT NULL,
    payload         JSONB NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_outbox_unpublished ON outbox_events (id) WHERE published_at IS NULL;

-- INBOX PATTERN: a consumer records (event_id, consumer_name) before
-- acting, so an at-least-once redelivered Kafka event is a no-op the
-- second time it's seen by the same consumer group.
CREATE TABLE IF NOT EXISTS inbox_events (
    event_id        TEXT NOT NULL,
    consumer_name   TEXT NOT NULL,
    processed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (event_id, consumer_name)
);

CREATE TABLE IF NOT EXISTS fraud_events (
    id              BIGSERIAL PRIMARY KEY,
    transaction_id  UUID NOT NULL REFERENCES transactions(id),
    score           NUMERIC(4,3) NOT NULL,
    decision        TEXT NOT NULL,
    reasons         JSONB NOT NULL DEFAULT '[]',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Simulated external systems (stand-ins for a real processor/bank
-- integration point — see common/processors.py for the abstraction).
CREATE TABLE IF NOT EXISTS processor_records (
    transaction_id  UUID PRIMARY KEY,
    status          TEXT NOT NULL,
    amount_minor    BIGINT NOT NULL,
    processor_ref   TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS bank_records (
    transaction_id  UUID PRIMARY KEY,
    status          TEXT NOT NULL,
    amount_minor    BIGINT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Inbound webhooks (simulated processor callbacks). Every webhook is
-- recorded here BEFORE being acted on, keyed by the processor's event_id,
-- so a replayed/duplicated webhook delivery is detected and ignored —
-- the same inbox idea applied to HTTP instead of Kafka.
CREATE TABLE IF NOT EXISTS webhook_events (
    event_id        TEXT PRIMARY KEY,
    processor       TEXT NOT NULL,
    payload         JSONB NOT NULL,
    signature_valid BOOLEAN NOT NULL,
    received_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at    TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS reconciliation_reports (
    id                  BIGSERIAL PRIMARY KEY,
    transaction_id      UUID NOT NULL,
    internal_status     TEXT,
    processor_status    TEXT,
    bank_status         TEXT,
    mismatch            BOOLEAN NOT NULL,
    category            TEXT,  -- MISSING_INTERNAL, MISSING_PROCESSOR, MISSING_BANK,
                                -- AMOUNT_MISMATCH, CURRENCY_MISMATCH, STATUS_MISMATCH,
                                -- DUPLICATE, SETTLEMENT_DELAY
    detail              TEXT,
    resolved            BOOLEAN NOT NULL DEFAULT false,
    resolved_at         TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Append-only audit trail: WHO / WHAT / WHEN / BEFORE / AFTER. Application
-- code only ever INSERTs here, never UPDATEs or DELETEs.
CREATE TABLE IF NOT EXISTS audit_log (
    id              BIGSERIAL PRIMARY KEY,
    actor           TEXT NOT NULL,       -- e.g. "service/payment-api"
    action          TEXT NOT NULL,       -- e.g. "PAYMENT_CREATED", "REFUND_ISSUED"
    transaction_id  TEXT NOT NULL,
    before_state    JSONB,
    after_state     JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- PAYMENT RECOVERY ENGINE: every automated attempt to resolve a parked
-- UNKNOWN transaction by querying the processor, with a full audit trail
-- (what was tried, when, and what came back) rather than a silent retry
-- loop with no record. Exponential backoff is computed from attempt_number
-- at query time, not stored — see recovery.py in ledger-service.
CREATE TABLE IF NOT EXISTS recovery_attempts (
    id              BIGSERIAL PRIMARY KEY,
    transaction_id  UUID NOT NULL REFERENCES transactions(id),
    attempt_number  INT NOT NULL,
    method          TEXT NOT NULL DEFAULT 'processor_status_query',
    outcome         TEXT NOT NULL,  -- PENDING, SETTLED, FAILED, ESCALATED_TO_MANUAL_REVIEW
    detail          TEXT,
    attempted_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_recovery_transaction ON recovery_attempts(transaction_id);

CREATE INDEX IF NOT EXISTS idx_txn_status ON transactions(status);
CREATE INDEX IF NOT EXISTS idx_txn_idempotency ON transactions(idempotency_key);
CREATE INDEX IF NOT EXISTS idx_journal_account ON journal_entries(account_id);
CREATE INDEX IF NOT EXISTS idx_recon_mismatch ON reconciliation_reports(mismatch) WHERE mismatch;
CREATE INDEX IF NOT EXISTS idx_recon_unresolved ON reconciliation_reports(resolved) WHERE mismatch AND NOT resolved;
CREATE INDEX IF NOT EXISTS idx_audit_transaction ON audit_log(transaction_id);

-- Seed demo accounts. The float account exists so seeded balances are
-- actually explained by journal history (see /ledger/consistency-check) —
-- a balance that only ever existed because someone ran an UPDATE
-- statement, with no journal entry behind it, is exactly the kind of gap
-- that check exists to catch, so the seed data has to satisfy it too.
INSERT INTO accounts (id, name, currency, balance_minor)
VALUES
    ('00000000-0000-0000-0000-000000000000', 'Opening Balance Float Account', 'INR', -10000000),
    ('11111111-1111-1111-1111-111111111111', 'Customer Wallet - Vishal', 'INR', 10000000),
    ('22222222-2222-2222-2222-222222222222', 'Merchant Settlement Account', 'INR', 0)
ON CONFLICT (id) DO NOTHING;

-- Wrapped in one transaction: the deferred journal-balance trigger fires at
-- COMMIT, so both legs must land together or psql's default autocommit
-- (one transaction per statement) would trip "fewer than 2 legs" on the
-- first INSERT alone.
BEGIN;

INSERT INTO transactions (id, idempotency_key, source_account_id, dest_account_id, amount_minor, currency, status, transaction_type)
VALUES (
    '99999999-9999-9999-9999-999999999999', 'genesis-seed-customer-wallet',
    '00000000-0000-0000-0000-000000000000', '11111111-1111-1111-1111-111111111111',
    10000000, 'INR', 'SETTLED', 'GENESIS'
)
ON CONFLICT (idempotency_key) DO NOTHING;

INSERT INTO journal_entries (transaction_id, account_id, direction, amount_minor, currency)
SELECT '99999999-9999-9999-9999-999999999999', '00000000-0000-0000-0000-000000000000', 'DEBIT', 10000000, 'INR'
WHERE NOT EXISTS (
    SELECT 1 FROM journal_entries WHERE transaction_id = '99999999-9999-9999-9999-999999999999' AND direction = 'DEBIT'
);

INSERT INTO journal_entries (transaction_id, account_id, direction, amount_minor, currency)
SELECT '99999999-9999-9999-9999-999999999999', '11111111-1111-1111-1111-111111111111', 'CREDIT', 10000000, 'INR'
WHERE NOT EXISTS (
    SELECT 1 FROM journal_entries WHERE transaction_id = '99999999-9999-9999-9999-999999999999' AND direction = 'CREDIT'
);

COMMIT;
