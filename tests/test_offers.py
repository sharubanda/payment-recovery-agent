"""app/offers.py through the real pipeline with the fixture client: shadow mode (zero outbound calls
across process, run_due, poll and cancel), the partial-payment offer and its outcomes, the
repeat-failer memory, the rail preference on HARD_DECLINE, and the live-400 fallbacks."""
import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app import config, executor, ingest, offers, pipeline
from app.models import AuditEvent, Outcome, PaymentAttempt, RecoveryDecision, RecoveryJob, with_reminders
from app.pipeline import poll_outcomes, process_attempt
from app.razorpay_client import FixtureRazorpayClient, RazorpayError
from app.scheduler import run_due
from app.taxonomy import Action, FailureClass, JobStatus
from scripts.seed import SEED_EVENTS, to_attempt

NOW = datetime(2026, 9, 5, 12, 0, 0)   # 17:30 IST: outside quiet hours, before the salary wait
NOSLEEP = lambda s: None  # noqa: E731


def _attempt(session, cls=FailureClass.INSUFFICIENT_FUNDS, index=0, **overrides):
    row = to_attempt([e for e in SEED_EVENTS if e["expected_class"] is cls][index], now=NOW)
    row.customer_contact, row.customer_email, row.merchant_id = "+919876500001", "offers@example.com", "m1"
    row.customer_language = None
    for k, v in overrides.items():
        setattr(row, k, v)
    session.add(row)
    session.commit()
    return row


def _audits(session, attempt_id, stage=None, contains=None):
    rows = session.execute(select(AuditEvent).where(AuditEvent.attempt_id == attempt_id).order_by(AuditEvent.id)).scalars()
    return [r for r in rows if (stage is None or r.stage == stage) and (contains is None or contains in r.message)]


def _all_jobs(session, attempt_id):
    return list(session.execute(with_reminders(select(RecoveryJob).where(RecoveryJob.attempt_id == attempt_id)
                                               .order_by(RecoveryJob.id))).scalars().all())


def _shadow(monkeypatch):
    """PRA_MODE=shadow whether or not app/config.py (owned elsewhere) declares the attribute."""
    monkeypatch.setenv("PRA_MODE", "shadow")
    monkeypatch.setattr(config, "PRA_MODE", "shadow", raising=False)
    assert offers.shadow_mode()


def _history(session, cls, n, days_ago=10, tag="", **kw):
    """n prior decided attempts of class `cls` for the same customer and merchant."""
    for i in range(n):
        a = _attempt(session, cls, index=0, razorpay_payment_id=f"pay_hist{cls.value[:4]}{tag}{i}",
                     order_id=f"order_hist{cls.value[:4]}{tag}{i}", failed_at=NOW - timedelta(days=days_ago), **kw)
        session.add(RecoveryDecision(attempt_id=a.id, failure_class=cls.value, action="recovery_link", reason="seed"))
    session.commit()


# ---- 1. shadow mode -------------------------------------------------------------------------------

