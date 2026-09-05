"""Operator actions (app/actions.py): each one's happy path and one refusal, every one audited
with the actor under stage "operator"; apply_proposal writes a merchant file with _history and
rollback restores it; a manual override without evidence is refused."""
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app import actions, config, insights, merchants, razorpay_client
from app.insights import Proposal, Rate, Report
from app.models import AuditEvent, Outcome, PaymentAttempt, RecoveryJob
from app.pipeline import process_all
from app.taxonomy import JobStatus
from scripts.seed import seed

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 9, 5, 12, 0, 0)
NOSLEEP = lambda s: None  # noqa: E731


@pytest.fixture()
def world(session):
    """Seed + process everything now (sent links, human-queued rows), then one more attempt
    processed WITHOUT --execute-now so a pending job exists."""
    razorpay_client.reset_fixture()
    seed(session, now=NOW)
    session.commit()
    process_all(session, execute_now=True, now=NOW, rz_client=razorpay_client.fixture(), sleep=NOSLEEP)
    session.add(PaymentAttempt(
        merchant_id="acc_test", order_id="order_pending_1", razorpay_payment_id="pay_PENDING0000001", amount_paise=99900,
        method="card", error_code="BAD_REQUEST_ERROR", error_source="bank", error_step="payment_authorization",
        error_reason="payment_failed", error_description="Issuer bank is down, please try later", customer_name="P",
        customer_contact="+919888877777", customer_email="p@example.com", failed_at=NOW))
    session.commit()
    process_all(session, execute_now=False, now=NOW, rz_client=razorpay_client.fixture(), sleep=NOSLEEP)
    return session


def latest_job(a: PaymentAttempt) -> RecoveryJob:
    return max(a.jobs, key=lambda j: j.id)


def queued_attempt(session) -> PaymentAttempt:
    return next(a for a in session.execute(select(PaymentAttempt)).scalars() if a.jobs
                and latest_job(a).status == JobStatus.HUMAN_QUEUE.value)


def sent_job(session) -> RecoveryJob:
    return session.execute(select(RecoveryJob).where(RecoveryJob.status == JobStatus.SENT.value,
                                                     RecoveryJob.razorpay_link_id.is_not(None))).scalars().first()


def pending_job(session) -> RecoveryJob:
    return session.execute(select(RecoveryJob).where(RecoveryJob.status == JobStatus.PENDING.value)).scalars().first()


def operator_rows(session, attempt_id):
    return session.execute(select(AuditEvent).where(AuditEvent.attempt_id == attempt_id,
                                                    AuditEvent.stage == actions.STAGE)).scalars().all()


# ---- resolve --------------------------------------------------------------------------------------

def test_resolve_recovered_and_closed_write_outcome_and_operator_audit(world):
    a = queued_attempt(world)
    r = actions.resolve(world, a.id, recovered_paise=249900, note="paid by NEFT", actor="asha")
    assert r["ok"] and r["attempt_id"] == a.id
    o = world.get(Outcome, r["outcome_id"])
    assert o.recovered and o.amount_recovered_paise == 249900 and "paid by NEFT" in o.note
    rows = operator_rows(world, a.id)
    assert len(rows) == 1 and rows[0].message.startswith("resolve by asha:")
    assert json.loads(rows[0].data_json)["actor"] == "asha"
    # a second decision needs force
    r2 = actions.resolve(world, a.id, closed=True, actor="asha")
    assert not r2["ok"] and "already has an outcome" in r2["message"]
    r3 = actions.resolve(world, a.id, closed=True, actor="asha", force=True)
    assert r3["ok"] and "closed" in r3["message"]


def test_resolve_refuses_a_non_queued_attempt_bad_input_and_missing_actor(world):
    j = sent_job(world)
    r = actions.resolve(world, j.attempt_id, closed=True, actor="asha")
    assert not r["ok"] and "not human-queued" in r["message"] and f"job#{j.id} is sent" in r["message"]
    assert not operator_rows(world, j.attempt_id)  # a refusal writes nothing
    a = queued_attempt(world)
    assert "exactly one" in actions.resolve(world, a.id, actor="asha")["message"]
    assert "positive amount" in actions.resolve(world, a.id, recovered_paise=0, actor="asha")["message"]
    assert "actor is required" in actions.resolve(world, a.id, closed=True, actor="")["message"]
    assert "actor is required" in actions.resolve(world, a.id, closed=True, actor="x" * 41)["message"]
    assert "no attempt" in actions.resolve(world, 999999, closed=True, actor="asha")["message"]


