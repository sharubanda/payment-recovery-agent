"""If only one test file exists, it is this one: the same failed payment can never be recovered twice.
Layer 1: the sha256 key + DB UNIQUE constraint at schedule time.
Layer 2: execute_job refuses already-executed jobs before any outbound call.
Layer 3: Razorpay's reference_id uniqueness maps to 'sent', never to a second link."""
from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.executor import idempotency_key, reference_id_for, schedule_job, execute_job
from app.models import AuditEvent, PaymentAttempt, RecoveryDecision, RecoveryJob
from app.razorpay_client import FixtureRazorpayClient, RazorpayError
from app.taxonomy import Action, JobStatus, PolicyDecision

NOW = datetime(2026, 9, 5, 12, 0, 0)


def _attempt(session, payment_id="pay_IdemTest000001"):
    a = PaymentAttempt(merchant_id="m_1", order_id="order_ABC", razorpay_payment_id=payment_id, amount_paise=49900,
                       currency="INR", method="card", error_code="BAD_REQUEST_ERROR", error_source="customer",
                       error_step="payment_authorization", error_reason="payment_failed",
                       error_description="Insufficient funds", has_token=False, customer_name="Asha",
                       customer_contact="+919999999999", customer_email="asha@example.com", failed_at=NOW)
    session.add(a)
    session.commit()
    return a


def _decision(session, attempt, action="recovery_link", failure_class="INSUFFICIENT_FUNDS"):
    d = RecoveryDecision(attempt_id=attempt.id, failure_class=failure_class, action=action, delay_seconds=0,
                         max_attempts=3, reason="test", classified_by="rules", confidence=1.0)
    session.add(d)
    session.commit()
    return d


def _policy(action=Action.RECOVERY_LINK, delay=0):
    return PolicyDecision(action=action, delay_seconds=delay, max_attempts=3, backoff_multiplier=1.0,
                          rationale="test", nudge=True)


def _audits(session, stage=None, contains=None):
    rows = session.execute(select(AuditEvent).order_by(AuditEvent.id)).scalars().all()
    if stage:
        rows = [r for r in rows if r.stage == stage]
    if contains:
        rows = [r for r in rows if contains in r.message]
    return rows


def test_key_is_stable_and_unique_per_payment_and_seq():
    assert idempotency_key("pay_A", 1) == idempotency_key("pay_A", 1)
    assert len(idempotency_key("pay_A", 1)) == 64 and set(idempotency_key("pay_A", 1)) <= set("0123456789abcdef")
    assert idempotency_key("pay_A", 1) != idempotency_key("pay_A", 2)
    assert idempotency_key("pay_A", 1) != idempotency_key("pay_B", 1)
    assert len(reference_id_for(idempotency_key("pay_A", 1))) == 40
    assert reference_id_for(idempotency_key("pay_A", 1)) != reference_id_for(idempotency_key("pay_A", 2))


def test_second_schedule_for_same_event_is_skipped_duplicate(session):
    attempt = _attempt(session)
    decision = _decision(session, attempt)
    job1, created1 = schedule_job(session, attempt, decision, _policy(), now=NOW)
    assert created1 is True and job1.status == JobStatus.PENDING.value
    assert job1.idempotency_key == idempotency_key(attempt.razorpay_payment_id, 1)

    # the same event delivered again: a fresh decision row is fine, a second job is not
    decision2 = _decision(session, attempt)
    job2, created2 = schedule_job(session, attempt, decision2, _policy(), now=NOW)
    assert created2 is False and job2.id == job1.id

    jobs = session.execute(select(RecoveryJob).where(RecoveryJob.attempt_id == attempt.id)).scalars().all()
    assert len(jobs) == 1
    skipped = _audits(session, stage="schedule", contains="skipped_duplicate")
    assert len(skipped) == 1 and "idempotency key already present" in skipped[0].message
    assert session.get(RecoveryDecision, decision2.id) is not None  # the rollback did not eat the caller's rows


def test_db_unique_constraint_rejects_raw_duplicate_insert(session):
    attempt = _attempt(session)
    key = idempotency_key(attempt.razorpay_payment_id, 1)
    session.add(RecoveryJob(attempt_id=attempt.id, retry_seq=1, action="recovery_link", idempotency_key=key))
    session.commit()
    session.add(RecoveryJob(attempt_id=attempt.id, retry_seq=1, action="recovery_link", idempotency_key=key))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()
    assert len(session.execute(select(RecoveryJob)).scalars().all()) == 1


def test_execute_on_already_sent_job_makes_zero_client_calls(session):
    attempt = _attempt(session)
    decision = _decision(session, attempt)
    job, _ = schedule_job(session, attempt, decision, _policy(), now=NOW)
    job.status = JobStatus.SENT.value
    job.razorpay_link_id = "plink_alreadyThere"
    session.commit()

    class Tripwire:
        name = "tripwire"

        def create_payment_link(self, payload):
            raise AssertionError("outbound call on an already-sent job")

        def fetch_payment_link(self, link_id):
            raise AssertionError("outbound call on an already-sent job")

    out = execute_job(session, job, client=Tripwire(), now=NOW, sleep=lambda s: None)
    assert out.status == JobStatus.SENT.value and out.razorpay_link_id == "plink_alreadyThere"
    assert len(_audits(session, stage="execute", contains="already executed")) == 1


