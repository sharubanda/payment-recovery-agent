"""pra init / doctor / serve / digest / alert-test (app/ops.py): the product surface.
Every test runs against the conftest database; nothing here touches the network."""
import io
import json
import threading
import urllib.request
from datetime import datetime
from urllib.error import HTTPError

import pytest

from app import config, db, main, ops, razorpay_client, web
from app.models import RecoveryJob
from app.pipeline import process_all
from app.taxonomy import JobStatus
from scripts.seed import seed

NOW = datetime(2026, 9, 5, 12, 0, 0)
NOSLEEP = lambda s: None  # noqa: E731


def _args(**kw):
    """The init namespace with every flag at its default, overridden by kw."""
    base = dict(yes=True, force=False, env=None, mode=None, key_id=None, key_secret=None, anthropic_key=None,
                webhook_secret=None, alert_webhook_url=None, operator_token=None, database_url=None,
                skip_verify=False, no_doctor=True, port=8080)
    base.update(kw)
    return type("Args", (), base)()


def get(url: str, data: bytes | None = None) -> tuple[int, str]:
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode("utf-8")
    except HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


# ---- init ----------------------------------------------------------------------------------------

def test_init_writes_env_from_flags_and_refuses_to_overwrite(tmp_path):
    env = tmp_path / ".env"
    out = []
    rc = ops.cmd_init(_args(env=str(env), mode="shadow", webhook_secret="whs_fixed", alert_webhook_url="https://hooks.example/x"),
                      out=out.append)
    assert rc == 0
    text = env.read_text()
    assert "PRA_MODE=shadow" in text
    assert "RAZORPAY_WEBHOOK_SECRET=whs_fixed" in text
    assert "ALERT_WEBHOOK_URL=https://hooks.example/x" in text
    assert "RAZORPAY_KEY_ID=\n" in text  # fixture mode
    assert "# ---- Razorpay (TEST MODE ONLY)" in text  # .env.example's comments survive
    assert any("Settings -> Webhooks" in line for line in out)
    assert any("payment_link.expired" in line for line in out)
    assert any("ngrok http 8080" in line for line in out)
    assert any("shadow" in line and "NO outbound call" in line for line in out)
    # a second run must not touch the file
    before = env.read_text()
    rc = ops.cmd_init(_args(env=str(env), mode="live"), out=out.append)
    assert rc == 1
    assert env.read_text() == before
    # --force does
    rc = ops.cmd_init(_args(env=str(env), mode="live", force=True), out=out.append)
    assert rc == 0
    assert "PRA_MODE=live" in env.read_text()


def test_init_generates_a_webhook_secret(tmp_path):
    env = tmp_path / ".env"
    assert ops.cmd_init(_args(env=str(env)), out=lambda s: None) == 0
    assert len(ops.read_env_file(env)["RAZORPAY_WEBHOOK_SECRET"]) == 48


def test_init_refuses_live_key(tmp_path):
    env = tmp_path / ".env"
    out = []
    rc = ops.cmd_init(_args(env=str(env), key_id="rzp_live_ABCDEFGH", key_secret="s"), out=out.append)
    assert rc == 1 and not env.exists()
    assert any("not a test-mode key" in line for line in out)


