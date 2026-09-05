"""Executor behaviour per action, the bounded backoff under injected faults, and the
Payment Link payload shape. sleep is always injected: no test waits."""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app import config, executor, faults
from app.clock import to_unix, utcnow
from app.executor import (build_payment_link_payload, execute_job, failure_kind, next_retry_seq, reconcile_failed_job,
                          record_outcome, reference_id_for, schedule_job)
from app.models import AuditEvent, Outcome, PaymentAttempt, RecoveryDecision, RecoveryJob
from app.razorpay_client import FaultingRazorpayClient, FixtureRazorpayClient, RazorpayError, reset_fixture
from app.taxonomy import Action, JobStatus, PolicyDecision

NOW = datetime(2026, 9, 5, 12, 0, 0)


def _attempt(session, payment_id="pay_ExecTest000001", **overrides):
    fields = dict(merchant_id="m_1", order_id="order_XYZ", razorpay_payment_id=payment_id, amount_paise=125000,
                  currency="INR", method="card", error_code="GATEWAY_ERROR", error_source="bank",
                  error_step="payment_authorization", error_reason="bank_technical_error",
                  error_description="Issuer is down", has_token=False, customer_name="Ravi",
                  customer_contact="+919876543210", customer_email="ravi@example.com", failed_at=NOW)
    fields.update(overrides)
    a = PaymentAttempt(**fields)
    session.add(a)
    session.commit()
    return a


def _decision(session, attempt, action="recovery_link", failure_class="ISSUER_DOWN"):
    d = RecoveryDecision(attempt_id=attempt.id, failure_class=failure_class, action=action, delay_seconds=900,
                         max_attempts=3, reason="test", classified_by="rules", confidence=1.0)
    session.add(d)
    session.commit()
    return d


def _policy(action=Action.RECOVERY_LINK, delay=900, max_attempts=3, mult=1.0):
    return PolicyDecision(action=action, delay_seconds=delay, max_attempts=max_attempts,
                          backoff_multiplier=mult, rationale="test", nudge=True)


def _scheduled(session, action=Action.RECOVERY_LINK, **attempt_overrides):
    attempt = _attempt(session, **attempt_overrides)
    decision = _decision(session, attempt, action=action.value)
    job, created = schedule_job(session, attempt, decision, _policy(action=action), now=NOW)
    assert created
    return attempt, decision, job


def _audits(session, contains=None):
    rows = session.execute(select(AuditEvent).order_by(AuditEvent.id)).scalars().all()
    return [r for r in rows if contains is None or contains in r.message]


class _Flaky:
    """Raises the given errors in order for create, then delegates to the fixture."""
    name = "flaky"

    def __init__(self, errors):
        self.errors, self.inner, self.create_calls = list(errors), FixtureRazorpayClient(), 0

    def create_payment_link(self, payload):
        self.create_calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return self.inner.create_payment_link(payload)

    def fetch_payment_link(self, link_id):
        return self.inner.fetch_payment_link(link_id)

    def list_payment_links(self, *, reference_id):
        return self.inner.list_payment_links(reference_id=reference_id)


def test_schedule_sets_delay_status_and_audit(session):
    attempt = _attempt(session)
    decision = _decision(session, attempt)
    job, created = schedule_job(session, attempt, decision, _policy(delay=900), now=NOW)
    assert created and job.status == JobStatus.PENDING.value and job.retry_seq == 1
    assert job.scheduled_at == NOW + timedelta(seconds=900)
    assert job.action == "recovery_link" and job.decision_id == decision.id and job.attempts_made == 0
    assert len(_audits(session, "scheduled recovery_link")) == 1


def test_schedule_uses_policy_delay_verbatim_and_stops_at_max(session):
    """policy.decide(retry_seq=n) already grows delay_seconds by backoff**(n-1); the executor must
    not apply the multiplier a second time. Follow-ups get distinct keys and a stop rule."""
    attempt = _attempt(session)
    decision = _decision(session, attempt)
    j1, _ = schedule_job(session, attempt, decision, _policy(delay=900, mult=3.0), now=NOW)
    assert next_retry_seq(session, attempt) == 2
    j2, _ = schedule_job(session, attempt, decision, _policy(delay=2700, mult=3.0), retry_seq=2, now=NOW)
    j3, _ = schedule_job(session, attempt, decision, _policy(delay=8100, mult=3.0), retry_seq=3, now=NOW)
    j4, created4 = schedule_job(session, attempt, decision, _policy(delay=24300, mult=3.0), retry_seq=4, now=NOW)
    assert (j1.scheduled_at, j2.scheduled_at, j3.scheduled_at) == (
        NOW + timedelta(seconds=900), NOW + timedelta(seconds=2700), NOW + timedelta(seconds=8100))
    assert created4 and j4.status == JobStatus.NO_ACTION.value  # belt to policy's braces: recorded, not sent
    assert len({j.idempotency_key for j in (j1, j2, j3, j4)}) == 4 and next_retry_seq(session, attempt) == 5
    assert len(_audits(session, "stopping rule")) == 1
    fx = FixtureRazorpayClient()
    assert execute_job(session, j4, client=fx, now=NOW, sleep=lambda s: None).status == JobStatus.NO_ACTION.value
    assert fx.calls == []


