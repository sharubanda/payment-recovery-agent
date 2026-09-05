"""Learn from outcomes without ever changing behaviour silently.

    python -m app.insights [--write docs/insights_report.md] [--min-samples N] [--merchant ID]

Reads the four tables the pipeline writes (payment_attempts, recovery_decisions, recovery_jobs,
outcomes) and reports, per failure class: how many events, how many links went out, how many
recovered and for how much, the recovery rate with a 95% Wilson score interval, the human-queue
and stub counts, the average time to recovery, and the same rate + interval per delay bucket the
policy table actually used. Then LLM usage (how often a model was consulted, every fallback by
name, latency percentiles) and money (at risk, recovered, open, parked) per merchant.

The PROPOSALS section is the point. For each class it compares the bucket the policy table uses
against every other bucket that has data and proposes a change ONLY when both buckets have at
least --min-samples events (default 30) and their 95% intervals do not overlap. Anything else is
"insufficient evidence (n=..)". A proposal is printed and, with --write, written to Markdown. It
is never applied: app/policy.py is code-reviewed and this report is evidence for the person
doing that review, not a bandit. A class whose human-queue share exceeds 30% is flagged as a
rules gap and pointed at `python -m app.rule_candidates`.

Wilson rather than the normal approximation because the numbers here are small: 3 of 5 is not
"60%", it is "60% (17%-93%)", and the interval must say so without going below 0 or above 1.
Stdlib only: the interval is a few lines of arithmetic.
"""
import argparse
import math
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlalchemy import select

from . import db
from .models import Outcome, PaymentAttempt, RecoveryDecision, RecoveryJob
from .nudge_templates import format_rupees
from .policy import POLICY, human_delay
from .taxonomy import FailureClass, JobStatus

MIN_SAMPLES = 30
RULES_GAP_SHARE = 0.30
Z95 = 1.959963984540054  # two-sided 95% normal quantile
MINUTE, HOUR = 60, 3600

# (label, upper bound in seconds, inclusive). The bucket is the delay the job was scheduled with,
# not when it actually ran: --execute-now runs a 48h job immediately and it still counts as 48h,
# because the policy decision under test is the delay in the table.
BUCKETS: tuple[tuple[str, int], ...] = (
    ("now", 0), ("<=15m", 15 * MINUTE), ("<=1h", HOUR), ("<=24h", 24 * HOUR), ("<=48h", 48 * HOUR), (">48h", 1 << 62),
)
BUCKET_NAMES = tuple(name for name, _ in BUCKETS)
INSUFFICIENT = "insufficient evidence"


# ---- statistics ----------------------------------------------------------------------------------

def wilson_interval(k: int, n: int, z: float = Z95) -> tuple[float, float]:
    """95% Wilson score interval for k successes in n trials. n == 0 -> (0.0, 1.0): with no trials
    the honest answer is that the rate could be anything, never a point at zero."""
    if n <= 0:
        return 0.0, 1.0
    k = min(max(int(k), 0), int(n))
    p = k / n
    z2 = z * z
    denom = 1 + z2 / n
    centre = (p + z2 / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denom
    lo, hi = max(0.0, centre - half), min(1.0, centre + half)
    return (0.0 if k == 0 else lo), (1.0 if k == n else hi)  # exact at the edges, no 5e-17 residue


def percentile(values: list[int | float], q: float) -> float | None:
    """Nearest-rank percentile (q in 0..1) over a small list; None when empty."""
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))
    return float(ordered[idx])


def bucket_for(delay_seconds: int | float | None) -> str:
    """Delay in seconds -> the bucket name. None or negative counts as now."""
    s = 0 if delay_seconds is None else max(0, int(delay_seconds))
    for name, upper in BUCKETS:
        if s <= upper:
            return name
    return BUCKET_NAMES[-1]


def policy_bucket(failure_class: str) -> str | None:
    """The bucket the policy table puts this class's first attempt in; None for terminal classes."""
    try:
        entry = POLICY[FailureClass(failure_class)]
    except (KeyError, ValueError):
        return None
    if entry["max_attempts"] == 0:
        return None
    return bucket_for(entry["delay_seconds"])


