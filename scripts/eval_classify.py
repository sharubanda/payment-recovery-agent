"""Run the deterministic classifier over the hand-labelled eval set and report how it did.

The set (tests/fixtures/failure_cases.json) is hand-labelled and partly synthetic, not
production traffic; the number this prints is the rules' accuracy on that set, nothing more.
A wrong class is worse than UNKNOWN (UNKNOWN goes to the LLM, then a person), so the
report separates the two: "wrong" is a class that is not the expected one and not UNKNOWN,
"missed" is UNKNOWN where a class was expected. tests/test_eval_classify.py asserts wrong == 0.

Usage:  python scripts/eval_classify.py [--cases PATH] [--classifier PATH] [--write docs/classifier_eval.md]

--classifier loads another copy of classify.py (e.g. an older revision) as app.classify_<name>
so two rule sets can be scored on the same cases; the default is app.classify as installed.
No database, no network, no keys.
"""
import argparse
import importlib.util
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # importable from a clean clone, any cwd

from app import classify as default_classify  # noqa: E402
from app.taxonomy import FailureClass  # noqa: E402

DEFAULT_CASES = ROOT / "tests" / "fixtures" / "failure_cases.json"
UNKNOWN = FailureClass.UNKNOWN.value
CASE_FIELDS = ("id", "method", "error_code", "error_source", "error_step", "error_reason", "error_description",
               "expected_class", "note")
METHODS = ("card", "upi", "netbanking", "wallet", "emandate")
CODES = ("BAD_REQUEST_ERROR", "GATEWAY_ERROR", "SERVER_ERROR")
SOURCES = ("customer", "business", "bank", "gateway", "internal", "network")
STEPS = ("payment_initiation", "payment_authentication", "payment_authorization", "payment_capture")
REASONS = ("payment_failed", "payment_cancelled", "payment_timed_out", "card_declined", "input_validation_failed",
           "bank_technical_error", "gateway_technical_error", "server_error", "payment_risk_check_failed",
           "international_transaction_not_allowed", "transaction_limit_exceeded", "insufficient_funds")

CAVEAT = ("This set is hand-labelled and partly synthetic, not production traffic: the descriptions are Razorpay's "
          "standardised checkout-facing messages as remembered, bank/network messages (ISO 8583, NACH, NPCI) and "
          "synthetic or Hinglish variants, and the exact `error_reason` vocabulary Razorpay emits is partially "
          "uncertain. The numbers below are the rules' accuracy on this set, nothing more; the rules were tuned "
          "against this very set, so a perfect score here is a regression floor, not an estimate of production "
          "accuracy, and it says nothing about the class mix or the wording of a real merchant's failures.")

_RULE_RE = re.compile(r"^rule (\S+):")


@dataclass(frozen=True)
class Scored:
    case: dict
    expected: str
    got: str
    rule: str          # the rule that fired, or "-" for no rule

    @property
    def correct(self) -> bool:
        return self.got == self.expected

    @property
    def wrong(self) -> bool:
        """A class that is neither the expected one nor UNKNOWN: the one outcome the tests forbid."""
        return self.got != self.expected and self.got != UNKNOWN

    @property
    def missed(self) -> bool:
        """UNKNOWN where a class was expected: safe (a person looks), but a gap in the rules."""
        return self.got == UNKNOWN and self.expected != UNKNOWN


def load_cases(path: Path = DEFAULT_CASES) -> list[dict]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    cases = payload["cases"] if isinstance(payload, dict) else payload
    validate_cases(cases)
    return cases


def validate_cases(cases: list[dict]) -> None:
    """Shape checks so a typo in the fixture fails loudly rather than scoring as a miss."""
    ids = [c["id"] for c in cases]
    assert len(set(ids)) == len(ids), "duplicate case ids"
    known = {c.value for c in FailureClass}
    for c in cases:
        assert set(c) == set(CASE_FIELDS), (c.get("id"), set(c) ^ set(CASE_FIELDS))
        assert c["method"] in METHODS, (c["id"], c["method"])
        assert c["error_code"] in CODES or c["error_code"] is None, (c["id"], c["error_code"])
        assert c["error_source"] in SOURCES or c["error_source"] is None, (c["id"], c["error_source"])
        assert c["error_step"] in STEPS or c["error_step"] is None, (c["id"], c["error_step"])
        assert c["error_reason"] in REASONS or c["error_reason"] is None, (c["id"], c["error_reason"])
        assert c["expected_class"] in known, (c["id"], c["expected_class"])
        assert c["note"], c["id"]


def to_attempt(case: dict) -> SimpleNamespace:
    """A PaymentAttempt-shaped object with the five error_* attributes; no ORM, no DB."""
    return SimpleNamespace(error_code=case["error_code"], error_source=case["error_source"],
                           error_step=case["error_step"], error_reason=case["error_reason"],
                           error_description=case["error_description"], method=case["method"])


