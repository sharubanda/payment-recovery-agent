"""Baseline vs agent over synthetic failure events. Pure in-memory: no DB, no network, no keys.

Two policies run over the SAME events with the SAME coin flips, so the comparison is paired:
  baseline  "retry everything once at T+1h": token charge if a token exists, else a payment link;
            no idempotency, so a duplicate delivery is acted on twice.
  agent     app.policy.decide over the ground-truth class: delays per the policy table, stop
            rules honoured, RISK_BLOCKED and UNKNOWN parked for a person, duplicate deliveries
            rejected by the same sha256 idempotency key the executor uses.

Usage:  python scripts/simulate.py [--n 500] [--seed 42] [--write docs/simulation_report.md]

RECOVERY MODEL (hand-specified assumptions, not measurements; the report copies this table)
P(recover | class, action, delay bucket)

class               action         delay  P     note
INSUFFICIENT_FUNDS  link           48h    0.42  agent: link + nudge on the salary cycle
INSUFFICIENT_FUNDS  link           1h     0.14  baseline: the balance has not changed in an hour
INSUFFICIENT_FUNDS  token_retry    1h     0.08  baseline: same empty account, silently
INSUFFICIENT_FUNDS  token_retry    48h    0.35  unused: the table never token-retries this class
LIMIT_EXCEEDED      link           24h    0.45  agent: link + nudge once the daily limit window has reset
LIMIT_EXCEEDED      link           1h     0.12  baseline: the limit has not reset in an hour
LIMIT_EXCEEDED      token_retry    1h     0.10  baseline: same cap, silently
AUTH_ABANDONED      link           10m    0.38  agent: intent is still warm
AUTH_ABANDONED      link           1h     0.22  baseline
AUTH_ABANDONED      link           48h    0.08  unused: intent decays fast
AUTH_ABANDONED      token_retry    1h     0.10  baseline; own guess: the authentication the customer abandoned recurs
ISSUER_DOWN         token_retry    15m    0.70  agent: cumulative over the x3 backoff chain (15m, 45m, 2h15m)
ISSUER_DOWN         token_retry    1h     0.55  baseline
ISSUER_DOWN         link           15m    0.40  agent
ISSUER_DOWN         link           1h     0.30  baseline
NETWORK_TIMEOUT     token_retry    5m     0.75  agent: cumulative over the x2 backoff chain (5m, 10m)
NETWORK_TIMEOUT     token_retry    1h     0.55  baseline
NETWORK_TIMEOUT     link           5m     0.40  agent
NETWORK_TIMEOUT     link           1h     0.30  baseline
HARD_DECLINE        token_retry    any    0.00  the card can never authorise
HARD_DECLINE        change_method  now    0.12  agent: link that asks for a different method
HARD_DECLINE        link           1h     0.03  baseline: plain link back to the same dead card
RISK_BLOCKED        token_retry    any    0.00  every automated attempt is one policy violation
RISK_BLOCKED        link           any    0.00  every automated attempt is one policy violation
RISK_BLOCKED        human_queue    now    0.00  out of scope for the simulation
UNKNOWN             human_queue    now    0.00  agent: offline in the simulation (no LLM, no person)
UNKNOWN             token_retry    1h     0.05  baseline: a blind retry of an unestablished cause
UNKNOWN             link           1h     0.05  baseline: a blind retry of an unestablished cause

A chain's cumulative probability is split evenly per attempt (p = 1 - (1 - P) ** (1 / max_attempts))
so the chain as a whole recovers with P. A sent link is never re-issued (Razorpay's reminders
re-nudge), so a link is always exactly one attempt. Coins are derived from (seed, event index,
attempt number): a duplicate delivery reuses its original's coins because it fires at the same
moment against the same world, which is exactly what makes the baseline's second token charge a
double charge rather than a second chance.
"""
import argparse
import hashlib
import math
import random
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # importable from a clean clone, any cwd

