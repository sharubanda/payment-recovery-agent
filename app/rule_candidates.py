"""The model path feeding the rules table, with a human gate.

    python -m app.rule_candidates [--write docs/rule_candidates.md] [--min-occurrences N]

classify.py is deterministic on purpose, so anything it cannot name goes to the LLM and then to a
person. Every one of those decisions is stored with classified_by = "llm" or "fallback". This
module reads them back, groups them by a normalised description signature (lowercase, digits ->
#, whitespace collapsed, payment/order/link ids stripped) plus the structured error fields, and
turns recurring groups into rule CANDIDATES:

  * a group with >= --min-occurrences decisions whose LLM classifications agree on one class
    with mean confidence >= 0.85 proposes that class;
  * a group the model never resolved (fallback only) is "recurring unmapped: needs a human label".

For each candidate it prints the signature, the count, the proposed class (or "unlabelled"), up
to three example descriptions (descriptions only: never a name, contact or email), the structured
fields, and a ready-to-paste Rule stub for app/classify.py. It never edits classify.py: the stub
goes into a pull request, with cases added to tests/fixtures/failure_cases.json first, and the
ordered rule list stays a thing a reviewer reads. Groups that recur but disagree, or agree with
low confidence, are listed under "rejected" with the reason, so the report says what it saw.
"""
import argparse
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlalchemy import select

from . import db
from .models import PaymentAttempt, RecoveryDecision
from .taxonomy import FailureClass

MIN_OCCURRENCES = 3
MIN_CONFIDENCE = 0.85
MAX_EXAMPLES = 3
MAX_STUB_WORDS = 4
MODEL_SOURCES = ("llm", "fallback")
UNLABELLED = "unlabelled"
UNMAPPED_NOTE = "recurring unmapped: needs a human label"
REVIEW_NOTE = "review and add tests/fixtures/failure_cases.json cases before adding"

_ID_RE = re.compile(r"\b(?:pay|order|plink|rzp|txn|ref|umrn|utr)_?[a-z0-9]{6,}\b", re.IGNORECASE)
_DIGIT_RE = re.compile(r"\d")
_HASH_RUN_RE = re.compile(r"#+")
_WORD_RE = re.compile(r"[a-z][a-z']+")
STOPWORDS = frozenset("""
the a an and or of to in on at by for with is was were be been being this that these those it its your you
please has have had not no do does did as from into than then there their they them we our us am are but if
so such can could would should may might will shall after before again also very kripya apne aapka hai ke
""".split())


# ---- normalisation -------------------------------------------------------------------------------

def signature(description: str | None) -> str:
    """Normalised description: lowercase, ids stripped, digits -> # (runs collapsed), whitespace collapsed."""
    text = (description or "").lower()
    text = _ID_RE.sub(" ", text)
    text = _DIGIT_RE.sub("#", text)
    text = _HASH_RUN_RE.sub("#", text)
    return " ".join(text.split()).strip()


def _norm(value: str | None) -> str:
    return " ".join(str(value or "").split()).lower()


def group_key(description: str | None, code: str | None, source: str | None, step: str | None,
              reason: str | None) -> tuple[str, str, str, str, str]:
    return signature(description), _norm(code), _norm(source), _norm(step), _norm(reason)


def stub_words(sig: str, limit: int = MAX_STUB_WORDS) -> list[str]:
    """The content words of a signature, in order, for the regex in the Rule stub."""
    out = []
    for w in _WORD_RE.findall(sig):
        if len(w) >= 4 and w not in STOPWORDS and w not in out:
            out.append(w)
        if len(out) == limit:
            break
    return out


# ---- candidates ----------------------------------------------------------------------------------

@dataclass
class Candidate:
    signature: str
    count: int
    proposed_class: str                  # a FailureClass value or UNLABELLED
    fields: dict[str, str]               # error_code / error_source / error_step / error_reason
    examples: list[str]                  # raw descriptions, max MAX_EXAMPLES, distinct
    llm_count: int
    fallback_count: int
    mean_confidence: float | None
    note: str                            # UNMAPPED_NOTE for fallback-only groups, else the agreement summary
    rule_stub: str = ""

    @property
    def labelled(self) -> bool:
        return self.proposed_class != UNLABELLED


@dataclass
class Rejected:
    signature: str
    count: int
    reason: str


@dataclass
class CandidateReport:
    generated_at: datetime
    database: str
    min_occurrences: int
    scanned: int                         # model-path decisions read
    groups: int
    candidates: list[Candidate] = field(default_factory=list)
    rejected: list[Rejected] = field(default_factory=list)


