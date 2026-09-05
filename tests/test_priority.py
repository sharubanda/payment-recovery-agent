"""Expected-value prioritisation: the prior is the simulation's table (pinned equal to the copy),
the source swaps to insights' empirical rate at n >= 30 and says so, a parked attempt is valued
as if a person took the table's action, and the queue and batch orderings are by value."""
from datetime import datetime

import pytest

from app import insights, priority, razorpay_client
from app.insights import Rate
from app.models import PaymentAttempt
from app.pipeline import process_all
from app.taxonomy import Action, FailureClass, JobStatus
from scripts import simulate
from scripts.seed import seed

NOW = datetime(2026, 9, 5, 12, 0, 0)
NOSLEEP = lambda s: None  # noqa: E731


class Attempt:
    def __init__(self, amount_paise: int, has_token: bool = False):
        self.amount_paise = amount_paise
        self.has_token = has_token


def test_prior_is_the_simulation_model_and_the_copy_cannot_drift():
    table = priority.prior_table()
    assert table == {(c.value, k, b): p for (c, k, b), p in simulate.RECOVERY_MODEL.items()}
    assert priority._PRIOR_FALLBACK == table  # the fallback copy in app/priority.py is the same numbers
    assert "simulate.py" in priority.prior_origin()


@pytest.mark.parametrize("cls, action, p", [
    (FailureClass.INSUFFICIENT_FUNDS, Action.RECOVERY_LINK, 0.42),   # link @ 48h
    (FailureClass.LIMIT_EXCEEDED, Action.RECOVERY_LINK, 0.45),       # link @ 24h
    (FailureClass.AUTH_ABANDONED, Action.RECOVERY_LINK, 0.38),       # link @ 10m
    (FailureClass.ISSUER_DOWN, Action.TOKEN_RETRY, 0.70),            # token @ 15m
    (FailureClass.ISSUER_DOWN, Action.RECOVERY_LINK, 0.40),
    (FailureClass.NETWORK_TIMEOUT, Action.TOKEN_RETRY, 0.75),
    (FailureClass.HARD_DECLINE, Action.NUDGE_CHANGE_METHOD, 0.12),   # change_method @ now
    (FailureClass.HARD_DECLINE, Action.TOKEN_RETRY, 0.00),           # "any" cell
    (FailureClass.RISK_BLOCKED, Action.RECOVERY_LINK, 0.00),
    (FailureClass.RISK_BLOCKED, Action.HUMAN_QUEUE, 0.00),
])
def test_prior_probability_at_the_tables_delay(cls, action, p):
    assert priority.prior_probability(cls, action) == p


def test_prior_for_an_unmodelled_delay_takes_the_most_pessimistic_cell_or_zero():
    # an override's 24h on INSUFFICIENT_FUNDS: no cell; the class/link cells are 0.42 and 0.14 -> 0.14
    assert priority.prior_probability(FailureClass.INSUFFICIENT_FUNDS, Action.RECOVERY_LINK, 24 * 3600) == 0.14
    assert priority.prior_probability(FailureClass.UNKNOWN, Action.RECOVERY_LINK, 0) == 0.05
    assert priority.prior_probability(FailureClass.UNKNOWN, Action.NUDGE_CHANGE_METHOD, 0) == 0.0
    assert priority.prior_probability("garbage", "garbage") == 0.0


def test_expected_recovery_is_amount_times_prior_with_the_source_named():
    est = priority.expected_recovery(Attempt(10_000), FailureClass.INSUFFICIENT_FUNDS, Action.RECOVERY_LINK)
    assert est.expected_paise == 4200 and est.probability == 0.42 and est.source == "simulation prior"
    assert est.basis == "recovery_link@48h" and est.as_if is False
    assert "Rs 42.00 = Rs 100.00 x 0.42 [simulation prior, recovery_link@48h]" == est.text()
    # a delay from an override is looked up at that delay, not the table's
    est = priority.expected_recovery(Attempt(10_000), "INSUFFICIENT_FUNDS", "recovery_link", delay_seconds=3600)
    assert est.probability == 0.14 and est.basis == "recovery_link@1h"


