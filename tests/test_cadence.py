"""Delivery through Razorpay (notify_by on all three clients, the `deliver` rows), the reminder
cadence (offsets, keys, quiet hours, the cap, the skips, run_due with receipts, voided when the
order is paid elsewhere, idempotent on redelivery) and the subscription lifecycle events."""
import copy
import json
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app import cadence, config, executor, ingest, pipeline
from app.ingest import ingest_event, parse_subscription_event
from app.models import AuditEvent, Outcome, PaymentAttempt, RecoveryJob
from app.pipeline import poll_outcomes, process_attempt
from app.razorpay_client import FaultingRazorpayClient, FixtureRazorpayClient, LiveRazorpayClient, RazorpayError
from app.scheduler import due_jobs, run_due
from app.scheduling import contact_sends
from app.taxonomy import FailureClass
from scripts.seed import SEED_EVENTS, to_attempt

EXAMPLES = Path(__file__).resolve().parents[1] / "docs" / "examples"
NOW = datetime(2026, 9, 5, 12, 0, 0)   # 17:30 IST: outside quiet hours
NOSLEEP = lambda s: None  # noqa: E731


def _attempt(session, cls=FailureClass.INSUFFICIENT_FUNDS, index=0, **overrides):
    row = to_attempt([e for e in SEED_EVENTS if e["expected_class"] is cls][index], now=NOW)
    row.customer_contact, row.customer_email = "+919876500001", "cadence@example.com"
    for k, v in overrides.items():
        setattr(row, k, v)
    session.add(row)
    session.commit()
    return row


def _sent(session, fx, cls=FailureClass.INSUFFICIENT_FUNDS, index=0, now=NOW, **overrides):
    a = _attempt(session, cls, index, **overrides)
    s = process_attempt(session, a, execute_now=True, now=now, rz_client=fx, sleep=NOSLEEP)
    assert s["job_status"] == "sent", s
    return a, session.get(RecoveryJob, s["job_id"])


def _audits(session, attempt_id, stage=None, contains=None):
    rows = session.execute(select(AuditEvent).where(AuditEvent.attempt_id == attempt_id).order_by(AuditEvent.id)).scalars()
    return [r for r in rows if (stage is None or r.stage == stage) and (contains is None or contains in r.message)]


def _load(name):
    return json.loads((EXAMPLES / name).read_text())


# ---- notify_payment_link on the three clients ---------------------------------------------------

def test_fixture_notify_records_per_link_and_refuses_unknown_closed_or_bad_medium():
    fx = FixtureRazorpayClient()
    link = fx.create_payment_link({"amount": 100, "currency": "INR", "reference_id": "r1", "customer": {}})
    assert fx.notify_payment_link(link["id"], "sms") == {"success": True}
    assert fx.notify_payment_link(link["id"], "email") == {"success": True}
    assert [n["medium"] for n in fx.notifications[link["id"]]] == ["sms", "email"]
    assert fx.calls[-1] == ("notify_payment_link", {"id": link["id"], "medium": "email"})
    with pytest.raises(RazorpayError, match="medium"):
        fx.notify_payment_link(link["id"], "whatsapp")
    with pytest.raises(RazorpayError, match="does not exist"):
        fx.notify_payment_link("plink_nope", "sms")
    fx.mark_paid(link["id"])
    with pytest.raises(RazorpayError, match="paid"):
        fx.notify_payment_link(link["id"], "sms")
    fx.reset()
    assert fx.notifications == {}


def test_faulting_client_delegates_notify_and_counts_it():
    fx = FixtureRazorpayClient()
    link = fx.create_payment_link({"amount": 100, "currency": "INR", "reference_id": "r1", "customer": {}})
    down = FaultingRazorpayClient(fx, "razorpay_5xx")
    assert down.notify_payment_link(link["id"], "sms") == {"success": True} and down.notify_calls == 1
    assert len(fx.notifications[link["id"]]) == 1