def rule_name(sig: str, proposed_class: str) -> str:
    words = stub_words(sig, 3) or ["unmapped"]
    return f"candidate_{'_'.join(words)}"


def rule_stub(cand: Candidate) -> str:
    words = stub_words(cand.signature)
    regex = r".{0,40}".join(rf"\b{re.escape(w)}\b" for w in words) if words else re.escape(cand.signature[:40])
    cls = cand.proposed_class if cand.labelled else FailureClass.UNKNOWN.value
    label_note = "" if cand.labelled else "\n# UNKNOWN is a placeholder: a person picks the class before this is added"
    structured = ", ".join(f"{k}={v!r}" for k, v in cand.fields.items() if v)
    desc = (f"the description says {' ... '.join(words) if words else cand.signature[:40]} (in that order, within 40 chars)"
            + (f"; seen with {structured}" if structured else ""))
    return (f"# {REVIEW_NOTE}\n"
            f"# seen {cand.count}x; {cand.note}{label_note}\n"
            f"Rule(\"{rule_name(cand.signature, cand.proposed_class)}\", FailureClass.{cls},\n"
            f"     lambda s: s.has(r\"{regex}\"),\n"
            f"     \"{desc}\"),")


def build_candidates(rows: list[tuple[RecoveryDecision, PaymentAttempt]], *,
                     min_occurrences: int = MIN_OCCURRENCES, min_confidence: float = MIN_CONFIDENCE
                     ) -> tuple[list[Candidate], list[Rejected], int]:
    """Pure: (decision, attempt) pairs from the model path -> candidates, rejected groups, group count."""
    groups: dict[tuple, list[tuple[RecoveryDecision, PaymentAttempt]]] = defaultdict(list)
    for d, a in rows:
        if (d.classified_by or "") not in MODEL_SOURCES:
            continue
        groups[group_key(a.error_description, a.error_code, a.error_source, a.error_step, a.error_reason)].append((d, a))

    candidates: list[Candidate] = []
    rejected: list[Rejected] = []
    for key, members in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        sig, code, source, step, reason = key
        n = len(members)
        if n < min_occurrences:
            continue
        llm = [d for d, _ in members if d.classified_by == "llm"]
        fallback = [d for d, _ in members if d.classified_by == "fallback"]
        examples: list[str] = []
        for _, a in members:
            desc = " ".join((a.error_description or "").split())
            if desc and desc not in examples:
                examples.append(desc)
            if len(examples) == MAX_EXAMPLES:
                break
        fields = {"error_code": code, "error_source": source, "error_step": step, "error_reason": reason}
        if llm:
            classes = {d.failure_class for d in llm}
            confs = [float(d.confidence or 0.0) for d in llm]
            mean_conf = sum(confs) / len(confs)
            if len(classes) != 1:
                rejected.append(Rejected(sig, n, f"LLM disagrees with itself: {', '.join(sorted(classes))}; a person labels it"))
                continue
            (cls,) = classes
            if cls == FailureClass.UNKNOWN.value:
                rejected.append(Rejected(sig, n, "LLM answered UNKNOWN consistently; nothing to map"))
                continue
            if mean_conf < min_confidence:
                rejected.append(Rejected(sig, n, f"LLM agrees on {cls} but mean confidence {mean_conf:.2f} < {min_confidence}"))
                continue
            note = (f"LLM classified all {len(llm)} as {cls} with mean confidence {mean_conf:.2f}"
                    + (f"; {len(fallback)} more fell back to a person" if fallback else ""))
            cand = Candidate(sig, n, cls, fields, examples, len(llm), len(fallback), mean_conf, note)
        else:
            cand = Candidate(sig, n, UNLABELLED, fields, examples, 0, len(fallback), None, UNMAPPED_NOTE)
        cand.rule_stub = rule_stub(cand)
        candidates.append(cand)
    return candidates, rejected, len(groups)


def scan(session, *, min_occurrences: int = MIN_OCCURRENCES, min_confidence: float = MIN_CONFIDENCE) -> CandidateReport:
    stmt = (select(RecoveryDecision, PaymentAttempt).join(PaymentAttempt, RecoveryDecision.attempt_id == PaymentAttempt.id)
            .where(RecoveryDecision.classified_by.in_(MODEL_SOURCES)).order_by(RecoveryDecision.id))
    rows = [(d, a) for d, a in session.execute(stmt).all()]
    candidates, rejected, groups = build_candidates(rows, min_occurrences=min_occurrences, min_confidence=min_confidence)
    return CandidateReport(generated_at=datetime.utcnow().replace(microsecond=0), database=db.url(),
                           min_occurrences=min_occurrences, scanned=len(rows), groups=groups,
                           candidates=candidates, rejected=rejected)