from app import executor, policy  # noqa: E402
from app.nudge_templates import format_rupees  # noqa: E402
from app.taxonomy import Action, FailureClass  # noqa: E402

DEFAULT_N, DEFAULT_SEED = 500, 42
SENSITIVITY_SEEDS = (1, 2, 3, 4, 5)
BASELINE_DELAY_SECONDS = 3600
DUPLICATE_SHARE = 0.04
TOKEN_SHARE_OF_CARDS = 0.30
AMOUNT_RUPEES = (199, 24999)  # log-uniform between these

CLASS_MIX: dict[FailureClass, float] = {
    FailureClass.INSUFFICIENT_FUNDS: 0.25,
    FailureClass.AUTH_ABANDONED: 0.25,
    FailureClass.HARD_DECLINE: 0.15,
    FailureClass.ISSUER_DOWN: 0.12,
    FailureClass.NETWORK_TIMEOUT: 0.08,
    FailureClass.RISK_BLOCKED: 0.05,
    FailureClass.LIMIT_EXCEEDED: 0.05,
    FailureClass.UNKNOWN: 0.05,
}

# method mix per class (assumption): hard declines are a card thing, wallets mostly surface as risk holds
METHOD_MIX: dict[FailureClass, dict[str, float]] = {
    FailureClass.INSUFFICIENT_FUNDS: {"card": 0.45, "upi": 0.40, "netbanking": 0.15},
    FailureClass.LIMIT_EXCEEDED: {"upi": 0.50, "card": 0.40, "netbanking": 0.10},
    FailureClass.AUTH_ABANDONED: {"card": 0.45, "upi": 0.35, "netbanking": 0.20},
    FailureClass.HARD_DECLINE: {"card": 1.0},
    FailureClass.ISSUER_DOWN: {"card": 0.50, "netbanking": 0.30, "upi": 0.20},
    FailureClass.NETWORK_TIMEOUT: {"card": 0.50, "upi": 0.50},
    FailureClass.RISK_BLOCKED: {"card": 0.80, "wallet": 0.20},
    FailureClass.UNKNOWN: {"card": 0.60, "netbanking": 0.30, "upi": 0.10},
}

# (class, action kind, delay bucket) -> P(recover). Same numbers as the docstring table; a test pins that.
RECOVERY_MODEL: dict[tuple[FailureClass, str, str], float] = {
    (FailureClass.INSUFFICIENT_FUNDS, "link", "48h"): 0.42,
    (FailureClass.INSUFFICIENT_FUNDS, "link", "1h"): 0.14,
    (FailureClass.INSUFFICIENT_FUNDS, "token_retry", "1h"): 0.08,
    (FailureClass.INSUFFICIENT_FUNDS, "token_retry", "48h"): 0.35,
    (FailureClass.LIMIT_EXCEEDED, "link", "24h"): 0.45,
    (FailureClass.LIMIT_EXCEEDED, "link", "1h"): 0.12,
    (FailureClass.LIMIT_EXCEEDED, "token_retry", "1h"): 0.10,
    (FailureClass.AUTH_ABANDONED, "link", "10m"): 0.38,
    (FailureClass.AUTH_ABANDONED, "link", "1h"): 0.22,
    (FailureClass.AUTH_ABANDONED, "link", "48h"): 0.08,
    (FailureClass.AUTH_ABANDONED, "token_retry", "1h"): 0.10,
    (FailureClass.ISSUER_DOWN, "token_retry", "15m"): 0.70,
    (FailureClass.ISSUER_DOWN, "token_retry", "1h"): 0.55,
    (FailureClass.ISSUER_DOWN, "link", "15m"): 0.40,
    (FailureClass.ISSUER_DOWN, "link", "1h"): 0.30,
    (FailureClass.NETWORK_TIMEOUT, "token_retry", "5m"): 0.75,
    (FailureClass.NETWORK_TIMEOUT, "token_retry", "1h"): 0.55,
    (FailureClass.NETWORK_TIMEOUT, "link", "5m"): 0.40,
    (FailureClass.NETWORK_TIMEOUT, "link", "1h"): 0.30,
    (FailureClass.HARD_DECLINE, "token_retry", "any"): 0.00,
    (FailureClass.HARD_DECLINE, "change_method", "now"): 0.12,
    (FailureClass.HARD_DECLINE, "link", "1h"): 0.03,
    (FailureClass.RISK_BLOCKED, "token_retry", "any"): 0.00,
    (FailureClass.RISK_BLOCKED, "link", "any"): 0.00,
    (FailureClass.RISK_BLOCKED, "human_queue", "now"): 0.00,
    (FailureClass.UNKNOWN, "human_queue", "now"): 0.00,
    (FailureClass.UNKNOWN, "token_retry", "1h"): 0.05,
    (FailureClass.UNKNOWN, "link", "1h"): 0.05,
}

