"""The read-only operator view: every route answers, it mirrors `show`/`audit`, it escapes,
it never writes. The server binds port 0 on loopback in a thread against the conftest database."""
import re
import threading
from datetime import datetime
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from sqlalchemy import select

from app import priority, razorpay_client, web
from app.models import PaymentAttempt, RecoveryJob
from app.pipeline import process_all
from app.taxonomy import FailureClass, JobStatus
from scripts.seed import seed

NOW = datetime(2026, 9, 5, 12, 0, 0)
NOSLEEP = lambda s: None  # noqa: E731
XSS = "<script>alert('pwned')</script>"


@pytest.fixture()
def seeded(session):
    """Seed rows, add one attempt with a hostile description, run the whole pipeline once."""
    razorpay_client.reset_fixture()
    seed(session, now=NOW)
    session.add(PaymentAttempt(
        merchant_id="acc_test", order_id="order_web_xss", razorpay_payment_id="pay_WEBXSS00000001",
        amount_paise=123456, method="card", error_code="BAD_REQUEST_ERROR", error_source="customer",
        error_step="payment_authorization", error_reason="card_declined", error_description=XSS,
        customer_name="Evil <b>Bob</b>", customer_contact="+919999999999", customer_email="bob@example.com",
        failed_at=NOW))
    session.commit()
    process_all(session, execute_now=True, now=NOW, rz_client=razorpay_client.fixture(), sleep=NOSLEEP)
    return session


@pytest.fixture()
def base_url(seeded):
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


def get(url: str, method: str = "GET") -> tuple[int, str]:
    try:
        with urlopen(Request(url, method=method), timeout=10) as r:
            return r.status, r.read().decode("utf-8")
    except HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def test_health(base_url):
    status, body = get(f"{base_url}/health")
    assert status == 200 and body.strip() == "ok"


def test_index_lists_every_attempt_with_show_columns_and_totals(base_url, seeded):
    status, body = get(f"{base_url}/")
    assert status == 200
    attempts = seeded.execute(select(PaymentAttempt)).scalars().all()
    for a in attempts:
        assert a.razorpay_payment_id in body
        assert f'href="/attempt/{a.id}"' in body
    for col in ("payment_id", "method", "amount", "class", "by", "action", "delay", "job", "status", "link", "nudge",
                "outcome"):
        assert f"<th>{col}</th>" in body
    for col in ("attempts", "sent", "human_queue", "stubbed", "failed", "links created", "recovered", "amount recovered"):
        assert f"<th>{col}</th>" in body
    assert f"attempts ({len(attempts)} of {len(attempts)} shown)" in body
    # honesty header: fixture client, no LLM key, database URL, no faults
    assert "fixture client" in body and "ANTHROPIC_API_KEY unset" in body and "sqlite:///" in body and "faults" in body
    assert 'http-equiv="refresh"' in body


def test_index_renders_payment_links_as_anchors(base_url, seeded):
    sent = seeded.execute(select(RecoveryJob).where(RecoveryJob.status == JobStatus.SENT.value,
                                                   RecoveryJob.razorpay_link_url.is_not(None))).scalars().first()
    assert sent is not None, "the seed should produce at least one sent link"
    _, body = get(f"{base_url}/")
    assert f'<a href="{sent.razorpay_link_url}"' in body


def test_index_filters_by_class_and_status(base_url, seeded):
    attempts = seeded.execute(select(PaymentAttempt)).scalars().all()
    hard = [a for a in attempts if a.decisions and a.decisions[-1].failure_class == FailureClass.HARD_DECLINE.value]
    assert hard
    _, body = get(f"{base_url}/?class={FailureClass.HARD_DECLINE.value}")
    assert f"attempts ({len(hard)} of {len(attempts)} shown)" in body
    _, body = get(f"{base_url}/?status=no_such_status")
    assert f"attempts (0 of {len(attempts)} shown)" in body and "(no rows)" in body
    # every class and status from the enums is offered, none hardcoded
    for e in list(FailureClass) + list(JobStatus):
        assert f'<option value="{e.value}"' in body