def test_live_notify_posts_to_notify_by_and_returns_the_body(monkeypatch):
    seen = {}

    class R:
        status = 200

        def read(self):
            return b'{"success": true}'

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(req, timeout=None):
        seen["req"] = req
        return R()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = LiveRazorpayClient("rzp_test_abc", "s3cret")
    assert client.notify_payment_link("plink_live1", "email") == {"success": True}
    assert seen["req"].full_url == "https://api.razorpay.com/v1/payment_links/plink_live1/notify_by/email"
    assert seen["req"].get_method() == "POST" and json.loads(seen["req"].data.decode()) == {}
    with pytest.raises(ValueError):
        client.notify_payment_link("plink_live1", "pigeon")


# ---- the deliver stage --------------------------------------------------------------------------

def test_deliver_row_says_not_delivered_when_notifications_are_off(session):
    fx = FixtureRazorpayClient()
    a, job = _sent(session, fx)
    rows = _audits(session, a.id, "deliver")
    assert len(rows) == 1 and rows[0].message == cadence.NOT_DELIVERED_OFF
    assert json.loads(rows[0].data_json)["delivered"] is False
    assert fx.notifications == {} and fx.links()[0]["notify"] == {"sms": False, "email": False}


def test_deliver_rows_per_medium_with_receipts_when_notifications_are_on(session, monkeypatch):
    monkeypatch.setattr(config, "RAZORPAY_NOTIFY_CUSTOMER", True)
    fx = FixtureRazorpayClient()
    a, job = _sent(session, fx)
    assert fx.links()[0]["notify"] == {"sms": True, "email": True}
    rows = _audits(session, a.id, "deliver")
    data = [json.loads(r.data_json) for r in rows]
    assert [d["medium"] for d in data] == ["sms", "email"]
    assert all(d["delivered"] and d["receipt"] == {"success": True} and d["link_id"] == job.razorpay_link_id for d in data)
    assert all("drafted nudge text is stored, not sent" in r.message for r in rows)
    assert job.nudge_body and job.razorpay_link_url in job.nudge_body  # the nudge is still drafted and stored
    # email only: one row
    b, job_b = _sent(session, fx, FailureClass.AUTH_ABANDONED, customer_contact=None)
    assert [json.loads(r.data_json)["medium"] for r in _audits(session, b.id, "deliver")] == ["email"]


# ---- reminders: creation ------------------------------------------------------------------------

def test_reminders_are_created_with_offsets_keys_parent_and_quiet_hours(session):
    fx = FixtureRazorpayClient()
    a, job = _sent(session, fx)  # INSUFFICIENT_FUNDS: +24h, +60h
    rems = cadence.reminders_for(session, a)
    assert pipeline.reminders_for(session, a) == rems and len(rems) == 2
    r1, r2 = rems
    assert r1.scheduled_at == NOW + timedelta(hours=24) and r1.schedule_note == "reminder 1: +24h after send"
    # +60h = 8 Sep 00:00 UTC = 05:30 IST, inside quiet hours: moved to 09:00 IST = 03:30 UTC
    assert r2.scheduled_at == datetime(2026, 9, 8, 3, 30) and "quiet hours: 8 Sep 09:00 IST" in r2.schedule_note
    for n, r in enumerate(rems, 1):
        assert r.action == "reminder" and r.status == "pending" and r.parent_job_id == job.id and r.retry_seq == 1
        assert r.idempotency_key == cadence.reminder_key(a.razorpay_payment_id, 1, n)
        assert r.razorpay_link_id is None and r.razorpay_link_url == job.razorpay_link_url and r.link_source == "payment_link"
    assert len({r.idempotency_key for r in rems} | {job.idempotency_key}) == 3
    # hidden from plain job queries and the relationship; visible when asked for
    assert [j.id for j in session.execute(select(RecoveryJob)).scalars().all()] == [job.id]
    session.refresh(a)
    assert [j.id for j in a.jobs] == [job.id]
    assert cadence.get_job(session, r1.id).id == r1.id and session.get(RecoveryJob, job.id) is job
    assert len(_audits(session, a.id, "schedule", "reminder 1 of link")) == 1
    assert len(fx.links()) == 1 and len([c for c in fx.calls if c[0] == "create_payment_link"]) == 1


