"""Real Razorpay inputs: the payment.failed envelope and the bare entity, the signature check,
redelivery through the idempotency key, payment_link.paid closing the loop once, the dashboard
CSV export, the webhook receiver's four answers (200 / 401 / 400 / 500) and the resolve command."""
import copy
import hashlib
import hmac
import io
import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from app import db, executor, ingest
from app.ingest import (IngestError, close_paid_link, handle_webhook_request, ingest_event, parse_payment_failed,
                        parse_payment_link_paid, parse_payments_csv, verify_signature)
from app.main import main as cli_main
from app.models import AuditEvent, Outcome, PaymentAttempt, RecoveryJob
from app.razorpay_client import FixtureRazorpayClient

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "docs" / "examples"
NOW = datetime(2026, 9, 5, 12, 0, 0)
NOSLEEP = lambda s: None  # noqa: E731
SECRET = "whsec_test_secret"


def _load(name: str) -> dict:
    return json.loads((EXAMPLES / name).read_text())


def _sign(body: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _audits(session, attempt_id, stage=None):
    rows = session.execute(select(AuditEvent).where(AuditEvent.attempt_id == attempt_id).order_by(AuditEvent.id)).scalars()
    return [r for r in rows if stage is None or r.stage == stage]


# ---- parsing -------------------------------------------------------------------------------------

def test_parse_payment_failed_envelope_maps_every_attempt_field():
    f = parse_payment_failed(_load("payment_failed_webhook.json"))
    assert f["razorpay_payment_id"] == "pay_NfKq2vT8xRb1Zc" and f["amount_paise"] == 249900 and f["currency"] == "INR"
    assert f["method"] == "card" and f["order_id"] == "order_NfKp9wQ2sYd7Lm"
    assert f["merchant_id"] == "acc_HZbGxYwNk4pQ2R"  # account_id wins
    assert (f["error_code"], f["error_source"], f["error_step"], f["error_reason"]) == (
        "BAD_REQUEST_ERROR", "customer", "payment_authorization", "payment_failed")
    assert "insufficient funds" in f["error_description"]
    assert f["has_token"] is False  # token_id absent
    assert f["customer_name"] == "Priya Sharma" and f["customer_contact"] == "+919876543210"
    assert f["customer_email"] == "priya.sharma@example.com"
    assert f["failed_at"] == datetime(2026, 9, 5, 10, 14, 32)  # entity created_at (unix) -> naive UTC
    PaymentAttempt(**f)  # every key is a column


def test_parse_bare_payments_api_entity_and_has_token_from_token_id():
    ent = copy.deepcopy(_load("payment_failed_webhook.json")["payload"]["payment"]["entity"])
    ent["token_id"] = "token_NfKq7yUiOpAsDf"
    ent["notes"] = {"merchant_id": "merchant_acme"}
    f = parse_payment_failed(ent)
    assert f["razorpay_payment_id"] == "pay_NfKq2vT8xRb1Zc" and f["has_token"] is True
    assert f["merchant_id"] == "merchant_acme"  # no account_id on a bare entity: notes.merchant_id
    assert f["customer_name"] == "Priya Sharma"  # from card.name now that notes.name is gone
    ent["notes"] = []  # Razorpay sends [] for empty notes
    del ent["card"]
    g = parse_payment_failed(ent)
    assert g["merchant_id"] == "merchant_default" and g["customer_name"] is None


def test_parse_upi_example_and_placeholder_email():
    f = parse_payment_failed(_load("payment_failed_upi_webhook.json"))
    assert f["method"] == "upi" and f["has_token"] is False and f["customer_name"] == "Divya Krishnan"
    assert f["error_step"] == "payment_authentication" and f["error_reason"] == "payment_timed_out"
    ent = copy.deepcopy(_load("payment_failed_upi_webhook.json"))
    ent["payload"]["payment"]["entity"]["email"] = "void@razorpay.com"
    assert parse_payment_failed(ent)["customer_email"] is None


@pytest.mark.parametrize("mutate,match", [
    (lambda e: e.update(amount=0), "positive integer"),
    (lambda e: e.update(amount=2499.0), "positive integer"),
    (lambda e: e.update(amount="abc"), "positive integer"),
    (lambda e: e.update(id="order_123"), "pay_"),
    (lambda e: e.update(status="captured"), "not 'failed'"),
])
def test_parse_payment_failed_validates(mutate, match):
    payload = _load("payment_failed_webhook.json")
    mutate(payload["payload"]["payment"]["entity"])
    with pytest.raises(IngestError, match=match):
        parse_payment_failed(payload)


def test_parse_payment_failed_rejects_wrong_event_and_shape():
    wrong = _load("payment_link_paid_webhook.json")
    with pytest.raises(IngestError, match="expected event payment.failed"):
        parse_payment_failed(wrong)
    with pytest.raises(IngestError, match="neither"):
        parse_payment_failed({"hello": "world"})
    with pytest.raises(IngestError, match="JSON object"):
        parse_payment_failed(["not", "a", "dict"])
    ent = copy.deepcopy(_load("payment_failed_webhook.json")["payload"]["payment"]["entity"])
    del ent["status"]  # status absent is allowed
    assert parse_payment_failed(ent)["amount_paise"] == 249900


def test_parse_payment_link_paid():
    p = parse_payment_link_paid(_load("payment_link_paid_webhook.json"))
    assert p["link_id"] == "plink_Ik78ZrxQCVebo8" and p["amount_paid_paise"] == 249900
    assert p["reference_id"] == executor.reference_id_for(executor.idempotency_key("pay_NfKq2vT8xRb1Zc", 1))
    assert p["payment_id"] == "pay_NgRt5kD2wLx8Qa" and p["paid_at"] == datetime(2026, 9, 7, 9, 3, 15)
    with pytest.raises(IngestError, match="expected event payment_link.paid"):
        parse_payment_link_paid(_load("payment_failed_webhook.json"))


# ---- signature -----------------------------------------------------------------------------------

def test_verify_signature_true_false_and_constant_time(monkeypatch):
    body = (EXAMPLES / "payment_failed_webhook.json").read_bytes()
    good = _sign(body)
    assert verify_signature(body, good, SECRET) is True
    assert verify_signature(body, good.upper(), SECRET) is True  # hex case does not matter
    assert verify_signature(body, _sign(body, "other"), SECRET) is False
    assert verify_signature(body + b" ", good, SECRET) is False  # the raw body, byte for byte
    assert verify_signature(body, "", SECRET) is False and verify_signature(body, good, "") is False
    assert verify_signature(body, "not-hex", SECRET) is False
    calls = []
    real = hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)
    monkeypatch.setattr(ingest.hmac, "compare_digest", spy)
    assert verify_signature(body, good, SECRET) is True and len(calls) == 1