# ---- cancel ---------------------------------------------------------------------------------------

def test_cancel_link_cancels_at_razorpay_and_records_outcome(world):
    fx = razorpay_client.fixture()
    j = sent_job(world)
    r = actions.cancel_link(world, j.id, "asha", rz_client=fx, reason="customer asked us to stop")
    assert r["ok"], r["message"]
    world.refresh(j)
    assert j.status == JobStatus.CANCELLED.value and "asha" in j.last_error
    assert fx.fetch_payment_link(j.razorpay_link_id)["status"] == "cancelled"
    o = world.get(Outcome, r["outcome_id"])
    assert not o.recovered and "cancelled" in o.note and "customer asked us to stop" in o.note
    assert any("cancel by asha" in row.message for row in operator_rows(world, j.attempt_id))
    assert world.execute(select(RecoveryJob).where(RecoveryJob.attempt_id == j.attempt_id)).scalars().all() == [j]  # never a second link


def test_cancel_link_refuses_a_non_sent_job_and_a_failed_cancel_changes_nothing(world):
    p = pending_job(world)
    r = actions.cancel_link(world, p.id, "asha", rz_client=razorpay_client.fixture())
    assert not r["ok"] and "not a sent payment-link job" in r["message"]
    j = sent_job(world)

    class Refusing:
        name = "refusing"

        def cancel_payment_link(self, link_id):
            raise razorpay_client.RazorpayError(500, "SERVER_ERROR", "gateway down")

    r = actions.cancel_link(world, j.id, "asha", rz_client=Refusing())
    assert not r["ok"] and "unchanged" in r["message"]
    world.refresh(j)
    assert j.status == JobStatus.SENT.value
    assert world.execute(select(Outcome).where(Outcome.job_id == j.id)).first() is None


# ---- resend ---------------------------------------------------------------------------------------

def test_resend_notification_honours_the_flag_and_the_cap(world, monkeypatch):
    fx = razorpay_client.fixture()
    j = sent_job(world)
    later = NOW + timedelta(seconds=1)
    monkeypatch.setattr(config, "RAZORPAY_NOTIFY_CUSTOMER", False)
    r = actions.resend_notification(world, j.id, "sms", "asha", rz_client=fx, now=later)
    assert not r["ok"] and "RAZORPAY_NOTIFY_CUSTOMER" in r["message"]
    monkeypatch.setattr(config, "RAZORPAY_NOTIFY_CUSTOMER", True)
    monkeypatch.setattr(config, "MAX_CONTACTS_PER_CUSTOMER_PER_WEEK", 1)  # the link itself already used the one send
    r = actions.resend_notification(world, j.id, "sms", "asha", rz_client=fx, now=later)
    assert not r["ok"] and "contact cap" in r["message"]
    assert not fx.notifications.get(j.razorpay_link_id)
    monkeypatch.setattr(config, "MAX_CONTACTS_PER_CUSTOMER_PER_WEEK", 3)
    r = actions.resend_notification(world, j.id, "sms", "asha", rz_client=fx, now=later)
    assert r["ok"] and r["receipt"] == {"success": True}
    assert fx.notifications[j.razorpay_link_id][-1]["medium"] == "sms"
    assert any("resend by asha" in row.message for row in operator_rows(world, j.attempt_id))
    assert "bogus" in actions.resend_notification(world, j.id, "bogus", "asha", rz_client=fx)["message"]
    assert "not a sent" in actions.resend_notification(world, pending_job(world).id, "sms", "asha", rz_client=fx)["message"]


# ---- retry now ------------------------------------------------------------------------------------

def test_retry_now_executes_a_pending_job_and_refuses_a_sent_one(world):
    fx = razorpay_client.fixture()
    p = pending_job(world)
    assert p.scheduled_at > NOW
    r = actions.retry_now(world, p.id, "asha", rz_client=fx, now=NOW)
    assert r["ok"] and r["status"] in (JobStatus.SENT.value, JobStatus.STUBBED.value), r
    world.refresh(p)
    assert p.status == r["status"] and p.executed_at == NOW
    rows = operator_rows(world, p.attempt_id)
    assert len(rows) == 1 and "ahead of its schedule" in rows[0].message and "retry by asha" in rows[0].message
    j = sent_job(world)
    r = actions.retry_now(world, j.id, "asha", rz_client=fx)
    assert not r["ok"] and "not pending" in r["message"]