def test_init_verifies_test_keys_with_one_read_only_call(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(ops, "verify_razorpay_keys", lambda k, s: (calls.append((k, s)) or (True, "200 ok")))
    env = tmp_path / ".env"
    rc = ops.cmd_init(_args(env=str(env), key_id="rzp_test_ABCDEFGH", key_secret="sekret"), out=lambda s: None)
    assert rc == 0 and calls == [("rzp_test_ABCDEFGH", "sekret")]
    assert ops.read_env_file(env)["RAZORPAY_KEY_ID"] == "rzp_test_ABCDEFGH"
    # a rejected pair writes nothing
    monkeypatch.setattr(ops, "verify_razorpay_keys", lambda k, s: (False, "401"))
    out = []
    rc = ops.cmd_init(_args(env=str(tmp_path / "other.env"), key_id="rzp_test_ABCDEFGH", key_secret="bad"), out=out.append)
    assert rc == 1 and not (tmp_path / "other.env").exists()
    assert any("rejected" in line for line in out)


def test_verify_razorpay_keys_uses_the_live_client(monkeypatch):
    seen = []

    def fake_request(self, method, path, body=None):
        seen.append((method, path))
        return {"entity": "collection", "count": 1, "items": [{}]}

    monkeypatch.setattr(razorpay_client.LiveRazorpayClient, "_request", fake_request)
    ok, msg = ops.verify_razorpay_keys("rzp_test_ABCDEFGH", "s")
    assert ok and seen == [("GET", "/payments?count=1")] and "200" in msg

    def failing(self, method, path, body=None):
        raise razorpay_client.RazorpayError(401, "BAD_REQUEST_ERROR", "Authentication failed")

    monkeypatch.setattr(razorpay_client.LiveRazorpayClient, "_request", failing)
    ok, msg = ops.verify_razorpay_keys("rzp_test_ABCDEFGH", "s")
    assert not ok and "401" in msg
    assert ops.verify_razorpay_keys("rzp_live_X", "s")[0] is False  # the client itself refuses


def test_key_and_llm_format_checks():
    assert ops.check_key_id("")[0] == "fixture"
    assert ops.check_key_id("rzp_test_x")[0] == "test"
    assert ops.check_key_id("rzp_live_x")[0] == "refused"
    assert ops.check_anthropic_key("")[0] == "absent"
    assert ops.check_anthropic_key("sk-ant-api03-" + "x" * 30)[0] == "present"
    assert ops.check_anthropic_key("hunter2")[0] == "malformed"


# ---- doctor --------------------------------------------------------------------------------------

def test_doctor_clean_db_passes_and_exits_0(session, capsys):
    rc = main.main(["doctor", "--port", "0", "--web-port", "0"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "PASS database" in out and "PASS pipeline" in out and "synthetic payment.failed" in out
    assert "FAIL" not in out.replace("0 FAIL", "")
    assert "mode" in out and "-> ready" in out
    # the smoke run never touched the configured database
    assert session.query(RecoveryJob).count() == 0


def test_doctor_unreadable_db_exits_1(session, capsys):
    url = db.url()
    db.configure(main.UNOPENABLE_DB_URL)
    try:
        rc = main.main(["doctor", "--port", "0", "--web-port", "0"])
    finally:
        db.configure(url)
    out = capsys.readouterr().out
    assert rc == 1 and "FAIL database" in out and "-> not ready" in out


def test_doctor_reports_refused_key_and_shadow_mode():
    checks = {c.name: c for c in ops.run_doctor(env={"RAZORPAY_KEY_ID": "rzp_live_X", "PRA_MODE": "shadow"}, port=0, web_port=0)}
    assert checks["razorpay keys"].level == "FAIL"
    assert checks["mode"].level == "PASS" and "shadow" in checks["mode"].detail


# ---- serve ---------------------------------------------------------------------------------------

@pytest.fixture()
def served(session):
    razorpay_client.reset_fixture()
    web.Handler.quiet = True
    log: list[str] = []
    srv = ops.build_serve(host="127.0.0.1", port=0, web_port=0, interval=3600, out=log.append)
    srv.start()
    try:
        yield srv, log
    finally:
        srv.shutdown()


def test_serve_answers_health_on_both_servers_then_stops(served):
    srv, log = served
    status, body = get(srv.webhook_url.replace("/razorpay/webhook", "/health"))
    assert status == 200 and json.loads(body)["ok"] is True
    status, body = get(srv.web_url)
    assert status == 200 and "<" in body
    assert all(t.is_alive() for t in srv.threads)
    assert any("scheduler: run_due every 3600s" in line for line in srv.banner())
    assert any("mode     :" in line for line in srv.banner())
    srv.shutdown()
    assert not any(t.is_alive() for t in srv.threads)


def test_serve_processes_a_webhook_and_alerts_on_new_human_queue(served, monkeypatch):
    srv, log = served
    posted = []

    class Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    real_urlopen = urllib.request.urlopen

    def fake_urlopen(req, timeout=10):  # the alert hook is faked; the test's own webhook POST goes through
        if req.full_url.startswith("https://hooks.example/"):
            posted.append(json.loads(req.data))
            return Resp()
        return real_urlopen(req, timeout=timeout)

    monkeypatch.setattr(config, "ALERT_WEBHOOK_URL", "https://hooks.example/slack")
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    payload = ops.synthetic_payment_failed()
    payload["payload"]["payment"]["entity"]["error_description"] = "Payment declined by risk check: suspected fraud"
    payload["payload"]["payment"]["entity"]["error_reason"] = "payment_risk_check_failed"
    status, body = get(srv.webhook_url, json.dumps(payload).encode())
    assert status == 200 and json.loads(body)["job_status"] == JobStatus.HUMAN_QUEUE.value
    srv.sweep()  # the scheduler thread is asleep for an hour; one explicit sweep polls the queue
    assert len(posted) == 1 and "needs a person" in posted[0]["text"] and "pay_DOCTOR000000001" in posted[0]["text"]
    assert any(line.startswith("alert: needs a person") for line in log)
    srv.sweep()  # dedup: the same job never alerts twice
    assert len(posted) == 1


# ---- digest and alerts ------------------------------------------------------------------------------

@pytest.fixture()
def demo_db(session):
    razorpay_client.reset_fixture()
    seed(session, now=NOW)
    process_all(session, execute_now=True, now=NOW, rz_client=razorpay_client.fixture(), sleep=NOSLEEP)
    return session


def test_digest_has_every_section(demo_db, capsys, tmp_path):
    path = tmp_path / "digest.md"
    rc = main.main(["digest", "--since", "7d", "--write", str(path)])
    out = capsys.readouterr().out
    assert rc == 0 and path.read_text().startswith("# payment-recovery-agent digest: last 7d")
    for needle in ("## Flow", "events in: 22", "links created:", "reminders sent:", "recovered:", "expired unpaid:",
                   "cancelled (paid elsewhere):", "human queue additions:", "LLM calls:", "fallbacks:",
                   "## Top 5 by expected recovery", "## Needs a person", "IST", "mode live"):
        assert needle in out, needle
    assert out.count("\n- pay_") >= 5


def test_digest_post_sends_slack_json(demo_db, monkeypatch, capsys):
    posted = []

    class Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=10):
        posted.append((req.full_url, req.get_header("Content-type"), json.loads(req.data)))
        return Resp()

    monkeypatch.setattr(config, "ALERT_WEBHOOK_URL", "https://hooks.example/slack")
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert main.main(["digest", "--since", "24h", "--post"]) == 0
    assert len(posted) == 1
    url, ctype, body = posted[0]
    assert url == "https://hooks.example/slack" and ctype == "application/json"
    assert set(body) == {"text"} and "## Flow" in body["text"]
    assert main.main(["alert-test"]) == 0 and "alert test" in posted[1][2]["text"]


def test_post_without_url_or_with_error_never_raises(monkeypatch):
    monkeypatch.setattr(config, "ALERT_WEBHOOK_URL", "")
    assert ops.post_alert("x") == (False, "ALERT_WEBHOOK_URL unset; not posted")
    assert main.main(["alert-test"]) == 1

    def boom(req, timeout=10):
        raise HTTPError(req.full_url, 500, "boom", {}, io.BytesIO(b""))

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    ok, note = ops.post_alert("x", "https://hooks.example/x")
    assert not ok and "500" in note


def test_parse_since():
    assert ops.parse_since("24h").total_seconds() == 86400
    assert ops.parse_since("7d").days == 7
    assert ops.parse_since("30m").total_seconds() == 1800
    with pytest.raises(ValueError):
        ops.parse_since("yesterday")


def test_local_time_is_fixed_ist_offset():
    assert ops.local_time(datetime(2026, 9, 5, 12, 0)) == "2026-09-05 17:30 IST"