def test_attempt_page_shows_error_decision_job_and_audit_stages(base_url, seeded):
    a = seeded.get(PaymentAttempt, 1)
    status, body = get(f"{base_url}/attempt/1")
    assert status == 200
    assert a.razorpay_payment_id in body and (a.error_reason or "-") in body
    d, j = a.decisions[-1], a.jobs[-1]
    assert d.failure_class in body and d.action in body and d.classified_by in body
    assert j.idempotency_key in body and j.status in body
    assert f"audit trail for attempt 1 ({a.razorpay_payment_id}" in body
    for stage in ("classify", "policy", "schedule"):
        assert f"<td>{stage}</td>" in body
    assert "<details>" in body and "<summary>data</summary>" in body
    # also reachable by payment id
    assert get(f"{base_url}/attempt/{a.razorpay_payment_id}")[0] == 200


def test_html_is_escaped(base_url, seeded):
    a = seeded.execute(select(PaymentAttempt).where(PaymentAttempt.razorpay_payment_id == "pay_WEBXSS00000001")).scalars().one()
    for path in (f"/attempt/{a.id}", "/"):
        status, body = get(f"{base_url}{path}")
        assert status == 200
        assert "<script>" not in body and "<b>Bob</b>" not in body
    _, body = get(f"{base_url}/attempt/{a.id}")
    assert "&lt;script&gt;alert(&#x27;pwned&#x27;)&lt;/script&gt;" in body
    assert "Evil &lt;b&gt;Bob&lt;/b&gt;" in body


def test_human_queue_lists_parked_attempts_by_expected_value(base_url, seeded):
    items = priority.human_queue(seeded)
    status, body = get(f"{base_url}/human-queue")
    assert status == 200
    assert "python -m app.main resolve --attempt ID" in body
    assert f"{len(items)} attempts parked for a person, highest expected recovery first" in body
    for col in ("expected", "p", "source", "reason", "amount"):
        assert f"<th>{col}</th>" in body
    positions = [body.index(it.attempt.razorpay_payment_id) for it in items]  # already value-ordered
    assert positions == sorted(positions)
    values = [it.estimate.expected_paise for it in items]
    assert values == sorted(values, reverse=True) and values[0] > 0
    assert "simulation prior" in body and "python -m app.main queue" in body
    assert 'http-equiv="refresh"' in body


METRIC_LINE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{([a-zA-Z_][a-zA-Z0-9_]*="[^"]*"(,[a-zA-Z_][a-zA-Z0-9_]*="[^"]*")*)?\})? (-?\d+(\.\d+)?)$')
EXPECTED_METRICS = {"pra_build_info", "pra_attempts_total", "pra_jobs", "pra_links_created_total", "pra_recovered_total",
                    "pra_recovered_amount_paise", "pra_human_queue_size", "pra_llm_decisions_total",
                    "pra_llm_fallbacks_total", "pra_open_links"}


