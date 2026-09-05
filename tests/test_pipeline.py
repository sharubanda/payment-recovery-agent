"""The five stages in order, every fallback reachable through the fault registry, the duplicate
delivery, the database outage that must create zero links, and the poll that closes the loop.
Every Razorpay client here is a fixture; every LLM is a double; sleep is always injected."""
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app import db, executor, faults, pipeline, razorpay_client
from app.llm import LLMResponse, ScriptedLLM
from app.models import AuditEvent, Outcome, PaymentAttempt, RecoveryDecision, RecoveryJob
from app.pipeline import PipelineDBError, poll_outcomes, process_all, process_attempt
from app.razorpay_client import FaultingRazorpayClient, FixtureRazorpayClient, RazorpayError
from app.taxonomy import FailureClass

ROOT = Path(__file__).resolve().parents[1]

from scripts.seed import SEED_EVENTS, seed, to_attempt  # noqa: E402

NOW = datetime(2026, 9, 5, 12, 0, 0)
NOSLEEP = lambda s: None  # noqa: E731
SUMMARY_KEYS = {"attempt_id", "payment_id", "failure_class", "classified_by", "fallback_taken", "action",
                "delay_seconds", "job_status", "link_url", "nudge_source", "duplicate"}


def _event(cls: FailureClass, index: int = 0) -> dict:
    return [e for e in SEED_EVENTS if e["expected_class"] is cls][index]


def _attempt(session, cls=FailureClass.INSUFFICIENT_FUNDS, index=0, **overrides):
    row = to_attempt(_event(cls, index), now=NOW)
    for k, v in overrides.items():
        setattr(row, k, v)
    session.add(row)
    session.commit()
    return row


def _audits(session, attempt_id, stage=None, contains=None):
    rows = session.execute(select(AuditEvent).where(AuditEvent.attempt_id == attempt_id)
                           .order_by(AuditEvent.id)).scalars().all()
    return [r for r in rows if (stage is None or r.stage == stage) and (contains is None or contains in r.message)]


def _stages(session, attempt_id):
    return [r.stage for r in _audits(session, attempt_id)]


class EchoLLM:
    """A cooperative model double: classifies as INSUFFICIENT_FUNDS at 0.86 and writes a nudge that
    obeys the content rules (amount and link verbatim) by reading them back from the prompt."""
    name, model = "echo", "echo-model"

    def __init__(self, confidence=0.86):
        self.confidence, self.calls = confidence, []

    def complete_json(self, system, user, schema):
        self.calls.append(user)
        if "failure_class" in schema["properties"]:
            text = json.dumps({"failure_class": "INSUFFICIENT_FUNDS", "confidence": self.confidence,
                               "rationale": "Hinglish for the account lacking balance."})
        else:
            f = dict(line.split(": ", 1) for line in user.splitlines() if ": " in line)
            body = (f"Hi {f['customer_first_name']}, your payment of {f['amount']} for order {f['order_id']} "
                    f"did not go through. Complete it here: {f['link']}")
            text = json.dumps({"channel": f["channel"], "subject": "" if f["channel"] == "sms" else "Complete your payment",
                               "body": body})
        return LLMResponse(text=text, model=self.model, latency_ms=5, stop_reason="end_turn")


class _StatusOverride:
    """Wraps the fixture so a fetch can report a status the fixture cannot produce (expired)."""
    name = "override"

    def __init__(self, inner, statuses: dict):
        self.inner, self.statuses = inner, statuses

    def create_payment_link(self, payload):
        return self.inner.create_payment_link(payload)

    def fetch_payment_link(self, link_id):
        link = self.inner.fetch_payment_link(link_id)
        if link_id in self.statuses:
            link["status"] = self.statuses[link_id]
        return link


class _LostResponse:
    """The request lands (the fixture creates the link) but the answer never arrives, every time."""
    name = "lost-response"

    def __init__(self, inner):
        self.inner = inner

    def create_payment_link(self, payload):
        try:
            self.inner.create_payment_link(payload)
        except RazorpayError as exc:
            if "already exists" not in exc.description:
                raise
        raise RazorpayError(0, "NETWORK", "TimeoutError: sent, no answer")

    def fetch_payment_link(self, link_id):
        return self.inner.fetch_payment_link(link_id)

    def list_payment_links(self, *, reference_id):
        return self.inner.list_payment_links(reference_id=reference_id)


