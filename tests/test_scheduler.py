"""The poll-loop scheduler: only due jobs run, in order; a sent link gets its nudge; a failed
outbound earns the backoff chain from the policy table until the stop rule; one Razorpay
client per sweep so a rate limit costs one backoff, not one per job."""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app import db, faults, razorpay_client
from app.models import AuditEvent, Outcome, RecoveryJob
from app.pipeline import PipelineDBError, process_attempt
from app.razorpay_client import FaultingRazorpayClient, FixtureRazorpayClient
from app.scheduler import due_jobs, next_due, run_due
from app.taxonomy import FailureClass
from scripts.seed import SEED_EVENTS, to_attempt

NOW = datetime(2026, 9, 5, 12, 0, 0)
NOSLEEP = lambda s: None  # noqa: E731


def _pending(session, cls: FailureClass, index: int = 0, **overrides) -> RecoveryJob:
    """Process one seed event without executing it: a pending job at NOW + the table delay."""
    row = to_attempt([e for e in SEED_EVENTS if e["expected_class"] is cls][index], now=NOW)
    for k, v in overrides.items():
        setattr(row, k, v)
    session.add(row)
    session.commit()
    s = process_attempt(session, row, now=NOW, rz_client=FixtureRazorpayClient(), sleep=NOSLEEP)
    assert s["job_status"] == "pending"
    return session.get(RecoveryJob, s["job_id"])


def _audits(session, attempt_id, contains):
    rows = session.execute(select(AuditEvent).where(AuditEvent.attempt_id == attempt_id).order_by(AuditEvent.id)).scalars()
    return [r for r in rows if contains in r.message]


def test_due_jobs_filters_by_time_and_orders_by_schedule(session):
    funds = _pending(session, FailureClass.INSUFFICIENT_FUNDS)          # +48h
    auth = _pending(session, FailureClass.AUTH_ABANDONED)               # +10m
    net = _pending(session, FailureClass.NETWORK_TIMEOUT, index=1)      # +5m (upi, no token)
    assert due_jobs(session, NOW) == []
    assert due_jobs(session, NOW + timedelta(minutes=5)) == [net]
    assert due_jobs(session, NOW + timedelta(minutes=10)) == [net, auth]
    assert next_due(session, NOW + timedelta(minutes=10)) == funds and next_due(session, NOW + timedelta(days=3)) is None


def test_run_due_executes_due_link_jobs_drafts_nudges_and_leaves_the_future_alone(session):
    funds = _pending(session, FailureClass.INSUFFICIENT_FUNDS)
    auth = _pending(session, FailureClass.AUTH_ABANDONED)
    net = _pending(session, FailureClass.NETWORK_TIMEOUT, index=1)
    fx = FixtureRazorpayClient()
    later = NOW + timedelta(minutes=10)
    done = run_due(session, now=later, rz_client=fx, sleep=NOSLEEP)
    assert [j.id for j in done] == [net.id, auth.id]
    for j in done:
        assert j.status == "sent" and j.executed_at == later and j.razorpay_link_url in j.nudge_body
        assert j.nudge_source == "template" and _audits(session, j.attempt_id, "NOT sent")
    assert session.get(RecoveryJob, funds.id).status == "pending" and len(fx.links()) == 2
    assert run_due(session, now=later, rz_client=fx, sleep=NOSLEEP) == [] and len(fx.links()) == 2


def test_run_due_stubs_a_due_token_retry_without_a_nudge(session):
    job = _pending(session, FailureClass.ISSUER_DOWN)  # card with a saved token: token_retry in 15m
    assert job.action == "token_retry"
    fx = FixtureRazorpayClient()
    done = run_due(session, now=NOW + timedelta(minutes=15), rz_client=fx, sleep=NOSLEEP)
    assert [j.status for j in done] == ["stubbed"] and done[0].nudge_body is None and fx.calls == []


