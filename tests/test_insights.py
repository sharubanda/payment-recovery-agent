"""The insights report: the Wilson interval against known values, bucket assignment, the proposal
rule (non-overlapping intervals with enough n -> proposal; overlapping or small n -> insufficient
evidence), an empty database, the demo database (small n everywhere -> every proposal says
insufficient evidence, which is the honest result), and the CLI. Rows go in through the ORM."""
import hashlib
import random
from datetime import datetime, timedelta

import pytest

from app import insights, razorpay_client
from app.insights import Rate, bucket_for, compare_buckets, compute, wilson_interval
from app.models import Outcome, PaymentAttempt, RecoveryDecision, RecoveryJob
from app.pipeline import poll_outcomes, process_all
from app.policy import POLICY
from app.taxonomy import Action, FailureClass, JobStatus
from scripts.seed import seed

NOW = datetime(2026, 9, 5, 12, 0, 0)
NOSLEEP = lambda s: None  # noqa: E731


# ---- test helper: synthetic outcomes, in tests only ---------------------------------------------

def simulate_outcomes(session, n: int, seed: int, *, failure_class: FailureClass = FailureClass.INSUFFICIENT_FUNDS,
                      bucket_rates: dict[int, float] | None = None, merchant: str = "merchant_sim",
                      classified_by: str = "rules", status: str = JobStatus.SENT.value) -> list[PaymentAttempt]:
    """Insert n attempts for one class, each with a decision, a sent job scheduled with one of the
    delays in bucket_rates (round-robin) and an outcome drawn with that delay's recovery
    probability. Deterministic in seed. Lives here, not in app/, on purpose: the agent never
    simulates its own evidence."""
    rng = random.Random(seed)
    bucket_rates = bucket_rates or {POLICY[failure_class]["delay_seconds"]: 0.4}
    delays = list(bucket_rates.items())
    rows = []
    for i in range(n):
        delay, p = delays[i % len(delays)]
        pid = f"pay_SIM{seed:04d}{i:07d}"[:18]
        a = PaymentAttempt(merchant_id=merchant, order_id=f"order_sim_{seed}_{i}", razorpay_payment_id=pid,
                           amount_paise=1000 * (i + 1), method="card", error_code="BAD_REQUEST_ERROR",
                           error_source="customer", error_step="payment_authorization", error_reason="payment_failed",
                           error_description="insufficient funds", customer_contact="+915800000000", failed_at=NOW)
        session.add(a)
        session.flush()
        d = RecoveryDecision(attempt_id=a.id, failure_class=failure_class.value, action=Action.RECOVERY_LINK.value,
                             delay_seconds=delay, max_attempts=2, reason="simulated", classified_by=classified_by,
                             llm_used=classified_by == "llm", confidence=1.0 if classified_by == "rules" else 0.9)
        session.add(d)
        session.flush()
        key = hashlib.sha256(f"{pid}:1".encode()).hexdigest()
        executed = NOW + timedelta(seconds=delay)
        j = RecoveryJob(attempt_id=a.id, decision_id=d.id, retry_seq=1, action=Action.RECOVERY_LINK.value,
                        scheduled_at=executed, idempotency_key=key, status=status, created_at=NOW,
                        executed_at=executed if status == JobStatus.SENT.value else None,
                        razorpay_link_id=f"plink_sim{i}" if status == JobStatus.SENT.value else None)
        session.add(j)
        session.flush()
        if status == JobStatus.SENT.value and rng.random() < p:
            session.add(Outcome(attempt_id=a.id, job_id=j.id, recovered=True, recovered_at=executed + timedelta(hours=2),
                                amount_recovered_paise=a.amount_paise, note="payment_link.paid via poll (simulated)"))
        elif status == JobStatus.HUMAN_QUEUE.value:
            session.add(Outcome(attempt_id=a.id, job_id=j.id, recovered=False, note="human_queue: simulated"))
        rows.append(a)
    session.commit()
    return rows


# ---- Wilson ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("k,n,lo,hi", [
    (5, 10, 0.2366, 0.7634),
    (50, 100, 0.4038, 0.5962),
    (0, 30, 0.0, 0.1135),
    (30, 30, 0.8865, 1.0),
    (1, 1, 0.2065, 1.0),
])
def test_wilson_matches_known_values(k, n, lo, hi):
    got_lo, got_hi = wilson_interval(k, n)
    assert got_lo == pytest.approx(lo, abs=5e-4)
    assert got_hi == pytest.approx(hi, abs=5e-4)