# ---- apply a proposal + rollback ----------------------------------------------------------------

def fake_report(n_current=40, n_other=45, kind="proposal"):
    p = Proposal("INSUFFICIENT_FUNDS", kind, "PROPOSAL: move first-attempt delay from bucket <=48h to <=24h: ...",
                 id="INSUFFICIENT_FUNDS-48h-to-24h" if kind == "proposal" else "", from_bucket="<=48h", to_bucket="<=24h",
                 n_current=n_current, n_other=n_other)
    return Report(generated_at=NOW, database="x", merchant=None, min_samples=30, attempts=100, outcomes=50, classes=[],
                  llm=insights.LLMStats(), money=[], proposals=[p])


@pytest.fixture()
def overrides_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MERCHANTS_DIR", str(tmp_path), raising=False)
    merchants.reload(tmp_path)
    yield tmp_path
    merchants.reload(ROOT / "merchants")


def test_apply_proposal_writes_the_file_with_history_and_rollback_restores_it(session, overrides_dir, monkeypatch):
    monkeypatch.setattr(insights, "compute", lambda s, **kw: fake_report())
    r = actions.apply_proposal(session, "INSUFFICIENT_FUNDS-48h-to-24h", "asha", merchant_id="merchant_acme")
    assert r["ok"], r["message"]
    path = overrides_dir / "merchant_acme.json"
    data = json.loads(path.read_text())
    assert data["classes"]["INSUFFICIENT_FUNDS"] == {"delay_seconds": 24 * 3600}
    h = data["_history"]
    assert len(h) == 1 and h[0]["actor"] == "asha" and h[0]["previous"] is None and "n=40 vs n=45" in h[0]["evidence"]
    assert merchants.get("merchant_acme").classes["INSUFFICIENT_FUNDS"].delay_seconds == 24 * 3600  # live in the registry
    row = session.execute(select(AuditEvent).where(AuditEvent.stage == actions.STAGE)).scalars().one()
    assert "apply_proposal by asha" in row.message and json.loads(row.data_json)["proposal_id"] == "INSUFFICIENT_FUNDS-48h-to-24h"
    # the file still loads through the normal loader (the _history key is ignored, not rejected)
    assert merchants.parse_file(path).classes["INSUFFICIENT_FUNDS"].delay_seconds == 24 * 3600
    # "all merchants" goes to _default.json
    r = actions.apply_proposal(session, "INSUFFICIENT_FUNDS-48h-to-24h", "asha")
    assert r["ok"] and (overrides_dir / "_default.json").is_file()
    # rollback restores the previous state: no class entry, no history
    merchants.rollback_override("merchant_acme", "INSUFFICIENT_FUNDS")
    data = json.loads(path.read_text())
    assert "classes" not in data and "_history" not in data
    assert merchants.load().merchants["merchant_acme"].classes == {}  # the merchant's own file is empty again
    assert merchants.get("merchant_acme").classes["INSUFFICIENT_FUNDS"].delay_seconds == 24 * 3600  # _default.json still applies
    with pytest.raises(merchants.OverrideRefused):
        merchants.rollback_override("merchant_acme", "INSUFFICIENT_FUNDS")
    merchants.rollback_override(None, "INSUFFICIENT_FUNDS")
    assert merchants.get("merchant_acme").classes == {}


def test_apply_proposal_refuses_thin_evidence_and_unknown_ids(session, overrides_dir, monkeypatch):
    monkeypatch.setattr(insights, "compute", lambda s, **kw: fake_report(n_current=40, n_other=12))
    r = actions.apply_proposal(session, "INSUFFICIENT_FUNDS-48h-to-24h", "asha", merchant_id="m", min_samples=10)
    assert not r["ok"] and "n >= 30" in r["message"] and not (overrides_dir / "m.json").exists()
    monkeypatch.setattr(insights, "compute", lambda s, **kw: fake_report())
    r = actions.apply_proposal(session, "NOPE-1-to-2", "asha", merchant_id="m")
    assert not r["ok"] and "no current proposal" in r["message"]
    assert not session.execute(select(AuditEvent).where(AuditEvent.stage == actions.STAGE)).scalars().all()