@pytest.mark.parametrize("action,status", [(Action.HUMAN_QUEUE, JobStatus.HUMAN_QUEUE.value),
                                           (Action.NO_ACTION, JobStatus.NO_ACTION.value)])
def test_stop_actions_are_parked_at_schedule_and_never_call_out(session, action, status):
    attempt, decision, job = _scheduled(session, action=action)
    assert job.status == status and job.scheduled_at == NOW
    fx = FixtureRazorpayClient()
    out = execute_job(session, job, client=fx, now=NOW, sleep=lambda s: None)
    assert out.status == status and fx.calls == [] and out.razorpay_link_id is None


def test_pending_human_queue_job_is_parked_without_calls(session):
    attempt = _attempt(session)
    decision = _decision(session, attempt, action="human_queue")
    job = RecoveryJob(attempt_id=attempt.id, decision_id=decision.id, retry_seq=1, action="human_queue",
                      idempotency_key="k" * 64, status="pending")
    session.add(job)
    session.commit()
    fx = FixtureRazorpayClient()
    out = execute_job(session, job, client=fx, now=NOW, sleep=lambda s: None)
    assert out.status == JobStatus.HUMAN_QUEUE.value and out.executed_at == NOW and fx.calls == []


def test_token_retry_is_stubbed_with_zero_calls(session):
    attempt, decision, job = _scheduled(session, action=Action.TOKEN_RETRY, has_token=True)
    fx = FixtureRazorpayClient()
    out = execute_job(session, job, client=fx, now=NOW, sleep=lambda s: None)
    assert out.status == JobStatus.STUBBED.value and out.last_error is None and out.executed_at == NOW
    assert fx.calls == [] and out.razorpay_link_id is None
    stub = _audits(session, "STUB")
    assert len(stub) == 1 and "no real tokens in test mode" in stub[0].message


def test_recovery_link_happy_path_and_payload_shape(session, monkeypatch):
    monkeypatch.setattr(config, "CALLBACK_URL", "")
    monkeypatch.setattr(config, "RAZORPAY_NOTIFY_CUSTOMER", True)
    monkeypatch.setattr(executor, "utcnow", lambda: NOW)  # expire_by is clamped to the real clock
    attempt, decision, job = _scheduled(session)
    fx = FixtureRazorpayClient()
    sleeps = []
    out = execute_job(session, job, client=fx, now=NOW, sleep=sleeps.append)
    assert out.status == JobStatus.SENT.value and out.attempts_made == 1 and sleeps == []
    assert out.razorpay_link_id.startswith("plink_") and out.razorpay_link_url.startswith("https://rzp.io/i/")
    assert out.executed_at == NOW and out.last_error is None

    method, payload = fx.calls[0]
    assert method == "create_payment_link"
    assert payload["amount"] == 125000 and payload["currency"] == "INR"
    assert payload["description"] == "Retry payment for order order_XYZ"
    assert payload["reference_id"] == reference_id_for(job.idempotency_key) and len(payload["reference_id"]) == 40
    assert payload["customer"] == {"name": "Ravi", "contact": "+919876543210", "email": "ravi@example.com"}
    assert payload["notify"] == {"sms": True, "email": True} and payload["reminder_enable"] is True
    expire = NOW + timedelta(hours=config.RECOVERY_LINK_EXPIRY_HOURS)
    assert payload["expire_by"] == to_unix(expire)
    assert payload["notes"] == {"payment_id": "pay_ExecTest000001", "order_id": "order_XYZ",
                                "failure_class": "ISSUER_DOWN", "retry_seq": "1", "agent": "payment-recovery-agent"}
    assert "callback_url" not in payload and "callback_method" not in payload
    assert fx.links()[0]["reference_id"] == payload["reference_id"]
    sent = _audits(session, "sent: payment link")
    assert len(sent) == 1 and out.razorpay_link_id in sent[0].message