@dataclass
class Rate:
    n: int = 0
    k: int = 0

    @property
    def rate(self) -> float | None:
        return self.k / self.n if self.n else None

    @property
    def interval(self) -> tuple[float, float]:
        return wilson_interval(self.k, self.n)

    def text(self) -> str:
        if not self.n:
            return "-"
        lo, hi = self.interval
        return f"{self.rate:.0%} ({lo:.0%}-{hi:.0%}) n={self.n}"


@dataclass
class Proposal:
    failure_class: str
    kind: str          # "proposal" | "supports_current" | "insufficient" | "not_applicable"
    text: str
    # set on kind == "proposal" only: a stable id ("INSUFFICIENT_FUNDS-48h-to-24h") the operator view
    # and `python -m app.merchants apply --proposal ID` name it by, the two buckets and their n
    id: str = ""
    from_bucket: str | None = None
    to_bucket: str | None = None
    n_current: int | None = None
    n_other: int | None = None

    @property
    def is_proposal(self) -> bool:
        return self.kind == "proposal"


def bucket_label(bucket: str) -> str:
    """"<=24h" -> "24h", ">48h" -> "over48h", "now" -> "now": id-safe, no comparison signs."""
    return bucket.replace("<=", "").replace(">", "over")


def proposal_id(failure_class: str, from_bucket: str, to_bucket: str) -> str:
    return f"{failure_class}-{bucket_label(from_bucket)}-to-{bucket_label(to_bucket)}"


def bucket_delay_seconds(bucket: str) -> int:
    """The delay_seconds an override should carry to land in `bucket`: its upper bound (the
    bucket is "<= bound"), 72h for the open-ended last one. Unknown bucket -> ValueError."""
    for name, upper in BUCKETS:
        if name == bucket:
            return upper if name != BUCKET_NAMES[-1] else 72 * HOUR
    raise ValueError(f"unknown bucket {bucket!r}; one of {', '.join(BUCKET_NAMES)}")


def proposal_by_id(report: "Report", proposal_id_: str) -> "Proposal | None":
    """The proposal with this id in the report, or None (a proposal that no longer holds has no id)."""
    for p in report.proposals:
        if p.id and p.id == proposal_id_:
            return p
    return None


def compare_buckets(policy_bucket_name: str | None, buckets: dict[str, Rate], min_samples: int,
                    failure_class: str = "") -> Proposal:
    """The proposal rule, pure. Change is proposed only when the policy bucket and a competing
    bucket both have n >= min_samples and their 95% intervals do not overlap. An interval that
    overlaps, or a bucket short of samples, is insufficient evidence, spelled out with the n."""
    if policy_bucket_name is None:
        return Proposal(failure_class, "not_applicable", "no automated retry in the policy table (human decides); nothing to tune")
    current = buckets.get(policy_bucket_name, Rate())
    others = {b: r for b, r in buckets.items() if b != policy_bucket_name and r.n > 0}
    if current.n < min_samples:
        tried = f"; other buckets tried: {', '.join(f'{b} n={r.n}' for b, r in others.items())}" if others else "; no other bucket tried"
        return Proposal(failure_class, "insufficient",
                        f"{INSUFFICIENT} (n={current.n} in the policy bucket {policy_bucket_name}, need {min_samples}{tried})")
    if not others:
        return Proposal(failure_class, "insufficient",
                        f"{INSUFFICIENT} (policy bucket {policy_bucket_name} n={current.n}; no other bucket has data to compare against)")
    lo_c, hi_c = current.interval
    better, worse, weak = [], [], []
    for b, r in sorted(others.items(), key=lambda kv: -kv[1].n):
        if r.n < min_samples:
            weak.append(f"{b} n={r.n} (need {min_samples})")
            continue
        lo_o, hi_o = r.interval
        if lo_o > hi_c:
            better.append((b, r))
        elif hi_o < lo_c:
            worse.append((b, r))
        else:
            weak.append(f"{b} n={r.n} overlaps ({r.text()} vs {current.text()})")
    if better:
        b, r = max(better, key=lambda br: br[1].rate or 0)
        return Proposal(failure_class, "proposal",
                        f"PROPOSAL: move first-attempt delay from bucket {policy_bucket_name} to {b}: "
                        f"{r.text()} vs {current.text()}; 95% intervals do not overlap. Not applied: a person edits app/policy.py.",
                        id=proposal_id(failure_class, policy_bucket_name, b), from_bucket=policy_bucket_name, to_bucket=b,
                        n_current=current.n, n_other=r.n)
    if worse and not weak:
        names = ", ".join(f"{b} {r.text()}" for b, r in worse)
        return Proposal(failure_class, "supports_current",
                        f"evidence supports the current bucket {policy_bucket_name} {current.text()} over {names}; no change")
    detail = "; ".join(weak) if weak else "no comparable bucket"
    return Proposal(failure_class, "insufficient", f"{INSUFFICIENT} ({detail})")