def test_wilson_handles_no_trials_and_stays_in_unit_interval():
    assert wilson_interval(0, 0) == (0.0, 1.0)  # no trials: the rate could be anything, never a point at zero
    for k, n in ((0, 1), (1, 1), (0, 3), (3, 3), (7, 9), (400, 1000)):
        lo, hi = wilson_interval(k, n)
        assert 0.0 <= lo <= k / n <= hi <= 1.0
    assert wilson_interval(-3, 5) == wilson_interval(0, 5) and wilson_interval(9, 5) == wilson_interval(5, 5)


def test_wilson_narrows_with_n():
    lo5, hi5 = wilson_interval(5, 10)
    lo50, hi50 = wilson_interval(50, 100)
    assert hi5 - lo5 > hi50 - lo50


# ---- buckets ------------------------------------------------------------------------------------

@pytest.mark.parametrize("seconds,bucket", [
    (0, "now"), (None, "now"), (-5, "now"), (1, "<=15m"), (600, "<=15m"), (900, "<=15m"), (901, "<=1h"), (3600, "<=1h"),
    (3601, "<=24h"), (86400, "<=24h"), (86401, "<=48h"), (172800, "<=48h"), (172801, ">48h"), (10 ** 9, ">48h"),
])
def test_bucket_assignment(seconds, bucket):
    assert bucket_for(seconds) == bucket


def test_policy_buckets_follow_the_table():
    assert insights.policy_bucket("INSUFFICIENT_FUNDS") == "<=48h"
    assert insights.policy_bucket("AUTH_ABANDONED") == "<=15m"
    assert insights.policy_bucket("LIMIT_EXCEEDED") == "<=24h"
    assert insights.policy_bucket("HARD_DECLINE") == "now"
    assert insights.policy_bucket("RISK_BLOCKED") is None and insights.policy_bucket("UNKNOWN") is None
    assert insights.policy_bucket("NOT_A_CLASS") is None


# ---- proposal rule -------------------------------------------------------------------------------

def test_proposal_when_intervals_do_not_overlap_and_n_is_enough():
    buckets = {"<=48h": Rate(n=40, k=8), "<=1h": Rate(n=40, k=30)}
    p = compare_buckets("<=48h", buckets, 30, "X")
    assert p.kind == "proposal" and p.is_proposal
    assert "<=1h" in p.text and "Not applied" in p.text and "app/policy.py" in p.text


def test_supports_current_when_the_other_bucket_is_clearly_worse():
    buckets = {"<=48h": Rate(n=40, k=30), "<=1h": Rate(n=40, k=8)}
    p = compare_buckets("<=48h", buckets, 30)
    assert p.kind == "supports_current" and "no change" in p.text


def test_insufficient_when_intervals_overlap():
    buckets = {"<=48h": Rate(n=40, k=16), "<=1h": Rate(n=40, k=20)}
    p = compare_buckets("<=48h", buckets, 30)
    assert p.kind == "insufficient" and "overlaps" in p.text and not p.is_proposal


def test_insufficient_when_n_is_small_even_if_rates_differ_wildly():
    p = compare_buckets("<=48h", {"<=48h": Rate(n=5, k=0), "<=1h": Rate(n=5, k=5)}, 30)
    assert p.kind == "insufficient" and "n=5" in p.text and "need 30" in p.text
    # the policy bucket has enough, the competitor does not
    p = compare_buckets("<=48h", {"<=48h": Rate(n=40, k=4), "<=1h": Rate(n=6, k=6)}, 30)
    assert p.kind == "insufficient" and "<=1h n=6" in p.text
    # the policy bucket alone has data
    p = compare_buckets("<=48h", {"<=48h": Rate(n=100, k=50)}, 30)
    assert p.kind == "insufficient" and "no other bucket" in p.text
    # nothing at all
    p = compare_buckets("<=48h", {}, 30)
    assert p.kind == "insufficient" and "n=0" in p.text


def test_terminal_classes_are_not_tuned():
    p = compare_buckets(None, {"now": Rate(n=100, k=100)}, 30, "RISK_BLOCKED")
    assert p.kind == "not_applicable" and not p.is_proposal


def test_min_samples_is_an_argument():
    buckets = {"<=48h": Rate(n=12, k=1), "<=1h": Rate(n=12, k=11)}
    assert compare_buckets("<=48h", buckets, 30).kind == "insufficient"
    assert compare_buckets("<=48h", buckets, 10).kind == "proposal"


# ---- from the database ---------------------------------------------------------------------------

def test_empty_database_says_no_outcomes_yet(session):
    report = compute(session)
    assert report.empty and report.attempts == 0
    text = insights.render_text(report)
    assert "no outcomes yet" in text
    md = insights.render_markdown(report)
    assert "No outcomes yet" in md
    assert not any(p.is_proposal for p in report.proposals)


