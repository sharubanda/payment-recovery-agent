"""End-to-end coverage of the money-path stops through the real ingest and pipeline code with
the fixture Razorpay client: stop when the order is paid elsewhere (before and after a link
went out), expiry-driven follow-ups up to the stop rule, and the timing rules as applied by
schedule_job. Payloads are the shipped examples in docs/examples/."""
import json
from datetime import datetime
from pathlib import Path

from sqlalchemy import select

from app import ingest, pipeline, razorpay_client, scheduling
from app.models import Outcome, PaymentAttempt, RecoveryJob, AuditEvent
from app.taxonomy import JobStatus

EX = Path(__file__).resolve().parent.parent / "docs" / "examples"


def load(name):
    return json.loads((EX / name).read_text())


def calls(fx, word):
    return [c for c in fx.calls if word in str(c[0])]


def jobs_for(session, attempt_id):
    """Link and token jobs only: reminder jobs (app/cadence.py) hang off a sent link and are
    counted separately; the invariant here is one LINK per payment."""
    return [j for j in session.execute(select(RecoveryJob).where(RecoveryJob.attempt_id == attempt_id)
                                       .order_by(RecoveryJob.id)).scalars()
            if j.action != "reminder"]


def test_captured_before_execution_voids_the_pending_job_with_zero_calls(session):
    fx = razorpay_client.FixtureRazorpayClient()
    out = ingest.ingest_event(session, load("payment_failed_webhook.json"), process=True, rz_client=fx)
    attempt = session.get(PaymentAttempt, out["attempt_id"])
    (job,) = jobs_for(session, attempt.id)
    assert job.status == JobStatus.PENDING.value          # 48h delay: not executed yet
    ingest.ingest_event(session, load("payment_captured_webhook.json"), rz_client=fx)
    session.expire_all()
    attempt = session.get(PaymentAttempt, attempt.id)
    assert attempt.order_paid_at is not None and attempt.paid_by_payment_id == "pay_NfKr8mW4vQz3Xd"
    (job,) = jobs_for(session, attempt.id)
    assert job.status == JobStatus.NO_ACTION.value and "already paid" in (job.last_error or "")
    assert calls(fx, "create") == []
    outcome = session.execute(select(Outcome).where(Outcome.attempt_id == attempt.id)).scalar_one()
    assert outcome.recovered is False and "paid elsewhere" in outcome.note
    # a later attempt to execute it is refused before any outbound call
    from app import executor
    executor.execute_job(session, job, client=fx)
    assert calls(fx, "create") == []


def test_captured_after_a_link_was_sent_cancels_it_and_closes_the_outcome(session):
    fx = razorpay_client.FixtureRazorpayClient()
    out = ingest.ingest_event(session, load("payment_failed_webhook.json"), process=True, execute_now=True, rz_client=fx)
    (job,) = jobs_for(session, out["attempt_id"])
    assert job.status == JobStatus.SENT.value and job.razorpay_link_id
    ingest.ingest_event(session, load("payment_captured_webhook.json"), rz_client=fx)
    session.expire_all()
    (job,) = jobs_for(session, out["attempt_id"])
    assert job.status == JobStatus.CANCELLED.value
    assert len(calls(fx, "cancel")) == 1
    assert fx.fetch_payment_link(job.razorpay_link_id)["status"] == "cancelled"
    outcome = session.execute(select(Outcome).where(Outcome.attempt_id == out["attempt_id"])).scalar_one()
    assert outcome.recovered is False and "paid" in outcome.note
    # the captured event redelivered changes nothing more
    ingest.ingest_event(session, load("payment_captured_webhook.json"), rz_client=fx)
    assert len(calls(fx, "cancel")) == 1


def test_expired_link_mints_the_next_attempt_until_the_stop_rule(session):
    fx = razorpay_client.FixtureRazorpayClient()
    out = ingest.ingest_event(session, load("payment_failed_webhook.json"), process=True, execute_now=True, rz_client=fx)
    attempt_id = out["attempt_id"]
    (first,) = jobs_for(session, attempt_id)
    expired = load("payment_link_expired_webhook.json")
    assert expired["payload"]["payment_link"]["entity"]["id"] == first.razorpay_link_id, "example must match the fixture id"
    ingest.ingest_event(session, expired, rz_client=fx)
    session.expire_all()
    jobs = jobs_for(session, attempt_id)
    assert [j.retry_seq for j in jobs] == [1, 2]
    assert jobs[1].status == JobStatus.PENDING.value and jobs[1].idempotency_key != jobs[0].idempotency_key
    outcomes = list(session.execute(select(Outcome).where(Outcome.attempt_id == attempt_id)).scalars())
    assert any("expired" in (o.note or "") for o in outcomes)
    # second attempt goes out, then expires too: INSUFFICIENT_FUNDS allows 2, so the stop rule closes it
    from app import executor
    executor.execute_job(session, jobs[1], client=fx)
    session.expire_all()
    second = session.get(RecoveryJob, jobs[1].id)
    assert second.status == JobStatus.SENT.value and len(calls(fx, "create")) == 2
    fx.mark_expired(second.razorpay_link_id)
    pipeline.poll_outcomes(session, rz_client=fx)
    session.expire_all()
    jobs = jobs_for(session, attempt_id)
    assert jobs[-1].status == JobStatus.NO_ACTION.value and jobs[-1].retry_seq == 3
    trail = [a.message for a in session.execute(select(AuditEvent).where(AuditEvent.attempt_id == attempt_id)).scalars()]
    notes = [o.note or "" for o in session.execute(select(Outcome).where(Outcome.attempt_id == attempt_id)).scalars()]
    assert any("max attempts" in m for m in trail + notes)
    assert len(calls(fx, "create")) == 2