class _AlwaysDown:
    """5xx on every create; the lookup fails too unless `lookup` says otherwise."""
    name = "always-down"

    def __init__(self, inner, lookup_error=None):
        self.inner, self.lookup_error = inner, lookup_error

    def create_payment_link(self, payload):
        raise RazorpayError(502, "SERVER_ERROR", "Gateway is down")

    def fetch_payment_link(self, link_id):
        return self.inner.fetch_payment_link(link_id)

    def list_payment_links(self, *, reference_id):
        if self.lookup_error is not None:
            raise self.lookup_error
        return self.inner.list_payment_links(reference_id=reference_id)


# ---- stage order and the three execution modes ----------------------------------------------

def test_rules_link_executed_now_runs_every_stage_in_order(session):
    a = _attempt(session)  # INSUFFICIENT_FUNDS with a token: the table still says link + nudge, never token_retry
    fx = FixtureRazorpayClient()
    s = process_attempt(session, a, execute_now=True, now=NOW, rz_client=fx, sleep=NOSLEEP)
    assert SUMMARY_KEYS <= set(s)
    assert (s["failure_class"], s["classified_by"], s["fallback_taken"]) == ("INSUFFICIENT_FUNDS", "rules", None)
    assert (s["action"], s["delay_seconds"], s["job_status"], s["duplicate"]) == ("recovery_link", 48 * 3600, "sent", False)
    assert s["link_url"].startswith("https://rzp.io/i/") and s["nudge_source"] == "template" and s["nudge_channel"] == "sms"

    d = session.execute(select(RecoveryDecision).where(RecoveryDecision.attempt_id == a.id)).scalar_one()
    assert d.failure_class == "INSUFFICIENT_FUNDS" and d.action == "recovery_link" and d.delay_seconds == 172800
    assert d.classified_by == "rules" and d.llm_used is False and d.confidence == 1.0 and d.fallback_taken is None
    assert " | " in d.reason and d.reason.startswith("rule insufficient_funds")

    job = session.get(RecoveryJob, s["job_id"])
    assert job.scheduled_at == NOW + timedelta(hours=48) and job.executed_at == NOW and job.decision_id == d.id
    assert "Rs 2,499.00" in job.nudge_body and job.razorpay_link_url in job.nudge_body
    assert len(fx.links()) == 1 and fx.links()[0]["reference_id"] == job.idempotency_key[:40]

    # after the nudge: one deliver row (RAZORPAY_NOTIFY_CUSTOMER is off in tests) and the class's two
    # reminders scheduled (+24h, +60h; app/cadence.py), which are not recovery jobs: still one link job
    assert _stages(session, a.id) == ["classify", "policy", "schedule", "execute", "execute", "nudge", "deliver",
                                      "schedule", "schedule"]
    assert len(session.execute(select(RecoveryJob)).scalars().all()) == 1  # reminders are hidden from plain job queries
    assert [r.schedule_note[:10] for r in pipeline.reminders_for(session, a)] == ["reminder 1", "reminder 2"]
    assert "ahead of schedule" in _audits(session, a.id, "execute")[0].message
    assert "NOT sent" in _audits(session, a.id, "nudge")[0].message
    assert session.execute(select(Outcome)).scalars().all() == []  # the loop is closed by poll, not by sending


def test_positive_delay_without_execute_now_stays_pending(session):
    a = _attempt(session)
    fx = FixtureRazorpayClient()
    s = process_attempt(session, a, now=NOW, rz_client=fx, sleep=NOSLEEP)
    assert s["job_status"] == "pending" and s["link_url"] is None and s["nudge_source"] is None
    assert s["scheduled_at"] == NOW + timedelta(hours=48) and fx.calls == []
    assert _stages(session, a.id) == ["classify", "policy", "schedule"]


def test_zero_delay_executes_immediately(session):
    a = _attempt(session, FailureClass.HARD_DECLINE)
    fx = FixtureRazorpayClient()
    s = process_attempt(session, a, now=NOW, rz_client=fx, sleep=NOSLEEP)
    assert s["action"] == "nudge_change_method" and s["delay_seconds"] == 0 and s["job_status"] == "sent"
    job = session.get(RecoveryJob, s["job_id"])
    assert "different card" in job.nudge_body and job.razorpay_link_url in job.nudge_body
    assert _audits(session, a.id, "execute", "ahead of schedule") == []