# ---- ingest_event ------------------------------------------------------------------------------

def test_ingest_failed_webhook_creates_one_attempt_with_an_ingest_audit_row(session):
    out = ingest_event(session, _load("payment_failed_webhook.json"), now=NOW, event_id="evt_1")
    assert out["event"] == "payment.failed" and out["created"] is True and out["processed"] is False
    a = session.execute(select(PaymentAttempt)).scalar_one()
    assert a.razorpay_payment_id == "pay_NfKq2vT8xRb1Zc" and a.has_token is False and a.amount_paise == 249900
    rows = _audits(session, a.id)
    assert [r.stage for r in rows] == ["ingest"] and '"event_id": "evt_1"' in rows[0].data_json
    assert rows[0].message == "failed payment event received"


def test_redelivery_makes_one_attempt_one_job_one_link_and_leaves_the_row_untouched(session):
    fx = FixtureRazorpayClient()
    payload = _load("payment_failed_webhook.json")
    first = ingest_event(session, payload, process=True, execute_now=True, now=NOW, rz_client=fx, sleep=NOSLEEP)
    assert first["created"] and first["job_status"] == "sent" and first["link_url"].startswith("https://rzp.io/i/")
    again = copy.deepcopy(payload)
    again["payload"]["payment"]["entity"]["amount"] = 999  # a differing redelivery must not rewrite the row
    again["payload"]["payment"]["entity"]["error_description"] = "changed"
    second = ingest_event(session, again, process=True, execute_now=True, now=NOW, rz_client=fx, sleep=NOSLEEP)
    assert second["redelivery"] is True and second["job_status"] == "skipped_duplicate" and second["job_id"] == first["job_id"]
    attempts = session.execute(select(PaymentAttempt)).scalars().all()
    assert len(attempts) == 1 and attempts[0].amount_paise == 249900 and "changed" not in attempts[0].error_description
    assert len(session.execute(select(RecoveryJob)).scalars().all()) == 1 and len(fx.links()) == 1
    ingests = _audits(session, attempts[0].id, "ingest")
    assert len(ingests) == 2 and "redelivery" in ingests[1].message