def test_shadow_mode_makes_zero_calls_across_process_run_due_poll_and_cancel(session, monkeypatch):
    _shadow(monkeypatch)
    fx = FixtureRazorpayClient()
    a = _attempt(session, FailureClass.INSUFFICIENT_FUNDS)
    s = process_attempt(session, a, execute_now=True, now=NOW, rz_client=fx, sleep=NOSLEEP)
    assert s["job_status"] == JobStatus.SHADOW.value and s["link_id"] is None and s["outcome_note"] is None
    job = session.get(RecoveryJob, s["job_id"])
    assert job.status == "shadow" and job.executed_at == NOW and job.razorpay_link_id is None
    (row,) = _audits(session, a.id, "execute", "shadow mode: would POST /payment_links with ")
    payload = json.loads(row.data_json)["payload"]
    assert payload["amount"] == a.amount_paise and payload["customer"]["contact"] == "+919876500001"
    assert json.dumps(payload, sort_keys=True, default=str) in row.message
    assert job.nudge_body and job.nudge_source == "template"
    reminders = [j for j in _all_jobs(session, a.id) if j.action == Action.REMINDER.value]
    assert reminders and all(r.status == "shadow" and r.executed_at == NOW for r in reminders)
    assert _audits(session, a.id, "schedule", "shadow mode: would schedule reminder 1")
    assert session.execute(select(Outcome)).scalars().all() == []
    # a pending job from before the switch: run_due ends it shadow too, with no call
    b = _attempt(session, FailureClass.HARD_DECLINE, razorpay_payment_id="pay_shadow2", order_id="order_shadow2")
    s2 = process_attempt(session, b, now=datetime(2026, 9, 5, 2, 0), rz_client=fx, sleep=NOSLEEP)  # quiet hours: pending
    assert s2["job_status"] == JobStatus.PENDING.value
    ran = run_due(session, now=NOW, rz_client=fx, sleep=NOSLEEP)
    assert [j.status for j in ran] == ["shadow"]
    # poll has nothing to ask about a shadow job; a redelivery is still skipped_duplicate
    assert poll_outcomes(session, rz_client=fx, now=NOW) == [] and pipeline.open_link_jobs(session) == []
    dup = process_attempt(session, a, execute_now=True, now=NOW, rz_client=fx, sleep=NOSLEEP)
    assert dup["duplicate"] and dup["job_status"] == JobStatus.SKIPPED_DUPLICATE.value
    assert fx.calls == []
    summary = pipeline.shadow_summary(session)
    shadow_reminders = [j for j in session.execute(with_reminders(select(RecoveryJob))).scalars()
                        if j.action == "reminder" and j.status == "shadow"]
    assert summary["jobs"] == 2 and summary["reminders"] == len(shadow_reminders) > len(reminders)
    assert summary["mode"] == "shadow"
    assert summary["amount_paise"] == a.amount_paise + b.amount_paise
    assert set(summary["by_action"]) == {"recovery_link", "nudge_change_method"}
    assert summary["expected_paise"] is not None and 0 < summary["expected_paise"] < summary["amount_paise"]


def test_shadow_cancel_of_a_live_link_is_audited_not_called(session, monkeypatch):
    fx = FixtureRazorpayClient()
    a = _attempt(session, FailureClass.HARD_DECLINE, order_id="order_live1")
    s = process_attempt(session, a, execute_now=True, now=NOW, rz_client=fx, sleep=NOSLEEP)
    assert s["job_status"] == "sent" and len(fx.calls) == 1
    _shadow(monkeypatch)
    # a live-mode reminder executed in shadow mode is recorded as would-have, no notify call
    (rem,) = [j for j in _all_jobs(session, a.id) if j.action == Action.REMINDER.value]
    executor.execute_job(session, rem, client=fx, now=NOW + timedelta(hours=25))
    assert rem.status == "shadow" and _audits(session, a.id, "execute", "shadow mode: would POST /payment_links/plink_")
    assert len(fx.calls) == 1
    # the order paid elsewhere: the cancel of the live link is a would-have too
    out = ingest.record_order_paid(session, {"event": "payment.captured", "order_id": "order_live1", "payment_id": "pay_x",
                                             "amount_paise": a.amount_paise, "paid_at": NOW}, rz_client=fx, now=NOW)
    assert out["shadow"] == [s["job_id"]] and out["cancelled"] == [] and out["parked"] == []
    assert _audits(session, a.id, "cancel", "shadow mode: would POST /payment_links/")
    assert session.get(RecoveryJob, s["job_id"]).status == "sent" and len(fx.calls) == 1


# ---- 2. the partial-payment offer -------------------------------------------------------------------

def test_partial_min_paise_rounds_to_rupees_and_floors_at_rs_100():
    assert offers.partial_min_paise(249900) == 125000        # Rs 1,249.50 -> Rs 1,250 (nearest whole rupee)
    assert offers.partial_min_paise(300000) == 150000
    assert offers.partial_min_paise(12000) == 10000          # half is Rs 60: the Rs 100 floor wins
    assert offers.partial_min_paise(10000) == 10000          # never above the amount
    assert offers.partial_offer_applies("INSUFFICIENT_FUNDS", 200000)
    assert offers.partial_offer_applies(FailureClass.LIMIT_EXCEEDED, 250000)
    assert not offers.partial_offer_applies("INSUFFICIENT_FUNDS", 199999)
    assert not offers.partial_offer_applies("HARD_DECLINE", 900000)


