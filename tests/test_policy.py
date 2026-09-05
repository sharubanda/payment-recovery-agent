"""The policy table is the one in README.md / ARCHITECTURE.md; these tests pin it, the stop rules,
the backoff arithmetic, the confidence gate that sends low-confidence LLM calls to a person and the
reachability gate that refuses to create a link for a customer nobody can send it to."""
import pytest

from app import config
from app.policy import POLICY, decide, delay_for, human_delay, policy_table
from app.taxonomy import Action, Classification, FailureClass, PolicyDecision

H, M = 3600, 60

# class -> (action without token, action with token, delay_seconds, max_attempts, backoff, nudge without token)
SPEC = {
    FailureClass.INSUFFICIENT_FUNDS: (Action.RECOVERY_LINK, Action.RECOVERY_LINK, 48 * H, 2, 1.0, True),
    FailureClass.ISSUER_DOWN: (Action.RECOVERY_LINK, Action.TOKEN_RETRY, 15 * M, 3, 3.0, True),
    FailureClass.AUTH_ABANDONED: (Action.RECOVERY_LINK, Action.RECOVERY_LINK, 10 * M, 2, 1.0, True),
    FailureClass.HARD_DECLINE: (Action.NUDGE_CHANGE_METHOD, Action.NUDGE_CHANGE_METHOD, 0, 1, 1.0, True),
    FailureClass.RISK_BLOCKED: (Action.HUMAN_QUEUE, Action.HUMAN_QUEUE, 0, 0, 1.0, False),
    FailureClass.NETWORK_TIMEOUT: (Action.RECOVERY_LINK, Action.TOKEN_RETRY, 5 * M, 2, 2.0, True),
    FailureClass.LIMIT_EXCEEDED: (Action.RECOVERY_LINK, Action.RECOVERY_LINK, 24 * H, 2, 1.0, True),
    FailureClass.UNKNOWN: (Action.HUMAN_QUEUE, Action.HUMAN_QUEUE, 0, 0, 1.0, False),
}


def test_policy_covers_every_class_exactly_once():
    assert set(POLICY) == set(FailureClass) == set(SPEC)


@pytest.mark.parametrize("cls", list(SPEC), ids=[c.value for c in SPEC])
def test_table_matches_spec(cls):
    action, token_action, delay, max_attempts, backoff, nudge = SPEC[cls]
    no_token = decide(cls, has_token=False)
    with_token = decide(cls, has_token=True)
    assert isinstance(no_token, PolicyDecision)
    assert (no_token.action, no_token.delay_seconds, no_token.max_attempts, no_token.nudge) == (action, delay, max_attempts, nudge)
    assert with_token.action is token_action
    assert with_token.delay_seconds == delay
    if action not in (Action.HUMAN_QUEUE, Action.NO_ACTION):
        assert no_token.backoff_multiplier == backoff


def test_token_retry_is_silent_and_link_is_nudged():
    for cls in (FailureClass.ISSUER_DOWN, FailureClass.NETWORK_TIMEOUT):
        assert decide(cls, has_token=True).nudge is False
        assert decide(cls, has_token=False).nudge is True
    # No token retry for insufficient funds even with a token: the spec says link + nudge on the salary cycle.
    assert decide(FailureClass.INSUFFICIENT_FUNDS, has_token=True).action is Action.RECOVERY_LINK
    # Nor for a hit limit: the same cap answers a token charge until the window resets, so link + nudge at 24h.
    assert decide(FailureClass.LIMIT_EXCEEDED, has_token=True).action is Action.RECOVERY_LINK
    assert decide(FailureClass.LIMIT_EXCEEDED, has_token=True).delay_seconds == 24 * H


def test_stop_rule_max_attempts():
    d = decide(FailureClass.ISSUER_DOWN, has_token=False, retry_seq=4)
    assert d.action is Action.NO_ACTION and d.nudge is False and d.delay_seconds == 0
    assert d.rationale.startswith("stop rule: max attempts reached")
    assert decide(FailureClass.ISSUER_DOWN, has_token=False, retry_seq=3).action is Action.RECOVERY_LINK
    assert decide(FailureClass.HARD_DECLINE, has_token=False, retry_seq=2).action is Action.NO_ACTION
    assert decide(FailureClass.INSUFFICIENT_FUNDS, has_token=False, retry_seq=3).action is Action.NO_ACTION


def test_human_queue_is_not_an_attempt_so_it_ignores_retry_seq():
    for cls in (FailureClass.RISK_BLOCKED, FailureClass.UNKNOWN):
        for seq in (1, 2, 99):
            d = decide(cls, has_token=True, retry_seq=seq)
            assert d.action is Action.HUMAN_QUEUE and d.max_attempts == 0 and d.nudge is False


def test_delay_grows_by_backoff_per_attempt():
    assert [delay_for(FailureClass.ISSUER_DOWN, n) for n in (1, 2, 3)] == [900, 2700, 8100]
    assert [delay_for(FailureClass.NETWORK_TIMEOUT, n) for n in (1, 2)] == [300, 600]
    assert [delay_for(FailureClass.INSUFFICIENT_FUNDS, n) for n in (1, 2)] == [48 * H, 48 * H]
    assert delay_for(FailureClass.HARD_DECLINE, 1) == 0
    assert decide(FailureClass.ISSUER_DOWN, has_token=True, retry_seq=2).delay_seconds == 2700
    assert delay_for(FailureClass.ISSUER_DOWN, 0) == delay_for(FailureClass.ISSUER_DOWN, 1)


