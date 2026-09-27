# ADR-004: Synchronous fraud scoring in the payment path

**Status:** Accepted

## Context
Fraud scoring could happen synchronously (block the payment response
until scored) or asynchronously (approve provisionally, score in the
background, reverse if fraudulent).

## Decision
Score synchronously, before authorization. A rule-based engine is fast
enough (p99 target <150ms, see `docs/SLO.md`) that the latency cost is
acceptable, and it avoids ever having to claw back money that already
moved.

## Consequences
- Fraud-engine latency directly adds to payment-api latency — this is why
  it has the tightest latency SLO in the platform and sits behind a
  circuit breaker.
- If a real ML-based scorer were added with higher latency, this decision
  would need revisiting (possibly a fast synchronous pre-check + async
  deeper scoring for review-decision transactions only).