def test_payload_callback_and_notify_follow_config_and_contact_fields(session, monkeypatch):
    monkeypatch.setattr(config, "CALLBACK_URL", "https://merchant.example/return")
    monkeypatch.setattr(config, "RAZORPAY_NOTIFY_CUSTOMER", True)
    attempt, decision, job = _scheduled(session, customer_contact=None, customer_name=None)
    payload = build_payment_link_payload(attempt, job, "ISSUER_DOWN", now=NOW)
    assert payload["callback_url"] == "https://merchant.example/return" and payload["callback_method"] == "get"
    assert payload["customer"] == {"email": "ravi@example.com"}
    assert payload["notify"] == {"sms": False, "email": True} and payload["reminder_enable"] is True


def test_notifications_are_off_unless_opted_in(session, monkeypatch):
    """Seed contacts look real; with live test keys Razorpay would message them, so notify is opt-in."""
    monkeypatch.setattr(config, "RAZORPAY_NOTIFY_CUSTOMER", False)
    attempt, decision, job = _scheduled(session)
    payload = build_payment_link_payload(attempt, job, "ISSUER_DOWN", now=NOW)
    assert payload["customer"]["contact"] == "+919876543210"  # the customer still travels with the link
    assert payload["notify"] == {"sms": False, "email": False} and payload["reminder_enable"] is False


def test_expire_by_is_in_the_real_future_even_for_a_replayed_clock(session, monkeypatch):
    attempt, decision, job = _scheduled(session)
    real_now = utcnow()
    long_ago = build_payment_link_payload(attempt, job, "ISSUER_DOWN", now=datetime(2025, 1, 1))["expire_by"]
    assert long_ago >= to_unix(real_now + timedelta(hours=config.RECOVERY_LINK_EXPIRY_HOURS)) - 5
    monkeypatch.setattr(config, "RECOVERY_LINK_EXPIRY_HOURS", 0)
    floor = build_payment_link_payload(attempt, job, "ISSUER_DOWN", now=real_now)["expire_by"]
    assert floor >= to_unix(real_now + timedelta(hours=executor.MIN_LINK_EXPIRY_HOURS)) - 5
    future = real_now + timedelta(days=30)  # a `now` ahead of the wall clock is honoured as is
    assert build_payment_link_payload(attempt, job, "ISSUER_DOWN", now=future)["expire_by"] == to_unix(
        future + timedelta(hours=executor.MIN_LINK_EXPIRY_HOURS))


def test_nudge_change_method_also_creates_a_link_and_keeps_nudge_fields(session):
    attempt, decision, job = _scheduled(session, action=Action.NUDGE_CHANGE_METHOD)
    job.nudge_channel, job.nudge_subject, job.nudge_body, job.nudge_source = "sms", "", "Please try another card", "template"
    session.commit()
    out = execute_job(session, job, client=FixtureRazorpayClient(), now=NOW, sleep=lambda s: None)
    assert out.status == JobStatus.SENT.value and out.razorpay_link_id
    assert (out.nudge_channel, out.nudge_subject, out.nudge_body, out.nudge_source) == (
        "sms", "", "Please try another card", "template")


def test_429_fault_backs_off_twice_then_creates_one_link(session):
    attempt, decision, job = _scheduled(session)
    fx = FixtureRazorpayClient()
    client = FaultingRazorpayClient(fx, "razorpay_429")
    sleeps = []
    out = execute_job(session, job, client=client, now=NOW, sleep=sleeps.append)
    assert sleeps == [0.5, 1.0]
    assert out.status == JobStatus.SENT.value and out.attempts_made == 3 and out.last_error is None
    assert client.create_calls == 3 and len(fx.links()) == 1 and out.razorpay_link_id == fx.links()[0]["id"]
    backoffs = _audits(session, "backing off")
    assert len(backoffs) == 2 and "429" in backoffs[0].message


def test_5xx_fault_fails_after_bounded_retries_with_no_link(session):
    attempt, decision, job = _scheduled(session)
    fx = FixtureRazorpayClient()
    client = FaultingRazorpayClient(fx, "razorpay_5xx")
    sleeps = []
    out = execute_job(session, job, client=client, now=NOW, sleep=sleeps.append)
    assert sleeps == [0.5, 1.0]  # no sleep after the final attempt
    assert out.status == JobStatus.FAILED.value and out.attempts_made == executor.MAX_HTTP_ATTEMPTS == 3
    assert "502" in out.last_error and "Gateway is down" in out.last_error
    assert failure_kind(out) == executor.SERVER_ERROR and executor.SERVER_ERROR in executor.AMBIGUOUS_KINDS
    assert client.create_calls == 3 and fx.links() == [] and out.razorpay_link_id is None
    assert len(_audits(session, "failed after 3 attempt")) == 1


