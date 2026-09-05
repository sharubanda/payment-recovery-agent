"""Expected-value prioritisation: which failed payment is worth a person's next ten minutes.

    expected_recovery(attempt, failure_class, action) = amount_paise * P(recover | class, action)

P comes from one of two sources, and every estimate says which:

  simulation prior   the hand-specified recovery model in scripts/simulate.py (RECOVERY_MODEL).
                     It is an ASSUMPTION, not a measurement: the same table the simulation report
                     carries its caveat for. Good enough to order a queue, never a forecast.
  empirical          app/insights.py's recovered-among-sent-links rate for the class, used instead
                     of the prior once the class has n >= EMPIRICAL_MIN_SAMPLES (30) sent links.
                     Below that the interval is too wide to beat a stated assumption.

For an attempt parked in the human queue (action human_queue) or closed (no_action) the prior
for the action itself is 0 by construction, which would make every parked row worth nothing.
The queue is ordered by what a person could recover by doing the table's action by hand, so the
estimate uses the class's own table action (with the attempt's token state) and says so.

Money is in paise throughout; format_rupees renders it. Nothing here writes to the database.
"""
import sys
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config, insights, policy
from .models import PaymentAttempt, RecoveryJob
from .nudge_templates import format_rupees
from .taxonomy import Action, FailureClass, JobStatus, coerce

EMPIRICAL_MIN_SAMPLES = insights.MIN_SAMPLES  # 30: the same bar insights.py sets for a proposal
PRIOR_SOURCE = "simulation prior"
PRIOR_NOTE = ("expected recovery is an estimate: amount x P(recover | class, action) from the hand-specified "
              "simulation prior in scripts/simulate.py, replaced by the measured rate for a class once it has "
              f"n >= {EMPIRICAL_MIN_SAMPLES} sent links (app/insights.py)")

# Copied from scripts/simulate.py RECOVERY_MODEL (the "RECOVERY MODEL" table in its docstring), used
# only if that module cannot be imported; tests/test_priority.py pins the two tables equal so they
# cannot drift. Keys: (failure class, action kind, delay bucket as policy.human_delay renders it).
_PRIOR_FALLBACK: dict[tuple[str, str, str], float] = {
    ("INSUFFICIENT_FUNDS", "link", "48h"): 0.42, ("INSUFFICIENT_FUNDS", "link", "1h"): 0.14,
    ("INSUFFICIENT_FUNDS", "token_retry", "1h"): 0.08, ("INSUFFICIENT_FUNDS", "token_retry", "48h"): 0.35,
    ("LIMIT_EXCEEDED", "link", "24h"): 0.45, ("LIMIT_EXCEEDED", "link", "1h"): 0.12,
    ("LIMIT_EXCEEDED", "token_retry", "1h"): 0.10,
    ("AUTH_ABANDONED", "link", "10m"): 0.38, ("AUTH_ABANDONED", "link", "1h"): 0.22,
    ("AUTH_ABANDONED", "link", "48h"): 0.08, ("AUTH_ABANDONED", "token_retry", "1h"): 0.10,
    ("ISSUER_DOWN", "token_retry", "15m"): 0.70, ("ISSUER_DOWN", "token_retry", "1h"): 0.55,
    ("ISSUER_DOWN", "link", "15m"): 0.40, ("ISSUER_DOWN", "link", "1h"): 0.30,
    ("NETWORK_TIMEOUT", "token_retry", "5m"): 0.75, ("NETWORK_TIMEOUT", "token_retry", "1h"): 0.55,
    ("NETWORK_TIMEOUT", "link", "5m"): 0.40, ("NETWORK_TIMEOUT", "link", "1h"): 0.30,
    ("HARD_DECLINE", "token_retry", "any"): 0.00, ("HARD_DECLINE", "change_method", "now"): 0.12,
    ("HARD_DECLINE", "link", "1h"): 0.03,
    ("RISK_BLOCKED", "token_retry", "any"): 0.00, ("RISK_BLOCKED", "link", "any"): 0.00,
    ("RISK_BLOCKED", "human_queue", "now"): 0.00,
    ("UNKNOWN", "human_queue", "now"): 0.00, ("UNKNOWN", "token_retry", "1h"): 0.05, ("UNKNOWN", "link", "1h"): 0.05,
}
KIND_OF_ACTION = {Action.TOKEN_RETRY: "token_retry", Action.RECOVERY_LINK: "link", Action.NUDGE_CHANGE_METHOD: "change_method",
                  Action.HUMAN_QUEUE: "human_queue", Action.NO_ACTION: "no_action"}