def test_payment_link_paid_closes_the_outcome_once(session):
    fx = FixtureRazorpayClient()
    ingest_event(session, _load("payment_failed_webhook.json"), process=True, execute_now=True, now=NOW,
                 rz_client=fx, sleep=NOSLEEP)
    job = session.execute(select(RecoveryJob)).scalar_one()
    assert job.razorpay_link_id == "plink_Ik78ZrxQCVebo8"  # the example's link id is what the fixture mints
    paid = _load("payment_link_paid_webhook.json")
    out = ingest_event(session, paid, now=NOW)
    assert out["closed"] is True and out["recovered"] is True and out["amount_recovered_paise"] == 249900
    o = session.execute(select(Outcome)).scalar_one()
    assert o.recovered and o.job_id == job.id and o.note == "payment_link.paid via webhook (plink_Ik78ZrxQCVebo8)"
    assert o.recovered_at == datetime(2026, 9, 7, 9, 3, 15)
    again = ingest_event(session, paid, now=NOW)
    assert again["closed"] is False and again["already_closed"] is True
    assert len(session.execute(select(Outcome)).scalars().all()) == 1  # idempotent on redelivery
    assert not [j for j in session.execute(select(RecoveryJob)).scalars().all() if j.id != job.id]


def test_payment_link_paid_matches_by_reference_id_when_the_link_id_was_never_recorded(session):
    fx = FixtureRazorpayClient()
    ingest_event(session, _load("payment_failed_webhook.json"), process=True, execute_now=True, now=NOW,
                 rz_client=fx, sleep=NOSLEEP)
    job = session.execute(select(RecoveryJob)).scalar_one()
    job.razorpay_link_id, job.status = None, "pending"  # a crash between the create call and the sent write
    session.commit()
    paid = parse_payment_link_paid(_load("payment_link_paid_webhook.json"))
    out = close_paid_link(session, paid, now=NOW)
    assert out["closed"] and out["job_id"] == job.id
    session.refresh(job)
    assert job.razorpay_link_id == "plink_Ik78ZrxQCVebo8" and job.status == "sent"


def test_payment_link_paid_for_an_unknown_link_is_ignored_not_raised(session):
    paid = _load("payment_link_paid_webhook.json")
    paid["payload"]["payment_link"]["entity"]["id"] = "plink_unknown000000"
    paid["payload"]["payment_link"]["entity"]["reference_id"] = "0" * 40
    out = ingest_event(session, paid, now=NOW)
    assert out["matched"] is False and out["closed"] is False
    assert session.execute(select(Outcome)).scalars().all() == []


def test_unknown_event_is_ignored(session):
    out = ingest_event(session, {"entity": "event", "event": "refund.created", "payload": {}}, now=NOW)
    assert out == {"ignored": "refund.created", "event": "refund.created"}
    assert session.execute(select(PaymentAttempt)).scalars().all() == []
    with pytest.raises(IngestError):
        ingest_event(session, ["nope"], now=NOW)


# ---- CSV -----------------------------------------------------------------------------------------

def test_parse_payments_csv_example_maps_headers_filters_status_and_reports_bad_rows():
    r = parse_payments_csv(EXAMPLES / "failed_payments_export.csv")
    assert r.total_rows == 12 and len(r.rows) == 9
    assert r.not_failed == {"captured": 1, "refunded": 1}
    assert r.skipped == [{"line": 13, "id": "pay_NhAl2mN3oP4qR5", "reason": "missing amount"}]
    by_id = {row["razorpay_payment_id"]: row for row in r.rows}
    assert by_id["pay_NhAa1bC2dE3fG4"]["amount_paise"] == 249900          # "2499.00" rupees
    assert by_id["pay_NhAb2cD3eF4gH5"]["amount_paise"] == 149900          # "1,499.00" rupees
    assert by_id["pay_NhAc3dE4fG5hI6"]["amount_paise"] == 59900           # "599": integer, header says Amount -> rupees
    assert by_id["pay_NhAe5fG6hI7jK8"]["has_token"] is True and by_id["pay_NhAa1bC2dE3fG4"]["has_token"] is False
    row = by_id["pay_NhAg7hI8jK9lM0"]
    assert (row["error_code"], row["error_source"], row["error_step"], row["error_reason"]) == (
        "BAD_REQUEST_ERROR", "bank", "payment_authorization", "card_declined")
    assert row["method"] == "card" and row["customer_contact"] == "+919910011223" and row["order_id"] == "order_NhAg6fE5dC4bA3"
    assert row["failed_at"] == datetime(2026, 9, 5, 11, 15, 54)  # DD/MM/YYYY HH:MM:SS
    assert r.columns["razorpay_payment_id"] == "Payment Id" and r.columns["error_code"] == "Error Code"
    for f in r.rows:
        PaymentAttempt(**f)


