"""Unit tests for common/state_machine.py.

These require no infrastructure and run on every commit — this is the
module every transaction status change in the platform goes through, so
it's the highest-leverage thing to have airtight test coverage on.
"""
import pytest
from common.state_machine import validate_transition, is_terminal, IllegalTransitionError, ALLOWED_TRANSITIONS


def test_happy_path_payment():
    validate_transition("CREATED", "RISK_CHECK")
    validate_transition("RISK_CHECK", "AUTHORIZED")
    validate_transition("AUTHORIZED", "PROCESSING")
    validate_transition("PROCESSING", "SETTLED")


def test_happy_path_refund():
    validate_transition("SETTLED", "REFUND_REQUESTED")
    validate_transition("REFUND_REQUESTED", "REFUNDED")


def test_happy_path_unknown_resolution():
    validate_transition("PROCESSING", "UNKNOWN")
    validate_transition("UNKNOWN", "RECONCILIATION")
    validate_transition("RECONCILIATION", "SETTLED")


def test_manual_review_escalation_path():
    validate_transition("PROCESSING", "UNKNOWN")
    validate_transition("UNKNOWN", "RECONCILIATION")
    validate_transition("RECONCILIATION", "MANUAL_REVIEW")
    validate_transition("MANUAL_REVIEW", "SETTLED")


def test_manual_review_can_also_resolve_to_failed():
    validate_transition("RECONCILIATION", "MANUAL_REVIEW")
    validate_transition("MANUAL_REVIEW", "FAILED")


def test_manual_review_is_not_terminal_until_resolved():
    assert not is_terminal("MANUAL_REVIEW")


def test_cannot_skip_risk_check():
    with pytest.raises(IllegalTransitionError):
        validate_transition("CREATED", "AUTHORIZED")


def test_cannot_settle_from_created():
    with pytest.raises(IllegalTransitionError):
        validate_transition("CREATED", "SETTLED")


def test_cannot_leave_terminal_states():
    for terminal in ("BLOCKED", "FAILED", "REFUNDED"):
        with pytest.raises(IllegalTransitionError):
            validate_transition(terminal, "PROCESSING")


def test_cannot_refund_twice():
    validate_transition("SETTLED", "REFUND_REQUESTED")
    with pytest.raises(IllegalTransitionError):
        validate_transition("REFUNDED", "REFUND_REQUESTED")


def test_is_terminal():
    assert is_terminal("BLOCKED")
    assert is_terminal("FAILED")
    assert is_terminal("REFUNDED")
    assert not is_terminal("CREATED")
    assert not is_terminal("PROCESSING")


def test_every_non_terminal_state_has_an_exit():
    """Guards against a future edit accidentally stranding a state with no
    way out other than the ones we've deliberately marked terminal.
    """
    for state, targets in ALLOWED_TRANSITIONS.items():
        if not is_terminal(state):
            assert len(targets) > 0, f"{state} is not terminal but has no outgoing transitions"
