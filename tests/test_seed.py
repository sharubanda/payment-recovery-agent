"""Seed data is the ground truth for the classifier tests and the demo, so its shape is pinned:
22 events, every class at least twice, Razorpay-shaped ids and error objects, idempotent insert."""
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

from app.models import AuditEvent, PaymentAttempt  # noqa: E402
from app.taxonomy import FailureClass  # noqa: E402
from scripts.seed import SEED_EVENTS, existing_ids, seed, to_attempt  # noqa: E402

PAY_ID = re.compile(r"^pay_[A-Za-z0-9]{14}$")
ORDER_ID = re.compile(r"^order_[A-Za-z0-9]{14}$")


def test_twenty_two_events_every_class_at_least_twice_including_unknown():
    assert len(SEED_EVENTS) == 22
    counts = {cls: sum(1 for e in SEED_EVENTS if e["expected_class"] is cls) for cls in FailureClass}
    assert all(n >= 2 for n in counts.values()), counts
    assert counts[FailureClass.UNKNOWN] >= 2


def test_ids_are_razorpay_shaped_deterministic_and_unique():
    pay_ids = [e["razorpay_payment_id"] for e in SEED_EVENTS]
    order_ids = [e["order_id"] for e in SEED_EVENTS]
    assert all(PAY_ID.match(p) for p in pay_ids) and len(set(pay_ids)) == 22
    assert all(ORDER_ID.match(o) for o in order_ids) and len(set(order_ids)) == 22
    assert pay_ids[0] == "pay_LrfTLGu5EgYUPo"  # literal, not generated: the demo transcript must be reproducible


def test_event_fields_look_real():
    methods = set()
    for e in SEED_EVENTS:
        assert set(e["error"]) == {"code", "description", "source", "step", "reason", "metadata"}
        assert e["error"]["code"] in ("BAD_REQUEST_ERROR", "GATEWAY_ERROR", "SERVER_ERROR")
        assert e["error"]["source"] in ("customer", "business", "bank", "gateway", "internal", "network")
        assert e["error"]["step"] in ("payment_initiation", "payment_authentication", "payment_authorization", "payment_capture")
        assert e["error"]["description"]
        assert 19900 <= e["amount_paise"] <= 2499900 and e["currency"] == "INR"
        assert e["method"] in ("card", "upi", "netbanking", "wallet", "emandate")
        assert not e["has_token"] or e["method"] in ("card", "emandate")
        assert e["merchant_id"].startswith("merchant_")
        assert re.match(r"^\+91\d{10}$", e["customer_contact"])
        assert e["customer_email"].endswith("@example.com")
        assert e["customer_name"]
        methods.add(e["method"])
    assert {"card", "upi", "netbanking", "wallet"} <= methods
    assert any(e["has_token"] for e in SEED_EVENTS)


def test_to_attempt_flattens_the_error_object_and_uses_now():
    now = datetime(2026, 9, 5, 12, 0, 0)
    row = to_attempt(SEED_EVENTS[0], now=now)
    assert isinstance(row, PaymentAttempt) and row.id is None
    assert row.error_code == SEED_EVENTS[0]["error"]["code"]
    assert row.error_description == SEED_EVENTS[0]["error"]["description"]
    assert row.failed_at < now and (now - row.failed_at).total_seconds() == SEED_EVENTS[0]["failed_minutes_ago"] * 60


def test_seed_inserts_all_and_is_idempotent(session):
    rows = seed(session)
    assert len(rows) == 22 and all(r.id for r in rows)
    assert [r.razorpay_payment_id for r in rows] == [e["razorpay_payment_id"] for e in SEED_EVENTS]
    assert session.query(PaymentAttempt).count() == 22
    assert session.query(AuditEvent).filter_by(stage="ingest").count() == 22

    again = seed(session)
    assert len(again) == 22
    assert session.query(PaymentAttempt).count() == 22
    assert session.query(AuditEvent).filter_by(stage="ingest").count() == 22
    assert [r.id for r in again] == [r.id for r in rows]


def test_seed_fills_only_the_missing_rows(session):
    seed(session)
    victims = session.query(PaymentAttempt).filter(PaymentAttempt.id <= 5).all()
    victim_ids = [v.id for v in victims]
    # audit_log keeps a plain FK (no ORM cascade) so the trail has to be removed before the attempt
    session.query(AuditEvent).filter(AuditEvent.attempt_id.in_(victim_ids)).delete(synchronize_session=False)
    for v in victims:
        session.delete(v)
    session.commit()
    assert len(existing_ids(session)) == 17
    rows = seed(session)
    assert len(rows) == 22 and session.query(PaymentAttempt).count() == 22


def test_cli_prints_counts_and_is_safe_to_rerun(tmp_path):
    env = dict(os.environ, DATABASE_URL=f"sqlite:///{tmp_path / 'cli.db'}")
    cmd = [sys.executable, str(ROOT / "scripts" / "seed.py")]
    first = subprocess.run(cmd, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    assert first.returncode == 0, first.stderr
    assert first.stdout.strip() == "seeded 22 events (0 already present)"
    second = subprocess.run(cmd, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    assert second.returncode == 0, second.stderr
    assert second.stdout.strip() == "seeded 0 events (22 already present)"


def test_cli_help_prints_usage_and_writes_nothing(tmp_path):
    env = dict(os.environ, DATABASE_URL=f"sqlite:///{tmp_path / 'never.db'}")
    out = subprocess.run([sys.executable, str(ROOT / "scripts" / "seed.py"), "--help"], cwd=tmp_path, env=env,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and "usage:" in out.stdout and "seeded" not in out.stdout
    assert not (tmp_path / "never.db").exists() and not list(tmp_path.glob("*.db*"))
    bad = subprocess.run([sys.executable, str(ROOT / "scripts" / "seed.py"), "--bogus"], cwd=tmp_path, env=env,
                         capture_output=True, text=True, timeout=60)
    assert bad.returncode == 2 and not (tmp_path / "never.db").exists()
