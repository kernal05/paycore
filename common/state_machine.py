"""Payment State Machine
================
Financial systems can't assume every request resolves synchronously into
success/failure — a processor can time out, a webhook can arrive minutes
later, a refund is a whole second lifecycle on top of the original payment.
This module is the single source of truth for which transitions are legal;
every service that changes a transaction's status must go through
`transition()` rather than writing a status string directly.

    CREATED -> RISK_CHECK -> BLOCKED
                          -> AUTHORIZED -> PROCESSING -> SETTLED -> REFUND_REQUESTED -> REFUNDED
                                                       -> FAILED
                                                       -> UNKNOWN -> RECONCILIATION -> SETTLED
                                                                                    -> FAILED
                                                                                    -> MANUAL_REVIEW -> SETTLED
                                                                                                      -> FAILED

MANUAL_REVIEW is reached by the recovery engine (see recovery.py in
ledger-service) after exhausting its automated retry budget querying the
processor — a human resolves it from there via /transactions/{id}/manual-resolve,
never silently.
"""

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "CREATED": {"RISK_CHECK"},
    "RISK_CHECK": {"BLOCKED", "AUTHORIZED"},
    "BLOCKED": set(),  # terminal
    "AUTHORIZED": {"PROCESSING", "FAILED"},  # FAILED: human rejects a fraud-held payment
    "PROCESSING": {"SETTLED", "FAILED", "UNKNOWN"},
    "UNKNOWN": {"RECONCILIATION"},
    "RECONCILIATION": {"SETTLED", "FAILED", "MANUAL_REVIEW"},
    "MANUAL_REVIEW": {"SETTLED", "FAILED"},
    "FAILED": set(),  # terminal
    "SETTLED": {"REFUND_REQUESTED"},
    "REFUND_REQUESTED": {"REFUNDED", "FAILED"},
    "REFUNDED": set(),  # terminal
}


class IllegalTransitionError(Exception):
    def __init__(self, current: str, target: str):
        super().__init__(f"Illegal transition: {current} -> {target}")
        self.current = current
        self.target = target


def validate_transition(current: str, target: str) -> None:
    if target not in ALLOWED_TRANSITIONS.get(current, set()):
        raise IllegalTransitionError(current, target)


def is_terminal(status: str) -> bool:
    return len(ALLOWED_TRANSITIONS.get(status, set())) == 0
