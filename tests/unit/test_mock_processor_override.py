"""The mock processor's outcome is a pure function of the transaction id, plus an env override for demos."""
from common.processors import MockProcessor

# Ids chosen by running MockProcessor._outcome_for with no override.
DECLINED_ID = "3be3c371-d10c-4123-accf-5203a523b630"
SUCCESS_ID = "45962dfd-2321-4731-a0fa-f35ed25d1cf9"


def test_outcome_is_deterministic_per_transaction_id(monkeypatch):
    monkeypatch.delenv("MOCK_OUTCOME_OVERRIDE", raising=False)
    m = MockProcessor()
    assert m._outcome_for(DECLINED_ID) == "DECLINED"
    assert m._outcome_for(DECLINED_ID) == "DECLINED"
    assert m._outcome_for(SUCCESS_ID) == "SUCCESS"


def test_declined_authorization_carries_a_reason(monkeypatch):
    monkeypatch.delenv("MOCK_OUTCOME_OVERRIDE", raising=False)
    result = MockProcessor().authorize(DECLINED_ID, 15000, "INR")
    assert result.status == "DECLINED"
    assert result.detail


def test_override_forces_every_outcome(monkeypatch):
    monkeypatch.setenv("MOCK_OUTCOME_OVERRIDE", "SUCCESS")
    assert MockProcessor().authorize(DECLINED_ID, 15000, "INR").status == "SUCCESS"
    monkeypatch.setenv("MOCK_OUTCOME_OVERRIDE", "DECLINED")
    assert MockProcessor().authorize(SUCCESS_ID, 15000, "INR").status == "DECLINED"


def test_empty_override_is_ignored(monkeypatch):
    monkeypatch.setenv("MOCK_OUTCOME_OVERRIDE", "")
    assert MockProcessor()._outcome_for(DECLINED_ID) == "DECLINED"


def test_outcome_weights_sum_to_100():
    assert sum(w for _, w in MockProcessor.OUTCOME_WEIGHTS) == 100