# a retry of the same instrument on these classes cannot succeed or is a blind guess: every one is waste
NON_RECOVERABLE_BY_RETRY = frozenset({FailureClass.HARD_DECLINE, FailureClass.RISK_BLOCKED, FailureClass.UNKNOWN})

KIND_OF_ACTION = {
    Action.TOKEN_RETRY: "token_retry",
    Action.RECOVERY_LINK: "link",
    Action.NUDGE_CHANGE_METHOD: "change_method",
    Action.HUMAN_QUEUE: "human_queue",
    Action.NO_ACTION: "no_action",
}

CAVEAT = ("Measured on {n} synthetic failure events with a hand-specified recovery model. This is a simulation, "
          "not production data, and the absolute numbers depend on the assumptions stated below (and in "
          "scripts/simulate.py). The comparison between policies is meaningful; the absolute recovery rate is not.")

_BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


@dataclass(frozen=True)
class Event:
    index: int              # the underlying payment's position; coins key off this, so a duplicate shares them
    payment_id: str
    failure_class: FailureClass
    method: str
    has_token: bool
    amount_paise: int
    duplicate: bool = False  # a re-delivery of an earlier event: same payment_id, same index


@dataclass
class Tally:
    policy: str
    events: int = 0                 # deliveries, duplicates included
    payments: int = 0               # unique payments
    attempts: int = 0               # outbound automated attempts: token charges + links created
    recovered: int = 0
    amount_recovered_paise: int = 0
    wasted_attempts: int = 0
    risk_violations: int = 0
    duplicate_charges: int = 0      # a customer charged twice for one payment
    double_charged_paise: int = 0
    duplicate_links: int = 0        # a second link created for one payment
    duplicates_acted_on: int = 0
    duplicates_rejected: int = 0    # by the idempotency key
    human_queued: int = 0

    @property
    def recovery_rate(self) -> float:
        return self.recovered / self.payments if self.payments else 0.0

    @property
    def attempts_per_recovery(self) -> float:
        return self.attempts / self.recovered if self.recovered else math.inf


def coin(seed: int, index: int, attempt_no: int) -> float:
    """Uniform in [0, 1), fixed by (seed, event, attempt): both policies see the same coin for the same event."""
    digest = hashlib.sha256(f"{seed}:{index}:{attempt_no}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") / 2 ** 64


def p_recover(failure_class: FailureClass, kind: str, bucket: str) -> float:
    """A missing cell is a modelling bug: raise rather than default to some number."""
    for key in ((failure_class, kind, bucket), (failure_class, kind, "any")):
        if key in RECOVERY_MODEL:
            return RECOVERY_MODEL[key]
    raise KeyError(f"recovery model has no cell for {failure_class.value}/{kind}@{bucket}")


def _weighted(rng: random.Random, table: dict):
    keys = list(table)
    return rng.choices(keys, weights=[table[k] for k in keys], k=1)[0]