def llm(cls, confidence, source="llm"):
    return Classification(failure_class=cls, reason="model said so", source=source, confidence=confidence,
                          llm_model="claude-test", llm_latency_ms=12)


def test_low_confidence_llm_classification_goes_to_a_human():
    d = decide(FailureClass.INSUFFICIENT_FUNDS, has_token=False, classification=llm(FailureClass.INSUFFICIENT_FUNDS, 0.55))
    assert d.action is Action.HUMAN_QUEUE and d.nudge is False and d.max_attempts == 0
    assert str(config.LLM_MIN_CONFIDENCE) in d.rationale and "0.55" in d.rationale


def test_confident_llm_classification_follows_the_table():
    at_threshold = decide(FailureClass.INSUFFICIENT_FUNDS, has_token=False,
                          classification=llm(FailureClass.INSUFFICIENT_FUNDS, config.LLM_MIN_CONFIDENCE))
    assert at_threshold.action is Action.RECOVERY_LINK and at_threshold.delay_seconds == 48 * H
    assert decide(FailureClass.ISSUER_DOWN, has_token=True, classification=llm(FailureClass.ISSUER_DOWN, 0.93)).action \
        is Action.TOKEN_RETRY


def test_fallback_classification_never_reaches_a_retry():
    fell_back = Classification(failure_class=FailureClass.ISSUER_DOWN, reason="llm timed out", source="fallback",
                               confidence=0.0, fallback_taken="llm_timeout->human_queue")
    assert decide(FailureClass.ISSUER_DOWN, has_token=True, classification=fell_back).action is Action.HUMAN_QUEUE


def test_rules_classification_is_not_confidence_gated():
    rules = Classification(failure_class=FailureClass.ISSUER_DOWN, reason="rule x", source="rules", confidence=1.0)
    assert decide(FailureClass.ISSUER_DOWN, has_token=False, classification=rules).action is Action.RECOVERY_LINK


def test_decide_never_raises_and_degrades_to_a_human():
    assert decide("garbage", has_token=False).action is Action.HUMAN_QUEUE
    assert decide(None, has_token=False).action is Action.HUMAN_QUEUE
    assert decide("ISSUER_DOWN", has_token=True).action is Action.TOKEN_RETRY  # string values coerce
    assert decide(FailureClass.ISSUER_DOWN, has_token=True, retry_seq=-3).delay_seconds == 900
    assert decide(FailureClass.ISSUER_DOWN, has_token=True, retry_seq="two").action is Action.TOKEN_RETRY


def test_every_rationale_is_one_reviewer_readable_sentence():
    rationales = [decide(cls, has_token=t).rationale for cls in FailureClass for t in (True, False)]
    rationales += [decide(FailureClass.ISSUER_DOWN, has_token=False, retry_seq=9).rationale,
                   decide(FailureClass.UNKNOWN, has_token=False, classification=llm(FailureClass.UNKNOWN, 0.1)).rationale]
    for r in rationales:
        assert r.endswith(".") and r.count(". ") == 0 and len(r) > 40, r


def test_policy_table_for_docs():
    table = policy_table()
    assert [r["failure_class"] for r in table] == [c.value for c in FailureClass]
    by_class = {r["failure_class"]: r for r in table}
    assert by_class["ISSUER_DOWN"]["delay"] == "15m" and by_class["ISSUER_DOWN"]["action_with_token"] == "token_retry"
    assert by_class["INSUFFICIENT_FUNDS"]["delay"] == "48h"
    assert by_class["HARD_DECLINE"]["delay"] == "now"
    assert human_delay(90) == "90s"


def test_unreachable_customer_goes_to_a_person_for_link_actions_only():
    for cls in (FailureClass.INSUFFICIENT_FUNDS, FailureClass.AUTH_ABANDONED, FailureClass.HARD_DECLINE,
                FailureClass.ISSUER_DOWN, FailureClass.NETWORK_TIMEOUT, FailureClass.LIMIT_EXCEEDED):
        d = decide(cls, has_token=False, reachable=False)
        assert d.action is Action.HUMAN_QUEUE and d.nudge is False and "nobody" in d.rationale, cls
        assert decide(cls, has_token=False, reachable=True).action is not Action.HUMAN_QUEUE
    # a token retry needs no channel: the saved instrument is charged silently
    assert decide(FailureClass.ISSUER_DOWN, has_token=True, reachable=False).action is Action.TOKEN_RETRY
    # the stop rule still comes first: seq 4 is no_action whether or not the customer is reachable
    assert decide(FailureClass.ISSUER_DOWN, has_token=False, retry_seq=4, reachable=False).action is Action.NO_ACTION


def test_decide_accepts_overrides_keyword_and_none_is_the_table():
    """The override machinery lives in tests/test_merchants.py; here only the signature is pinned."""
    for cls in FailureClass:
        assert decide(cls, has_token=False, overrides=None) == decide(cls, has_token=False)
    assert "override" in policy_table()[0] and policy_table(overrides=None) == policy_table()