_prior_cache: dict[tuple[str, str, str], float] | None = None
_prior_origin: str = ""


def prior_table() -> dict[tuple[str, str, str], float]:
    """scripts/simulate.py's RECOVERY_MODEL when importable (it has no import-time side effects
    beyond a sys.path insert), else the copy above. prior_origin() says which."""
    global _prior_cache, _prior_origin
    if _prior_cache is not None:
        return _prior_cache
    try:
        if str(config.BASE_DIR) not in sys.path:  # scripts/ is a sibling of app/, as app.main's seed does it
            sys.path.insert(0, str(config.BASE_DIR))
        from scripts.simulate import RECOVERY_MODEL  # noqa: WPS433
        _prior_cache = {(c.value, k, b): p for (c, k, b), p in RECOVERY_MODEL.items()}
        _prior_origin = "scripts/simulate.py RECOVERY_MODEL"
    except Exception:  # a clone without scripts/, or a simulate.py mid-edit: the copy is the same numbers
        _prior_cache = dict(_PRIOR_FALLBACK)
        _prior_origin = "app/priority.py copy of scripts/simulate.py RECOVERY_MODEL"
    return _prior_cache


def prior_origin() -> str:
    prior_table()
    return _prior_origin


def prior_probability(failure_class: FailureClass | str, action: Action | str, delay_seconds: int | None = None) -> float:
    """P(recover) from the simulation prior for (class, action) at the table's delay bucket (or the
    given delay). Falls back to the class/action "any" cell, then to 0.0: an unmodelled cell is
    worth nothing rather than something invented."""
    cls = coerce(failure_class, FailureClass, FailureClass.UNKNOWN)
    act = coerce(action, Action, Action.HUMAN_QUEUE)
    kind = KIND_OF_ACTION[act]
    delay = policy.delay_for(cls, 1) if delay_seconds is None else max(0, int(delay_seconds))
    table = prior_table()
    for key in ((cls.value, kind, policy.human_delay(delay)), (cls.value, kind, "any")):
        if key in table:
            return float(table[key])
    # a delay the model has no cell for (an override's 24h on a class modelled at 48h, or a class the
    # table never links): the most pessimistic cell the model has for this class and action, or 0.0
    cells = [p for (c, k, _), p in table.items() if c == cls.value and k == kind]
    return float(min(cells)) if cells else 0.0


@dataclass(frozen=True)
class Estimate:
    amount_paise: int
    probability: float
    source: str            # "simulation prior" | "empirical (n=..)"
    basis: str             # "recovery_link@48h" etc.: the (action, delay) the probability is for
    as_if: bool = False    # True when the action was human_queue/no_action and the table action stood in

    @property
    def expected_paise(self) -> int:
        return int(round(self.amount_paise * self.probability))

    def text(self) -> str:
        return (f"{format_rupees(self.expected_paise)} = {format_rupees(self.amount_paise)} x {self.probability:.2f} "
                f"[{self.source}, {'as if ' if self.as_if else ''}{self.basis}]")


def empirical_rates(session: Session) -> dict[str, insights.Rate]:
    """class -> recovered-among-sent-links rate from insights.compute (one pass; compute once per batch)."""
    report = insights.compute(session)
    return {cs.failure_class: cs.rate for cs in report.classes if cs.rate.n}