# ---- computation ---------------------------------------------------------------------------------

@dataclass
class ClassStats:
    failure_class: str
    attempts: int = 0
    sent: int = 0
    stubbed: int = 0
    human_queued: int = 0
    pending: int = 0
    recovered: int = 0
    recovered_paise: int = 0
    ttr_seconds: list[float] = field(default_factory=list)
    rate: Rate = field(default_factory=Rate)                       # recovered among sent links
    buckets: dict[str, Rate] = field(default_factory=dict)         # bucket -> recovered among sent links

    @property
    def human_share(self) -> float | None:
        return self.human_queued / self.attempts if self.attempts else None

    @property
    def avg_ttr_seconds(self) -> float | None:
        return sum(self.ttr_seconds) / len(self.ttr_seconds) if self.ttr_seconds else None

    @property
    def rules_gap(self) -> bool:
        """More than 30% of this class parked for a person. A class the policy table sends to a
        person by design (RISK_BLOCKED) is not a gap; UNKNOWN is the gap by definition, because
        every UNKNOWN in the queue is a description neither the rules nor the model resolved."""
        if self.failure_class != FailureClass.UNKNOWN.value and policy_bucket(self.failure_class) is None:
            return False
        return self.attempts >= 1 and (self.human_share or 0.0) > RULES_GAP_SHARE


@dataclass
class LLMStats:
    decisions: int = 0
    llm_used: int = 0
    by_source: Counter = field(default_factory=Counter)
    fallbacks: Counter = field(default_factory=Counter)
    latencies_ms: list[int] = field(default_factory=list)

    @property
    def share(self) -> float | None:
        return self.llm_used / self.decisions if self.decisions else None

    @property
    def median_ms(self) -> float | None:
        return percentile(self.latencies_ms, 0.5)

    @property
    def p95_ms(self) -> float | None:
        return percentile(self.latencies_ms, 0.95)


@dataclass
class Money:
    merchant_id: str
    attempts: int = 0
    at_risk_paise: int = 0
    recovered_paise: int = 0
    open_paise: int = 0
    parked_paise: int = 0


@dataclass
class Report:
    generated_at: datetime
    database: str
    merchant: str | None
    min_samples: int
    attempts: int
    outcomes: int
    classes: list[ClassStats]
    llm: LLMStats
    money: list[Money]
    proposals: list[Proposal]

    @property
    def empty(self) -> bool:
        return self.attempts == 0


def _latest(items):
    return max(items, key=lambda x: x.id) if items else None


def job_delay_seconds(job: RecoveryJob, decision: RecoveryDecision | None) -> int:
    """The delay a job was scheduled with. The decision row holds it exactly for the first attempt;
    a follow-up's delay is scheduled_at - created_at, rounded to the minute so clock drift at a
    bucket edge (48h + 0.4s) cannot tip it into the next bucket."""
    if decision is not None and job.retry_seq == 1:
        return int(decision.delay_seconds or 0)
    if job.scheduled_at and job.created_at:
        raw = (job.scheduled_at - job.created_at).total_seconds()
        return max(0, int(round(raw / MINUTE)) * MINUTE)
    return int(decision.delay_seconds or 0) if decision is not None else 0


