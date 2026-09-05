"""Rule candidates: signature normalisation, the candidate thresholds (occurrences, agreement,
confidence), fallback-only groups, the paste-ready stub, and that classify.py is never touched."""
import hashlib
from datetime import datetime
from pathlib import Path

import pytest

from app import rule_candidates as rc
from app.models import PaymentAttempt, RecoveryDecision
from app.taxonomy import Action, FailureClass

NOW = datetime(2026, 9, 5, 12, 0, 0)
CLASSIFY = Path(__file__).resolve().parent.parent / "app" / "classify.py"


def add_decision(session, i: int, description: str, *, classified_by: str, failure_class: str = "UNKNOWN",
                 confidence: float | None = None, code="BAD_REQUEST_ERROR", source="bank",
                 step="payment_authorization", reason="payment_failed") -> RecoveryDecision:
    pid = f"pay_RC{i:012d}"
    a = PaymentAttempt(merchant_id="merchant_rc", order_id=f"order_rc_{i}", razorpay_payment_id=pid, amount_paise=100 * (i + 1),
                       method="card", error_code=code, error_source=source, error_step=step, error_reason=reason,
                       error_description=description, customer_name=f"Person {i}", customer_contact="+915800000001",
                       customer_email=f"p{i}@example.com", failed_at=NOW)
    session.add(a)
    session.flush()
    d = RecoveryDecision(attempt_id=a.id, failure_class=failure_class, action=Action.HUMAN_QUEUE.value, delay_seconds=0,
                         max_attempts=0, reason="test", classified_by=classified_by, llm_used=classified_by != "rules",
                         confidence=confidence, fallback_taken="llm_unavailable->human_queue" if classified_by == "fallback" else None)
    session.add(d)
    session.commit()
    return d


# ---- normalisation -------------------------------------------------------------------------------

@pytest.mark.parametrize("raw,sig", [
    ("Transaction declined by the issuer. Response code 05: Do not honour.",
     "transaction declined by the issuer. response code #: do not honour."),
    ("  Ref: SBIN-91   failed\n twice ", "ref: sbin-# failed twice"),
    ("Payment pay_LrfTLGu5EgYUPo for order_ct86nidKocRa56 failed", "payment for failed"),
    ("Link plink_TBJfbhCOZ3wnwH expired at 12:30", "link expired at #:#"),
    ("", ""), (None, ""),
    ("Code 1234567 vs code 89", "code # vs code #"),
])
def test_signature_normalisation(raw, sig):
    assert rc.signature(raw) == sig


def test_group_key_is_case_and_space_insensitive_on_structured_fields():
    k1 = rc.group_key("Declined 05", "BAD_REQUEST_ERROR", "Bank", "payment_authorization", "card_declined")
    k2 = rc.group_key("declined   99", "bad_request_error", "bank ", "payment_authorization", "CARD_DECLINED")
    assert k1 == k2
    assert rc.group_key("declined 05", None, None, None, None)[1:] == ("", "", "", "")


def test_stub_words_skip_stopwords_short_words_and_placeholders():
    assert rc.stub_words("transaction declined by the issuer. response code #: do not honour.") == [
        "transaction", "declined", "issuer", "response"]
    assert rc.stub_words("the a an #") == []


# ---- thresholds ----------------------------------------------------------------------------------

def test_below_min_occurrences_is_not_a_candidate(session):
    for i in range(2):
        add_decision(session, i, "Declined by the issuer. Response code 05.", classified_by="fallback")
    report = rc.scan(session)
    assert report.scanned == 2 and report.groups == 1 and report.candidates == [] and report.rejected == []
    assert rc.scan(session, min_occurrences=2).candidates[0].count == 2


def test_fallback_only_group_is_an_unlabelled_candidate(session):
    for i in range(3):
        add_decision(session, i, f"Transaction declined by the issuer. Response code 0{i}: Do not honour.", classified_by="fallback")
    report = rc.scan(session)
    assert len(report.candidates) == 1
    c = report.candidates[0]
    assert c.count == 3 and c.llm_count == 0 and c.fallback_count == 3
    assert c.proposed_class == rc.UNLABELLED and not c.labelled and c.note == rc.UNMAPPED_NOTE
    assert c.fields == {"error_code": "bad_request_error", "error_source": "bank", "error_step": "payment_authorization",
                        "error_reason": "payment_failed"}
    assert len(c.examples) == 3 and all("Response code" in e for e in c.examples)
    assert "FailureClass.UNKNOWN" in c.rule_stub and "a person picks the class" in c.rule_stub
    assert rc.REVIEW_NOTE in c.rule_stub