def generate_events(n: int, seed: int) -> list[Event]:
    """n deliveries: round(4%) of them are duplicates of an earlier event, inserted after their original."""
    rng = random.Random(seed)
    n_dup = int(round(n * DUPLICATE_SHARE)) if n >= 2 else 0
    originals: list[Event] = []
    used_ids: set[str] = set()
    for index in range(n - n_dup):
        cls = _weighted(rng, CLASS_MIX)
        method = _weighted(rng, METHOD_MIX[cls])
        has_token = method == "card" and rng.random() < TOKEN_SHARE_OF_CARDS
        rupees = int(round(math.exp(rng.uniform(math.log(AMOUNT_RUPEES[0]), math.log(AMOUNT_RUPEES[1])))))
        while True:
            payment_id = "pay_" + "".join(rng.choices(_BASE62, k=14))
            if payment_id not in used_ids:
                used_ids.add(payment_id)
                break
        originals.append(Event(index, payment_id, cls, method, has_token, rupees * 100))

    stream = list(originals)
    for _ in range(n_dup):
        original = originals[rng.randrange(len(originals))]
        position = next(i for i, e in enumerate(stream) if e is original)
        dup = Event(original.index, original.payment_id, original.failure_class, original.method,
                    original.has_token, original.amount_paise, duplicate=True)
        stream.insert(rng.randint(position + 1, len(stream)), dup)
    return stream


def _count(tally: Tally, events: list[Event]) -> None:
    tally.events = len(events)
    tally.payments = sum(1 for e in events if not e.duplicate)


def run_baseline(events: list[Event], seed: int) -> Tally:
    """Retry everything once at T+1h, no idempotency, no stop rules, no human queue."""
    t = Tally("baseline")
    _count(t, events)
    recovered_ids: set[str] = set()
    for e in events:
        kind = "token_retry" if e.has_token else "link"
        p = p_recover(e.failure_class, kind, policy.human_delay(BASELINE_DELAY_SECONDS))
        t.attempts += 1
        if e.failure_class is FailureClass.RISK_BLOCKED:
            t.risk_violations += 1
        if e.duplicate:
            # same coin as the original: the second attempt lands the same way the first did
            t.duplicates_acted_on += 1
            t.wasted_attempts += 1
            if kind == "token_retry":
                if e.payment_id in recovered_ids:
                    t.duplicate_charges += 1
                    t.double_charged_paise += e.amount_paise
            else:
                t.duplicate_links += 1
            continue
        if e.failure_class in NON_RECOVERABLE_BY_RETRY:
            t.wasted_attempts += 1
        if coin(seed, e.index, 1) < p:
            recovered_ids.add(e.payment_id)
            t.recovered += 1
            t.amount_recovered_paise += e.amount_paise
    return t


def run_agent(events: list[Event], seed: int) -> Tally:
    """The policy table over the ground-truth class, idempotency-keyed exactly like the executor."""
    t = Tally("agent")
    _count(t, events)
    seen_keys: set[str] = set()
    for e in events:
        first_key = executor.idempotency_key(e.payment_id, 1)
        if first_key in seen_keys:
            t.duplicates_rejected += 1  # the UNIQUE constraint would reject this INSERT before any outbound call
            continue
        seen_keys.add(first_key)

        first = policy.decide(e.failure_class, has_token=e.has_token, retry_seq=1)
        if first.action is Action.HUMAN_QUEUE:
            t.human_queued += 1
            continue
        if first.action is Action.NO_ACTION:
            continue
        kind = KIND_OF_ACTION[first.action]
        cumulative = p_recover(e.failure_class, kind, policy.human_delay(first.delay_seconds))
        # a token chain gets max_attempts tries whose cumulative success is the table value; a link gets one
        per_attempt = cumulative if kind != "token_retry" else 1 - (1 - cumulative) ** (1 / first.max_attempts)

        seq = 1
        while True:
            d = policy.decide(e.failure_class, has_token=e.has_token, retry_seq=seq)
            if d.action is Action.NO_ACTION:
                break  # stop rule: max attempts reached
            seen_keys.add(executor.idempotency_key(e.payment_id, seq))
            t.attempts += 1
            if e.failure_class is FailureClass.RISK_BLOCKED:
                t.risk_violations += 1  # unreachable by construction; counted so the table proves it, not the code
            if kind in ("token_retry", "link") and e.failure_class in NON_RECOVERABLE_BY_RETRY:
                t.wasted_attempts += 1
            if coin(seed, e.index, seq) < per_attempt:
                t.recovered += 1
                t.amount_recovered_paise += e.amount_paise
                break
            if kind != "token_retry":
                break  # a sent link is never re-issued
            seq += 1
    return t