def compute(session, *, min_samples: int = MIN_SAMPLES, merchant: str | None = None) -> Report:
    """Everything the report shows, from the database, in one pass over the attempts."""
    stmt = select(PaymentAttempt).order_by(PaymentAttempt.id)
    if merchant:
        stmt = stmt.where(PaymentAttempt.merchant_id == merchant)
    attempts = session.execute(stmt).scalars().all()
    ids = {a.id for a in attempts}

    decisions = session.execute(select(RecoveryDecision).order_by(RecoveryDecision.id)).scalars().all()
    decisions = [d for d in decisions if d.attempt_id in ids]
    by_id = {d.id: d for d in decisions}
    latest_decision: dict[int, RecoveryDecision] = {}
    for d in decisions:
        latest_decision[d.attempt_id] = d

    jobs = [j for j in session.execute(select(RecoveryJob).order_by(RecoveryJob.id)).scalars().all() if j.attempt_id in ids]
    latest_job: dict[int, RecoveryJob] = {}
    for j in jobs:
        latest_job[j.attempt_id] = j
    job_by_id = {j.id: j for j in jobs}

    outcomes = [o for o in session.execute(select(Outcome).order_by(Outcome.id)).scalars().all() if o.attempt_id in ids]
    recovered_outcome: dict[int, Outcome] = {}
    for o in outcomes:
        if o.recovered and o.attempt_id not in recovered_outcome:
            recovered_outcome[o.attempt_id] = o

    classes: dict[str, ClassStats] = {c.value: ClassStats(c.value) for c in FailureClass}
    money: dict[str, Money] = {}
    for a in attempts:
        d = latest_decision.get(a.id)
        cls_name = d.failure_class if d is not None else FailureClass.UNKNOWN.value
        cs = classes.setdefault(cls_name, ClassStats(cls_name))
        cs.attempts += 1
        m = money.setdefault(a.merchant_id or "-", Money(a.merchant_id or "-"))
        m.attempts += 1
        m.at_risk_paise += int(a.amount_paise or 0)

        j = latest_job.get(a.id)
        status = j.status if j is not None else None
        rec = recovered_outcome.get(a.id)
        if rec is not None:
            cs.recovered += 1
            cs.recovered_paise += int(rec.amount_recovered_paise or 0)
            m.recovered_paise += int(rec.amount_recovered_paise or 0)
            rj = job_by_id.get(rec.job_id) if rec.job_id else j
            if rj is not None and rj.executed_at and rec.recovered_at:
                cs.ttr_seconds.append(max(0.0, (rec.recovered_at - rj.executed_at).total_seconds()))
        if status == JobStatus.SENT.value:
            cs.sent += 1
            delay = job_delay_seconds(j, by_id.get(j.decision_id) if j.decision_id else d)
            b = bucket_for(delay)
            cs.rate.n += 1
            br = cs.buckets.setdefault(b, Rate())
            br.n += 1
            if rec is not None:
                cs.rate.k += 1
                br.k += 1
            else:
                m.open_paise += int(a.amount_paise or 0)
        elif status == JobStatus.STUBBED.value:
            cs.stubbed += 1
        elif status == JobStatus.HUMAN_QUEUE.value:
            cs.human_queued += 1
            m.parked_paise += int(a.amount_paise or 0)
        elif status == JobStatus.PENDING.value:
            cs.pending += 1

    llm = LLMStats(decisions=len(decisions))
    for d in decisions:
        llm.by_source[d.classified_by or "rules"] += 1
        if d.llm_used:
            llm.llm_used += 1
        if d.fallback_taken:
            llm.fallbacks[d.fallback_taken] += 1
        if d.llm_latency_ms is not None:
            llm.latencies_ms.append(int(d.llm_latency_ms))

    ordered = [classes[c.value] for c in FailureClass] + [cs for k, cs in classes.items()
                                                          if k not in {c.value for c in FailureClass}]
    proposals = [compare_buckets(policy_bucket(cs.failure_class), cs.buckets, min_samples, cs.failure_class)
                 for cs in ordered]
    return Report(generated_at=datetime.utcnow().replace(microsecond=0), database=db.url(), merchant=merchant,
                  min_samples=min_samples, attempts=len(attempts), outcomes=len(outcomes), classes=ordered, llm=llm,
                  money=sorted(money.values(), key=lambda mm: mm.merchant_id), proposals=proposals)


# ---- rendering -----------------------------------------------------------------------------------

def _table(headers: list[str], rows: list[list[str]], right: set[int] = frozenset()) -> str:
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]
    fmt = lambda cells: "  ".join((c.rjust(w) if i in right else c.ljust(w))  # noqa: E731
                                  for i, (c, w) in enumerate(zip(cells, widths))).rstrip()
    return "\n".join([fmt(headers), fmt(["-" * w for w in widths])] + [fmt(r) for r in rows])


def _md_table(headers: list[str], rows: list[list[str]]) -> str:
    esc = lambda c: c.replace("|", "\\|")  # noqa: E731
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join(esc(c) for c in r) + " |" for r in rows]
    return "\n".join(lines)