@pytest.mark.parametrize("cls,index,expected", [
    (FailureClass.AUTH_ABANDONED, 0, [2, 24]), (FailureClass.LIMIT_EXCEEDED, 0, [24]),
    (FailureClass.HARD_DECLINE, 0, [24]), (FailureClass.NETWORK_TIMEOUT, 1, [24]), (FailureClass.ISSUER_DOWN, 1, [24])])
def test_cadence_per_class(session, cls, index, expected):
    fx = FixtureRazorpayClient()
    a, job = _sent(session, fx, cls, index)
    hours = [(r.scheduled_at - job.executed_at).total_seconds() / 3600 for r in cadence.reminders_for(session, a)]
    assert hours == expected


def test_offsets_past_the_link_expiry_are_skipped_with_a_note(session, monkeypatch):
    monkeypatch.setattr(config, "RECOVERY_LINK_EXPIRY_HOURS", 30)
    fx = FixtureRazorpayClient()
    a, job = _sent(session, fx)
    assert [r.schedule_note for r in cadence.reminders_for(session, a)] == ["reminder 1: +24h after send"]
    skipped = _audits(session, a.id, "schedule", "reminder 2 skipped")
    assert len(skipped) == 1 and "+60h lands at or after the link expiry" in skipped[0].message


def test_reminders_off_by_config_and_no_cadence_for_a_parked_class(session, monkeypatch):
    monkeypatch.setattr(config, "REMINDERS_ENABLED", False)
    fx = FixtureRazorpayClient()
    a, job = _sent(session, fx)
    assert cadence.reminders_for(session, a) == [] and _audits(session, a.id, "schedule", "REMINDERS_ENABLED is off")
    monkeypatch.setattr(config, "REMINDERS_ENABLED", True)
    r = _attempt(session, FailureClass.RISK_BLOCKED)
    s = process_attempt(session, r, execute_now=True, now=NOW, rz_client=fx, sleep=NOSLEEP)
    assert s["job_status"] == "human_queue" and cadence.reminders_for(session, r) == []


def test_reminder_scheduling_is_idempotent_on_redelivery(session):
    fx = FixtureRazorpayClient()
    a, job = _sent(session, fx)
    before = [r.id for r in cadence.reminders_for(session, a)]
    decision = session.get(pipeline.RecoveryDecision, job.decision_id)
    assert cadence.schedule_reminders(session, a, job, decision, now=NOW) == []
    assert [r.id for r in cadence.reminders_for(session, a)] == before
    assert len(_audits(session, a.id, "schedule", "skipped_duplicate, key already present")) == 2
    again = process_attempt(session, a, execute_now=True, now=NOW, rz_client=fx, sleep=NOSLEEP)  # the event redelivered
    assert again["duplicate"] is True and [r.id for r in cadence.reminders_for(session, a)] == before and len(fx.links()) == 1


def test_contact_cap_parks_a_reminder_at_schedule_time(session, monkeypatch):
    monkeypatch.setattr(config, "MAX_CONTACTS_PER_CUSTOMER_PER_WEEK", 1)
    fx = FixtureRazorpayClient()
    a, job = _sent(session, fx)  # the link itself is the one allowed send
    rems = cadence.reminders_for(session, a)
    assert [r.status for r in rems] == ["human_queue", "human_queue"] and all("contact cap" in r.last_error for r in rems)
    assert _audits(session, a.id, "schedule", "reminder 1 parked human_queue")
    assert session.execute(select(Outcome)).scalars().all() == []  # a parked reminder writes no outcome


# ---- reminders: execution -----------------------------------------------------------------------

