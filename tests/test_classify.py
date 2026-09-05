"""Deterministic classifier: every seed event lands on its ground-truth class, the rule
order holds (risk beats everything), and anything ambiguous is UNKNOWN, never a guess."""
from types import SimpleNamespace

import pytest

from app.classify import NO_RULE_MATCHED, RULES, classify, classify_error, is_mapped, rules_table
from app.taxonomy import Classification, FailureClass
from scripts.seed import SEED_EVENTS, to_attempt


def attempt(code="BAD_REQUEST_ERROR", source="bank", step="payment_authorization", reason="payment_failed", desc=""):
    return SimpleNamespace(error_code=code, error_source=source, error_step=step, error_reason=reason,
                           error_description=desc)


@pytest.mark.parametrize("event", SEED_EVENTS, ids=[e["razorpay_payment_id"] for e in SEED_EVENTS])
def test_every_seed_event_classifies_to_its_ground_truth(event):
    c = classify(to_attempt(event))
    assert c.failure_class is event["expected_class"]
    assert c.source == "rules" and c.confidence == 1.0
    if c.failure_class is FailureClass.UNKNOWN:
        assert c.reason == NO_RULE_MATCHED
    else:
        assert c.reason.startswith("rule ")


def test_seed_covers_every_class_at_least_twice():
    counts = {cls: sum(1 for e in SEED_EVENTS if e["expected_class"] is cls) for cls in FailureClass}
    assert all(n >= 2 for n in counts.values()), counts


def test_risk_flag_beats_every_other_signal():
    loaded = attempt(code="GATEWAY_ERROR", source="network", step="payment_authentication",
                     reason="payment_risk_check_failed",
                     desc="insufficient funds; card expired; OTP cancelled; bank down; gateway timed out")
    assert classify(loaded).failure_class is FailureClass.RISK_BLOCKED
    by_desc = attempt(reason="card_declined", desc="Card blocked: suspected fraud on the account")
    assert classify(by_desc).failure_class is FailureClass.RISK_BLOCKED
    assert RULES[0].failure_class is FailureClass.RISK_BLOCKED


def test_generic_decline_is_unknown_not_hard_decline():
    c = classify(attempt(reason="card_declined", desc="Payment was declined by the issuing bank."))
    assert c.failure_class is FailureClass.UNKNOWN
    assert c.reason == NO_RULE_MATCHED
    assert classify(attempt(desc="Transaction declined by the issuer. Response code 05: Do not honour.")).failure_class \
        is FailureClass.UNKNOWN


def test_mixed_language_and_empty_error_objects_are_unknown():
    assert classify(attempt(desc="Aapka transaction bank dwara reject kar diya gaya hai.")).failure_class is FailureClass.UNKNOWN
    assert classify(attempt(code=None, source=None, step=None, reason=None, desc=None)).failure_class is FailureClass.UNKNOWN
    assert classify(SimpleNamespace()).failure_class is FailureClass.UNKNOWN


def test_hard_decline_needs_a_hard_keyword_and_loses_to_insufficient_funds():
    assert classify(attempt(reason="card_declined", desc="Card declined: insufficient balance")).failure_class \
        is FailureClass.INSUFFICIENT_FUNDS
    for word in ("blocked", "expired", "invalid", "not enabled", "restricted", "lost", "stolen", "international"):
        assert classify(attempt(reason="card_declined", desc=f"Card declined: card {word}")).failure_class \
            is FailureClass.HARD_DECLINE, word
    assert classify(attempt(reason="payment_failed", desc="Your card has expired.")).failure_class is FailureClass.HARD_DECLINE


def test_invalid_otp_at_authentication_is_abandonment_not_a_dead_card():
    c = classify(attempt(step="payment_authentication", reason="payment_failed", desc="Invalid OTP entered for the card."))
    assert c.failure_class is FailureClass.AUTH_ABANDONED


def test_timeout_class_depends_on_step():
    at_auth = attempt(code="BAD_REQUEST_ERROR", source="customer", step="payment_authentication", reason="payment_timed_out",
                      desc="Payment timed out")
    at_gateway = attempt(code="GATEWAY_ERROR", source="gateway", step="payment_authorization", reason="payment_timed_out",
                         desc="Payment timed out")
    assert classify(at_auth).failure_class is FailureClass.AUTH_ABANDONED
    assert classify(at_gateway).failure_class is FailureClass.NETWORK_TIMEOUT
    assert classify(attempt(source="network", desc="")).failure_class is FailureClass.NETWORK_TIMEOUT


def test_issuer_down_keys_on_reason_or_bank_plus_outage_word():
    assert classify(attempt(source="gateway", reason="bank_technical_error", desc="")).failure_class is FailureClass.ISSUER_DOWN
    assert classify(attempt(source="bank", desc="Server unavailable")).failure_class is FailureClass.ISSUER_DOWN
    assert classify(attempt(source="customer", desc="Issuer is not responding")).failure_class is FailureClass.ISSUER_DOWN
    assert classify(attempt(source="bank", desc="Declined by bank")).failure_class is FailureClass.UNKNOWN


def test_keywords_match_on_word_boundaries():
    assert classify(attempt(step="payment_authentication", desc="digital footprint mismatch")).failure_class is FailureClass.UNKNOWN
    assert classify(attempt(desc="risky transaction pattern")).failure_class is FailureClass.RISK_BLOCKED


def test_classify_never_raises():
    weird = SimpleNamespace(error_code=42, error_source=b"bank", error_step=["x"], error_reason=3.5, error_description=object())
    assert isinstance(classify(weird), Classification)
    assert isinstance(classify(None), Classification)
    assert classify_error(None, None, None, None, None).failure_class is FailureClass.UNKNOWN


def test_is_mapped():
    assert is_mapped(attempt(reason="bank_technical_error"))
    assert not is_mapped(attempt(desc="Payment was declined by the issuing bank."))


def test_rules_table_lists_rules_in_evaluation_order():
    table = rules_table()
    assert [r["rule"] for r in table] == [r.name for r in RULES]
    assert [r["order"] for r in table] == list(range(1, len(RULES) + 1))
    assert all(set(r) == {"order", "rule", "failure_class", "matches"} for r in table)
    classes_in_order = []
    for r in RULES:
        if r.failure_class not in classes_in_order:
            classes_in_order.append(r.failure_class)
    assert classes_in_order == [FailureClass.RISK_BLOCKED, FailureClass.HARD_DECLINE, FailureClass.INSUFFICIENT_FUNDS,
                                FailureClass.LIMIT_EXCEEDED, FailureClass.AUTH_ABANDONED, FailureClass.ISSUER_DOWN,
                                FailureClass.NETWORK_TIMEOUT]
    assert FailureClass.UNKNOWN not in classes_in_order  # UNKNOWN is the fall-through, never a rule
