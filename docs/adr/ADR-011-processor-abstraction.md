# ADR-011: PaymentProcessor interface + MockProcessor

**Status:** Accepted

## Context
Integrating a real processor (Stripe, a card network, a bank rail) needs
a merchant account and real money — out of scope for a portfolio project
— but the rest of the platform (state machine, webhook handling,
reconciliation) needs something realistic to be built and tested against.

## Decision
Define `PaymentProcessor` (`authorize`, `capture`, `refund`, `get_status`)
as an abstract interface. `MockProcessor` implements it with a
deterministic-but-varied outcome distribution (85% SUCCESS, 8% DECLINED,
3% TIMEOUT, 2% SLOW_RESPONSE, 1% UNKNOWN, 1% DUPLICATE) derived from a
hash of the transaction ID, so outcomes are reproducible for tests and
demos. `ProcessorRouter` selects a processor by name, ready for a second
real implementation to be added without touching call sites.

## Consequences
- The platform genuinely has to handle TIMEOUT/UNKNOWN as first-class
  outcomes (see the UNKNOWN state in `common/state_machine.py` and
  `resolve-unknown` in ledger-service), not just SUCCESS/FAILURE — this is
  the realistic behavior a production integration would also need to
  handle.
- Swapping in Stripe later means implementing `PaymentProcessor` once and
  registering it in `ProcessorRouter` — no other service changes.