def test_token_retry_is_stubbed_and_silent_and_closed(session):
    a = _attempt(session, FailureClass.ISSUER_DOWN)  # has_token True in the seed
    fx = FixtureRazorpayClient()
    s = process_attempt(session, a, execute_now=True, now=NOW, rz_client=fx, sleep=NOSLEEP)
    assert s["action"] == "token_retry" and s["job_status"] == "stubbed" and s["delay_seconds"] == 900
    assert s["nudge_source"] is None and fx.calls == [] and "nudge" not in _stages(session, a.id)
    # a stub cannot recover anything: the outcome says so instead of leaving the payment open forever
    o = session.execute(select(Outcome)).scalar_one()
    assert o.recovered is False and o.job_id == s["job_id"] and o.note.startswith("token_retry stubbed")
    assert s["outcome_note"] == o.note and _stages(session, a.id)[-1] == "outcome"


def test_unreachable_customer_is_parked_not_linked(session):
    a = _attempt(session, FailureClass.AUTH_ABANDONED, customer_contact=None, customer_email=None)
    fx = FixtureRazorpayClient()
    s = process_attempt(session, a, execute_now=True, now=NOW, rz_client=fx, sleep=NOSLEEP)
    assert s["failure_class"] == "AUTH_ABANDONED" and s["action"] == "human_queue" and s["job_status"] == "human_queue"
    assert fx.calls == [] and "nobody" in session.execute(select(RecoveryDecision)).scalar_one().reason
    assert '"reachable": false' in _audits(session, a.id, "policy")[0].data_json
    # a token retry needs no channel
    b = _attempt(session, FailureClass.ISSUER_DOWN, customer_contact=None, customer_email=None)
    assert process_attempt(session, b, execute_now=True, now=NOW, rz_client=fx, sleep=NOSLEEP)["job_status"] == "stubbed"


# ---- the LLM seam: unmapped -> llm -> gate -> human ---------------------------------------------

def test_unknown_without_llm_is_human_queued_not_guessed(session):
    a = _attempt(session, FailureClass.UNKNOWN)
    fx = FixtureRazorpayClient()
    s = process_attempt(session, a, execute_now=True, now=NOW, rz_client=fx, sleep=NOSLEEP)
    assert (s["failure_class"], s["classified_by"], s["fallback_taken"]) == ("UNKNOWN", "fallback", "llm_unavailable->human_queue")
    assert s["action"] == "human_queue" and s["job_status"] == "human_queue" and fx.calls == []
    d = session.execute(select(RecoveryDecision)).scalar_one()
    assert d.classified_by == "fallback" and d.llm_used is False and d.llm_model is None and d.confidence == 0.0
    assert _stages(session, a.id) == ["classify", "llm", "policy", "schedule", "outcome"]
    assert "llm_unavailable->human_queue" in _audits(session, a.id, "llm")[0].message
    o = session.execute(select(Outcome)).scalar_one()
    assert o.recovered is False and o.note.startswith("human_queue") and o.job_id == s["job_id"]


def test_llm_answer_above_threshold_takes_the_link_path_and_writes_the_nudge(session):
    a = _attempt(session, FailureClass.UNKNOWN, index=1)  # the Hinglish SBI message
    fx, echo = FixtureRazorpayClient(), EchoLLM()
    s = process_attempt(session, a, execute_now=True, now=NOW, rz_client=fx, llm_client=echo, sleep=NOSLEEP)
    assert (s["failure_class"], s["classified_by"], s["confidence"]) == ("INSUFFICIENT_FUNDS", "llm", 0.86)
    assert s["action"] == "recovery_link" and s["job_status"] == "sent" and s["nudge_source"] == "llm"
    d = session.execute(select(RecoveryDecision)).scalar_one()
    assert d.llm_used is True and d.llm_model == "echo-model" and d.llm_latency_ms is not None and d.fallback_taken is None
    job = session.get(RecoveryJob, s["job_id"])
    assert job.razorpay_link_url in job.nudge_body and "Rs 1,599.00" in job.nudge_body
    assert len(echo.calls) == 2 and a.customer_contact not in echo.calls[0]  # no PII in the classify prompt
    assert "classified as INSUFFICIENT_FUNDS with confidence 0.86" in _audits(session, a.id, "llm")[0].message


