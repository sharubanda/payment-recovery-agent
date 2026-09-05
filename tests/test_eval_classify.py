"""The rule classifier against the hand-labelled eval set (tests/fixtures/failure_cases.json).

The one hard invariant: zero WRONG classifications. UNKNOWN where a class was expected is a
gap (the LLM and then a person cover it); a wrong class drives the wrong recovery action in a
money path and is never acceptable. Coverage is pinned at the level the rules reach today so
a regression in either direction is visible. The set is hand-labelled and partly synthetic,
so these numbers describe the rules on this set, not production accuracy."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from app.taxonomy import FailureClass
from scripts import eval_classify as ev

ROOT = Path(__file__).resolve().parents[1]
CASES = ev.load_cases(ev.DEFAULT_CASES)
SCORED = ev.score(CASES)
METRICS = ev.metrics(SCORED)

MIN_CASES = 90
COVERAGE_FLOOR = 0.87               # share of all cases not UNKNOWN; the 15 UNKNOWN-by-design cases cap it at 87.3%
COVERAGE_OF_CLASSIFIABLE_FLOOR = 1.0  # every case that has a class is classified today; a drop is a regression


def test_fixture_shape_and_vocabulary():
    payload = json.loads(ev.DEFAULT_CASES.read_text(encoding="utf-8"))
    assert "uncertain" in payload["_comment"]  # the header says the reason vocabulary is partly a guess
    assert len(CASES) >= MIN_CASES
    ev.validate_cases(CASES)  # raises on a bad field
    notes = " ".join(c["note"] for c in CASES)
    for kind in ("razorpay-standard message", "bank/network message", "synthetic"):
        assert kind in notes, kind
    assert any("Hinglish" in c["note"] for c in CASES)
    assert any("ISO 8583" in c["note"] for c in CASES)
    assert {c["method"] for c in CASES} == {"card", "upi", "netbanking", "wallet", "emandate"}


def test_every_class_has_support_including_unknown_by_design():
    support = {cls.value: METRICS["per_class"][cls.value]["support"] for cls in FailureClass}
    assert all(n >= 8 for n in support.values()), support
    assert support["UNKNOWN"] >= 10  # the generic soft decline, ISO 05, tricky negatives


def test_zero_wrong_classifications():
    wrong = [(s.case["id"], s.expected, s.got, s.rule) for s in SCORED if s.wrong]
    assert wrong == [], wrong


def test_unknown_by_design_stays_unknown():
    leaked = [(s.case["id"], s.got, s.rule) for s in SCORED if s.expected == "UNKNOWN" and s.got != "UNKNOWN"]
    assert leaked == [], leaked


def test_coverage_floor():
    missed = [(s.case["id"], s.expected) for s in SCORED if s.missed]
    assert METRICS["coverage"] >= COVERAGE_FLOOR, (METRICS["coverage"], missed)
    assert METRICS["coverage_classifiable"] >= COVERAGE_OF_CLASSIFIABLE_FLOOR, (METRICS["coverage_classifiable"], missed)


@pytest.mark.parametrize("case_id, expected", [
    ("UK-08", "UNKNOWN"),   # "brisk" must not fire the risk rule
    ("UK-09", "UNKNOWN"),   # "not blocked" must not fire the hard-decline rule
    ("UK-10", "UNKNOWN"),   # "footprint" must not fire the OTP rule
    ("UK-01", "UNKNOWN"),   # Razorpay's generic soft decline is UNKNOWN by design
    ("AA-12", "AUTH_ABANDONED"),   # "time limit" is not a transaction limit
    ("NT-10", "NETWORK_TIMEOUT"),  # a gateway rate limit is not a customer limit
    ("IF-12", "INSUFFICIENT_FUNDS"),  # a credit limit is the card's balance
    ("ID-12", "ISSUER_DOWN"),      # "balance confirmation ... bank not responding" is the bank
    ("HD-19", "HARD_DECLINE"),     # a cancelled mandate is a dead instrument, not an abandoned OTP
    ("LE-03", "LIMIT_EXCEEDED"),   # ISO 8583 code 61
])
def test_named_tricky_cases(case_id, expected):
    s = next(s for s in SCORED if s.case["id"] == case_id)
    assert s.expected == expected  # the fixture still says what this test thinks it says
    assert s.got == expected, (case_id, s.got, s.rule)


def test_report_opens_with_the_caveat_and_carries_the_tables(tmp_path):
    out = tmp_path / "classifier_eval.md"
    assert ev.main(["--write", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    assert paragraphs[0] == "# Classifier evaluation"
    assert paragraphs[1] == ev.CAVEAT
    assert "hand-labelled and partly synthetic" in ev.CAVEAT and "nothing more" in ev.CAVEAT
    for heading in ("## Headline", "## Per class", "## Confusion matrix", "## Wrong classifications",
                    "## Missed (UNKNOWN where a class was expected)", "## UNKNOWN by design", "## Rules in evaluation order"):
        assert heading in text, heading
    assert "| wrong (a class that is neither the expected one nor UNKNOWN) | 0 |" in text
    assert "`risk_flag`" in text and "`limit_exceeded_description`" in text


def test_committed_report_matches_the_script():
    committed = ROOT / "docs" / "classifier_eval.md"
    assert committed.exists(), "run: python scripts/eval_classify.py --write docs/classifier_eval.md"
    payload = json.loads(ev.DEFAULT_CASES.read_text(encoding="utf-8"))
    from app.classify import rules_table
    expected = ev.report_markdown(METRICS, ev.DEFAULT_CASES, "app/classify.py", payload["_comment"], rules_table())
    assert committed.read_text(encoding="utf-8") == expected, "docs/classifier_eval.md is stale; regenerate it"


def test_cli_scores_an_alternative_classifier_and_exits_nonzero_on_a_wrong_class(tmp_path):
    # a classifier that calls everything HARD_DECLINE is wrong on most of the set: exit 1, and the report says so
    bad = tmp_path / "classify_bad.py"
    bad.write_text(
        "from .taxonomy import Classification, FailureClass\n"
        "def classify(a):\n"
        "    return Classification(FailureClass.HARD_DECLINE, 'rule everything_is_hard: always', 'rules', 1.0)\n"
        "def rules_table():\n"
        "    return [{'order': 1, 'rule': 'everything_is_hard', 'failure_class': 'HARD_DECLINE', 'matches': 'always'}]\n",
        encoding="utf-8")
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "eval_classify.py"), "--classifier", str(bad),
                           "--write", str(tmp_path / "bad.md")], cwd=tmp_path, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 1, proc.stderr
    assert "WRONG" in proc.stdout and "everything_is_hard" in proc.stdout
    assert not list(tmp_path.glob("*.db*"))
    good = subprocess.run([sys.executable, str(ROOT / "scripts" / "eval_classify.py")], cwd=tmp_path,
                          capture_output=True, text=True, timeout=120)
    assert good.returncode == 0 and "wrong 0" in good.stdout