def test_simulated_evidence_produces_a_proposal_only_with_enough_n(session):
    # 40 links at the policy delay (48h) recovering ~10%, 40 at 1h recovering ~80%: non-overlapping
    simulate_outcomes(session, 80, seed=7, bucket_rates={48 * 3600: 0.10, 3600: 0.80})
    report = compute(session, min_samples=30)
    cs = next(c for c in report.classes if c.failure_class == "INSUFFICIENT_FUNDS")
    assert cs.attempts == 80 and cs.sent == 80 and cs.rate.n == 80
    assert cs.buckets["<=48h"].n == 40 and cs.buckets["<=1h"].n == 40
    assert cs.recovered == cs.rate.k == cs.buckets["<=48h"].k + cs.buckets["<=1h"].k
    assert cs.avg_ttr_seconds == pytest.approx(2 * 3600)
    prop = next(p for p in report.proposals if p.failure_class == "INSUFFICIENT_FUNDS")
    assert prop.is_proposal and "<=1h" in prop.text
    text = insights.render_text(report)
    assert "PROPOSAL" in text and "1 proposal(s)" in text and "never applied" in text
    # the same evidence with a stricter bar is not enough
    strict = compute(session, min_samples=50)
    assert not any(p.is_proposal for p in strict.proposals)
    assert "insufficient evidence" in next(p.text for p in strict.proposals if p.failure_class == "INSUFFICIENT_FUNDS")


def test_rules_gap_flag_and_money_by_merchant(session):
    simulate_outcomes(session, 6, seed=1, merchant="m_a", failure_class=FailureClass.AUTH_ABANDONED,
                      bucket_rates={600: 1.0})
    simulate_outcomes(session, 4, seed=2, merchant="m_b", failure_class=FailureClass.AUTH_ABANDONED,
                      status=JobStatus.HUMAN_QUEUE.value, classified_by="llm")
    report = compute(session)
    cs = next(c for c in report.classes if c.failure_class == "AUTH_ABANDONED")
    assert cs.attempts == 10 and cs.human_queued == 4 and cs.human_share == pytest.approx(0.4) and cs.rules_gap
    assert "rules gap: see propose-rules" in insights.render_text(report)
    money = {m.merchant_id: m for m in report.money}
    assert money["m_a"].recovered_paise == money["m_a"].at_risk_paise == sum(1000 * (i + 1) for i in range(6))
    assert money["m_a"].open_paise == 0 and money["m_a"].parked_paise == 0
    assert money["m_b"].parked_paise == money["m_b"].at_risk_paise and money["m_b"].recovered_paise == 0
    assert report.llm.decisions == 10 and report.llm.llm_used == 4 and report.llm.share == pytest.approx(0.4)
    only_b = compute(session, merchant="m_b")
    assert only_b.attempts == 4 and [m.merchant_id for m in only_b.money] == ["m_b"]


def test_by_design_human_queue_classes_are_not_a_rules_gap(session):
    simulate_outcomes(session, 5, seed=3, failure_class=FailureClass.RISK_BLOCKED, status=JobStatus.HUMAN_QUEUE.value)
    simulate_outcomes(session, 5, seed=4, failure_class=FailureClass.UNKNOWN, status=JobStatus.HUMAN_QUEUE.value,
                      classified_by="fallback")
    report = compute(session)
    by = {c.failure_class: c for c in report.classes}
    assert not by["RISK_BLOCKED"].rules_gap and by["UNKNOWN"].rules_gap


def test_llm_latency_percentiles_and_fallbacks(session):
    rows = simulate_outcomes(session, 5, seed=5, classified_by="llm")
    for i, a in enumerate(rows):
        d = a.decisions[0]
        d.llm_latency_ms = (i + 1) * 100
        d.llm_model = "scripted-model"
        if i == 4:
            d.classified_by, d.fallback_taken = "fallback", "llm_timeout->human_queue"
    session.commit()
    report = compute(session)
    assert report.llm.median_ms == 300 and report.llm.p95_ms == 500
    assert report.llm.fallbacks == {"llm_timeout->human_queue": 1}
    assert report.llm.by_source["llm"] == 4 and report.llm.by_source["fallback"] == 1
    assert "llm_timeout->human_queue" in insights.render_text(report)


# ---- the demo database: small n everywhere, and the report says so ------------------------------