def test_low_confidence_llm_answer_goes_to_a_person(session):
    a = _attempt(session, FailureClass.UNKNOWN)
    fx = FixtureRazorpayClient()
    scripted = ScriptedLLM([json.dumps({"failure_class": "ISSUER_DOWN", "confidence": 0.55, "rationale": "maybe"})])
    s = process_attempt(session, a, execute_now=True, now=NOW, rz_client=fx, llm_client=scripted, sleep=NOSLEEP)
    assert (s["failure_class"], s["classified_by"], s["action"]) == ("ISSUER_DOWN", "llm", "human_queue")
    d = session.execute(select(RecoveryDecision)).scalar_one()
    assert d.confidence == 0.55 and "below the 0.7 threshold" in d.reason and d.action == "human_queue"
    assert s["job_status"] == "human_queue" and fx.calls == []


@pytest.mark.parametrize("fault,expected", [
    ("llm_timeout", "llm_timeout->human_queue"),
    ("llm_bad_json", "llm_bad_json->human_queue"),
    ("llm_hallucinated_class", "llm_hallucinated_class->human_queue"),
])
def test_llm_faults_from_the_registry_degrade_to_human_queue(session, fault, expected):
    faults.activate(fault)
    a = _attempt(session, FailureClass.UNKNOWN)
    fx = FixtureRazorpayClient()
    s = process_attempt(session, a, execute_now=True, now=NOW, rz_client=fx, sleep=NOSLEEP)
    assert (s["classified_by"], s["fallback_taken"], s["job_status"]) == ("fallback", expected, "human_queue")
    d = session.execute(select(RecoveryDecision)).scalar_one()
    assert d.fallback_taken == expected and d.llm_used is True and d.llm_model == f"faulting-{fault}"
    assert fx.calls == [] and expected in _audits(session, a.id, "llm")[0].message


# ---- idempotency and the database outage ------------------------------------------------------

def test_duplicate_delivery_is_skipped_and_makes_one_link(session):
    a = _attempt(session)
    fx = FixtureRazorpayClient()
    first = process_attempt(session, a, execute_now=True, now=NOW, rz_client=fx, sleep=NOSLEEP)
    second = process_attempt(session, a, execute_now=True, now=NOW + timedelta(minutes=1), rz_client=fx, sleep=NOSLEEP)
    assert first["job_status"] == "sent" and second["job_status"] == "skipped_duplicate" and second["duplicate"] is True
    assert second["job_id"] == first["job_id"] and second["existing_job_status"] == "sent"
    assert second["link_url"] == first["link_url"] and second["nudge_source"] is None
    assert len(fx.links()) == 1 and len([c for c in fx.calls if c[0] == "create_payment_link"]) == 1
    assert len(_audits(session, a.id, "schedule", "skipped_duplicate")) == 1
    assert _audits(session, a.id, "execute", "already executed") == []  # a duplicate is not executed at all
    assert len(session.execute(select(RecoveryJob)).scalars().all()) == 1
    assert len(session.execute(select(RecoveryDecision)).scalars().all()) == 2  # the trail keeps both deliveries


def test_db_unavailable_raises_and_creates_zero_links():
    """The ordering guarantee: nothing goes out before the job row is committed, so with no
    database there is no link, and the event is reported as NOT acknowledged."""
    url_before = db.url()
    db.configure("sqlite:////nonexistent/dir/x.db")
    broken = db.session()
    fx = FixtureRazorpayClient()
    try:
        attempt = to_attempt(SEED_EVENTS[0], now=NOW)  # a raw event, never persisted
        with pytest.raises(PipelineDBError, match="event not acknowledged"):
            process_attempt(broken, attempt, execute_now=True, now=NOW, rz_client=fx, sleep=NOSLEEP)
        assert fx.calls == [] and fx.links() == []
    finally:
        broken.close()
        db.configure(url_before)


# ---- the Razorpay faults through the pipeline --------------------------------------------------