def test_expired_first_link_makes_the_follow_up_a_partial_offer_with_the_nudge_sentence(session):
    fx = FixtureRazorpayClient()
    a = _attempt(session, FailureClass.INSUFFICIENT_FUNDS, amount_paise=249900)
    s = process_attempt(session, a, execute_now=True, now=NOW, rz_client=fx, sleep=NOSLEEP)
    first = session.get(RecoveryJob, s["job_id"])
    assert first.offer is None and "accept_partial" not in fx.calls[0][1] or fx.calls[0][1].get("accept_partial") is False
    fx.mark_expired(first.razorpay_link_id, now=NOW + timedelta(hours=72))
    poll_outcomes(session, rz_client=fx, now=NOW + timedelta(hours=73))
    second = [j for j in _all_jobs(session, a.id) if j.action != "reminder" and j.retry_seq == 2][0]
    assert second.offer == "partial" and second.status == "pending"
    assert _audits(session, a.id, "policy", "partial offer: link")
    executor.execute_job(session, second, client=fx, now=second.scheduled_at, sleep=NOSLEEP)
    pipeline.finish_job(session, a, second, session.get(RecoveryDecision, second.decision_id), rz_client=fx, now=second.scheduled_at)
    sent = fx.calls[-1][1]
    assert sent["accept_partial"] is True and sent["first_min_partial_amount"] == 125000
    link = fx.fetch_payment_link(second.razorpay_link_id)
    assert link["accept_partial"] is True and link["first_min_partial_amount"] == 125000
    assert second.nudge_body.endswith("You can also pay part of it now (at least Rs 1,250.00) and the rest later.")
    # the customer pays part: recovered for that amount, note says partially paid, the link stays open
    fx.mark_paid(second.razorpay_link_id, 125000)
    (outcome,) = poll_outcomes(session, rz_client=fx, now=second.scheduled_at + timedelta(hours=1))
    assert outcome.recovered and outcome.amount_recovered_paise == 125000 and outcome.note.startswith("partially paid")
    assert session.get(PaymentAttempt, a.id).order_paid_at is None


def test_small_amount_or_other_class_gets_no_partial_offer_on_expiry(session):
    fx = FixtureRazorpayClient()
    a = _attempt(session, FailureClass.INSUFFICIENT_FUNDS, amount_paise=150000)
    s = process_attempt(session, a, execute_now=True, now=NOW, rz_client=fx, sleep=NOSLEEP)
    fx.mark_expired(s["link_id"], now=NOW + timedelta(hours=72))
    poll_outcomes(session, rz_client=fx, now=NOW + timedelta(hours=73))
    second = [j for j in _all_jobs(session, a.id) if j.action != "reminder" and j.retry_seq == 2][0]
    assert second.offer is None and not _audits(session, a.id, "policy", "partial offer")


def test_partial_paid_webhook_records_the_amount_paid_and_leaves_the_order_open(session):
    fx = FixtureRazorpayClient()
    a = _attempt(session, FailureClass.LIMIT_EXCEEDED, amount_paise=400000, order_id="order_partial1")
    s = process_attempt(session, a, execute_now=True, now=NOW, rz_client=fx, sleep=NOSLEEP)
    payload = {"event": "payment_link.paid", "payload": {"payment_link": {"entity": {
        "id": s["link_id"], "status": "partially_paid", "amount": 400000, "amount_paid": 200000, "reference_id": "x"}},
        "payment": {"entity": {"id": "pay_part1", "amount": 200000, "created_at": 1757100000}}}}
    out = ingest.ingest_event(session, payload, rz_client=fx, now=NOW + timedelta(hours=2))
    assert out["closed"] and out["amount_recovered_paise"] == 200000
    (outcome,) = session.execute(select(Outcome).where(Outcome.attempt_id == a.id)).scalars().all()
    assert outcome.recovered and "partially paid" in outcome.note
    assert session.get(PaymentAttempt, a.id).order_paid_at is None and not [c for c in fx.calls if c[0] == "cancel_payment_link"]