def test_parse_payments_csv_tolerant_headers_paise_column_and_timestamps():
    text = io.StringIO(
        "id,amount_paise,status,method,error code,error-reason,token,created_at,email\n"
        "pay_A1,249900,failed,card,BAD_REQUEST_ERROR,card_declined,token_x,1788603272,a@example.com\n"
        "pay_B2,\"Rs 1,499.50\",failed,upi,,,,2026-09-05T10:21:05+05:30,void@razorpay.com\n"
        ",100,failed,card,,,,,\n"
        "order_C3,100,failed,card,,,,,\n"
        "pay_D4,abc,failed,card,,,,,\n"
        "pay_E5,-5,failed,card,,,,,\n")
    r = parse_payments_csv(text)
    assert [x["razorpay_payment_id"] for x in r.rows] == ["pay_A1", "pay_B2"]
    assert r.rows[0]["amount_paise"] == 249900 and r.rows[0]["has_token"] is True
    assert r.rows[0]["error_code"] == "BAD_REQUEST_ERROR" and r.rows[0]["error_reason"] == "card_declined"
    assert r.rows[0]["failed_at"] == datetime(2026, 9, 5, 10, 14, 32)
    assert r.rows[1]["amount_paise"] == 149950 and r.rows[1]["customer_email"] is None
    assert r.rows[1]["failed_at"] == datetime(2026, 9, 5, 4, 51, 5)  # +05:30 normalised to UTC
    assert [s["reason"] for s in r.skipped] == ["missing payment id", "payment id does not start with pay_",
                                                "bad amount (amount 'abc' is not a number)",
                                                "bad amount (amount '-5' is not positive)"]
    assert "4 skipped" not in r.skipped_reasons() and "1 missing payment id" in r.skipped_reasons()
    with pytest.raises(IngestError, match="no payment id column"):
        parse_payments_csv(io.StringIO("foo,bar\n1,2\n"))
    with pytest.raises(IngestError, match="no amount column"):
        parse_payments_csv(io.StringIO("id,status\npay_1,failed\n"))


def test_import_rows_inserts_once_and_leaves_present_rows_alone(session):
    r = parse_payments_csv(EXAMPLES / "failed_payments_export.csv")
    created, present = ingest.import_rows(session, r.rows, now=NOW)
    assert len(created) == 9 and present == []
    assert all(_audits(session, a.id, "ingest") for a in created)
    created2, present2 = ingest.import_rows(session, r.rows, now=NOW)
    assert created2 == [] and len(present2) == 9
    assert len(session.execute(select(PaymentAttempt)).scalars().all()) == 9


# ---- the receiver --------------------------------------------------------------------------------