def test_razorpay_5xx_fails_closed_and_schedules_the_backoff_followup(session):
    a = _attempt(session, FailureClass.ISSUER_DOWN, index=1)  # netbanking, no token: link path, 15m x3, max 3
    fx = FixtureRazorpayClient()
    sleeps = []
    s = process_attempt(session, a, execute_now=True, now=NOW, rz_client=FaultingRazorpayClient(fx, "razorpay_5xx"),
                        sleep=sleeps.append)
    assert s["job_status"] == "failed" and s["attempts_made"] == 3 and "502" in s["last_error"] and sleeps == [0.5, 1.0]
    assert s["link_url"] is None and s["nudge_source"] is None and fx.links() == []
    assert s["followup_status"] == "pending" and s["followup_scheduled_at"] == NOW + timedelta(seconds=2700)
    follow = session.get(RecoveryJob, s["followup_job_id"])
    assert follow.retry_seq == 2 and follow.action == "recovery_link" and follow.attempts_made == 0
    assert "follow-up seq 2" in _audits(session, a.id, "policy")[1].message
    assert "no link at Razorpay" in _audits(session, a.id, "reconcile")[0].message  # reconciled before seq 2 was minted


def test_transport_failure_that_landed_is_reconciled_to_one_link(session):
    """The lost-response timeout: every attempt reaches Razorpay, no answer comes back. A naive
    follow-up would mint a second link under a new key; the reconcile step finds the first."""
    a = _attempt(session, FailureClass.AUTH_ABANDONED)
    fx = FixtureRazorpayClient()
    s = process_attempt(session, a, execute_now=True, now=NOW, rz_client=_LostResponse(fx), sleep=NOSLEEP)
    job = session.get(RecoveryJob, s["job_id"])
    assert s["job_status"] == "sent" and s["followup_job_id"] is None and s["attempts_made"] == 3
    assert len(fx.links()) == 1 and job.razorpay_link_id == fx.links()[0]["id"] and job.razorpay_link_url in job.nudge_body
    assert "reconciled" in job.last_error and executor.failure_kind(job) is None
    assert _stages(session, a.id) == ["classify", "policy", "schedule", "execute", "execute", "execute", "execute",
                                      "reconcile", "nudge", "deliver", "schedule", "schedule"]  # deliver row + AUTH_ABANDONED's two reminders
    assert "had landed" in _audits(session, a.id, "reconcile")[0].message
    assert len(session.execute(select(RecoveryJob)).scalars().all()) == 1
    assert pipeline.open_link_jobs(session) == [job]  # poll will close it when the customer pays


def test_ambiguous_failure_with_no_lookup_answer_parks_for_a_person(session):
    a = _attempt(session, FailureClass.ISSUER_DOWN, index=1)
    fx = FixtureRazorpayClient()
    down = _AlwaysDown(fx, lookup_error=RazorpayError(0, "NETWORK", "still down"))
    s = process_attempt(session, a, execute_now=True, now=NOW, rz_client=down, sleep=NOSLEEP)
    assert s["job_status"] == "failed" and s["followup_job_id"] is None and fx.links() == []
    assert s["outcome_note"].startswith("human_queue") and "could not tell" in s["outcome_note"]
    assert "could not tell" in _audits(session, a.id, "reconcile")[0].message
    assert len(session.execute(select(RecoveryJob)).scalars().all()) == 1


def test_same_request_failures_do_not_walk_the_backoff_chain(session):
    """A final 4xx or a local guard would fail the same way at seq 2, 3, ...: park it instead."""
    fx = FixtureRazorpayClient()

    class Rejects:
        name = "rejects"

        def create_payment_link(self, payload):
            raise RazorpayError(400, "BAD_REQUEST_ERROR", "contact is invalid")

        def fetch_payment_link(self, link_id):
            raise AssertionError("never")

    a = _attempt(session, FailureClass.ISSUER_DOWN, index=1)
    s = process_attempt(session, a, execute_now=True, now=NOW, rz_client=Rejects(), sleep=NOSLEEP)
    assert s["job_status"] == "failed" and s["attempts_made"] == 1 and s["followup_job_id"] is None
    assert s["outcome_note"].startswith("human_queue") and "final_4xx" in s["outcome_note"]
    b = _attempt(session, FailureClass.AUTH_ABANDONED, amount_paise=0)
    s2 = process_attempt(session, b, execute_now=True, now=NOW, rz_client=fx, sleep=NOSLEEP)
    assert s2["job_status"] == "failed" and s2["followup_job_id"] is None and fx.calls == []
    assert "local_guard" in s2["outcome_note"]
    assert len(session.execute(select(RecoveryJob)).scalars().all()) == 2
    assert "reconcile" not in _stages(session, a.id) + _stages(session, b.id)