def test_failed_outbound_walks_the_backoff_chain_to_the_stop_rule(session):
    """ISSUER_DOWN without a token: link at 15m, then x3 backoff (45m, 2h15m), max 3 attempts."""
    first = _pending(session, FailureClass.ISSUER_DOWN, index=1)
    attempt_id = first.attempt_id
    fx = FixtureRazorpayClient()
    down = FaultingRazorpayClient(fx, "razorpay_5xx")

    t1 = NOW + timedelta(seconds=900)
    assert [j.status for j in run_due(session, now=t1, rz_client=down, sleep=NOSLEEP)] == ["failed"]
    second = next_due(session, t1)
    assert second.retry_seq == 2 and second.scheduled_at == t1 + timedelta(seconds=2700)

    t2 = second.scheduled_at
    assert [j.id for j in run_due(session, now=t2, rz_client=down, sleep=NOSLEEP)] == [second.id]
    third = next_due(session, t2)
    assert third.retry_seq == 3 and third.scheduled_at == t2 + timedelta(seconds=8100)

    t3 = third.scheduled_at
    assert [j.status for j in run_due(session, now=t3, rz_client=down, sleep=NOSLEEP)] == ["failed"]
    assert next_due(session, t3) is None and run_due(session, now=t3 + timedelta(days=1), rz_client=down) == []

    jobs = session.execute(select(RecoveryJob).where(RecoveryJob.attempt_id == attempt_id).order_by(RecoveryJob.id)).scalars().all()
    assert [j.retry_seq for j in jobs] == [1, 2, 3, 4] and [j.status for j in jobs] == ["failed"] * 3 + ["no_action"]
    assert len({j.idempotency_key for j in jobs}) == 4 and all(j.attempts_made == 3 for j in jobs[:3])
    assert fx.links() == [] and down.create_calls == 9
    assert _audits(session, attempt_id, "follow-up seq 4: no_action") and _audits(session, attempt_id, "stop rule: max attempts reached")
    assert _audits(session, attempt_id, "no_action: nothing will be sent")
    o = session.execute(select(Outcome).where(Outcome.attempt_id == attempt_id)).scalar_one()
    assert o.recovered is False and o.job_id == jobs[3].id and o.note.startswith("no_action")


def test_one_client_per_sweep_pays_the_rate_limit_once(session):
    a = _pending(session, FailureClass.AUTH_ABANDONED)
    b = _pending(session, FailureClass.AUTH_ABANDONED, index=1)
    faults.activate("razorpay_429")
    razorpay_client.reset_fixture()
    try:
        sleeps = []
        done = run_due(session, now=NOW + timedelta(minutes=10), sleep=sleeps.append)
        assert [j.status for j in done] == ["sent", "sent"] and [j.attempts_made for j in done] == [3, 1]
        assert sleeps == [0.5, 1.0] and len(razorpay_client.fixture().links()) == 2
        assert {a.id, b.id} == {j.id for j in done}
    finally:
        razorpay_client.reset_fixture()


def test_run_due_reports_a_dead_database_as_unacknowledged():
    url_before = db.url()
    db.configure("sqlite:////nonexistent/dir/x.db")
    broken = db.session()
    try:
        with pytest.raises(PipelineDBError, match="event not acknowledged"):
            run_due(broken, now=NOW, rz_client=FixtureRazorpayClient(), sleep=NOSLEEP)
    finally:
        broken.close()
        db.configure(url_before)


# ---- run-due --loop (app/main.py): fresh session per sweep, stops on Ctrl-C, refuses --now ----------

def test_cli_run_due_loop_sweeps_until_interrupted_with_a_fresh_session_each_time(session, monkeypatch, capsys):
    from app import main as cli
    net = _pending(session, FailureClass.NETWORK_TIMEOUT, index=1)   # +5m, upi, no token: a link job
    session.close()
    fx = FixtureRazorpayClient()
    monkeypatch.setattr(cli, "_clients", lambda: (fx, None))
    opened = []
    real_session = db.session

    def counting_session():
        s = real_session()
        opened.append(s)
        return s
    monkeypatch.setattr(cli.db, "session", counting_session)
    # cmd_run_due reads the clock once before the loop, then once per sweep
    import itertools
    clock = itertools.chain([NOW, NOW + timedelta(minutes=1)], itertools.repeat(NOW + timedelta(minutes=6)))
    monkeypatch.setattr(cli, "utcnow", lambda: next(clock))
    sleeps = []

    def sleep(n):
        sleeps.append(n)
        if len(sleeps) == 2:
            raise KeyboardInterrupt
    monkeypatch.setattr(cli.time, "sleep", sleep)
    assert cli.main(["run-due", "--loop", "--interval", "7"]) == 0
    out = capsys.readouterr().out
    assert "sweeping every 7s" in out and out.count("sweep at") == 2 and sleeps == [7, 7]
    assert "0 job(s) executed (none); next due job#" in out and "1 job(s) executed (sent 1)" in out
    assert len(opened) == 3 and len(fx.links()) == 1  # main's own session + one per sweep
    check = db.session()
    try:
        assert check.get(RecoveryJob, net.id).status == "sent"
    finally:
        check.close()
    assert cli.main(["run-due", "--loop", "--now", "2026-09-08T12:00:00"]) == 1
    assert "cannot be combined with --now" in capsys.readouterr().err
