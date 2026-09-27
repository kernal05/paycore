# Service Level Objectives

| Service | SLI | SLO | Error budget (30d) |
|---|---|---|---|
| payment-api | Availability (non-5xx / total) | 99.9% | 43m 12s |
| payment-api | Latency (p99, /payments) | < 500ms | — |
| ledger-service | Availability | 99.95% | 21m 36s |
| ledger-service | Journal integrity | 0 imbalanced transactions, ever | 0 |
| fraud-engine | Latency (p99, /score) | < 150ms | — |
| reconciliation-service | Detection lag | mismatches surfaced within 15 min (batch sweep interval) | — |

## Rationale

- **Ledger gets the tightest availability target and a zero-tolerance
  integrity SLO** because it's the one service where "eventually consistent"
  isn't an acceptable answer — money either balances or it doesn't.
- **payment-api's 99.9% is intentionally the loosest** number in the stack:
  it's the composed availability of everything behind it, so its budget has
  to cover fraud-engine and ledger-service dependencies too.
- **Fraud-engine's latency SLO is tight** because it sits synchronously in
  the payment path — every millisecond here is a millisecond added to
  checkout for a real customer.

## Multi-window burn-rate alerting

A single-window threshold (e.g. "5xx rate > 1% for 5m") either fires too
slowly on a fast, severe outage or too eagerly on brief blips. The
standard fix — used here for `payment-api` and `ledger-service` — is to
require a fast burn AND a slower-but-sustained burn to agree before
paging:

| Window pair | Burn rate threshold | Meaning | Severity |
|---|---|---|---|
| 5m AND 1h | > 14.4x budget | Would exhaust the entire 30-day budget in <2 days | page |
| 30m AND 6h | > 6x budget | Would exhaust the budget in <5 days | page |
| 2h AND 24h | > 1x budget | Slow, steady burn — budget exhausted by month-end at this rate | ticket |

See `PaymentAPIFastBurn`, `PaymentAPISlowBurn` in
`observability/alerting-rules.yml` for the actual PromQL. This is the same
multi-window burn-rate pattern Google's SRE book describes, sized to this
platform's 99.9%/99.95% targets.

## Error budget policy

- Burn > 50% of the monthly budget → freeze non-critical deploys to that
  service, focus on reliability work.
- Burn 100% → all feature work stops on that service until the SLO is
  recovered; postmortem required (see `RUNBOOK.md`).
- Alerts are wired to these exact thresholds in
  `observability/alerting-rules.yml` (`PaymentAPIHighErrorRate`,
  `PaymentAPIHighLatency`, `LedgerServiceDown`).

## How this is measured

`common/observability.py` exports `http_requests_total` and
`http_request_duration_seconds` from every service via `/metrics`.
Prometheus scrapes all four every 10s (`observability/prometheus.yml`).
Availability = `1 - (rate(5xx) / rate(total))` over the SLO window; the
exact PromQL is in the alerting rules file.