def simulate(n: int, seed: int) -> tuple[list[Event], Tally, Tally]:
    events = generate_events(n, seed)
    return events, run_baseline(events, seed), run_agent(events, seed)


def sensitivity(n: int, seeds=SENSITIVITY_SEEDS) -> list[int]:
    """Recovered-count delta (agent - baseline) per seed, so the report can show the gap is not a seed artefact."""
    return [a.recovered - b.recovered for _, b, a in (simulate(n, s) for s in seeds)]


def _rate(x: float) -> str:
    return f"{100 * x:.1f}%"


def _ratio(x: float) -> str:
    return "n/a" if math.isinf(x) else f"{x:.2f}"


def metric_rows(b: Tally, a: Tally) -> list[tuple[str, str, str, str]]:
    """(label, baseline, agent, delta) with deltas as agent minus baseline."""
    def num(attr):
        vb, va = getattr(b, attr), getattr(a, attr)
        return str(vb), str(va), f"{va - vb:+d}"

    def money(attr):
        vb, va = getattr(b, attr), getattr(a, attr)
        sign = "+" if va >= vb else "-"
        return format_rupees(vb), format_rupees(va), f"{sign}{format_rupees(abs(va - vb))}"

    rows = [
        ("events (deliveries)", *num("events")),
        ("unique payments", *num("payments")),
        ("attempts made", *num("attempts")),
        ("recovered", *num("recovered")),
        ("recovery rate", _rate(b.recovery_rate), _rate(a.recovery_rate),
         f"{100 * (a.recovery_rate - b.recovery_rate):+.1f} pp"),
        ("amount recovered", *money("amount_recovered_paise")),
        ("attempts per recovery", _ratio(b.attempts_per_recovery), _ratio(a.attempts_per_recovery),
         "" if math.isinf(b.attempts_per_recovery) or math.isinf(a.attempts_per_recovery)
         else f"{a.attempts_per_recovery - b.attempts_per_recovery:+.2f}"),
        ("wasted attempts", *num("wasted_attempts")),
        ("risk-block violations", *num("risk_violations")),
        ("duplicate charges", *num("duplicate_charges")),
        ("amount double-charged", *money("double_charged_paise")),
        ("duplicate links", *num("duplicate_links")),
        ("duplicates acted on", *num("duplicates_acted_on")),
        ("duplicates rejected (idempotency key)", *num("duplicates_rejected")),
        ("human-queued", *num("human_queued")),
    ]
    return rows


def class_counts(events: list[Event]) -> Counter:
    return Counter(e.failure_class for e in events if not e.duplicate)


def text_table(rows: list[tuple[str, str, str, str]]) -> str:
    widths = [max(len(r[i]) for r in rows) for i in range(4)]
    return "\n".join(f"{r[0]:<{widths[0]}}  {r[1]:>{widths[1]}}  {r[2]:>{widths[2]}}  {r[3]:>{widths[3]}}" for r in rows)