def test_run_due_executes_reminders_with_receipts_and_they_count_toward_the_cap(session, monkeypatch):
    monkeypatch.setattr(config, "RAZORPAY_NOTIFY_CUSTOMER", True)
    fx = FixtureRazorpayClient()
    a, job = _sent(session, fx)
    t1 = NOW + timedelta(hours=24)
    assert [j.action for j in due_jobs(session, t1)] == ["reminder"]
    done = run_due(session, now=t1, rz_client=fx, sleep=NOSLEEP)
    assert len(done) == 1 and done[0].status == "sent" and done[0].executed_at == t1 and done[0].attempts_made == 2
    assert [n["medium"] for n in fx.notifications[job.razorpay_link_id]] == ["sms", "email"]
    rows = [json.loads(r.data_json) for r in _audits(session, a.id, "deliver", "reminder 1")]
    assert [(d["medium"], d["receipt"]) for d in rows] == [("sms", {"success": True}), ("email", {"success": True})]
    assert contact_sends(session, a, t1 + timedelta(seconds=1)) == 2  # the link and the reminder
    assert len(fx.links()) == 1 and session.execute(select(Outcome)).scalars().all() == []
    assert run_due(session, now=t1, rz_client=fx, sleep=NOSLEEP) == []  # nothing left due; not re-sent
    # the second reminder, at 09:00 IST on the 8th
    t2 = datetime(2026, 9, 8, 3, 30)
    done = run_due(session, now=t2, rz_client=fx, sleep=NOSLEEP)
    assert [j.status for j in done] == ["sent"] and len(fx.notifications[job.razorpay_link_id]) == 4


def test_reminder_completes_no_action_when_notifications_are_off(session):
    fx = FixtureRazorpayClient()
    a, job = _sent(session, fx)
    done = run_due(session, now=NOW + timedelta(hours=24), rz_client=fx, sleep=NOSLEEP)
    assert [j.status for j in done] == ["no_action"] and done[0].last_error == "notifications off"
    assert _audits(session, a.id, "execute", "no_action: notifications off") and fx.notifications == {}


def test_reminder_due_in_quiet_hours_or_over_the_cap_is_moved_or_parked(session, monkeypatch):
    monkeypatch.setattr(config, "RAZORPAY_NOTIFY_CUSTOMER", True)
    fx = FixtureRazorpayClient()
    a, job = _sent(session, fx)
    r1 = cadence.reminders_for(session, a)[0]
    late = datetime(2026, 9, 6, 17, 0)  # 22:30 IST: the sweep is late and inside quiet hours
    done = run_due(session, now=late, rz_client=fx, sleep=NOSLEEP)
    assert [j.id for j in done] == [r1.id] and r1.status == "pending" and r1.scheduled_at == datetime(2026, 9, 7, 3, 30)
    assert fx.notifications == {} and _audits(session, a.id, "schedule", "due inside quiet hours")
    monkeypatch.setattr(config, "MAX_CONTACTS_PER_CUSTOMER_PER_WEEK", 1)
    done = run_due(session, now=r1.scheduled_at, rz_client=fx, sleep=NOSLEEP)
    assert r1.status == "human_queue" and "contact cap" in r1.last_error and fx.notifications == {}
    assert session.execute(select(Outcome)).scalars().all() == []