def test_manual_override_needs_evidence_and_may_not_relax_the_table(overrides_dir):
    with pytest.raises(merchants.OverrideRefused, match="evidence"):
        merchants.write_override("m", "INSUFFICIENT_FUNDS", {"delay_seconds": 3600}, evidence="", actor="asha")
    with pytest.raises(merchants.OverrideRefused, match="less conservative"):
        merchants.write_override("m", "INSUFFICIENT_FUNDS", {"delay_seconds": 3600}, evidence="gut feeling", actor="asha")
    with pytest.raises(merchants.OverrideRefused, match="less conservative"):
        merchants.write_override("m", "INSUFFICIENT_FUNDS", {"delay_seconds": 3600}, evidence="thin", actor="asha",
                                 proposal_n=(40, 12))
    with pytest.raises(merchants.OverrideRefused, match="fields"):
        merchants.write_override("m", "INSUFFICIENT_FUNDS", {"delay_secs": 1}, evidence="typo", actor="asha")
    with pytest.raises(merchants.MerchantConfigError):  # the schema still applies: max_attempts <= 5
        merchants.write_override("m", "INSUFFICIENT_FUNDS", {"max_attempts": 9}, evidence="x", actor="asha", proposal_n=(40, 40))
    # more conservative is fine with any evidence; a second write keeps the previous entry
    path = merchants.write_override("m", "INSUFFICIENT_FUNDS", {"delay_seconds": 4 * 86400}, evidence="ticket OPS-12", actor="asha")
    merchants.write_override("m", "INSUFFICIENT_FUNDS", {"nudge": False}, evidence="ticket OPS-13", actor="ravi")
    data = json.loads(path.read_text())
    assert data["classes"]["INSUFFICIENT_FUNDS"] == {"delay_seconds": 4 * 86400, "nudge": False}
    assert [h["actor"] for h in data["_history"]] == ["asha", "ravi"] and data["_history"][1]["previous"] == {"delay_seconds": 4 * 86400}
    merchants.rollback_override("m", "INSUFFICIENT_FUNDS")
    assert json.loads(path.read_text())["classes"]["INSUFFICIENT_FUNDS"] == {"delay_seconds": 4 * 86400}
    assert merchants.override_path(None).name == "_default.json" and merchants.override_path("all").name == "_default.json"
    with pytest.raises(merchants.OverrideRefused):
        merchants.override_path("../x")


def test_merchants_cli_apply_and_rollback(session, overrides_dir, monkeypatch, capsys):
    monkeypatch.setattr(insights, "compute", lambda s, **kw: fake_report())
    assert merchants.main(["apply", "--proposal", "INSUFFICIENT_FUNDS-48h-to-24h", "--merchant", "m", "--actor", "asha"]) == 0
    assert "applied" in capsys.readouterr().out and (overrides_dir / "m.json").is_file()
    assert merchants.main(["rollback", "--merchant", "m", "--class", "INSUFFICIENT_FUNDS"]) == 0
    assert merchants.main(["rollback", "--merchant", "m", "--class", "INSUFFICIENT_FUNDS"]) == 1
    assert "no recorded write" in capsys.readouterr().err


def test_proposal_ids_and_bucket_delays():
    assert insights.proposal_id("INSUFFICIENT_FUNDS", "<=48h", "<=24h") == "INSUFFICIENT_FUNDS-48h-to-24h"
    assert insights.proposal_id("ISSUER_DOWN", "<=1h", ">48h") == "ISSUER_DOWN-1h-to-over48h"
    assert insights.bucket_delay_seconds("<=24h") == 86400 and insights.bucket_delay_seconds("now") == 0
    assert insights.bucket_delay_seconds(">48h") == 72 * 3600
    with pytest.raises(ValueError):
        insights.bucket_delay_seconds("<=3d")
    buckets = {"<=48h": Rate(40, 8), "<=24h": Rate(45, 30)}
    p = insights.compare_buckets("<=48h", buckets, 30, "INSUFFICIENT_FUNDS")
    assert p.is_proposal and p.id == "INSUFFICIENT_FUNDS-48h-to-24h" and (p.n_current, p.n_other) == (40, 45)
    report = fake_report()
    assert insights.proposal_by_id(report, p.id) is report.proposals[0] and insights.proposal_by_id(report, "x") is None
    assert insights.compare_buckets("<=48h", {"<=48h": Rate(5, 1)}, 30, "X").id == ""