def summary_text(n: int, seed: int, events: list[Event], b: Tally, a: Tally, deltas: list[int]) -> str:
    counts = class_counts(events)
    dups = sum(1 for e in events if e.duplicate)
    tokens = sum(1 for e in events if not e.duplicate and e.has_token)
    lines = [
        f"simulate: {n} deliveries = {b.payments} unique payments + {dups} duplicate deliveries; "
        f"{tokens} with a saved token; seed {seed}",
        "class mix: " + ", ".join(f"{c.value} {counts.get(c, 0)}" for c in CLASS_MIX),
        "",
        text_table([("metric", "baseline", "agent", "delta")] + metric_rows(b, a)),
        "",
        sensitivity_line(deltas),
    ]
    return "\n".join(lines)


def sensitivity_line(deltas: list[int]) -> str:
    seeds = f"{SENSITIVITY_SEEDS[0]}..{SENSITIVITY_SEEDS[-1]}"
    verdict = "never negative" if min(deltas) > 0 else "NOT robust: the gap flips sign on some seed"
    return (f"sensitivity: recovered-count delta (agent - baseline) over seeds {seeds}: "
            f"min {min(deltas):+d}, max {max(deltas):+d} ({verdict})")


def model_table_from_docstring() -> str:
    """The table between the 'class ... note' header and the next blank line, verbatim."""
    lines = (__doc__ or "").splitlines()
    start = next(i for i, l in enumerate(lines) if re.match(r"^class\s+action\s+delay\s+P\s+note", l))
    end = next(i for i in range(start, len(lines)) if not lines[i].strip())
    return "\n".join(lines[start:end])