def test_raw_redelivery_of_an_ingested_event_is_skipped_not_a_db_error(session):
    """A webhook handing a fresh unsaved row for an already-ingested payment id must be reported
    skipped_duplicate, not raised as a DB outage from the UNIQUE constraint on the insert."""
    ev = _event(FailureClass.AUTH_ABANDONED, 0)
    fx = FixtureRazorpayClient()
    first = process_attempt(session, to_attempt(ev, now=NOW), execute_now=True, now=NOW, rz_client=fx, sleep=NOSLEEP)
    assert first["job_status"] == "sent"
    second = process_attempt(session, to_attempt(ev, now=NOW), execute_now=True, now=NOW + timedelta(minutes=1),
                             rz_client=fx, sleep=NOSLEEP)
    assert second["duplicate"] is True and second["job_status"] == "skipped_duplicate"
    assert second["job_id"] == first["job_id"] and len(fx.links()) == 1
    rows = session.execute(select(PaymentAttempt).where(
        PaymentAttempt.razorpay_payment_id == ev["razorpay_payment_id"])).scalars().all()
    assert len(rows) == 1  # one stored attempt, no second insert


class _Always429:
    name = "always-429"

    def create_payment_link(self, payload):
        raise RazorpayError(429, "BAD_REQUEST_ERROR", "Too many requests")

    def fetch_payment_link(self, link_id):
        raise AssertionError("never fetched")

    def list_payment_links(self, *, reference_id):
        return []


def test_rate_limited_on_a_max_one_class_is_parked_for_a_person_not_no_action(session):
    """A HARD_DECLINE (max_attempts=1) throttled on every attempt never delivered its recovery, so it
    is parked human_queue, not closed 'no_action: max attempts reached'."""
    a = _attempt(session, FailureClass.HARD_DECLINE)
    s = process_attempt(session, a, execute_now=True, now=NOW, rz_client=_Always429(), sleep=NOSLEEP)
    job = session.get(RecoveryJob, s["job_id"])
    assert s["job_status"] == "failed" and executor.failure_kind(job) == executor.RATE_LIMITED
    follow = session.get(RecoveryJob, s["followup_job_id"])
    assert follow is not None and follow.status == "human_queue"
    o = session.execute(select(Outcome).where(Outcome.attempt_id == a.id).order_by(Outcome.id)).scalars().all()[-1]
    assert not o.recovered and o.note.startswith("human_queue") and "throttled" in o.note


def test_rate_limited_multi_attempt_class_retries_after_a_short_backoff_not_the_class_delay(session):
    """INSUFFICIENT_FUNDS (48h delay) throttled on its first send retries after a short API backoff,
    not another 48 hours: a rate limit is a delivery failure, not the class's recovery delay."""
    a = _attempt(session)  # INSUFFICIENT_FUNDS
    s = process_attempt(session, a, execute_now=True, now=NOW, rz_client=_Always429(), sleep=NOSLEEP)
    follow = session.get(RecoveryJob, s["followup_job_id"])
    assert follow.status == "pending"
    assert follow.scheduled_at == NOW + timedelta(seconds=pipeline.DELIVERY_RETRY_BACKOFF_SECONDS)
    assert follow.scheduled_at < NOW + timedelta(hours=48)


class _LandsAfterLookup:
    """Every send fails transport and nothing lands during the first attempt's reconcile; the request
    only appears at Razorpay later, before the follow-up runs."""
    name = "lands-after-lookup"

    def __init__(self, inner):
        self.inner = inner

    def create_payment_link(self, payload):
        raise RazorpayError(0, "NETWORK", "TimeoutError: sent, no answer")

    def fetch_payment_link(self, link_id):
        return self.inner.fetch_payment_link(link_id)

    def list_payment_links(self, *, reference_id):
        return self.inner.list_payment_links(reference_id=reference_id)