def _serve(**kw):
    server = ingest.serve(host="127.0.0.1", port=0, secret=SECRET, **kw)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def _post(base: str, body: bytes, signature: str | None, event_id: str | None = None):
    req = urllib.request.Request(base + ingest.WEBHOOK_PATH, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    if signature is not None:
        req.add_header("X-Razorpay-Signature", signature)
    if event_id:
        req.add_header("X-Razorpay-Event-Id", event_id)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_webhook_server_answers_200_401_400_and_health(session):
    fx = FixtureRazorpayClient()
    logged = []
    server, base = _serve(process=True, execute_now=True, log=logged.append, rz_client=fx)
    try:
        with urllib.request.urlopen(base + ingest.HEALTH_PATH, timeout=10) as r:
            assert r.status == 200 and json.loads(r.read())["ok"] is True
        body = (EXAMPLES / "payment_failed_webhook.json").read_bytes()
        assert _post(base, body, None)[0] == 401
        assert _post(base, body, "0" * 64)[0] == 401
        assert session.execute(select(PaymentAttempt)).scalars().all() == []  # nothing ingested unsigned
        status, out = _post(base, body, _sign(body), event_id="evt_abc")
        assert status == 200 and out["ok"] is True and out["created"] is True and out["job_status"] == "sent"
        status, out = _post(base, body, _sign(body), event_id="evt_abc")
        assert status == 200 and out["redelivery"] is True and out["job_status"] == "skipped_duplicate"
        assert len(fx.links()) == 1
        bad = b'{"entity": "event", "event": '
        assert _post(base, bad, _sign(bad))[0] == 400
        wrong = json.dumps({"event": "payment.failed", "payload": {"payment": {"entity": {"id": "pay_x", "amount": 0}}}}).encode()
        status, out = _post(base, wrong, _sign(wrong))
        assert status == 400 and "positive integer" in out["error"]
        paid = (EXAMPLES / "payment_link_paid_webhook.json").read_bytes()
        status, out = _post(base, paid, _sign(paid))
        assert status == 200 and out["closed"] is True
        other = json.dumps({"event": "refund.created", "payload": {}}).encode()
        assert _post(base, other, _sign(other)) == (200, {"ok": True, "ignored": "refund.created", "event": "refund.created"})
        malformed = json.dumps({"event": "order.paid", "payload": {}}).encode()  # now a handled event: a bad shape is 400
        assert _post(base, malformed, _sign(malformed))[0] == 400
        assert any("401" in line for line in logged) and any("payment_link.paid" in line for line in logged)
        a = session.execute(select(PaymentAttempt)).scalar_one()
        assert '"event_id": "evt_abc"' in _audits(session, a.id, "ingest")[0].data_json
    finally:
        server.shutdown()
        server.server_close()


def test_webhook_handler_answers_500_and_does_not_acknowledge_when_the_database_is_down():
    url_before = db.url()
    db.configure("sqlite:////nonexistent/dir/x.db")
    fx = FixtureRazorpayClient()
    try:
        body = (EXAMPLES / "payment_failed_webhook.json").read_bytes()
        status, out = handle_webhook_request(body, _sign(body), secret=SECRET, process=True, execute_now=True,
                                             rz_client=fx)
        assert status == 500 and out["ok"] is False and out["acknowledged"] is False
        assert fx.calls == [] and fx.links() == []  # nothing went out
        # the signature is still checked first: a bad one is 401 even with the database down
        assert handle_webhook_request(body, "0" * 64, secret=SECRET)[0] == 401
    finally:
        db.configure(url_before)


def test_webhook_handler_without_a_secret_accepts_unsigned_bodies(session):
    body = (EXAMPLES / "payment_failed_upi_webhook.json").read_bytes()
    status, out = handle_webhook_request(body, None, secret="", process=False)
    assert status == 200 and out["created"] is True and out["processed"] is False
    assert handle_webhook_request(b"[]", None, secret="")[0] == 400


# ---- the CLI -------------------------------------------------------------------------------------

def test_cli_ingest_import_csv_and_resolve(tmp_path, capsys):
    env = dict(os.environ, DATABASE_URL=f"sqlite:///{tmp_path / 'cli.db'}", RAZORPAY_WEBHOOK_SECRET=SECRET)
    run = lambda *cmd: subprocess.run([sys.executable, "-m", "app.main", *cmd], cwd=ROOT, env=env,  # noqa: E731
                                      capture_output=True, text=True, timeout=120)
    failed = EXAMPLES / "payment_failed_webhook.json"
    sig = _sign(failed.read_bytes())
    r = run("ingest", str(failed), "--process", "--execute-now", "--signature", "0" * 64)
    assert r.returncode == 1 and "does not match" in r.stderr
    r = run("ingest", str(failed), "--process", "--execute-now", "--signature", sig)
    assert r.returncode == 0, r.stderr
    assert "signature ok" in r.stdout and "ingested as attempt 1" in r.stdout and "sent https://rzp.io/i/" in r.stdout
    r = run("ingest", str(failed), "--process")  # secret set, no signature: warn and continue
    assert r.returncode == 0 and "unverified" in r.stderr and "skipped_duplicate" in r.stdout
    r = run("ingest", str(EXAMPLES / "payment_link_paid_webhook.json"))
    assert r.returncode == 0 and "recovered Rs 2,499.00" in r.stdout
    r = run("ingest", str(EXAMPLES / "payment_link_paid_webhook.json"))
    assert r.returncode == 0 and "already recorded" in r.stdout
    r = run("ingest", str(tmp_path / "missing.json"))
    assert r.returncode == 1 and "cannot read" in r.stderr

    r = run("import-csv", str(EXAMPLES / "failed_payments_export.csv"), "--process", "--execute-now")
    assert r.returncode == 0, r.stderr
    assert "imported 9 failed payments (0 already present, 1 skipped: 1 missing amount)" in r.stdout
    assert "line 13: missing amount (pay_NhAl2mN3oP4qR5)" in r.stdout and "captured 1, refunded 1" in r.stdout
    assert r.stdout.count("nudge:template") == 7 and r.stdout.count("-> human_queue") == 2
    assert "batch: 9 events, Rs 28,951.00 at risk -> 7 got a link, 0 pending, 2 human-queued, 0 stubbed" in r.stdout
    r = run("import-csv", str(EXAMPLES / "failed_payments_export.csv"))
    assert r.returncode == 0 and "imported 0 failed payments (9 already present" in r.stdout

    r = run("resolve", "--attempt", "pay_NhAk1lM2nO3pQ4", "--recovered", "69900", "--note", "paid by bank transfer")
    assert r.returncode == 0 and "recovered Rs 699.00" in r.stdout
    r = run("resolve", "--attempt", "pay_NhAk1lM2nO3pQ4", "--closed")
    assert r.returncode == 1 and "already has an outcome" in r.stderr
    r = run("resolve", "--attempt", "pay_NhAk1lM2nO3pQ4", "--closed", "--force")
    assert r.returncode == 0 and "closed, not recovered" in r.stdout
    r = run("resolve", "--attempt", "pay_NhAa1bC2dE3fG4", "--closed")  # a sent link is not a person's call
    assert r.returncode == 1 and "not human-queued" in r.stderr
    r = run("resolve", "--attempt", "pay_NhAi9jK0lM1nO2", "--closed", "--note", "fraud confirmed")
    assert r.returncode == 0
    r = run("resolve", "--attempt", "999", "--closed")
    assert r.returncode == 1
    r = run("show")
    # pay_NhAk was force-closed after its recovery, so its latest outcome reads "resolved by a person";
    # the one remaining "recovered Rs" is the webhook-paid pay_NfKq link
    assert r.stdout.count("resolved by a person") == 2 and r.stdout.count("recovered Rs") == 1
    r = run("audit", "--attempt", "pay_NhAk1lM2nO3pQ4", "--no-data")
    assert "resolved by a person: recovered Rs 699.00 outside the agent (paid by bank transfer)" in r.stdout
    assert "resolved by a person: closed, not recovered" in r.stdout

    down = subprocess.run([sys.executable, "-m", "app.main", "ingest", str(failed), "--process"], cwd=ROOT,
                          env=dict(env, DATABASE_URL="sqlite:////nonexistent/dir/x.db"), capture_output=True, text=True)
    assert down.returncode == 2 and "acknowledged" in down.stderr  # exit 2: refused at connect, nothing acknowledged


def test_cli_resolve_writes_outcome_and_audit_row_in_process(session, capsys):
    fx = FixtureRazorpayClient()
    out = ingest_event(session, _load("payment_failed_upi_webhook.json"), process=True, execute_now=True, now=NOW,
                       rz_client=fx, sleep=NOSLEEP)
    a = session.get(PaymentAttempt, out["attempt_id"])
    a.jobs[0].status = "human_queue"  # make it a person's call
    session.commit()
    session.close()
    assert cli_main(["resolve", "--attempt", str(a.id), "--recovered", "45900", "--note", "cash"]) == 0
    assert "recovered Rs 459.00" in capsys.readouterr().out
    s2 = db.session()
    try:
        o = s2.execute(select(Outcome).where(Outcome.attempt_id == a.id).order_by(Outcome.id.desc())).scalars().first()
        assert o.recovered and o.amount_recovered_paise == 45900 and "(cash)" in o.note
        assert _audits(s2, a.id, "outcome")[-1].message.startswith("recovered: resolved by a person")
    finally:
        s2.close()