def expected_recovery(attempt, failure_class: FailureClass | str, action: Action | str, *,
                      delay_seconds: int | None = None, has_token: bool | None = None,
                      empirical: dict[str, insights.Rate] | None = None) -> Estimate:
    """amount_paise * P(recover | class, action), with the source named. `empirical` is the dict
    from empirical_rates(session); a class with n >= EMPIRICAL_MIN_SAMPLES uses its measured rate."""
    cls = coerce(failure_class, FailureClass, FailureClass.UNKNOWN)
    act = coerce(action, Action, Action.HUMAN_QUEUE)
    amount = int(getattr(attempt, "amount_paise", 0) or 0)
    token = bool(getattr(attempt, "has_token", False)) if has_token is None else bool(has_token)
    as_if = False
    if act in policy.TERMINAL_ACTIONS:
        table = policy.decide(cls, has_token=token)  # what the table would do if a person acted
        if table.action in policy.TERMINAL_ACTIONS:
            entry = policy.POLICY[cls]
            act = entry["action"] if entry["action"] not in policy.TERMINAL_ACTIONS else Action.RECOVERY_LINK
        else:
            act = table.action
        delay_seconds = policy.delay_for(cls, 1)
        as_if = True
    delay = policy.delay_for(cls, 1) if delay_seconds is None else max(0, int(delay_seconds))
    basis = f"{act.value}@{policy.human_delay(delay)}"
    rate = (empirical or {}).get(cls.value)
    if rate is not None and rate.n >= EMPIRICAL_MIN_SAMPLES and rate.rate is not None:
        return Estimate(amount, float(rate.rate), f"empirical (n={rate.n})", basis, as_if)
    return Estimate(amount, prior_probability(cls, act, delay), PRIOR_SOURCE, basis, as_if)


# ---- the queue and the batch, ordered by value --------------------------------------------------

@dataclass
class QueueItem:
    attempt: PaymentAttempt
    job: RecoveryJob
    failure_class: str
    classified_by: str
    fallback_taken: str | None
    reason: str
    queued_at: datetime | None
    estimate: Estimate


def _latest(items):
    return max(items, key=lambda x: x.id) if items else None


def human_queue(session: Session, *, empirical: dict[str, insights.Rate] | None = None) -> list[QueueItem]:
    """Attempts whose latest job is human_queue, highest expected recovery first (ties: oldest first)."""
    latest_jobs: dict[int, RecoveryJob] = {}
    for j in session.execute(select(RecoveryJob)).scalars().all():
        if j.attempt_id not in latest_jobs or j.id > latest_jobs[j.attempt_id].id:
            latest_jobs[j.attempt_id] = j
    rates = empirical_rates(session) if empirical is None else empirical
    items = []
    for j in latest_jobs.values():
        if j.status != JobStatus.HUMAN_QUEUE.value:
            continue
        a = j.attempt
        d = _latest(a.decisions)
        cls = d.failure_class if d else FailureClass.UNKNOWN.value
        est = expected_recovery(a, cls, Action.HUMAN_QUEUE, empirical=rates)
        items.append(QueueItem(a, j, cls, d.classified_by if d else "-", d.fallback_taken if d else None,
                               j.last_error or (d.reason if d else "-"), j.created_at, est))
    items.sort(key=lambda it: (-it.estimate.expected_paise, it.queued_at or datetime.min, it.job.id))
    return items


def rank_summaries(session: Session, summaries: list[dict]) -> list[tuple[dict, Estimate]]:
    """Pipeline summaries (pipeline._summary dicts) with an estimate each, highest first. Duplicates
    (skipped_duplicate) carry no new value and are left out."""
    rates = empirical_rates(session)
    ranked = []
    for s in summaries:
        if s.get("duplicate") or s.get("job_status") == JobStatus.SKIPPED_DUPLICATE.value:
            continue
        attempt = session.get(PaymentAttempt, s["attempt_id"])
        if attempt is None:
            continue
        est = expected_recovery(attempt, s.get("failure_class"), s.get("action"), delay_seconds=s.get("delay_seconds"),
                                empirical=rates)
        ranked.append((s, est))
    ranked.sort(key=lambda pair: (-pair[1].expected_paise, pair[0].get("attempt_id") or 0))
    return ranked


def batch_lines(session: Session, summaries: list[dict], top: int = 5) -> list[str]:
    """The lines after a batch's totals: total expected recovery and the top N, labelled as an estimate."""
    ranked = rank_summaries(session, summaries)
    if not ranked:
        return []
    total = sum(est.expected_paise for _, est in ranked)
    sources = sorted({est.source.split(" (")[0] for _, est in ranked})
    lines = [f"expected recovery (estimate, {'/'.join(sources)}): {format_rupees(total)} over {len(ranked)} event(s); "
             f"top {min(top, len(ranked))} by expected recovery:"]
    for s, est in ranked[:top]:
        lines.append(f"  {s['payment_id']:<18} {s['failure_class'] + '/' + s['action']:<40} {est.text()}")
    lines.append(f"  note: {PRIOR_NOTE}")
    return lines