def test_late_landing_earlier_attempt_is_reconciled_before_a_second_link(session):
    """The reconcile at the first attempt's failure answered 'absent' because the request had not
    landed yet. It lands before the follow-up runs; the follow-up must recover that link, not mint a
    second live link for the same payment."""
    from app.scheduler import run_due
    a = _attempt(session, FailureClass.ISSUER_DOWN, index=1)  # netbanking, no token: link path, max 3
    fx = FixtureRazorpayClient()
    client = _LandsAfterLookup(fx)
    s = process_attempt(session, a, execute_now=True, now=NOW, rz_client=client, sleep=NOSLEEP)
    assert s["job_status"] == "failed" and s["followup_status"] == "pending" and fx.links() == []

    # the seq-1 request lands late: the link now exists under seq 1's reference_id
    j1 = session.get(RecoveryJob, s["job_id"])
    fx.create_payment_link(executor.build_payment_link_payload(a, j1, "ISSUER_DOWN", now=NOW))
    assert len(fx.links()) == 1

    run_due(session, now=NOW + timedelta(minutes=30), rz_client=client, sleep=NOSLEEP)
    j1 = session.get(RecoveryJob, s["job_id"])
    j2 = session.get(RecoveryJob, s["followup_job_id"])
    assert j1.status == "sent" and j1.razorpay_link_id == fx.links()[0]["id"]
    assert j2.status == "no_action" and len(fx.links()) == 1  # no second live link
    assert "will not create a second" in _audits(session, a.id, "reconcile")[-1].message


def test_a_decision_without_a_job_is_picked_up_by_process_all(session):
    """A crash between the decision commit and the job commit must not strand the event."""
    a = _attempt(session)
    session.add(RecoveryDecision(attempt_id=a.id, failure_class="INSUFFICIENT_FUNDS", action="recovery_link",
                                 delay_seconds=172800, max_attempts=2, reason="stranded", classified_by="rules",
                                 confidence=1.0))
    session.commit()
    assert pipeline.unscheduled_attempts(session) == [a]
    fx = FixtureRazorpayClient()
    results = process_all(session, execute_now=True, now=NOW, rz_client=fx, sleep=NOSLEEP)
    assert [r["job_status"] for r in results] == ["sent"] and len(fx.links()) == 1
    assert len(session.execute(select(RecoveryDecision)).scalars().all()) == 2  # the trail keeps both
    assert pipeline.unscheduled_attempts(session) == [] and process_all(session, now=NOW, rz_client=fx) == []


def test_razorpay_429_from_the_registry_backs_off_then_sends(session):
    faults.activate("razorpay_429")
    razorpay_client.reset_fixture()
    try:
        a = _attempt(session, FailureClass.AUTH_ABANDONED)
        sleeps = []
        s = process_attempt(session, a, execute_now=True, now=NOW, sleep=sleeps.append)
        assert s["job_status"] == "sent" and s["attempts_made"] == 3 and sleeps == [0.5, 1.0]
        assert len(razorpay_client.fixture().links()) == 1 and s["nudge_source"] == "template"
        sent = _audits(session, a.id, "execute", "sent: payment link")
        assert '"client": "fault:razorpay_429"' in sent[0].data_json
        assert len(_audits(session, a.id, "execute", "backing off")) == 2
    finally:
        razorpay_client.reset_fixture()


# ---- batch and poll ----------------------------------------------------------------------------

def test_process_all_matches_ground_truth_and_skips_decided_attempts(session):
    rows = seed(session, now=NOW)
    fx = FixtureRazorpayClient()
    results = process_all(session, execute_now=True, now=NOW, rz_client=fx, sleep=NOSLEEP)
    assert [r["attempt_id"] for r in results] == [r.id for r in rows]
    for event, s in zip(SEED_EVENTS, results):
        if event["expected_class"] is FailureClass.UNKNOWN:
            assert s["classified_by"] == "fallback" and s["action"] == "human_queue"
        else:
            assert s["failure_class"] == event["expected_class"].value and s["classified_by"] == "rules"
    statuses = sorted(s["job_status"] for s in results)
    assert statuses.count("sent") == 15 and statuses.count("stubbed") == 2 and statuses.count("human_queue") == 5
    assert len(fx.links()) == 15
    blocked = {s["payment_id"] for s in results if s["failure_class"] in ("RISK_BLOCKED", "UNKNOWN")}
    assert all(s["link_url"] is None for s in results if s["payment_id"] in blocked)
    assert process_all(session, execute_now=True, now=NOW, rz_client=fx, sleep=NOSLEEP) == []
    assert len(fx.links()) == 15