def test_metrics_are_prometheus_text_format_computed_from_the_database(base_url, seeded):
    import urllib.request
    with urllib.request.urlopen(f"{base_url}/metrics", timeout=10) as r:
        assert r.status == 200 and r.headers["Content-Type"] == "text/plain; version=0.0.4; charset=utf-8"
        body = r.read().decode("utf-8")
    assert body.endswith("\n")
    samples: dict[str, dict[str, str]] = {}
    for line in body.splitlines():
        if line.startswith("# HELP ") or line.startswith("# TYPE "):
            assert len(line.split(" ", 3)) == 4
            continue
        m = METRIC_LINE.match(line)
        assert m, f"not a Prometheus sample line: {line!r}"
        samples[m.group(1) + (m.group(2) or "")] = m.group(5)
    names = {k.split("{")[0] for k in samples}
    assert EXPECTED_METRICS <= names
    assert samples['pra_build_info{version="0.1"}'] == "1"
    # the numbers are the ones the index page shows
    attempts = seeded.execute(select(PaymentAttempt)).scalars().all()
    assert sum(int(v) for k, v in samples.items() if k.startswith("pra_attempts_total{")) == len(attempts)
    parked = [j for j in web.latest_jobs(seeded).values() if j.status == JobStatus.HUMAN_QUEUE.value]
    assert samples["pra_human_queue_size"] == str(len(parked)) == samples['pra_jobs{status="human_queue"}']
    assert samples['pra_llm_decisions_total{by="fallback"}'] == str(len([a for a in attempts if a.decisions and a.decisions[-1].classified_by == "fallback"]))
    assert samples['pra_llm_fallbacks_total{fallback="llm_unavailable->human_queue"}'] == samples['pra_llm_decisions_total{by="fallback"}']
    assert int(samples["pra_links_created_total"]) == sum(1 for j in seeded.execute(select(RecoveryJob)).scalars() if j.razorpay_link_id)
    for cls in FailureClass:  # every class and status is always present, so a dashboard never sees a gap
        assert f'pra_attempts_total{{class="{cls.value}"}}' in samples
    for st in JobStatus:
        assert f'pra_jobs{{status="{st.value}"}}' in samples
    assert "pra_human_queue_expected_paise" in samples and "pra_merchant_overrides" in samples
    # a HEAD is answered too, and it is unauthenticated like every other page
    assert get(f"{base_url}/metrics", method="HEAD")[0] == 200


def test_policy_page_renders_the_table_and_a_merchant_view(base_url):
    status, body = get(f"{base_url}/policy")
    assert status == 200 and "<th>override</th>" in body and "INSUFFICIENT_FUNDS" in body and "48h" in body
    status, body = get(f"{base_url}/policy?merchant=merchant_acme")
    assert status == 200 and "merchant_acme" in body and "no override file" in body


def test_reports_render_or_explain_how_to_generate(base_url):
    for name, make_cmd in (("failure", "make chaos"), ("simulation", "make simulate")):
        status, body = get(f"{base_url}/reports/{name}")
        assert status == 200
        path = web.REPORTS[name][0]
        if path.is_file():
            first_line = path.read_text(encoding="utf-8").splitlines()[0]
            assert web.esc(first_line) in body
        else:
            assert "is not there yet" in body and make_cmd in body
    assert get(f"{base_url}/reports/nope")[0] == 404


def test_unknown_paths_are_404_and_writes_are_refused(base_url, seeded):
    for path in ("/nope", "/attempt/", "/attempt/999999", "/attempt/abc", "/static/x.js"):
        assert get(f"{base_url}{path}")[0] == 404, path
    before = seeded.execute(select(RecoveryJob.id)).all()
    assert get(f"{base_url}/", method="POST")[0] == 405
    assert seeded.execute(select(RecoveryJob.id)).all() == before


def test_pages_carry_no_scripts_or_external_assets(base_url):
    for path in ("/", "/attempt/1", "/human-queue", "/reports/failure", "/policy"):
        _, body = get(f"{base_url}{path}")
        assert "<script" not in body and 'src="http' not in body and '<link' not in body
        assert "read-only, no authentication, loopback by default" in body


def test_cli_defaults_to_loopback_and_refuses_a_busy_port(seeded, capsys):
    web.Handler.quiet = True
    server = web.make_server("127.0.0.1", 0)
    try:
        port = server.server_address[1]
        assert web.main(["--port", str(port)]) == 1  # already bound: refuse, do not hang
        assert "cannot bind 127.0.0.1" in capsys.readouterr().err
    finally:
        server.server_close()


# ---- operator actions (POST /action/*, docs/web.md) -----------------------------------------------

import http.client  # noqa: E402
from urllib.parse import urlencode, urlsplit  # noqa: E402

from app import actions, config  # noqa: E402
from app.models import AuditEvent, Outcome  # noqa: E402

TOKEN = "s3cret-operator-token"