def test_schedule_job_applies_salary_window_and_quiet_hours_with_an_audit_row(session):
    fx = razorpay_client.FixtureRazorpayClient()
    now = scheduling.from_ist(datetime(2026, 9, 26, 22, 0))      # 26 Sep 22:00 IST: late month, quiet hours
    out = ingest.ingest_event(session, load("payment_failed_webhook.json"), process=True, now=now, rz_client=fx)
    (job,) = jobs_for(session, out["attempt_id"])
    assert job.scheduled_at == scheduling.from_ist(datetime(2026, 10, 2, 10, 0))
    assert job.schedule_note and "salary window" in job.schedule_note
    notes = [a.message for a in session.execute(select(AuditEvent).where(AuditEvent.attempt_id == out["attempt_id"])).scalars()]
    assert any("salary window" in m for m in notes)
    assert calls(fx, "create") == []


def test_contact_cap_parks_the_fourth_link_for_a_person(session, monkeypatch):
    from app import config
    monkeypatch.setattr(config, "MAX_CONTACTS_PER_CUSTOMER_PER_WEEK", 1)
    fx = razorpay_client.FixtureRazorpayClient()
    first = ingest.ingest_event(session, load("payment_failed_webhook.json"), process=True, execute_now=True, rz_client=fx)
    assert jobs_for(session, first["attempt_id"])[0].status == JobStatus.SENT.value
    # a second failed payment for the same customer (same contact), different payment and order
    payload = load("payment_failed_webhook.json")
    ent = payload["payload"]["payment"]["entity"]
    ent["id"], ent["order_id"] = "pay_NfKq2vT8xRb2Zz", "order_NfKp9wQ2sYd8Zz"
    second = ingest.ingest_event(session, payload, process=True, execute_now=True, rz_client=fx)
    (job,) = jobs_for(session, second["attempt_id"])
    assert job.status == JobStatus.HUMAN_QUEUE.value and "contact cap" in (job.last_error or "")
    assert len(calls(fx, "create")) == 1


def test_quiet_hours_are_honoured_on_the_process_path_for_a_delay_zero_class(session):
    """A HARD_DECLINE at 02:00 IST is a delay-0 action, but quiet hours move the send to 09:00 IST;
    `process` without --execute-now must leave it pending for run-due, not send it at 02:00."""
    fx = razorpay_client.FixtureRazorpayClient()
    payload = load("payment_failed_webhook.json")
    ent = payload["payload"]["payment"]["entity"]
    ent.update({"id": "pay_NfKqHardDecl01", "order_id": "order_NfKqHardDecl01", "error_reason": "card_declined",
                "error_description": "Card declined: the card has expired."})
    now = scheduling.from_ist(datetime(2026, 9, 10, 2, 0))
    out = ingest.ingest_event(session, payload, process=True, now=now, rz_client=fx)
    (job,) = jobs_for(session, out["attempt_id"])
    assert out["failure_class"] == "HARD_DECLINE" and job.status == JobStatus.PENDING.value
    assert job.scheduled_at == scheduling.from_ist(datetime(2026, 9, 10, 9, 0))
    assert calls(fx, "create") == []
    from app import scheduler
    scheduler.run_due(session, now=scheduling.from_ist(datetime(2026, 9, 10, 9, 5)), rz_client=fx)
    session.expire_all()
    assert session.get(RecoveryJob, job.id).status == JobStatus.SENT.value and len(calls(fx, "create")) == 1


def test_customer_language_from_the_webhook_reaches_the_nudge(session):
    fx = razorpay_client.FixtureRazorpayClient()
    payload = load("payment_failed_webhook.json")
    payload["payload"]["payment"]["entity"]["notes"]["language"] = "hi-IN"
    out = ingest.ingest_event(session, payload, process=True, execute_now=True, rz_client=fx)
    attempt = session.get(PaymentAttempt, out["attempt_id"])
    (job,) = jobs_for(session, attempt.id)
    assert attempt.customer_language == "hi"
    assert job.nudge_source == "template" and any("\u0900" <= ch <= "\u097f" for ch in job.nudge_body)


def test_merchant_override_changes_the_delay_and_nudge_language(session, tmp_path, monkeypatch):
    from app import merchants
    (tmp_path / "acc_HZbGxYwNk4pQ2R.json").write_text(json.dumps(
        {"classes": {"INSUFFICIENT_FUNDS": {"delay_seconds": 3600, "max_attempts": 1}}, "nudge_language": "hinglish"}))
    merchants.reload(tmp_path)
    try:
        fx = razorpay_client.FixtureRazorpayClient()
        now = scheduling.from_ist(datetime(2026, 9, 10, 11, 0))
        out = ingest.ingest_event(session, load("payment_failed_webhook.json"), process=True, execute_now=True,
                                  now=now, rz_client=fx)
        attempt = session.get(PaymentAttempt, out["attempt_id"])
        assert attempt.merchant_id == "acc_HZbGxYwNk4pQ2R"
        assert (out.get("summary") or out)["delay_seconds"] == 3600
        (job,) = jobs_for(session, attempt.id)
        assert job.nudge_source == "template" and ("aapka" in job.nudge_body.lower() or "aap " in job.nudge_body.lower())
    finally:
        merchants._registry = None  # next load() re-resolves the default directory