def load_classifier(path: str | None):
    """app.classify by default; with a path, that file loaded as app.classify_<stem> so its relative imports resolve."""
    if not path:
        return default_classify
    p = Path(path).resolve()
    name = "app.classify_" + re.sub(r"\W", "_", p.stem)
    spec = importlib.util.spec_from_file_location(name, p)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def score(cases: list[dict], classifier=default_classify) -> list[Scored]:
    out = []
    for case in cases:
        c = classifier.classify(to_attempt(case))
        got = getattr(c.failure_class, "value", str(c.failure_class))
        m = _RULE_RE.match(c.reason or "")
        out.append(Scored(case=case, expected=case["expected_class"], got=got, rule=m.group(1) if m else "-"))
    return out


def class_order() -> list[str]:
    return [c.value for c in FailureClass]


def metrics(scored: list[Scored]) -> dict:
    n = len(scored)
    correct = sum(s.correct for s in scored)
    wrong = [s for s in scored if s.wrong]
    missed = [s for s in scored if s.missed]
    by_design = [s for s in scored if s.expected == UNKNOWN]
    classifiable = [s for s in scored if s.expected != UNKNOWN]
    covered = sum(1 for s in scored if s.got != UNKNOWN)
    covered_classifiable = sum(1 for s in classifiable if s.got != UNKNOWN)

    per_class = {}
    for cls in class_order():
        tp = sum(1 for s in scored if s.got == cls and s.expected == cls)
        fp = sum(1 for s in scored if s.got == cls and s.expected != cls)
        fn = sum(1 for s in scored if s.expected == cls and s.got != cls)
        support = sum(1 for s in scored if s.expected == cls)
        per_class[cls] = {
            "support": support,
            "predicted": tp + fp,
            "precision": tp / (tp + fp) if tp + fp else None,
            "recall": tp / (tp + fn) if tp + fn else None,
        }

    confusion: dict[str, Counter] = defaultdict(Counter)
    for s in scored:
        confusion[s.expected][s.got] += 1

    rules_fired = Counter(s.rule for s in scored if s.rule != "-")
    return {
        "n": n, "correct": correct, "accuracy": correct / n if n else 0.0,
        "wrong": wrong, "missed": missed, "by_design": by_design,
        "coverage": covered / n if n else 0.0,
        "coverage_classifiable": covered_classifiable / len(classifiable) if classifiable else 0.0,
        "n_classifiable": len(classifiable),
        "per_class": per_class, "confusion": confusion, "rules_fired": rules_fired,
    }


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{100 * x:.1f}%"


def _desc(case: dict, width: int = 70) -> str:
    d = case["error_description"]
    d = "(empty)" if not d else d
    return d if len(d) <= width else d[: width - 3] + "..."


def summary_text(m: dict, classifier_label: str) -> str:
    lines = [
        f"eval_classify: {m['n']} cases ({m['n_classifiable']} with a class, {len(m['by_design'])} UNKNOWN by design); "
        f"classifier {classifier_label}",
        f"accuracy {_pct(m['accuracy'])} ({m['correct']}/{m['n']} exact)  "
        f"wrong {len(m['wrong'])}  missed (UNKNOWN where a class was expected) {len(m['missed'])}",
        f"coverage {_pct(m['coverage'])} of all cases not UNKNOWN; "
        f"{_pct(m['coverage_classifiable'])} of the cases that have a class",
        "",
        f"{'class':<20} {'support':>7} {'predicted':>9} {'precision':>9} {'recall':>7}",
    ]
    for cls, r in m["per_class"].items():
        lines.append(f"{cls:<20} {r['support']:>7} {r['predicted']:>9} {_pct(r['precision']):>9} {_pct(r['recall']):>7}")
    if m["wrong"]:
        lines += ["", "WRONG (a class that is neither expected nor UNKNOWN):"]
        lines += [f"  {s.case['id']:<6} expected {s.expected:<18} got {s.got:<18} rule {s.rule}: {_desc(s.case)}" for s in m["wrong"]]
    if m["missed"]:
        lines += ["", "missed (UNKNOWN where a class was expected):"]
        lines += [f"  {s.case['id']:<6} expected {s.expected:<18}: {_desc(s.case)}" for s in m["missed"]]
    return "\n".join(lines)


def _md_table(header: list[str], rows: list[list[str]], align_right_from: int = 1) -> str:
    sep = ["---"] * align_right_from + ["---:"] * (len(header) - align_right_from)
    out = ["| " + " | ".join(header) + " |", "|" + "|".join(sep) + "|"]
    out += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(out)


def _md_cell(text: str) -> str:
    return text.replace("|", "\\|")