def human_seconds(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    s = int(round(seconds))
    if s < MINUTE:
        return f"{s}s"
    if s < HOUR:
        return f"{s // MINUTE}m{s % MINUTE:02d}s"
    return f"{s // HOUR}h{(s % HOUR) // MINUTE:02d}m"


def pct(value: float | None) -> str:
    return "-" if value is None else f"{value:.0%}"


def class_rows(report: Report) -> tuple[list[str], list[list[str]]]:
    headers = ["class", "attempts", "sent", "stubbed", "human", "human%", "recovered", "amount", "rate (95% Wilson)",
               "avg time-to-recovery", "flag"]
    rows = []
    for cs in report.classes:
        if cs.attempts == 0:
            continue
        rows.append([cs.failure_class, str(cs.attempts), str(cs.sent), str(cs.stubbed), str(cs.human_queued),
                     pct(cs.human_share), str(cs.recovered), format_rupees(cs.recovered_paise), cs.rate.text(),
                     human_seconds(cs.avg_ttr_seconds), "rules gap: see propose-rules" if cs.rules_gap else ""])
    return headers, rows


def bucket_rows(report: Report) -> tuple[list[str], list[list[str]]]:
    headers = ["class", "policy bucket", "policy delay"] + list(BUCKET_NAMES)
    rows = []
    for cs in report.classes:
        if not cs.buckets:
            continue
        pb = policy_bucket(cs.failure_class)
        try:
            delay = human_delay(POLICY[FailureClass(cs.failure_class)]["delay_seconds"])
        except (KeyError, ValueError):
            delay = "-"
        cells = [cs.failure_class, pb or "-", delay]
        for b in BUCKET_NAMES:
            r = cs.buckets.get(b)
            cells.append(("*" if b == pb else "") + (r.text() if r else "-"))
        rows.append(cells)
    return headers, rows


def llm_rows(report: Report) -> tuple[list[str], list[list[str]]]:
    l = report.llm
    headers = ["decisions", "model consulted", "share", "rules", "llm", "fallback", "latency median", "latency p95"]
    row = [str(l.decisions), str(l.llm_used), pct(l.share), str(l.by_source.get("rules", 0)), str(l.by_source.get("llm", 0)),
           str(l.by_source.get("fallback", 0)),
           "-" if l.median_ms is None else f"{l.median_ms:.0f} ms", "-" if l.p95_ms is None else f"{l.p95_ms:.0f} ms"]
    return headers, [row]


def fallback_rows(report: Report) -> tuple[list[str], list[list[str]]]:
    return ["fallback_taken", "count"], [[name, str(n)] for name, n in sorted(report.llm.fallbacks.items())]


def money_rows(report: Report) -> tuple[list[str], list[list[str]]]:
    headers = ["merchant", "attempts", "at risk", "recovered", "open (sent, unpaid)", "parked (human queue)"]
    rows = [[m.merchant_id, str(m.attempts), format_rupees(m.at_risk_paise), format_rupees(m.recovered_paise),
             format_rupees(m.open_paise), format_rupees(m.parked_paise)] for m in report.money]
    if len(rows) > 1:
        rows.append(["TOTAL", str(sum(m.attempts for m in report.money)),
                     format_rupees(sum(m.at_risk_paise for m in report.money)),
                     format_rupees(sum(m.recovered_paise for m in report.money)),
                     format_rupees(sum(m.open_paise for m in report.money)),
                     format_rupees(sum(m.parked_paise for m in report.money))])
    return headers, rows


def proposal_rows(report: Report) -> tuple[list[str], list[list[str]]]:
    return ["class", "verdict"], [[p.failure_class, p.text] for p in report.proposals
                                 if p.kind != "not_applicable" or any(c.failure_class == p.failure_class and c.attempts
                                                                       for c in report.classes)]


HEADER_NOTE = ("Every number below is an observation with a sample size; nothing here changes the agent. "
               "The policy table (app/policy.py) is code-reviewed; this report is evidence for the reviewer, not a bandit.")
RATE_NOTE = ("rate = recovered / sent links (a stubbed token retry cannot recover in test mode, so it is not in the denominator); "
             "interval = 95% Wilson score. * marks the bucket the policy table uses today.")


def _proposal_note(report: Report) -> str:
    return (f"A change is proposed only when the policy bucket and a competing bucket both have n >= {report.min_samples} "
            f"and their 95% intervals do not overlap. Proposals are printed, never applied.")


def render_text(report: Report) -> str:
    scope = f" (merchant {report.merchant})" if report.merchant else ""
    lines = [f"payment-recovery-agent insights{scope}", f"  database   : {report.database}",
             f"  generated  : {report.generated_at.isoformat()}Z",
             f"  min-samples: {report.min_samples}", f"  {HEADER_NOTE}", ""]
    if report.empty:
        lines.append("no outcomes yet: no payment attempts in this database. Run `python -m app.main demo` "
                     "or ingest real events, then come back.")
        return "\n".join(lines) + "\n"
    lines.append(f"== per failure class ({report.attempts} attempts, {report.outcomes} outcome rows)")
    lines.append(_table(*class_rows(report), right={1, 2, 3, 4, 5, 6, 7}))
    lines.append(f"  {RATE_NOTE}")
    lines.append("")
    lines.append("== recovery rate by scheduled delay bucket (sent links only)")
    h, rows = bucket_rows(report)
    lines.append(_table(h, rows) if rows else "(no sent links yet)")
    lines.append("")
    lines.append("== LLM usage")
    lines.append(_table(*llm_rows(report), right={0, 1, 2, 3, 4, 5}))
    h, rows = fallback_rows(report)
    lines.append(_table(h, rows, right={1}) if rows else "  fallbacks: none")
    lines.append("")
    lines.append("== money by merchant")
    lines.append(_table(*money_rows(report), right={1, 2, 3, 4, 5}))
    lines.append("")
    lines.append("== proposals (never applied)")
    lines.append(f"  {_proposal_note(report)}")
    for cls, text in proposal_rows(report)[1]:
        lines.append(f"  {cls:<20} {text}")
    gaps = [cs.failure_class for cs in report.classes if cs.rules_gap]
    if gaps:
        lines.append(f"  rules gap (human-queue share > {RULES_GAP_SHARE:.0%}): {', '.join(gaps)} -> "
                     "python -m app.rule_candidates")
    n_prop = sum(1 for p in report.proposals if p.is_proposal)
    lines.append(f"  {n_prop} proposal(s); {sum(1 for p in report.proposals if p.kind == 'insufficient')} class(es) with "
                 f"{INSUFFICIENT}.")
    return "\n".join(lines) + "\n"


def render_markdown(report: Report) -> str:
    scope = f" (merchant `{report.merchant}`)" if report.merchant else ""
    out = [f"# Insights report{scope}", "",
           f"Generated {report.generated_at.isoformat()}Z from `{report.database}` with min-samples {report.min_samples} "
           f"by `python -m app.insights`.", "", HEADER_NOTE, ""]
    if report.empty:
        out.append("**No outcomes yet**: no payment attempts in this database.")
        return "\n".join(out) + "\n"
    out += [f"## Per failure class ({report.attempts} attempts, {report.outcomes} outcome rows)", "",
            _md_table(*class_rows(report)), "", RATE_NOTE, "",
            "## Recovery rate by scheduled delay bucket (sent links only)", ""]
    h, rows = bucket_rows(report)
    out += [_md_table(h, rows) if rows else "(no sent links yet)", "", "## LLM usage", "", _md_table(*llm_rows(report)), ""]
    h, rows = fallback_rows(report)
    out += [_md_table(h, rows) if rows else "Fallbacks: none.", "", "## Money by merchant", "", _md_table(*money_rows(report)), "",
            "## Proposals (never applied)", "", _proposal_note(report), "", _md_table(*proposal_rows(report)), ""]
    gaps = [cs.failure_class for cs in report.classes if cs.rules_gap]
    if gaps:
        out.append(f"Rules gap (human-queue share > {RULES_GAP_SHARE:.0%}): {', '.join(gaps)}. "
                   "See `python -m app.rule_candidates`.")
    return "\n".join(out) + "\n"


# ---- CLI -----------------------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.insights", description=__doc__.split("\n\n")[0])
    p.add_argument("--write", metavar="PATH", help="also write the report as Markdown to this path")
    p.add_argument("--min-samples", type=int, default=MIN_SAMPLES,
                   help=f"both buckets need at least this many sent links before a proposal (default {MIN_SAMPLES})")
    p.add_argument("--merchant", help="restrict to one merchant_id")
    args = p.parse_args(argv)
    db.init_db()
    session = db.session()
    try:
        report = compute(session, min_samples=max(1, args.min_samples), merchant=args.merchant)
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