# ---- rendering -----------------------------------------------------------------------------------

HEADER_NOTE = ("Candidates are proposals for app/classify.py, printed for a person. Nothing here edits the rules: "
               "a rule lands through a pull request with fixture cases, in the order a reviewer chooses.")


def candidate_text(c: Candidate) -> str:
    fields = ", ".join(f"{k}={v or '-'}" for k, v in c.fields.items())
    lines = [f"signature : {c.signature}",
             f"count     : {c.count} (llm {c.llm_count}, fallback {c.fallback_count})",
             f"proposed  : {c.proposed_class}" + (f" (mean confidence {c.mean_confidence:.2f})" if c.mean_confidence is not None else ""),
             f"note      : {c.note}",
             f"fields    : {fields}",
             "examples  :"]
    lines += [f"  - {e}" for e in c.examples]
    lines += ["rule stub :"] + [f"  {ln}" for ln in c.rule_stub.splitlines()]
    return "\n".join(lines)


def render_text(report: CandidateReport) -> str:
    lines = ["payment-recovery-agent rule candidates", f"  database        : {report.database}",
             f"  generated       : {report.generated_at.isoformat()}Z",
             f"  min-occurrences : {report.min_occurrences}",
             f"  scanned         : {report.scanned} model-path decisions (classified_by in {', '.join(MODEL_SOURCES)}) "
             f"in {report.groups} signature group(s)",
             f"  {HEADER_NOTE}", ""]
    if report.scanned == 0:
        lines.append("no model-path decisions yet: every classification so far came from the rules, or nothing has been processed.")
        return "\n".join(lines) + "\n"
    lines.append(f"== candidates ({len(report.candidates)})")
    if not report.candidates:
        lines.append(f"  none: no signature recurs {report.min_occurrences}+ times with an agreed label "
                     f"(groups seen: {report.groups}). Lower --min-occurrences to look at smaller groups.")
    for i, c in enumerate(report.candidates, 1):
        lines.append(f"-- candidate {i} of {len(report.candidates)}")
        lines.append(candidate_text(c))
        lines.append("")
    if report.rejected:
        lines.append(f"== recurring but rejected ({len(report.rejected)})")
        for r in report.rejected:
            lines.append(f"  {r.count:>3}x  {r.signature[:70]:<70}  {r.reason}")
    return "\n".join(lines) + "\n"


def render_markdown(report: CandidateReport) -> str:
    out = ["# Rule candidates", "",
           f"Generated {report.generated_at.isoformat()}Z from `{report.database}` by `python -m app.rule_candidates` "
           f"(min-occurrences {report.min_occurrences}; {report.scanned} model-path decisions in {report.groups} groups).", "",
           HEADER_NOTE, ""]
    if report.scanned == 0:
        out.append("**No model-path decisions yet.**")
        return "\n".join(out) + "\n"
    out.append(f"## Candidates ({len(report.candidates)})")
    out.append("")
    if not report.candidates:
        out.append(f"None: no signature recurs {report.min_occurrences}+ times with an agreed label.")
    for i, c in enumerate(report.candidates, 1):
        out += [f"### {i}. `{c.signature}`", "",
                f"- count: {c.count} (llm {c.llm_count}, fallback {c.fallback_count})",
                f"- proposed class: **{c.proposed_class}**" + (f" (mean confidence {c.mean_confidence:.2f})" if c.mean_confidence is not None else ""),
                f"- note: {c.note}",
                "- fields: " + ", ".join(f"{k}=`{v or '-'}`" for k, v in c.fields.items()),
                "- examples:"] + [f"  - {e}" for e in c.examples] + ["", "```python", c.rule_stub, "```", ""]
    if report.rejected:
        out += ["## Recurring but rejected", "", "| count | signature | reason |", "|---|---|---|"]
        out += [f"| {r.count} | `{r.signature}` | {r.reason} |" for r in report.rejected]
        out.append("")
    return "\n".join(out) + "\n"


# ---- CLI -----------------------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.rule_candidates", description=__doc__.split("\n\n")[0])
    p.add_argument("--write", metavar="PATH", help="also write the candidates as Markdown to this path")
    p.add_argument("--min-occurrences", type=int, default=MIN_OCCURRENCES,
                   help=f"a signature must recur this many times to be a candidate (default {MIN_OCCURRENCES})")
    args = p.parse_args(argv)
    db.init_db()
    session = db.session()
    try:
        report = scan(session, min_occurrences=max(1, args.min_occurrences))
    finally:
        session.close()
    sys.stdout.write(render_text(report))
    if args.write:
        path = Path(args.write)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_markdown(report), encoding="utf-8")
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