def test_other_4xx_is_final_without_retry(session):
    attempt, decision, job = _scheduled(session)
    client = _Flaky([RazorpayError(400, "BAD_REQUEST_ERROR", "The amount must be at least INR 1.00")])
    sleeps = []
    out = execute_job(session, job, client=client, now=NOW, sleep=sleeps.append)
    assert out.status == JobStatus.FAILED.value and out.attempts_made == 1 and sleeps == []
    assert "at least INR 1.00" in out.last_error and client.inner.links() == []
    assert failure_kind(out) == executor.FINAL_4XX and executor.FINAL_4XX not in executor.AMBIGUOUS_KINDS


def test_rate_limit_exhausted_is_a_definite_non_delivery(session, monkeypatch):
    monkeypatch.setattr(executor, "MAX_HTTP_ATTEMPTS", 2)
    attempt, decision, job = _scheduled(session)
    client = FaultingRazorpayClient(FixtureRazorpayClient(), "razorpay_429")
    out = execute_job(session, job, client=client, now=NOW, sleep=lambda s: None)
    assert out.status == JobStatus.FAILED.value and failure_kind(out) == executor.RATE_LIMITED
    assert executor.RATE_LIMITED in executor.RETRYABLE_KINDS


def test_an_ambiguous_failure_wins_over_a_later_rate_limit(session):
    """502 then 429 then 429: the 502 might have created the link, so the kind must say so."""
    attempt, decision, job = _scheduled(session)
    client = _Flaky([RazorpayError(502, "SERVER_ERROR", "bad gateway"), RazorpayError(429, "BAD_REQUEST_ERROR", "slow"),
                     RazorpayError(429, "BAD_REQUEST_ERROR", "slow")])
    out = execute_job(session, job, client=client, now=NOW, sleep=lambda s: None)
    assert out.status == JobStatus.FAILED.value and failure_kind(out) == executor.SERVER_ERROR
    assert "429" in out.last_error  # the detail is still the last error seen


def test_duplicate_reference_wording_is_matched_loosely():
    dup = executor._is_duplicate_reference
    assert dup(RazorpayError(400, "BAD_REQUEST_ERROR", "Payment Link with reference_id abc already exists"))
    assert dup(RazorpayError(400, "BAD_REQUEST_ERROR", "A payment link already exists for the given reference"))
    assert dup(RazorpayError(400, "BAD_REQUEST_ERROR", "reference_id must be unique"))
    assert dup(RazorpayError(400, "BAD_REQUEST_ERROR", "Duplicate reference id"))
    assert not dup(RazorpayError(400, "BAD_REQUEST_ERROR", "reference_id must be at most 40 characters"))
    assert not dup(RazorpayError(400, "BAD_REQUEST_ERROR", "amount must be at least INR 1.00"))
    assert not dup(RazorpayError(500, "SERVER_ERROR", "reference_id already exists"))  # only a 400 is a verdict


def test_reconcile_failed_job_finds_absent_or_cannot_tell(session):
    fx = FixtureRazorpayClient()
    # absent: nothing under the reference -> a follow-up under the next key is safe
    attempt, decision, job = _scheduled(session)
    execute_job(session, job, client=FaultingRazorpayClient(fx, "razorpay_5xx"), now=NOW, sleep=lambda s: None)
    assert reconcile_failed_job(session, job, client=fx, now=NOW) == "absent" and job.status == JobStatus.FAILED.value
    assert _audits(session, "no link at Razorpay under reference_id")
    # found: the request had landed -> the job becomes sent with that link, nothing re-created
    landed = fx.create_payment_link(build_payment_link_payload(attempt, job, "ISSUER_DOWN", now=NOW))
    assert reconcile_failed_job(session, job, client=fx, now=NOW) == "sent"
    assert job.status == JobStatus.SENT.value and job.razorpay_link_id == landed["id"] and job.executed_at == NOW
    assert "reconciled" in job.last_error and len(fx.links()) == 1
    # unknown: the lookup itself fails -> nothing may be minted automatically
    attempt2, decision2, job2 = _scheduled(session, payment_id="pay_ExecTest000002")
    execute_job(session, job2, client=FaultingRazorpayClient(fx, "razorpay_5xx"), now=NOW, sleep=lambda s: None)

    class NoLookup:
        name = "no-lookup"

        def create_payment_link(self, payload):
            raise AssertionError("reconcile never creates")

        def fetch_payment_link(self, link_id):
            raise AssertionError("reconcile never fetches by id")

    assert reconcile_failed_job(session, job2, client=NoLookup(), now=NOW) == "unknown"
    assert job2.status == JobStatus.FAILED.value and _audits(session, "could not tell whether reference_id")