def report_markdown(m: dict, cases_path: Path, classifier_label: str, comment: str, rules_table: list[dict]) -> str:
    classes = class_order()
    per_class_rows = [[cls, str(r["support"]), str(r["predicted"]), _pct(r["precision"]), _pct(r["recall"])]
                      for cls, r in m["per_class"].items()]
    confusion_rows = [[exp] + [str(m["confusion"][exp].get(got, 0)) for got in classes] for exp in classes]
    wrong_rows = [[s.case["id"], s.expected, s.got, s.rule, _md_cell(_desc(s.case, 90))] for s in m["wrong"]]
    missed_rows = [[s.case["id"], s.expected, _md_cell(_desc(s.case, 90)), _md_cell(s.case["note"])] for s in m["missed"]]
    design_rows = [[s.case["id"], s.got, _md_cell(_desc(s.case, 90)), _md_cell(s.case["note"])] for s in m["by_design"]]
    rule_rows = [[str(r["order"]), f"`{r['rule']}`", r["failure_class"], str(m["rules_fired"].get(r["rule"], 0)),
                  _md_cell(r["matches"])] for r in rules_table]
    wrong_section = (_md_table(["id", "expected", "got", "rule fired", "description"], wrong_rows, 5)
                     if wrong_rows else "None. Every case landed on its expected class or on UNKNOWN.")
    missed_section = (_md_table(["id", "expected", "description", "note"], missed_rows, 4)
                      if missed_rows else "None. Every case that has a class was classified.")
    confusion_table = _md_table(["expected \\ got"] + classes, confusion_rows)
    # the fixture's own caveat about the reason vocabulary, verbatim
    return f"""# Classifier evaluation

{CAVEAT}

Generated by `python scripts/eval_classify.py --write docs/classifier_eval.md` over `{cases_path.relative_to(ROOT).as_posix()}`
({m['n']} cases: {m['n_classifiable']} with an expected class, {len(m['by_design'])} UNKNOWN by design) against `{classifier_label}`.
No database, no network, no LLM: `app.classify.classify` over a PaymentAttempt-shaped object per case.

## Headline

| metric | value |
|---|---:|
| accuracy (exact match, UNKNOWN-by-design counts when the rules say UNKNOWN) | {_pct(m['accuracy'])} ({m['correct']}/{m['n']}) |
| wrong (a class that is neither the expected one nor UNKNOWN) | {len(m['wrong'])} |
| missed (UNKNOWN where a class was expected) | {len(m['missed'])} |
| coverage (share of all cases not UNKNOWN) | {_pct(m['coverage'])} |
| coverage of the cases that have a class | {_pct(m['coverage_classifiable'])} ({m['n_classifiable'] - len(m['missed'])}/{m['n_classifiable']}) |

"Wrong" is the number that matters: a wrong class drives the wrong recovery action in a money path, while UNKNOWN
goes to the LLM and then a person. `tests/test_eval_classify.py` asserts wrong == 0 and pins the coverage floor.

## Per class

Precision is over what the rules predicted for the class; recall over the cases labelled with it.

{_md_table(["class", "support", "predicted", "precision", "recall"], per_class_rows)}

## Confusion matrix (rows: expected, columns: got)

{confusion_table}

## Wrong classifications

{wrong_section}

## Missed (UNKNOWN where a class was expected)

{missed_section}

## UNKNOWN by design

These must stay UNKNOWN: the description does not establish a cause, or a naive keyword rule would misfire on it.

{_md_table(["id", "got", "description", "note"], design_rows, 4)}

## Rules in evaluation order, with how often each fired on this set

{_md_table(["#", "rule", "class", "fired", "matches"], rule_rows, 5)}

## About the set

{comment}
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="score app.classify over the hand-labelled eval set")
    p.add_argument("--cases", default=str(DEFAULT_CASES), help="path to the cases JSON")
    p.add_argument("--classifier", metavar="PATH", help="score this copy of classify.py instead of app.classify")
    p.add_argument("--write", metavar="PATH", help="also write the Markdown report to this path")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    cases_path = Path(args.cases).resolve()
    payload = json.loads(cases_path.read_text(encoding="utf-8"))
    cases = payload["cases"] if isinstance(payload, dict) else payload
    validate_cases(cases)
    classifier = load_classifier(args.classifier)
    label = args.classifier or "app/classify.py"
    m = metrics(score(cases, classifier))
    print(summary_text(m, label))
    if args.write:
        comment = payload.get("_comment", "") if isinstance(payload, dict) else ""
        table = classifier.rules_table() if hasattr(classifier, "rules_table") else []
        out = Path(args.write)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report_markdown(m, cases_path, label, comment, table), encoding="utf-8")
        print(f"wrote {out}")
    return 0 if not m["wrong"] else 1


if __name__ == "__main__":
    sys.exit(main())