@pytest.mark.parametrize("how", ["paid", "expired", "cancelled", "order_paid"])
def test_reminder_is_skipped_or_voided_when_the_link_is_no_longer_open(session, monkeypatch, how):
    monkeypatch.setattr(config, "RAZORPAY_NOTIFY_CUSTOMER", True)
    fx = FixtureRazorpayClient()
    a, job = _sent(session, fx)
    t1 = NOW + timedelta(hours=24)
    if how == "order_paid":
        out = ingest_event(session, {"event": "payment.captured", "payload": {"payment": {"entity": {
            "id": "pay_Elsewhere0001", "entity": "payment", "status": "captured", "order_id": a.order_id,
            "amount": a.amount_paise, "created_at": 1757100000}}}}, now=NOW + timedelta(hours=1), rz_client=fx)
        assert out["cancelled"] == [job.id] and sorted(out["reminders_voided"]) == [r.id for r in cadence.reminders_for(session, a)]
        assert all(r.status == "no_action" and r.last_error.startswith("order already paid by") for r in cadence.reminders_for(session, a))
        assert run_due(session, now=t1, rz_client=fx, sleep=NOSLEEP) == []
    else:
        if how == "paid":
            fx.mark_paid(job.razorpay_link_id)
        elif how == "expired":
            fx.mark_expired(job.razorpay_link_id)
        else:
            fx.cancel_payment_link(job.razorpay_link_id)
        # the poll has not run yet: the reminder learns the state from Razorpay and skips
        done = run_due(session, now=t1, rz_client=fx, sleep=NOSLEEP)
        assert [j.status for j in done] == ["no_action"] and f"is {how} at Razorpay" in done[0].last_error
        # once the poll closes the link, the remaining reminder is voided outright
        poll_outcomes(session, rz_client=fx, now=t1)
        assert [r.status for r in cadence.reminders_for(session, a)] == ["no_action", "no_action"]
    assert fx.notifications == {} and len(fx.links()) == 1
    assert len([o for o in session.execute(select(Outcome)).scalars().all()]) == 1  # the link's outcome only


def test_reminder_notify_failure_marks_the_reminder_failed_without_a_chain(session, monkeypatch):
    monkeypatch.setattr(config, "RAZORPAY_NOTIFY_CUSTOMER", True)
    fx = FixtureRazorpayClient()

    class Refuses:
        name = "refuses"

        def fetch_payment_link(self, link_id):
            return fx.fetch_payment_link(link_id)

        def notify_payment_link(self, link_id, medium):
            raise RazorpayError(400, "BAD_REQUEST_ERROR", "notifications disabled on this account")

    a, job = _sent(session, fx)
    done = run_due(session, now=NOW + timedelta(hours=24), rz_client=Refuses(), sleep=NOSLEEP)
    assert [j.status for j in done] == ["failed"] and executor.failure_kind(done[0]) == executor.FINAL_4XX
    assert len(session.execute(select(RecoveryJob)).scalars().all()) == 1  # no follow-up minted for a reminder
    assert [r.status for r in cadence.reminders_for(session, a)] == ["failed", "pending"]


# ---- subscriptions ------------------------------------------------------------------------------

def test_parse_subscription_event_maps_the_payload():
    f = parse_subscription_event(_load("subscription_halted_webhook.json"))
    assert (f["subscription_id"], f["subscription_status"], f["subscription_url"]) == (
        "sub_ReAuthHalted001", "halted", "https://rzp.io/i/SubReAuth1")
    assert f["has_token"] is True and f["method"] == "card" and f["error_reason"] == "card_expired"
    assert f["razorpay_payment_id"] == "pay_SubHaltedFail001" and f["amount_paise"] == 49900
    assert f["customer_name"] == "Meera Iyer" and f["merchant_id"] == "acc_ChaosMerchant01"
    p = parse_subscription_event(_load("subscription_pending_webhook.json"))
    assert p["subscription_status"] == "pending" and p["method"] == "emandate" and p["has_token"] is True
    bad = copy.deepcopy(_load("subscription_pending_webhook.json"))
    del bad["payload"]["payment"]
    with pytest.raises(ingest.IngestError, match="payload.payment.entity"):
        parse_subscription_event(bad)
    bad = copy.deepcopy(_load("subscription_pending_webhook.json"))
    bad["payload"]["subscription"]["entity"]["status"] = "halted"
    with pytest.raises(ingest.IngestError, match="but the event is"):
        parse_subscription_event(bad)