def test_transport_failure_is_retried_then_succeeds(session):
    attempt, decision, job = _scheduled(session)
    client = _Flaky([RazorpayError(0, "NETWORK", "TimeoutError: timed out")])
    sleeps = []
    out = execute_job(session, job, client=client, now=NOW, sleep=sleeps.append)
    assert out.status == JobStatus.SENT.value and out.attempts_made == 2 and sleeps == [0.5]


def test_max_http_attempts_is_monkeypatchable(session, monkeypatch):
    monkeypatch.setattr(executor, "MAX_HTTP_ATTEMPTS", 1)
    monkeypatch.setattr(executor, "BACKOFF_BASE_SECONDS", 0.01)
    attempt, decision, job = _scheduled(session)
    client = FaultingRazorpayClient(FixtureRazorpayClient(), "razorpay_429")
    sleeps = []
    out = execute_job(session, job, client=client, now=NOW, sleep=sleeps.append)
    assert out.status == JobStatus.FAILED.value and out.attempts_made == 1 and sleeps == []


def test_unexpected_client_exception_never_raises(session):
    attempt, decision, job = _scheduled(session)

    class Broken:
        name = "broken"

        def create_payment_link(self, payload):
            raise KeyError("customer")

        def fetch_payment_link(self, link_id):
            raise KeyError(link_id)

    out = execute_job(session, job, client=Broken(), now=NOW, sleep=lambda s: None)
    assert out.status == JobStatus.FAILED.value and "KeyError" in out.last_error and out.attempts_made == 1


def test_refuses_to_create_a_link_for_a_non_positive_amount(session):
    attempt, decision, job = _scheduled(session, amount_paise=0)
    fx = FixtureRazorpayClient()
    out = execute_job(session, job, client=fx, now=NOW, sleep=lambda s: None)
    assert out.status == JobStatus.FAILED.value and fx.calls == [] and "amount_paise" in out.last_error
    assert failure_kind(out) == executor.LOCAL_GUARD


def test_unknown_action_fails_closed(session):
    attempt = _attempt(session)
    job = RecoveryJob(attempt_id=attempt.id, retry_seq=1, action="charge_anyway", idempotency_key="z" * 64)
    session.add(job)
    session.commit()
    fx = FixtureRazorpayClient()
    out = execute_job(session, job, client=fx, now=NOW, sleep=lambda s: None)
    assert out.status == JobStatus.FAILED.value and fx.calls == [] and "unknown action" in out.last_error
    assert failure_kind(out) == executor.LOCAL_GUARD


def test_execute_uses_get_client_when_none_given(session):
    faults.clear()
    reset_fixture()
    attempt, decision, job = _scheduled(session)
    out = execute_job(session, job, now=NOW, sleep=lambda s: None)
    assert out.status == JobStatus.SENT.value and out.razorpay_link_id.startswith("plink_")
    fired = _audits(session, "sent: payment link")
    assert '"client": "fixture"' in fired[0].data_json
    reset_fixture()


def test_execute_with_active_fault_and_no_client_uses_faulting_fixture(session):
    reset_fixture()
    faults.activate("razorpay_5xx")
    try:
        attempt, decision, job = _scheduled(session)
        sleeps = []
        out = execute_job(session, job, now=NOW, sleep=sleeps.append)
        assert out.status == JobStatus.FAILED.value and out.attempts_made == 3 and sleeps == [0.5, 1.0]
        assert '"client": "fault:razorpay_5xx"' in _audits(session, "failed after")[0].data_json
    finally:
        faults.clear()
        reset_fixture()


def test_record_outcome(session):
    attempt, decision, job = _scheduled(session)
    execute_job(session, job, client=FixtureRazorpayClient(), now=NOW, sleep=lambda s: None)
    won = record_outcome(session, attempt, job, True, 125000, "payment_link.paid via poll", now=NOW)
    assert isinstance(won, Outcome) and won.recovered and won.recovered_at == NOW
    assert won.amount_recovered_paise == 125000 and won.job_id == job.id and won.attempt_id == attempt.id
    lost = record_outcome(session, attempt, None, False, 125000, "link expired", now=NOW)
    assert not lost.recovered and lost.recovered_at is None and lost.amount_recovered_paise == 0 and lost.job_id is None
    assert len(session.execute(select(Outcome)).scalars().all()) == 2
    assert len(_audits(session, "recovered: payment_link.paid")) == 1 and len(_audits(session, "not recovered")) == 1