def test_parked_attempt_is_valued_as_if_a_person_took_the_tables_action():
    # HARD_DECLINE in the human queue (contact cap, say): worth amount x P(change_method)
    est = priority.expected_recovery(Attempt(10_000), FailureClass.HARD_DECLINE, Action.HUMAN_QUEUE)
    assert est.as_if and est.basis == "nudge_change_method@now" and est.expected_paise == 1200
    # ISSUER_DOWN with a token: the table would token-retry
    est = priority.expected_recovery(Attempt(10_000, has_token=True), FailureClass.ISSUER_DOWN, Action.NO_ACTION)
    assert est.as_if and est.basis == "token_retry@15m" and est.probability == 0.70
    # UNKNOWN / RISK_BLOCKED: the table has no action of its own; a link stands in (0.05 / 0.00)
    assert priority.expected_recovery(Attempt(10_000), FailureClass.UNKNOWN, Action.HUMAN_QUEUE).probability == 0.05
    assert priority.expected_recovery(Attempt(10_000), FailureClass.RISK_BLOCKED, Action.HUMAN_QUEUE).probability == 0.0


def test_empirical_rate_replaces_the_prior_only_at_thirty_samples():
    thin = {"INSUFFICIENT_FUNDS": Rate(n=29, k=29)}
    est = priority.expected_recovery(Attempt(10_000), FailureClass.INSUFFICIENT_FUNDS, Action.RECOVERY_LINK, empirical=thin)
    assert est.source == "simulation prior" and est.probability == 0.42
    enough = {"INSUFFICIENT_FUNDS": Rate(n=30, k=6)}
    est = priority.expected_recovery(Attempt(10_000), FailureClass.INSUFFICIENT_FUNDS, Action.RECOVERY_LINK, empirical=enough)
    assert est.source == "empirical (n=30)" and est.probability == 0.2 and est.expected_paise == 2000
    assert priority.EMPIRICAL_MIN_SAMPLES == insights.MIN_SAMPLES == 30
    # another class's sample does not leak
    est = priority.expected_recovery(Attempt(10_000), FailureClass.AUTH_ABANDONED, Action.RECOVERY_LINK, empirical=enough)
    assert est.source == "simulation prior"


@pytest.fixture()
def processed(session):
    razorpay_client.reset_fixture()
    seed(session, now=NOW)
    summaries = process_all(session, execute_now=True, now=NOW, rz_client=razorpay_client.fixture(), sleep=NOSLEEP)
    return session, summaries


def test_human_queue_is_ordered_by_expected_value_then_age(processed):
    session, _ = processed
    items = priority.human_queue(session)
    assert items and all(it.job.status == JobStatus.HUMAN_QUEUE.value for it in items)
    values = [it.estimate.expected_paise for it in items]
    assert values == sorted(values, reverse=True)
    # the seed parks UNKNOWNs (worth amount x 0.05) and RISK_BLOCKEDs (worth 0): unknowns come first
    classes = [it.failure_class for it in items]
    assert "UNKNOWN" in classes and "RISK_BLOCKED" in classes
    assert classes.index("RISK_BLOCKED") > max(i for i, c in enumerate(classes) if c == "UNKNOWN")
    for it in items:
        assert it.estimate.as_if and it.estimate.source == "simulation prior" and it.reason
    # ties (every RISK_BLOCKED is worth 0) are oldest first, then job id
    zeros = [it for it in items if it.estimate.expected_paise == 0]
    assert zeros == sorted(zeros, key=lambda it: (it.queued_at, it.job.id))


def test_batch_lines_rank_summaries_and_label_the_estimate(processed):
    session, summaries = processed
    ranked = priority.rank_summaries(session, summaries)
    assert len(ranked) == len(summaries)
    values = [est.expected_paise for _, est in ranked]
    assert values == sorted(values, reverse=True)
    lines = priority.batch_lines(session, summaries)
    assert lines[0].startswith("expected recovery (estimate, simulation prior): Rs ") and "top 5 by expected recovery" in lines[0]
    assert len(lines) == 1 + 5 + 1 and lines[-1].strip().startswith("note: expected recovery is an estimate")
    top = ranked[0][0]
    assert top["payment_id"] in lines[1] and "-> " not in lines[1]  # no "-> action" so batch greps stay honest
    # duplicates carry no new value
    dup = dict(summaries[0], job_status=JobStatus.SKIPPED_DUPLICATE.value, duplicate=True)
    assert len(priority.rank_summaries(session, [dup])) == 0 and priority.batch_lines(session, [dup]) == []


def test_amount_missing_is_worth_nothing_and_never_raises():
    est = priority.expected_recovery(object(), None, None)
    assert est.expected_paise == 0 and est.amount_paise == 0
    a = PaymentAttempt(merchant_id="m", order_id="o", razorpay_payment_id="pay_x", amount_paise=None, method="card")
    assert priority.expected_recovery(a, "ISSUER_DOWN", "recovery_link").expected_paise == 0