def report_markdown(n: int, seed: int, events: list[Event], b: Tally, a: Tally, deltas: list[int]) -> str:
    counts = class_counts(events)
    dups = sum(1 for e in events if e.duplicate)
    tokens = sum(1 for e in events if not e.duplicate and e.has_token)
    md_rows = "\n".join(f"| {r[0]} | {r[1]} | {r[2]} | {r[3]} |" for r in metric_rows(b, a))
    mix_rows = "\n".join(f"| {c.value} | {100 * share:.0f}% | {counts.get(c, 0)} |" for c, share in CLASS_MIX.items())
    delta_list = ", ".join(f"seed {s}: {d:+d}" for s, d in zip(SENSITIVITY_SEEDS, deltas))
    return f"""# Simulation: baseline retry-everything vs the policy table

{CAVEAT.format(n=n)}

Generated by `python scripts/simulate.py --n {n} --seed {seed} --write docs/simulation_report.md`.
Pure in-memory: no database, no network, no keys; deterministic under `--seed`.

## Policies compared

- **Baseline** ("retry everything once at T+1h"): a token charge if the payment has a saved token, otherwise a
  payment link; every class treated the same, no stop rules, no human queue, and no idempotency, so a duplicate
  delivery of the same failed-payment event is acted on twice.
- **Agent**: `app.policy.decide` over the ground-truth failure class (classification is assumed perfect here);
  delays and stop rules per the policy table; RISK_BLOCKED and UNKNOWN go to a person; duplicate deliveries are
  rejected by the same `sha256(payment_id:retry_seq)` idempotency key the executor inserts under a UNIQUE
  constraint before any outbound call.

Both policies see the same {n} deliveries and the same coin flips (derived from the seed, the event index and the
attempt number), so the comparison is paired.

## Results (seed {seed})

{n} deliveries = {b.payments} unique payments + {dups} duplicate deliveries; {tokens} payments carry a saved token.

| metric | baseline | agent | delta (agent - baseline) |
|---|---:|---:|---:|
{md_rows}

## Sensitivity

{sensitivity_line(deltas)}

Per seed: {delta_list}.
Each seed regenerates the events and the coins, so the gap is a property of the policies, not of one draw.

## Recovery model (assumptions)

Copied verbatim from the docstring at the top of `scripts/simulate.py`; the code reads the same numbers from
`RECOVERY_MODEL` and a test pins the two together.

```
{model_table_from_docstring()}
```

A chain's cumulative probability is split evenly per attempt (`p = 1 - (1 - P) ** (1 / max_attempts)`) so the chain
as a whole recovers with P. A sent link is never re-issued (Razorpay's reminders re-nudge), so a link is always one
attempt. A duplicate delivery reuses its original's coins because it fires at the same moment against the same
world; that is what makes the baseline's second token charge a double charge rather than a second chance.

## Event mix (assumptions)

| class | share | generated (seed {seed}) |
|---|---:|---:|
{mix_rows}

Amounts are log-uniform between Rs {AMOUNT_RUPEES[0]:,} and Rs {AMOUNT_RUPEES[1]:,}.
{100 * TOKEN_SHARE_OF_CARDS:.0f}% of card payments carry a saved token (UPI, net banking and wallets never do).
{100 * DUPLICATE_SHARE:.0f}% of deliveries are duplicates of an earlier event.

## Definitions

- **attempts made**: outbound automated attempts, token charges plus links created, duplicates included.
- **wasted attempt**: a token charge or plain link on a class where retrying the same instrument cannot succeed or is
  a blind guess (HARD_DECLINE, RISK_BLOCKED, UNKNOWN), plus every attempt triggered by a duplicate delivery. The
  3-5% that blind retries do recover in the model still count as waste: the metric is issuer attempts spent without
  an established cause. The agent's change-method link on a hard decline is not a retry of the dead card.
- **risk-block violation**: any automated attempt on a RISK_BLOCKED payment (the spec: never auto-retry a risk block).
- **duplicate charge**: a token charge made for a payment the customer had already paid (the double charge).
- **duplicate link**: a second payment link created for one payment.
- **human-queued**: payments parked for a person; they recover nothing inside the simulation.
- **recovery rate**: recovered / unique payments.

## Where the simulation is wrong

- The probabilities are hand-picked. The ordering (48h beats 1h for insufficient funds, minutes beat an hour for
  abandonment and outages) is the claim; the magnitudes are not.
- Classification is assumed perfect: the agent decides on the ground-truth class. Misclassification by the rules or
  the LLM is not modelled, and UNKNOWN (5%) recovers nothing because the human queue is offline here.
- A duplicate delivery is assumed to arrive at the same moment as the original. A redelivery hours later could behave
  differently; the idempotency key rejects it either way.
- A backoff chain splits its cumulative probability evenly per attempt. Real per-attempt odds rise as an outage ends,
  which would shorten the agent's chains and lower its attempt count.
- The CLI stubs token retries (no real tokens in test mode); the simulation models the chain the policy table
  describes, so the agent's attempt count is what the policy would spend, not what the demo executes.
- Nudges are folded into the link probabilities; the effect of message quality (LLM vs template) is not modelled.
- Amounts are independent of class and outcome; real insufficient-funds failures skew to larger tickets.
- Wasted attempts and risk violations are counted, not priced: issuer decline-rate penalties, network fees and
  review time for the human queue are all outside the model.
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="baseline retry-everything vs the policy table over synthetic failure events")
    p.add_argument("--n", type=int, default=DEFAULT_N, help=f"number of deliveries to simulate (default {DEFAULT_N})")
    p.add_argument("--seed", type=int, default=DEFAULT_SEED, help=f"random seed (default {DEFAULT_SEED})")
    p.add_argument("--write", metavar="PATH", help="also write the Markdown report to this path")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.n < 1:
        print("--n must be at least 1", file=sys.stderr)
        return 1
    events, b, a = simulate(args.n, args.seed)
    deltas = sensitivity(args.n)
    print(summary_text(args.n, args.seed, events, b, a, deltas))
    if args.write:
        path = Path(args.write)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(report_markdown(args.n, args.seed, events, b, a, deltas), encoding="utf-8")
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