def test_subscription_halted_uses_the_re_authorisation_url_and_creates_no_link(session):
    fx = FixtureRazorpayClient()
    out = ingest_event(session, _load("subscription_halted_webhook.json"), process=True, execute_now=True, now=NOW,
                       rz_client=fx, sleep=NOSLEEP)
    a = session.get(PaymentAttempt, out["attempt_id"])
    assert a.has_token is True and a.subscription_id == "sub_ReAuthHalted001" and a.subscription_status == "halted"
    assert out["failure_class"] == "HARD_DECLINE" and out["job_status"] == "sent"
    job = session.get(RecoveryJob, out["job_id"])
    assert job.link_source == "subscription_url" and job.razorpay_link_url == "https://rzp.io/i/SubReAuth1"
    assert job.razorpay_link_id is None and fx.calls == [] and fx.links() == []
    assert _audits(session, a.id, "execute", "subscription re-authorisation URL from the payload; no Payment Link created")
    assert job.nudge_body and "https://rzp.io/i/SubReAuth1" in job.nudge_body
    assert "no Payment Link to notify" in _audits(session, a.id, "deliver")[0].message
    assert cadence.reminders_for(session, a) == [] and _audits(session, a.id, "schedule", "no reminders")
    assert pipeline.open_link_jobs(session) == []  # nothing for the poll to fetch
    again = ingest_event(session, _load("subscription_halted_webhook.json"), process=True, execute_now=True, now=NOW,
                         rz_client=fx, sleep=NOSLEEP)
    assert again["redelivery"] is True and again["job_status"] == "skipped_duplicate" and fx.calls == []


def test_subscription_halted_turns_a_token_retry_into_the_re_authorisation_link(session):
    fx = FixtureRazorpayClient()
    payload = copy.deepcopy(_load("subscription_halted_webhook.json"))
    pay = payload["payload"]["payment"]["entity"]
    pay.update({"error_code": "GATEWAY_ERROR", "error_source": "bank", "error_step": "payment_initiation",
                "error_reason": "payment_failed",
                "error_description": "Net banking for the selected bank is currently unavailable due to scheduled maintenance."})
    out = ingest_event(session, payload, process=True, execute_now=True, now=NOW, rz_client=fx, sleep=NOSLEEP)
    assert out["failure_class"] == "ISSUER_DOWN" and out["action"] == "recovery_link" and out["job_status"] == "sent"
    assert out["link_url"] == "https://rzp.io/i/SubReAuth1" and fx.calls == []
    assert _audits(session, out["attempt_id"], "policy", "subscription override")[0].message.count("token_retry") == 1


def test_subscription_pending_is_parked_with_the_reason_except_for_hard_declines(session):
    fx = FixtureRazorpayClient()
    out = ingest_event(session, _load("subscription_pending_webhook.json"), process=True, execute_now=True, now=NOW,
                       rz_client=fx, sleep=NOSLEEP)
    assert out["failure_class"] == "INSUFFICIENT_FUNDS" and out["action"] == "human_queue" and out["job_status"] == "human_queue"
    a = session.get(PaymentAttempt, out["attempt_id"])
    assert a.has_token is True and a.subscription_status == "pending" and fx.calls == []
    o = session.execute(select(Outcome).where(Outcome.attempt_id == a.id)).scalar_one()
    assert o.note.startswith("human_queue: " + cadence.SUBSCRIPTION_PENDING_REASON)
    assert cadence.SUBSCRIPTION_PENDING_REASON in _audits(session, a.id, "policy", "subscription override")[0].message
    again = ingest_event(session, _load("subscription_pending_webhook.json"), process=True, execute_now=True, now=NOW,
                         rz_client=fx, sleep=NOSLEEP)
    assert again["redelivery"] is True and again["job_status"] == "skipped_duplicate"
    hard = copy.deepcopy(_load("subscription_pending_webhook.json"))
    hard["payload"]["subscription"]["entity"]["id"] = "sub_RetryPending002"
    hard["payload"]["payment"]["entity"].update({"id": "pay_SubPendingFail002", "error_reason": "card_expired",
                                                 "error_description": "Your card has expired."})
    out2 = ingest_event(session, hard, process=True, execute_now=True, now=NOW, rz_client=fx, sleep=NOSLEEP)
    assert out2["failure_class"] == "HARD_DECLINE" and out2["action"] == "nudge_change_method" and out2["job_status"] == "sent"
    assert session.get(RecoveryJob, out2["job_id"]).link_source == "payment_link" and len(fx.links()) == 1