def post(url: str, fields: dict) -> tuple[int, str, str]:
    """(status, body, Location) without following redirects."""
    u = urlsplit(url)
    conn = http.client.HTTPConnection(u.hostname, u.port, timeout=10)
    body = urlencode(fields)
    conn.request("POST", u.path, body=body, headers={"Content-Type": "application/x-www-form-urlencoded",
                                                     "Content-Length": str(len(body))})
    r = conn.getresponse()
    out = (r.status, r.read().decode("utf-8"), r.getheader("Location") or "")
    conn.close()
    return out


def queued_attempt_id(session) -> int:
    parked = [aid for aid, j in web.latest_jobs(session).items() if j.status == JobStatus.HUMAN_QUEUE.value]
    assert parked
    return min(parked)


def test_actions_are_refused_and_invisible_without_a_token(base_url, seeded, monkeypatch):
    monkeypatch.setattr(config, "OPERATOR_TOKEN", "", raising=False)
    monkeypatch.delenv("OPERATOR_TOKEN", raising=False)
    aid = queued_attempt_id(seeded)
    before = len(seeded.execute(select(Outcome)).scalars().all())
    status, body, _ = post(f"{base_url}/action/resolve", {"attempt": aid, "mode": "closed", "actor": "asha", "confirm": "1"})
    assert status == 403 and "set OPERATOR_TOKEN" in body
    assert len(seeded.execute(select(Outcome)).scalars().all()) == before
    for path in (f"/attempt/{aid}", "/human-queue", "/insights"):
        _, page_body = get(f"{base_url}{path}")
        assert "/action/" not in page_body and 'name="token"' not in page_body and "<script" not in page_body
        assert "set OPERATOR_TOKEN" in page_body or path == "/insights"
    assert post(f"{base_url}/", {"x": "1"})[0] == 405


def test_wrong_token_or_missing_actor_is_refused(base_url, seeded, monkeypatch):
    monkeypatch.setattr(config, "OPERATOR_TOKEN", TOKEN, raising=False)
    aid = queued_attempt_id(seeded)
    fields = {"attempt": aid, "mode": "closed", "confirm": "1", "back": f"/attempt/{aid}"}
    assert post(f"{base_url}/action/resolve", {**fields, "actor": "asha"})[0] == 403                  # no token
    assert post(f"{base_url}/action/resolve", {**fields, "actor": "asha", "token": "nope"})[0] == 403  # wrong token
    status, body, _ = post(f"{base_url}/action/resolve", {**fields, "token": TOKEN})                 # no actor
    assert status == 400 and "actor is required" in body
    assert post(f"{base_url}/action/resolve", {**fields, "token": TOKEN, "actor": "x" * 41})[0] == 400
    assert not seeded.execute(select(AuditEvent).where(AuditEvent.stage == actions.STAGE)).scalars().all()