def test_llm_group_with_agreement_and_confidence_proposes_the_class(session):
    for i in range(4):
        add_decision(session, i, "Bank server is inoperative, try later", classified_by="llm",
                     failure_class=FailureClass.ISSUER_DOWN.value, confidence=0.9 if i else 0.8)
    add_decision(session, 9, "Bank server is inoperative, try later", classified_by="fallback")
    report = rc.scan(session)
    assert len(report.candidates) == 1 and not report.rejected
    c = report.candidates[0]
    assert c.count == 5 and c.llm_count == 4 and c.fallback_count == 1
    assert c.proposed_class == "ISSUER_DOWN" and c.mean_confidence == pytest.approx(0.875)
    assert "1 more fell back" in c.note
    assert "FailureClass.ISSUER_DOWN" in c.rule_stub and 'Rule("candidate_bank_server_inoperative"' in c.rule_stub


def test_low_confidence_or_disagreement_is_rejected_with_a_reason(session):
    for i in range(3):
        add_decision(session, i, "Something odd happened at the bank", classified_by="llm",
                     failure_class=FailureClass.ISSUER_DOWN.value, confidence=0.7)
    for i in range(3, 6):
        add_decision(session, i, "Ambiguous decline message", classified_by="llm",
                     failure_class=[FailureClass.ISSUER_DOWN, FailureClass.INSUFFICIENT_FUNDS, FailureClass.ISSUER_DOWN][i - 3].value,
                     confidence=0.95)
    for i in range(6, 9):
        add_decision(session, i, "Model could not tell", classified_by="llm", failure_class="UNKNOWN", confidence=0.3)
    report = rc.scan(session)
    assert report.candidates == []
    reasons = {r.signature: r.reason for r in report.rejected}
    assert "mean confidence 0.70 < 0.85" in reasons["something odd happened at the bank"]
    assert "disagrees" in reasons["ambiguous decline message"]
    assert "UNKNOWN" in reasons["model could not tell"]


def test_rules_decisions_are_ignored_and_structured_fields_split_groups(session):
    for i in range(3):
        add_decision(session, i, "Same text", classified_by="rules", failure_class="HARD_DECLINE", confidence=1.0)
    for i in range(3, 6):
        add_decision(session, i, "Same text", classified_by="fallback", reason="card_declined")
    for i in range(6, 9):
        add_decision(session, i, "Same text", classified_by="fallback", reason="payment_failed")
    report = rc.scan(session)
    assert report.scanned == 6 and report.groups == 2 and len(report.candidates) == 2
    assert {c.fields["error_reason"] for c in report.candidates} == {"card_declined", "payment_failed"}


# ---- the stub and the boundaries -----------------------------------------------------------------

def test_stub_is_valid_python_and_matches_the_examples():
    import re

    from app.classify import signals_of
    c = rc.Candidate(rc.signature("Transaction declined by the issuer. Response code 05: Do not honour."), 3,
                     "HARD_DECLINE", {"error_code": "bad_request_error", "error_source": "bank", "error_step": "", "error_reason": ""},
                     ["Transaction declined by the issuer. Response code 05: Do not honour."], 3, 0, 0.9, "note")
    stub = rc.rule_stub(c)
    ns = {}
    exec("from app.classify import Rule\nfrom app.taxonomy import FailureClass\nRULES = [\n" + stub + "\n]", ns)  # noqa: S102
    rule = ns["RULES"][0]
    assert rule.failure_class is FailureClass.HARD_DECLINE and rule.name == "candidate_transaction_declined_issuer"
    s = signals_of("BAD_REQUEST_ERROR", "bank", "payment_authorization", "payment_failed", c.examples[0])
    assert rule.predicate(s)
    assert not rule.predicate(signals_of(None, None, None, None, "insufficient funds"))
    assert re.search(r"\\b", stub)  # the regex survives as a raw string, not a backspace


def test_examples_carry_descriptions_only(session):
    for i in range(3):
        add_decision(session, i, "Kuch galat ho gaya, bank se sampark karein", classified_by="fallback")
    report = rc.scan(session)
    text = rc.render_text(report) + rc.render_markdown(report)
    assert "Person 0" not in text and "+915800000001" not in text and "p0@example.com" not in text
    assert "Kuch galat ho gaya" in text and "needs a human label" in text


def test_empty_database_and_cli(session, tmp_path, capsys):
    assert rc.scan(session).scanned == 0
    out = tmp_path / "rule_candidates.md"
    assert rc.main(["--write", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "no model-path decisions yet" in printed and f"wrote {out}" in printed
    assert "No model-path decisions yet" in out.read_text(encoding="utf-8")
    for i in range(3):
        add_decision(session, i, "Declined by the issuer. Response code 05.", classified_by="fallback")
    assert rc.main(["--min-occurrences", "3"]) == 0
    printed = capsys.readouterr().out
    assert "== candidates (1)" in printed and "rule stub" in printed and "Nothing here edits the rules" in printed


def test_classify_is_never_modified(session):
    before = CLASSIFY.read_bytes()
    digest = hashlib.sha256(before).hexdigest()
    for i in range(3):
        add_decision(session, i, "Declined by the issuer. Response code 05.", classified_by="fallback")
    rc.main([])
    assert hashlib.sha256(CLASSIFY.read_bytes()).hexdigest() == digest
