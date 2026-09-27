# ADR-014: Bugs that only running the stack surfaced

**Status:** Accepted (record of findings, not a design decision to revisit)

## Context

Every prior ADR and the test suite were written and passing before this
project was ever actually run end-to-end against a real Postgres and
Redis. Static analysis (ruff), `py_compile`, and 23 unit tests all passed
throughout. None of them caught the three issues below — which is exactly
why "the tests pass" and "the system works" are different claims, and why
this ADR exists: to record what closing that gap actually found.

## Finding 1: SQLAlchemy 2.0 "autobegin" vs. explicit `session.begin()`

**Symptom:** every endpoint that did a read (e.g. an idempotency check)
before an explicit `with session.begin():` write block failed with
`InvalidRequestError: A transaction is already begun on this Session`.

**Root cause:** SQLAlchemy 2.0 sessions autobegin a transaction on first
use — including a plain `SELECT`. Calling `session.begin()` explicitly
afterward assumes no transaction is open yet, which was false in every
handler that checked-then-wrote on the same session.

**Fix:** `session.rollback()` immediately before every explicit
`with session.begin():` — safe even when no prior read happened (a no-op),
and correctly discards the autobegun read-only transaction otherwise.
Applied mechanically across all 14 occurrences in ledger-service,
payment-api, and reconciliation-service.

**Why the unit tests didn't catch it:** none of them touch a database —
by design, they test pure logic (state machine, hashing, circuit breaker,
mock processor, webhook signatures) precisely so they run in <1s with no
infra. That's the right tradeoff, but it means this entire class of bug
is only reachable by an integration test against a real database.

## Finding 2: payment-api forgot the service-auth header on one of two internal calls

**Symptom:** every payment failed fraud scoring with `401 Unauthorized`
from fraud-engine, tripping the circuit breaker after 5 failures and
turning into cascading `503`s.

**Root cause:** `_call_ledger()` correctly attached the HMAC bearer token
(`_service_headers("payment-api")`) to every ledger call. `_call_fraud()`
was written earlier, before service auth was added platform-wide, and was
never updated to attach the same header — a copy-paste-era gap, not a
logic error.

**Fix:** one line — attach `headers=_service_headers("payment-api")` to
the fraud-engine call too. Also hardened the general case: added an
explicit `httpx.HTTPStatusError` handler in `create_payment` so a future
instance of "a downstream call failed in a way the circuit breaker hasn't
tripped on yet" surfaces as a clean `502` with the downstream body, not an
unstructured bare `500`.

**Why nothing caught it sooner:** ADR-010 documents the auth scheme in
prose and the unit tests verify the HMAC signing/verification logic in
isolation — but nothing exercised two services actually calling each
other over HTTP until the integration suite ran for real.

## Finding 3: seeded account balances weren't journal-explained

**Symptom:** `/ledger/consistency-check` reported the seed customer wallet
as inconsistent — `balance_minor: 10000000` but `computed_balance: 0` —
immediately, before any transaction had ever touched it.

**Root cause:** `scripts/init_db.sql` originally set the opening balance
via a plain `INSERT ... balance_minor = 10000000`, with no corresponding
journal entry. ADR-005's own invariant — "a balance is always the sum of
journal history" — was violated by the seed data itself.

**Fix:** added an `Opening Balance Float Account` (balance `-10000000`)
and a `GENESIS`-type transaction with matching DEBIT/CREDIT journal legs
that actually produce the seeded balances, wrapped in one explicit
transaction so the deferred journal-balance trigger (which fires at
commit) validates both legs together. Every account's balance, including
the seed data, is now fully explained by journal history with no
exception carved out.

**Why the unit tests didn't catch it:** correctly — this was never a unit
of logic to test in isolation; it's a data-integrity property of the
seed data itself, only checkable by querying a real database. It's also a
polite reminder that "the code is correct" and "the fixture data satisfies
the code's own invariants" are two different things to verify.

## Consequences

- These three fixes were found and fixed in one working session by
  actually running `docker compose`-equivalent infrastructure (Postgres +
  Redis, direct process execution in this environment — see
  `docs/PERFORMANCE.md` for why Docker itself wasn't available) and
  executing the integration suite against it, not by more code review.
- All 11 integration tests (including the 20-concurrent-transfer test)
  and all 23 unit tests pass after these fixes — see
  `tests/integration/` output captured at the time in the project's
  execution log.
- The broader lesson, stated plainly rather than left implicit: a test
  suite that never touches real infrastructure will always have exactly
  this kind of blind spot. Both test tiers (`tests/unit`, `tests/integration`)
  exist for a reason, and only one of them can find bugs like these three.