def test_poll_records_paid_and_expired_and_leaves_open_links_alone(session):
    fx = FixtureRazorpayClient()
    jobs = []
    for cls in (FailureClass.INSUFFICIENT_FUNDS, FailureClass.AUTH_ABANDONED, FailureClass.HARD_DECLINE):
        s = process_attempt(session, _attempt(session, cls), execute_now=True, now=NOW, rz_client=fx, sleep=NOSLEEP)
        jobs.append(session.get(RecoveryJob, s["job_id"]))
    paid, expired, open_ = jobs
    fx.mark_paid(paid.razorpay_link_id)
    client = _StatusOverride(fx, {expired.razorpay_link_id: "expired"})

    out = poll_outcomes(session, rz_client=client, now=NOW)
    assert [o.job_id for o in out] == [paid.id, expired.id]
    assert out[0].recovered and out[0].amount_recovered_paise == paid.attempt.amount_paise and out[0].recovered_at == NOW
    assert not out[1].recovered and out[1].amount_recovered_paise == 0 and "expired" in out[1].note
    assert _audits(session, open_.attempt_id, "poll", "still open")
    assert pipeline.open_link_jobs(session) == [open_]

    assert poll_outcomes(session, rz_client=client, now=NOW) == []  # no second outcome for a closed job
    assert len(session.execute(select(Outcome)).scalars().all()) == 2


def test_poll_survives_a_fetch_failure(session):
    fx = FixtureRazorpayClient()
    s = process_attempt(session, _attempt(session), execute_now=True, now=NOW, rz_client=fx, sleep=NOSLEEP)

    class Broken:
        name = "broken"

        def create_payment_link(self, payload):
            raise AssertionError("poll never creates")

        def fetch_payment_link(self, link_id):
            raise RazorpayError(0, "NETWORK", "TimeoutError: timed out")

    assert poll_outcomes(session, rz_client=Broken(), now=NOW) == []
    assert _audits(session, s["attempt_id"], "poll", "could not fetch")
    assert session.execute(select(Outcome)).scalars().all() == []


# ---- the CLI, as a reviewer runs it -----------------------------------------------------------

def test_cli_demo_runs_twice_and_the_second_run_creates_nothing(tmp_path):
    env = dict(os.environ, DATABASE_URL=f"sqlite:///{tmp_path / 'demo.db'}")
    run = lambda *cmd: subprocess.run([sys.executable, "-m", "app.main", *cmd], cwd=ROOT, env=env,  # noqa: E731
                                      capture_output=True, text=True, timeout=120)
    first = run("demo")
    assert first.returncode == 0, first.stderr
    assert "razorpay : fixture client" in first.stdout and "llm      : none" in first.stdout
    assert "seeded 22 events (0 already present)" in first.stdout
    assert first.stdout.count("nudge:template") == 15 and "[llm_unavailable->human_queue]" in first.stdout
    assert "[fixture] simulating customer payment on 8 of 15 open links" in first.stdout
    assert "15 payment links created; 8 recovered" in first.stdout

    second = run("demo")
    assert second.returncode == 0, second.stderr
    assert "seeded 0 events (22 already present)" in second.stdout
    assert "nothing to process: all 22 attempts already decided" in second.stdout
    assert "15 payment links created; 8 recovered" in second.stdout  # nothing new went out

    trail = run("audit", "--attempt", "1", "--no-data")
    assert trail.returncode == 0 and "skipped_duplicate" not in trail.stdout
    assert [s for s in ("ingest", "classify", "policy", "schedule", "execute", "nudge", "outcome") if s in trail.stdout] == [
        "ingest", "classify", "policy", "schedule", "execute", "nudge", "outcome"]

    dup = run("process", "--attempt", "pay_LrfTLGu5EgYUPo", "--execute-now")
    assert dup.returncode == 0 and "skipped_duplicate" in dup.stdout

    down = subprocess.run([sys.executable, "-m", "app.main", "process", "--all"], cwd=ROOT,
                          env=dict(env, DATABASE_URL="sqlite:////nonexistent/dir/x.db"), capture_output=True, text=True)
    assert down.returncode == 2 and "database unavailable" in down.stderr