class _RefusesOffers:
    """A stricter API: a create carrying any offer field is a 400 naming it."""
    name = "strict"

    def __init__(self, inner):
        self.inner, self.refused = inner, []

    def create_payment_link(self, payload):
        if "accept_partial" in payload or "options" in payload:
            self.refused.append(dict(payload))
            field = "accept_partial" if "accept_partial" in payload else "options"
            raise RazorpayError(400, "BAD_REQUEST_ERROR", f"{field} is not a valid field for this payment link")
        return self.inner.create_payment_link(payload)

    def __getattr__(self, name):
        return getattr(self.inner, name)


def test_live_400_on_an_offer_field_falls_back_to_a_plain_link_and_is_audited(session):
    fx = FixtureRazorpayClient()
    strict = _RefusesOffers(fx)
    a = _attempt(session, FailureClass.HARD_DECLINE)
    s = process_attempt(session, a, execute_now=True, now=NOW, rz_client=strict, sleep=NOSLEEP)
    assert s["job_status"] == "sent" and len(strict.refused) == 1 and len(fx.calls) == 1
    assert "options" not in fx.calls[0][1] and "preferred_method" not in fx.calls[0][1]["notes"]
    (row,) = _audits(session, a.id, "execute", "offer fields refused by Razorpay")
    assert json.loads(row.data_json)["dropped"] == ["options"]
    job = session.get(RecoveryJob, s["job_id"])
    assert job.offer is None and job.attempts_made == 2 and "UPI works best" not in (job.nudge_body or "")
    assert len(fx.links()) == 1


# ---- 3. repeat-failer memory -------------------------------------------------------------------------

def test_customer_history_counts_same_merchant_same_contact_in_the_window_only(session):
    _history(session, FailureClass.INSUFFICIENT_FUNDS, 2)
    _history(session, FailureClass.HARD_DECLINE, 1, days_ago=90)               # outside 60 days
    _history(session, FailureClass.RISK_BLOCKED, 1, merchant_id="other")       # another merchant
    a = _attempt(session, FailureClass.INSUFFICIENT_FUNDS, razorpay_payment_id="pay_now", order_id="order_now")
    assert offers.customer_history(session, a, now=NOW) == {"INSUFFICIENT_FUNDS": 2}
    b = _attempt(session, FailureClass.INSUFFICIENT_FUNDS, razorpay_payment_id="pay_nobody", order_id="o",
                 customer_contact=None, customer_email=None)
    assert offers.customer_history(session, b, now=NOW) == {}


def test_three_soft_failures_make_the_first_link_the_partial_offer(session):
    fx = FixtureRazorpayClient()
    _history(session, FailureClass.INSUFFICIENT_FUNDS, 1)
    _history(session, FailureClass.LIMIT_EXCEEDED, 1)
    a = _attempt(session, FailureClass.INSUFFICIENT_FUNDS, amount_paise=300000, razorpay_payment_id="pay_third", order_id="order_third")
    s = process_attempt(session, a, execute_now=True, now=NOW, rz_client=fx, sleep=NOSLEEP)
    job = session.get(RecoveryJob, s["job_id"])
    assert job.status == "sent" and job.retry_seq == 1 and job.offer == "partial"
    assert fx.calls[0][1]["accept_partial"] is True and fx.calls[0][1]["first_min_partial_amount"] == 150000
    (row,) = _audits(session, a.id, "policy", "history override: first link carries the partial offer")
    assert json.loads(row.data_json)["counts"] == {"INSUFFICIENT_FUNDS": 2, "LIMIT_EXCEEDED": 1}
    assert "at least Rs 1,500.00" in job.nudge_body