def test_resolve_from_the_browser_confirms_then_redirects_with_a_flash(base_url, seeded, monkeypatch):
    monkeypatch.setattr(config, "OPERATOR_TOKEN", TOKEN, raising=False)
    aid = queued_attempt_id(seeded)
    # the forms are there for the operator, still no JavaScript, and the footer says actions are on
    _, page_body = get(f"{base_url}/attempt/{aid}")
    assert 'action="/action/resolve"' in page_body and 'type="password" name="token"' in page_body
    assert "<script" not in page_body and "Operator actions are ON" in page_body
    _, hq = get(f"{base_url}/human-queue")
    assert "<th>resolve</th>" in hq and 'action="/action/resolve"' in hq and 'http-equiv="refresh"' not in hq
    fields = {"attempt": aid, "mode": "recovered", "recovered_paise": "249900", "note": "paid by <NEFT>", "actor": "asha",
              "token": TOKEN, "back": f"/attempt/{aid}"}
    status, body, _ = post(f"{base_url}/action/resolve", fields)  # step one: a confirmation page, nothing written
    assert status == 200 and "Confirm <b>resolve</b>" in body and 'name="confirm" value="1"' in body
    assert "paid by &lt;NEFT&gt;" in body and TOKEN not in body.replace(f'value="{TOKEN}"', "")  # echoed only as the hidden field
    assert not seeded.execute(select(Outcome).where(Outcome.recovered.is_(True), Outcome.attempt_id == aid)).first()
    status, body, location = post(f"{base_url}/action/resolve", {**fields, "confirm": "1"})
    assert status == 303 and location.startswith(f"/attempt/{aid}?msg=") and "recovered" in location
    o = seeded.execute(select(Outcome).where(Outcome.attempt_id == aid, Outcome.recovered.is_(True))).scalars().one()
    assert o.amount_recovered_paise == 249900
    row = seeded.execute(select(AuditEvent).where(AuditEvent.attempt_id == aid, AuditEvent.stage == actions.STAGE)).scalars().one()
    assert "resolve by asha" in row.message
    status, body = get(f"{base_url}{location}")
    assert status == 200 and '<p class="flash">' in body and "recovered Rs 2,499.00" in body
    # a refusal comes back the same way, escaped, and writes nothing
    status, _, location = post(f"{base_url}/action/resolve", {**fields, "confirm": "1"})
    assert status == 303 and "?err=" in location
    _, body = get(f"{base_url}{location}")
    assert 'class="flash err"' in body and "already has an outcome" in body
    _, body = get(f"{base_url}/attempt/{aid}?msg=" + "%3Cscript%3Ealert(1)%3C/script%3E")
    assert "<script" not in body and "&lt;script&gt;" in body


def test_cancel_resend_retry_and_apply_routes(base_url, seeded, monkeypatch):
    monkeypatch.setattr(config, "OPERATOR_TOKEN", TOKEN, raising=False)
    sent = seeded.execute(select(RecoveryJob).where(RecoveryJob.status == JobStatus.SENT.value,
                                                   RecoveryJob.razorpay_link_id.is_not(None))).scalars().first()
    _, page_body = get(f"{base_url}/attempt/{sent.attempt_id}")
    assert 'action="/action/cancel"' in page_body and 'action="/action/resend"' in page_body
    creds = {"actor": "ravi", "token": TOKEN, "confirm": "1", "back": f"/attempt/{sent.attempt_id}"}
    # resend: notifications are off in tests -> refused, nothing sent, no crash
    status, _, location = post(f"{base_url}/action/resend", {**creds, "job": sent.id, "medium": "sms"})
    assert status == 303 and "?err=" in location and "RAZORPAY_NOTIFY_CUSTOMER" in location
    # retry on a sent job -> refused
    status, _, location = post(f"{base_url}/action/retry", {**creds, "job": sent.id})
    assert status == 303 and "not+pending" in location or "not%20pending" in location
    # cancel the live link -> job cancelled, outcome written, actor in the trail
    status, _, location = post(f"{base_url}/action/cancel", {**creds, "job": sent.id, "reason": "duplicate order"})
    assert status == 303 and "?msg=" in location
    seeded.expire_all()
    assert seeded.get(RecoveryJob, sent.id).status == JobStatus.CANCELLED.value
    assert razorpay_client.fixture().fetch_payment_link(sent.razorpay_link_id)["status"] == "cancelled"
    assert any("cancel by ravi" in r.message for r in seeded.execute(
        select(AuditEvent).where(AuditEvent.attempt_id == sent.attempt_id, AuditEvent.stage == actions.STAGE)).scalars())
    # apply-proposal with an id the current evidence does not support -> refused, no file written
    status, _, location = post(f"{base_url}/action/apply-proposal",
                               {**creds, "proposal": "INSUFFICIENT_FUNDS-48h-to-24h", "merchant": "all", "back": "/insights"})
    assert status == 303 and location.startswith("/insights?err=") and "no+current+proposal" in location.replace("%20", "+")
    _, body = get(f"{base_url}/insights")
    assert "python -m app.merchants apply" in body