@pytest.fixture()
def demo_db(session):
    razorpay_client.reset_fixture()
    seed(session, now=NOW)
    fx = razorpay_client.fixture()
    process_all(session, execute_now=True, now=NOW, rz_client=fx, sleep=NOSLEEP)
    sent = session.query(RecoveryJob).filter(RecoveryJob.status == JobStatus.SENT.value,
                                             RecoveryJob.razorpay_link_id.is_not(None)).order_by(RecoveryJob.id).all()
    for i, j in enumerate(sent):
        if i % 2 == 0:
            fx.mark_paid(j.razorpay_link_id)
    poll_outcomes(session, rz_client=fx, now=NOW + timedelta(minutes=5))
    return session


def test_demo_database_is_honest_about_small_n(demo_db):
    report = compute(demo_db)
    assert report.attempts == 22
    assert not any(p.is_proposal for p in report.proposals), [p.text for p in report.proposals]
    tuned = [p for p in report.proposals if p.kind != "not_applicable"]
    assert tuned and all(p.kind == "insufficient" and "insufficient evidence (n=" in p.text for p in tuned)
    assert {p.failure_class for p in report.proposals if p.kind == "not_applicable"} == {"RISK_BLOCKED", "UNKNOWN"}
    by = {c.failure_class: c for c in report.classes}
    assert by["UNKNOWN"].human_queued == 3 and by["UNKNOWN"].rules_gap and not by["RISK_BLOCKED"].rules_gap
    assert sum(c.recovered for c in report.classes) == 8
    assert report.llm.fallbacks == {"llm_unavailable->human_queue": 3}
    text = insights.render_text(report)
    assert "0 proposal(s)" in text and "rules gap (human-queue share > 30%): UNKNOWN" in text
    assert "not a bandit" in text
    for cls in ("INSUFFICIENT_FUNDS", "AUTH_ABANDONED", "HARD_DECLINE", "LIMIT_EXCEEDED"):
        assert cls in text
    md = insights.render_markdown(report)
    assert "| INSUFFICIENT_FUNDS |" in md and "## Proposals (never applied)" in md


def test_cli_prints_and_writes_markdown(demo_db, tmp_path, capsys):
    out = tmp_path / "insights_report.md"
    assert insights.main(["--write", str(out), "--min-samples", "5"]) == 0
    printed = capsys.readouterr().out
    assert "min-samples: 5" in printed and "== proposals (never applied)" in printed and f"wrote {out}" in printed
    assert out.read_text(encoding="utf-8").startswith("# Insights report")
    assert insights.main(["--merchant", "merchant_acme"]) == 0
    assert "(merchant merchant_acme)" in capsys.readouterr().out


# ---- web routes ----------------------------------------------------------------------------------

@pytest.fixture()
def base_url(demo_db):
    import threading

    from app import web
    web.Handler.quiet = True
    server = web.make_server("127.0.0.1", 0)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        t.join(timeout=5)


def _get(url: str) -> tuple[int, str]:
    from urllib.error import HTTPError
    from urllib.request import urlopen
    try:
        with urlopen(url, timeout=10) as r:
            return r.status, r.read().decode("utf-8")
    except HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def test_web_insights_route_renders_the_report(base_url):
    status, body = _get(f"{base_url}/insights")
    assert status == 200
    for col in ("class", "attempts", "sent", "recovered", "rate (95% Wilson)", "policy bucket", "fallback_taken", "merchant",
                "verdict"):
        assert f"<th>{col}</th>" in body, col
    assert "INSUFFICIENT_FUNDS" in body and "insufficient evidence (n=" in body and "not a bandit" in body
    assert "llm_unavailable-&gt;human_queue" in body
    assert 'href="/rule-candidates"' in body and "<script" not in body
    status, body = _get(f"{base_url}/insights?min_samples=2&merchant=merchant_acme")
    assert status == 200 and "(merchant merchant_acme)" in body and "n &gt;= 2" in body
    assert _get(f"{base_url}/insights?min_samples=abc")[0] == 200


def test_web_insights_route_on_empty_database(session):
    from app import web
    assert "no outcomes yet" in web.render_insights(session, {})
    assert "no model-path decisions yet" in web.render_rule_candidates(session, {})


def test_web_rule_candidates_route_renders_candidates(base_url):
    status, body = _get(f"{base_url}/rule-candidates")
    assert status == 200 and "scanned 3 model-path decisions" in body and "candidates (0)" in body
    status, body = _get(f"{base_url}/rule-candidates?min_occurrences=1")
    assert status == 200 and "candidates (3)" in body and "needs a human label" in body
    assert "Do not honour" in body and "<pre>" in body and "review and add tests/fixtures/failure_cases.json" in body
    # descriptions only: no seed customer name, contact or email
    assert "Varun Kulkarni" not in body and "+915922334455" not in body and "varun.kulkarni@example.com" not in body
    assert "<script" not in body