def test_three_hard_declines_go_to_a_person(session):
    fx = FixtureRazorpayClient()
    _history(session, FailureClass.HARD_DECLINE, 2)
    a = _attempt(session, FailureClass.HARD_DECLINE, razorpay_payment_id="pay_hd3", order_id="order_hd3")
    s = process_attempt(session, a, execute_now=True, now=NOW, rz_client=fx, sleep=NOSLEEP)
    assert s["job_status"] == "human_queue" and fx.calls == []
    assert "repeat hard declines" in s["outcome_note"] and "instrument is dead" in s["outcome_note"]
    (row,) = _audits(session, a.id, "policy", "history override: human_queue")
    assert json.loads(row.data_json)["counts"]["HARD_DECLINE"] == 3
    # two are not enough
    b = _attempt(session, FailureClass.HARD_DECLINE, razorpay_payment_id="pay_hd_other", order_id="o2",
                 customer_contact="+910000000000", customer_email="two@example.com")
    _history(session, FailureClass.HARD_DECLINE, 1, tag="b", customer_contact="+910000000000", customer_email="two@example.com")
    assert process_attempt(session, b, execute_now=True, now=NOW, rz_client=fx, sleep=NOSLEEP)["job_status"] == "sent"


def test_any_risk_block_in_history_parks_everything(session):
    fx = FixtureRazorpayClient()
    _history(session, FailureClass.RISK_BLOCKED, 1)
    a = _attempt(session, FailureClass.AUTH_ABANDONED, razorpay_payment_id="pay_risky", order_id="order_risky")
    s = process_attempt(session, a, execute_now=True, now=NOW, rz_client=fx, sleep=NOSLEEP)
    assert s["job_status"] == "human_queue" and fx.calls == [] and "RISK_BLOCKED" in s["outcome_note"]


# ---- 4. rail preference on HARD_DECLINE ----------------------------------------------------------------

def test_hard_decline_link_disables_card_prefers_upi_and_the_template_says_so(session):
    fx = FixtureRazorpayClient()
    a = _attempt(session, FailureClass.HARD_DECLINE)
    s = process_attempt(session, a, execute_now=True, now=NOW, rz_client=fx, sleep=NOSLEEP)
    sent = fx.calls[0][1]
    assert sent["options"] == {"checkout": {"method": {"card": False, "upi": True, "netbanking": True, "wallet": True}}}
    assert sent["notes"]["preferred_method"] == "upi" and sent["notes"]["failure_class"] == "HARD_DECLINE"
    assert fx.fetch_payment_link(s["link_id"])["options"] == sent["options"]
    job = session.get(RecoveryJob, s["job_id"])
    assert job.offer == "rail_upi" and job.nudge_source == "template" and job.nudge_body.endswith("UPI works best.")
    # an INSUFFICIENT_FUNDS link carries neither
    b = _attempt(session, FailureClass.INSUFFICIENT_FUNDS, razorpay_payment_id="pay_plain", order_id="order_plain",
                 customer_contact="+911111111111", customer_email="plain@example.com")
    process_attempt(session, b, execute_now=True, now=NOW, rz_client=fx, sleep=NOSLEEP)
    assert "options" not in fx.calls[-1][1] and "preferred_method" not in fx.calls[-1][1]["notes"]


def test_rail_sentence_is_template_only_and_language_aware():
    class J:
        offer, razorpay_link_url, action = "rail_upi", "https://rzp.io/i/x", "nudge_change_method"

    class A:
        amount_paise, customer_language = 100000, "hinglish"
    assert offers.nudge_suffix(A(), J(), "HARD_DECLINE", "template") == "UPI sabse aasaan rahega."
    assert offers.nudge_suffix(A(), J(), "HARD_DECLINE", "llm") is None
    J.offer = "partial"
    assert offers.nudge_suffix(A(), J(), "INSUFFICIENT_FUNDS", "llm") == \
        "Aap abhi iska ek hissa (kam se kam Rs 500.00) bhi de sakte hain aur baaki baad mein."