@pytest.mark.parametrize("status", [JobStatus.STUBBED.value, JobStatus.SKIPPED_DUPLICATE.value])
def test_execute_refuses_other_terminal_statuses(session, status):
    attempt = _attempt(session)
    decision = _decision(session, attempt)
    job, _ = schedule_job(session, attempt, decision, _policy(), now=NOW)
    job.status = status
    session.commit()
    fx = FixtureRazorpayClient()
    out = execute_job(session, job, client=fx, now=NOW, sleep=lambda s: None)
    assert out.status == status and fx.calls == []


def test_executing_the_same_job_twice_creates_one_link(session):
    attempt = _attempt(session)
    decision = _decision(session, attempt)
    job, _ = schedule_job(session, attempt, decision, _policy(), now=NOW)
    fx = FixtureRazorpayClient()
    first = execute_job(session, job, client=fx, now=NOW, sleep=lambda s: None)
    second = execute_job(session, job, client=fx, now=NOW, sleep=lambda s: None)
    assert first.status == second.status == JobStatus.SENT.value
    assert len(fx.links()) == 1 and len([c for c in fx.calls if c[0] == "create_payment_link"]) == 1


def test_api_duplicate_reference_id_maps_to_sent_not_a_second_link(session):
    """Layer 3: our row says pending (say we crashed after the HTTP call), Razorpay says the
    reference_id already exists. That is proof the link went out: look it up by that reference,
    record it on the job as sent, create nothing."""
    attempt = _attempt(session)
    decision = _decision(session, attempt)
    job, _ = schedule_job(session, attempt, decision, _policy(), now=NOW)
    fx = FixtureRazorpayClient()
    earlier = fx.create_payment_link({"amount": 49900, "currency": "INR", "reference_id": reference_id_for(job.idempotency_key)})
    out = execute_job(session, job, client=fx, now=NOW, sleep=lambda s: None)
    assert out.status == JobStatus.SENT.value and out.attempts_made == 1
    assert out.razorpay_link_id == earlier["id"] and out.razorpay_link_url == earlier["short_url"]
    assert "already exists" in out.last_error and "recovered" in out.last_error
    assert len(fx.links()) == 1
    assert len(_audits(session, stage="execute", contains="already existed")) == 1


def test_api_duplicate_reference_id_without_a_lookup_parks_for_a_person(session):
    """Layer 3 fired but the link cannot be found: never guess, never re-create; a person reconciles."""
    attempt = _attempt(session)
    decision = _decision(session, attempt)
    job, _ = schedule_job(session, attempt, decision, _policy(), now=NOW)

    class DuplicateOnly:
        name = "duplicate-only"

        def create_payment_link(self, payload):
            raise RazorpayError(400, "BAD_REQUEST_ERROR", "A payment link already exists for the given reference")

        def fetch_payment_link(self, link_id):
            raise AssertionError("not fetched")

    out = execute_job(session, job, client=DuplicateOnly(), now=NOW, sleep=lambda s: None)
    assert out.status == JobStatus.HUMAN_QUEUE.value and out.razorpay_link_id is None
    assert "could not be fetched" in out.last_error and "a person reconciles" in out.last_error
    assert len(_audits(session, stage="execute", contains="human_queue: Razorpay says reference_id")) == 1
    # terminal: a re-run makes no call
    out2 = execute_job(session, out, client=DuplicateOnly(), now=NOW, sleep=lambda s: None)
    assert out2.status == JobStatus.HUMAN_QUEUE.value and len(_audits(session, contains="already executed")) == 1


def test_atomic_claim_refuses_a_second_worker_on_one_pending_job(session):
    """The pending->executing claim is a compare-and-set: a second worker that races in after the
    first claimed the job is refused before any outbound call, so only one link is created."""
    attempt = _attempt(session)
    decision = _decision(session, attempt)
    job, _ = schedule_job(session, attempt, decision, _policy(), now=NOW)
    fx_a, fx_b = FixtureRazorpayClient(), FixtureRazorpayClient()

    class _Reentrant:
        name = "reentrant"

        def __init__(self, inner):
            self.inner, self.raced = inner, False

        def create_payment_link(self, payload):
            if not self.raced:  # worker B races in after A claimed the row but before A's create lands
                self.raced = True
                execute_job(session, job, client=fx_b, now=NOW, sleep=lambda s: None)
            return self.inner.create_payment_link(payload)

        def fetch_payment_link(self, link_id):
            return self.inner.fetch_payment_link(link_id)

        def list_payment_links(self, *, reference_id):
            return self.inner.list_payment_links(reference_id=reference_id)

    out = execute_job(session, job, client=_Reentrant(fx_a), now=NOW, sleep=lambda s: None)
    assert out.status == JobStatus.SENT.value
    assert len(fx_a.links()) == 1 and fx_b.calls == []  # B refused before any outbound call
    assert len(session.execute(select(RecoveryJob)).scalars().all()) == 1
    assert any("status executing" in r.message for r in _audits(session, contains="already executed"))


def test_duplicate_event_flow_end_to_end(session):
    """The chaos scenario: deliver, execute, deliver again -> one link total, second is skipped."""
    attempt = _attempt(session)
    fx = FixtureRazorpayClient()
    job1, created1 = schedule_job(session, attempt, _decision(session, attempt), _policy(), now=NOW)
    execute_job(session, job1, client=fx, now=NOW, sleep=lambda s: None)
    job2, created2 = schedule_job(session, attempt, _decision(session, attempt), _policy(), now=NOW)
    assert created1 and not created2 and job2.id == job1.id and job2.status == JobStatus.SENT.value
    execute_job(session, job2, client=fx, now=NOW, sleep=lambda s: None)
    assert len(fx.links()) == 1
    assert len(_audits(session, contains="skipped_duplicate")) == 1
    assert len(_audits(session, contains="already executed")) == 1
