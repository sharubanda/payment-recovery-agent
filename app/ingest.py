"""Real Razorpay inputs into the pipeline: webhooks, the bare Payments API entity, and the
dashboard CSV export. Three entry points, one destination (a PaymentAttempt row, then
pipeline.process_attempt), and three closing events: payment_link.paid -> a recovered Outcome;
payment_link.expired -> a not-recovered Outcome and the next attempt (or the stop rule);
payment.captured / order.paid -> the order is recorded paid, pending jobs end no_action and
any live link is CANCELLED at Razorpay (or parked for a person if the cancel fails).

  parse_payment_failed        payment.failed envelope, or the bare GET /payments/{id} entity
  parse_payment_link_paid     payment_link.paid envelope -> {link_id, reference_id, amount_paid_paise, ...}
  parse_payment_link_expired  payment_link.expired envelope -> {link_id, reference_id, expired_at}
  parse_payment_captured      payment.captured / order.paid envelope -> {order_id, payment_id, amount_paise, paid_at}
  record_order_paid           the money-path stop: mark the order paid, void pending jobs, cancel live links
  verify_signature            hex HMAC-SHA256 of the raw body against X-Razorpay-Signature
  ingest_event                one webhook payload -> upsert-or-skip the attempt (-> process it) / close the loop
  parse_payments_csv       dashboard payments export -> attempt field dicts, with a skip report
  import_rows              batch insert of parsed rows; a payment id already present is left untouched
  handle_webhook_request   the HTTP handler's logic, without the socket, so it can be unit-tested
  serve                    stdlib http.server on POST /razorpay/webhook and GET /health

Redelivery. Webhooks are at-least-once. A payment id that already has a row is a redelivery:
the stored row is NOT modified (Razorpay's first report is the one the audit trail keeps) and
the event is processed again so the idempotency key in executor.schedule_job rejects the job
("skipped_duplicate"), exactly as the rest of the agent treats it. The
X-Razorpay-Event-Id, when the caller has it, goes into the ingest audit row; the payment id is
the dedup key because it is what the UNIQUE constraint already guards.

Acknowledgement. A PipelineDBError (the database refused a write) propagates out of
ingest_event and the HTTP handler answers 500 without acknowledging, so Razorpay redelivers.
Everything Razorpay- or LLM-shaped degrades inside the pipeline and is acknowledged with 200.
"""
import csv
import hashlib
import hmac
import io
import json
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, IO

from sqlalchemy import or_, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from . import cadence, config, db, executor, offers, pipeline
from .clock import utcnow
from .models import Outcome, PaymentAttempt, RecoveryJob, audit, with_reminders
from .razorpay_client import get_client
from .taxonomy import Action, JobStatus

__all__ = ["IngestError", "parse_payment_failed", "parse_payment_link_paid", "parse_payment_link_expired",
           "parse_payment_captured", "parse_subscription_event", "verify_signature", "ingest_event", "ingest_attempt", "close_paid_link",
           "close_expired_link", "record_order_paid", "parse_payments_csv", "ParsedCSV", "import_rows",
           "handle_webhook_request", "serve", "WEBHOOK_PATH", "HEALTH_PATH"]

EVENT_PAYMENT_FAILED = "payment.failed"
EVENT_LINK_PAID = "payment_link.paid"
EVENT_LINK_EXPIRED = "payment_link.expired"
EVENT_PAYMENT_CAPTURED = "payment.captured"
EVENT_ORDER_PAID = "order.paid"
EVENT_SUBSCRIPTION_PENDING = "subscription.pending"
EVENT_SUBSCRIPTION_HALTED = "subscription.halted"
ORDER_PAID_EVENTS = (EVENT_PAYMENT_CAPTURED, EVENT_ORDER_PAID)
SUBSCRIPTION_EVENTS = (EVENT_SUBSCRIPTION_PENDING, EVENT_SUBSCRIPTION_HALTED)
HANDLED_EVENTS = (EVENT_PAYMENT_FAILED, EVENT_LINK_PAID, EVENT_LINK_EXPIRED, EVENT_PAYMENT_CAPTURED, EVENT_ORDER_PAID,
                  EVENT_SUBSCRIPTION_PENDING, EVENT_SUBSCRIPTION_HALTED)
DEFAULT_MERCHANT = "merchant_default"
# Razorpay substitutes this address when the checkout collected no email; it is not a recipient.
PLACEHOLDER_EMAILS = frozenset({"void@razorpay.com"})
WEBHOOK_PATH = "/razorpay/webhook"
HEALTH_PATH = "/health"
SIGNATURE_HEADER = "X-Razorpay-Signature"
EVENT_ID_HEADER = "X-Razorpay-Event-Id"
MAX_BODY_BYTES = 1 << 20  # a payment.failed envelope is a few KB; anything bigger is not Razorpay


class IngestError(ValueError):
    """The payload is not something the agent can act on. The message says what is wrong."""


# ---- webhook payloads ---------------------------------------------------------------------------

def _entity(payload: dict, kind: str) -> dict:
    """The entity dict for `kind` out of a webhook envelope, or the payload itself when it is the
    bare API entity (GET /payments/{id} returns {"id": "pay_...", "entity": "payment", ...})."""
    if not isinstance(payload, dict):
        raise IngestError(f"payload must be a JSON object, got {type(payload).__name__}")
    inner = payload.get("payload")
    if isinstance(inner, dict):
        block = inner.get(kind)
        ent = block.get("entity") if isinstance(block, dict) else None
        if not isinstance(ent, dict):
            raise IngestError(f"webhook envelope has no payload.{kind}.entity")
        return ent
    if payload.get("entity") in (kind, None) and "id" in payload:
        return payload
    raise IngestError(f"payload is neither a webhook envelope nor a bare {kind} entity")


def _event_name(payload: dict) -> str | None:
    ev = payload.get("event") if isinstance(payload, dict) else None
    return str(ev) if ev else None


def _notes(entity: dict) -> dict:
    notes = entity.get("notes")
    return notes if isinstance(notes, dict) else {}  # Razorpay sends [] for empty notes


def _unix_to_naive_utc(value) -> datetime | None:
    try:
        if value is None or value == "" or isinstance(value, bool):
            return None
        return datetime.fromtimestamp(int(value), tz=timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _positive_paise(value, what: str) -> int:
    if isinstance(value, bool) or value is None:
        raise IngestError(f"{what} must be a positive integer in paise, got {value!r}")
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if not isinstance(value, int):
        raise IngestError(f"{what} must be a positive integer in paise, got {value!r}")
    if value <= 0:
        raise IngestError(f"{what} must be a positive integer in paise, got {value!r}")
    return value


def _payment_id(value) -> str:
    if not isinstance(value, str) or not value.startswith("pay_") or len(value) <= 4:
        raise IngestError(f"payment id must start with 'pay_', got {value!r}")
    return value


def _language(notes: dict) -> str | None:
    """notes.language / notes.lang / notes.customer_language -> "en" | "hi" | "hinglish" | None.
    Aliases (hi-IN, Hindi, hi-Latn, ...) are resolved by app/nudge_templates.py; unknown values are dropped."""
    from .nudge_templates import normalise_language
    for key in ("language", "lang", "customer_language"):
        value = normalise_language(_clean((notes or {}).get(key)))
        if value:
            return value
    return None


def _clean(value) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def parse_payment_failed(payload: dict) -> dict:
    """A payment.failed webhook envelope, or the bare payment entity, -> PaymentAttempt fields.

    Validates: id starts with pay_; amount is a positive int in paise; status is "failed" or
    absent; the envelope's event (when present) is payment.failed. Raises IngestError otherwise.
    has_token = bool(token_id). merchant_id = account_id, else notes.merchant_id, else
    "merchant_default". customer_name from notes.name / notes.customer_name / card.name.
    failed_at from the entity's created_at (unix), else the envelope's, else now."""
    ev = _event_name(payload)
    if ev is not None and ev != EVENT_PAYMENT_FAILED:
        raise IngestError(f"expected event {EVENT_PAYMENT_FAILED}, got {ev!r}")
    ent = _entity(payload, "payment")
    payment_id = _payment_id(ent.get("id"))
    amount = _positive_paise(ent.get("amount"), f"{payment_id}: amount")
    status = _clean(ent.get("status"))
    if status is not None and status.lower() != "failed":
        raise IngestError(f"{payment_id}: status is {status!r}, not 'failed'; only failed payments are recovered")

    notes = _notes(ent)
    card = ent.get("card") if isinstance(ent.get("card"), dict) else {}
    name = _clean(notes.get("name")) or _clean(notes.get("customer_name")) or _clean(card.get("name"))
    email = _clean(ent.get("email"))
    if email and email.lower() in PLACEHOLDER_EMAILS:
        email = None
    failed_at = (_unix_to_naive_utc(ent.get("created_at"))
                 or _unix_to_naive_utc(payload.get("created_at") if payload is not ent else None)
                 or utcnow())
    merchant = _clean(payload.get("account_id") if payload is not ent else None) or _clean(notes.get("merchant_id")) \
        or DEFAULT_MERCHANT
    return {
        "merchant_id": merchant,
        "order_id": _clean(ent.get("order_id")) or "",
        "razorpay_payment_id": payment_id,
        "amount_paise": amount,
        "currency": (_clean(ent.get("currency")) or "INR").upper()[:3],
        "method": (_clean(ent.get("method")) or "unknown").lower()[:16],
        "error_code": _clean(ent.get("error_code")),
        "error_description": _clean(ent.get("error_description")),
        "error_source": _clean(ent.get("error_source")),
        "error_step": _clean(ent.get("error_step")),
        "error_reason": _clean(ent.get("error_reason")),
        "has_token": bool(_clean(ent.get("token_id"))),
        "customer_name": name,
        "customer_contact": _clean(ent.get("contact")),
        "customer_email": email,
        "customer_language": _language(notes),
        "failed_at": failed_at,
    }


def parse_subscription_event(payload: dict) -> dict:
    """A subscription.pending or subscription.halted envelope -> PaymentAttempt fields.

    ASSUMED payload (docs unreachable offline; the shape follows Razorpay's other envelopes):
      {"event": "subscription.pending"|"subscription.halted",
       "payload": {"subscription": {"entity": {"id": "sub_...", "plan_id", "customer_id",
                                                "status": "pending"|"halted", "paid_count", "remaining_count",
                                                "current_start", "current_end", "short_url", "notes"}},
                   "payment": {"entity": {...the failed payment, with error_* fields...}}}}
    The payment entity is REQUIRED here: it carries the payment id (the dedup key), the amount, the
    method and the error object the classifier reads. has_token is True (a mandate exists, whatever
    its state); method comes from the payment entity, else "emandate"; order_id from the payment,
    else the subscription id (so a later order.paid / payment.captured can still match); the
    subscription's id, status and short_url go on the attempt for app/cadence.py."""
    ev = _event_name(payload)
    if ev not in SUBSCRIPTION_EVENTS:
        raise IngestError(f"expected event {EVENT_SUBSCRIPTION_PENDING} or {EVENT_SUBSCRIPTION_HALTED}, got {ev!r}")
    sub = _entity(payload, "subscription")
    sub_id = _clean(sub.get("id"))
    if not sub_id or not sub_id.startswith("sub_"):
        raise IngestError(f"subscription id must start with 'sub_', got {sub_id!r}")
    inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
    pay_block = inner.get("payment") if isinstance(inner, dict) else None
    if not (isinstance(pay_block, dict) and isinstance(pay_block.get("entity"), dict)):
        raise IngestError(f"{ev} for {sub_id} carries no payload.payment.entity; the failed charge is what gets recovered")
    ent = pay_block["entity"]
    payment_id = _payment_id(ent.get("id"))
    amount = _positive_paise(ent.get("amount"), f"{payment_id}: amount")
    status = ev.split(".", 1)[1]  # the event names the state; the entity's status, when present, must agree
    ent_status = _clean(sub.get("status"))
    if ent_status is not None and ent_status.lower() != status:
        raise IngestError(f"{sub_id}: subscription status is {ent_status!r} but the event is {ev}")
    notes = {**_notes(sub), **_notes(ent)}
    card = ent.get("card") if isinstance(ent.get("card"), dict) else {}
    name = _clean(notes.get("name")) or _clean(notes.get("customer_name")) or _clean(card.get("name"))
    email = _clean(ent.get("email"))
    if email and email.lower() in PLACEHOLDER_EMAILS:
        email = None
    failed_at = _unix_to_naive_utc(ent.get("created_at")) or _unix_to_naive_utc(payload.get("created_at")) or utcnow()
    merchant = _clean(payload.get("account_id")) or _clean(notes.get("merchant_id")) or DEFAULT_MERCHANT
    return {
        "merchant_id": merchant,
        "order_id": _clean(ent.get("order_id")) or sub_id,
        "razorpay_payment_id": payment_id,
        "amount_paise": amount,
        "currency": (_clean(ent.get("currency")) or "INR").upper()[:3],
        "method": (_clean(ent.get("method")) or "emandate").lower()[:16],
        "error_code": _clean(ent.get("error_code")),
        "error_description": _clean(ent.get("error_description")),
        "error_source": _clean(ent.get("error_source")),
        "error_step": _clean(ent.get("error_step")),
        "error_reason": _clean(ent.get("error_reason")),
        "has_token": True,
        "customer_name": name,
        "customer_contact": _clean(ent.get("contact")),
        "customer_email": email,
        "customer_language": _language(notes),
        "failed_at": failed_at,
        "subscription_id": sub_id,
        "subscription_status": status,
        "subscription_url": _clean(sub.get("short_url")),
    }


def parse_payment_link_paid(payload: dict) -> dict:
    """A payment_link.paid envelope -> {link_id, reference_id, amount_paid_paise, payment_id, paid_at}.
    payload.payment_link.entity carries the link (id, reference_id, status, amount_paid);
    payload.payment.entity, when present, carries the payment that paid it."""
    ev = _event_name(payload)
    if ev is not None and ev != EVENT_LINK_PAID:
        raise IngestError(f"expected event {EVENT_LINK_PAID}, got {ev!r}")
    link = _entity(payload, "payment_link")
    link_id = _clean(link.get("id"))
    if not link_id or not link_id.startswith("plink_"):
        raise IngestError(f"payment link id must start with 'plink_', got {link_id!r}")
    status = _clean(link.get("status"))
    if status is not None and status.lower() not in ("paid", "partially_paid"):
        raise IngestError(f"{link_id}: status is {status!r}, not 'paid'")
    payment = None
    inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
    pay_block = inner.get("payment") if isinstance(inner, dict) else None
    if isinstance(pay_block, dict) and isinstance(pay_block.get("entity"), dict):
        payment = pay_block["entity"]
    amount_paid = link.get("amount_paid")
    if amount_paid in (None, 0, "") and payment is not None:
        amount_paid = payment.get("amount")
    if amount_paid in (None, ""):
        amount_paid = link.get("amount")
    paid_at = None
    if payment is not None:
        paid_at = _unix_to_naive_utc(payment.get("created_at"))
    paid_at = paid_at or _unix_to_naive_utc(link.get("updated_at")) or _unix_to_naive_utc(payload.get("created_at"))
    return {
        "link_id": link_id,
        "reference_id": _clean(link.get("reference_id")),
        "amount_paid_paise": _positive_paise(amount_paid, f"{link_id}: amount_paid"),
        "payment_id": _clean(payment.get("id")) if payment else None,
        "paid_at": paid_at,
    }


def parse_payment_link_expired(payload: dict) -> dict:
    """A payment_link.expired envelope -> {link_id, reference_id, expired_at}. The link entity
    carries expired_at (unix) once it has expired; expire_by is the fallback, then the envelope's
    created_at, then None (the handler uses its own clock)."""
    ev = _event_name(payload)
    if ev is not None and ev != EVENT_LINK_EXPIRED:
        raise IngestError(f"expected event {EVENT_LINK_EXPIRED}, got {ev!r}")
    link = _entity(payload, "payment_link")
    link_id = _clean(link.get("id"))
    if not link_id or not link_id.startswith("plink_"):
        raise IngestError(f"payment link id must start with 'plink_', got {link_id!r}")
    status = _clean(link.get("status"))
    if status is not None and status.lower() != "expired":
        raise IngestError(f"{link_id}: status is {status!r}, not 'expired'")
    expired_at = (_unix_to_naive_utc(link.get("expired_at")) or _unix_to_naive_utc(link.get("expire_by"))
                  or _unix_to_naive_utc(payload.get("created_at")))
    return {"link_id": link_id, "reference_id": _clean(link.get("reference_id")), "expired_at": expired_at}


def parse_payment_captured(payload: dict) -> dict:
    """A payment.captured or order.paid envelope -> {event, order_id, payment_id, amount_paise, paid_at}.

    payment.captured: payload.payment.entity with status "captured" and an order_id (a payment
    without an order matches nothing here and is reported with order_id None). order.paid:
    payload.order.entity (the id) plus payload.payment.entity (the payment that paid it)."""
    ev = _event_name(payload)
    if ev is not None and ev not in ORDER_PAID_EVENTS:
        raise IngestError(f"expected event {EVENT_PAYMENT_CAPTURED} or {EVENT_ORDER_PAID}, got {ev!r}")
    inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
    payment = None
    pay_block = inner.get("payment") if isinstance(inner, dict) else None
    if isinstance(pay_block, dict) and isinstance(pay_block.get("entity"), dict):
        payment = pay_block["entity"]
    elif payload.get("entity") == "payment" and "id" in payload:
        payment = payload
    order = None
    order_block = inner.get("order") if isinstance(inner, dict) else None
    if isinstance(order_block, dict) and isinstance(order_block.get("entity"), dict):
        order = order_block["entity"]
    if payment is None and order is None:
        raise IngestError("payload has neither payload.payment.entity nor payload.order.entity")
    payment_id = _payment_id(payment.get("id")) if payment is not None else None
    if payment is not None:
        status = _clean(payment.get("status"))
        if ev == EVENT_PAYMENT_CAPTURED and status is not None and status.lower() != "captured":
            raise IngestError(f"{payment_id}: status is {status!r}, not 'captured'")
    order_id = _clean(order.get("id")) if order is not None else None
    if not order_id and payment is not None:
        order_id = _clean(payment.get("order_id"))
    amount = (payment or {}).get("amount")
    if amount in (None, "") and order is not None:
        amount = order.get("amount_paid") or order.get("amount")
    paid_at = (_unix_to_naive_utc((payment or {}).get("created_at")) or _unix_to_naive_utc(payload.get("created_at")))
    return {
        "event": ev or EVENT_PAYMENT_CAPTURED,
        "order_id": order_id,
        "payment_id": payment_id or "?",
        "amount_paise": _positive_paise(amount, f"{payment_id or order_id}: amount") if amount not in (None, "") else None,
        "paid_at": paid_at,
    }


# ---- signature ----------------------------------------------------------------------------------

def verify_signature(body: bytes, signature: str, secret: str) -> bool:
    """Razorpay signs the raw request body with HMAC-SHA256 under the webhook secret and sends the
    hex digest as X-Razorpay-Signature. Constant-time compare; an empty secret or signature is False."""
    if not secret or not signature or body is None:
        return False
    if isinstance(body, str):
        body = body.encode("utf-8")
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    given = signature.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", given):
        return False
    return hmac.compare_digest(expected, given)


# ---- one event into the database ----------------------------------------------------------------

def ingest_attempt(session: Session, fields: dict, *, source: str = "webhook", event_id: str | None = None,
                   now: datetime | None = None) -> tuple[PaymentAttempt, bool]:
    """Insert the attempt if its payment id is new; otherwise return the stored row untouched.
    Writes one "ingest" audit row either way and commits. Caller wraps in pipeline.db_guarded."""
    now = now or utcnow()
    pid = fields["razorpay_payment_id"]
    existing = session.execute(select(PaymentAttempt).where(PaymentAttempt.razorpay_payment_id == pid)).scalars().first()
    if existing is not None:
        audit(session, existing.id, "ingest",
              f"redelivery: {pid} already ingested as attempt {existing.id}; stored row untouched, "
              f"re-processing so the idempotency key decides",
              {"razorpay_payment_id": pid, "source": source, "event_id": event_id, "redelivery": True})
        session.commit()
        return existing, False
    row = PaymentAttempt(**fields)
    session.add(row)
    session.flush()
    what = "failed payment event received"
    if row.subscription_id:
        what = (f"subscription.{row.subscription_status} received for {row.subscription_id}: failed charge {pid}, "
                f"mandate on file (has_token)")
    audit(session, row.id, "ingest", what,
          {"razorpay_payment_id": pid, "method": row.method, "amount_paise": row.amount_paise,
           "error": {"code": row.error_code, "description": row.error_description, "source": row.error_source,
                     "step": row.error_step, "reason": row.error_reason},
           "has_token": row.has_token, "source": source, "event_id": event_id,
           "subscription_id": row.subscription_id, "subscription_status": row.subscription_status,
           "subscription_url": row.subscription_url})
    session.commit()
    return row, True


def _find_job_for_link(session: Session, link_id: str | None, reference_id: str | None) -> RecoveryJob | None:
    clauses = []
    if link_id:
        clauses.append(RecoveryJob.razorpay_link_id == link_id)
    if reference_id:
        # reference_id is idempotency_key[:40]; a prefix match on the stored key finds the job even
        # when the link id was never recorded (a crash between the create call and the sent write)
        clauses.append(RecoveryJob.idempotency_key.like(reference_id + "%"))
    if not clauses:
        return None
    jobs = session.execute(select(RecoveryJob).where(or_(*clauses)).order_by(RecoveryJob.id)).scalars().all()
    for j in jobs:  # prefer the row whose link id matches exactly
        if link_id and j.razorpay_link_id == link_id:
            return j
    for j in jobs:
        if reference_id and executor.reference_id_for(j.idempotency_key) == reference_id:
            return j
    return None


def close_paid_link(session: Session, paid: dict, *, event_id: str | None = None,
                    now: datetime | None = None, rz_client=None) -> dict:
    """Record recovered=True for the job behind a paid link, once. A redelivered paid event, or a
    link poll already closed, writes an audit row and no second outcome. The order behind the
    attempt is then recorded paid (by the link's payment), so a sibling attempt for the same order
    cannot send another link, and any sibling link still live is cancelled (record_order_paid)."""
    now = now or utcnow()
    job = _find_job_for_link(session, paid["link_id"], paid.get("reference_id"))
    if job is None:
        audit(session, None, "ingest",
              f"{EVENT_LINK_PAID} for {paid['link_id']} (reference_id {paid.get('reference_id')}) matches no "
              f"recovery job; ignored", {**paid, "event_id": event_id, "matched": False})
        session.commit()
        return {"event": EVENT_LINK_PAID, "matched": False, "link_id": paid["link_id"], "closed": False}
    attempt = job.attempt if job.attempt is not None else session.get(PaymentAttempt, job.attempt_id)
    existing = session.execute(select(Outcome).where(Outcome.job_id == job.id).order_by(Outcome.id.desc())).scalars().first()
    base = {"event": EVENT_LINK_PAID, "matched": True, "link_id": paid["link_id"], "job_id": job.id,
            "attempt_id": attempt.id, "payment_id": attempt.razorpay_payment_id}
    if existing is not None:
        audit(session, attempt.id, "ingest",
              f"{EVENT_LINK_PAID} for {paid['link_id']} redelivered: outcome already recorded on job#{job.id} "
              f"({'recovered' if existing.recovered else existing.note}); nothing changed",
              {**paid, "event_id": event_id, "outcome_id": existing.id, "redelivery": True})
        session.commit()
        return {**base, "closed": False, "already_closed": True, "recovered": bool(existing.recovered),
                "amount_recovered_paise": existing.amount_recovered_paise}
    if job.razorpay_link_id is None or job.status != JobStatus.SENT.value:
        # the link was paid, so it exists and it went out; repair the job row rather than argue with it
        job.razorpay_link_id = job.razorpay_link_id or paid["link_id"]
        job.status, job.executed_at = JobStatus.SENT.value, job.executed_at or now
        audit(session, attempt.id, "ingest",
              f"{EVENT_LINK_PAID}: link {paid['link_id']} was paid, so job#{job.id} is marked sent with it",
              {"job_id": job.id, "link_id": paid["link_id"]})
    audit(session, attempt.id, "ingest", f"{EVENT_LINK_PAID} received for {paid['link_id']} (job#{job.id})",
          {**paid, "event_id": event_id, "job_id": job.id})
    partial = int(paid["amount_paid_paise"]) < int(attempt.amount_paise or 0)
    note = (f"{pipeline.PARTIAL_NOTE} via webhook ({paid['link_id']}): {paid['amount_paid_paise']} of {attempt.amount_paise} paise"
            if partial else f"payment_link.paid via webhook ({paid['link_id']})")
    outcome = executor.record_outcome(session, attempt, job, True, paid["amount_paid_paise"], note, now=paid.get("paid_at") or now)
    settled = None
    if partial:
        # the partial offer was taken: the balance is still owed and the link stays open at Razorpay,
        # so the order is NOT recorded paid and no sibling link is cancelled
        from . import cadence
        cadence.void_reminders(session, attempt, f"link {paid['link_id']} partially paid", now=now, parent=job)
    elif attempt.order_id:
        settled = record_order_paid(session, {"event": EVENT_LINK_PAID, "order_id": attempt.order_id,
                                              "payment_id": paid.get("payment_id") or f"link {paid['link_id']}",
                                              "amount_paise": paid["amount_paid_paise"], "paid_at": paid.get("paid_at")},
                                    rz_client=rz_client, event_id=event_id, now=now, quiet=True)
    return {**base, "closed": True, "recovered": True, "amount_recovered_paise": outcome.amount_recovered_paise,
            "outcome_id": outcome.id, "siblings": settled}


def close_expired_link(session: Session, expired: dict, *, event_id: str | None = None,
                       now: datetime | None = None) -> dict:
    """payment_link.expired: the outcome (not recovered) for the job behind the link, once, then the
    next attempt from the policy table or the stop rule (pipeline.schedule_expiry_followup), with
    the class delay counted from the expiry time. A redelivery, or a link poll already closed,
    writes an audit row and nothing else."""
    now = now or utcnow()
    job = _find_job_for_link(session, expired["link_id"], expired.get("reference_id"))
    if job is None:
        audit(session, None, "ingest",
              f"{EVENT_LINK_EXPIRED} for {expired['link_id']} (reference_id {expired.get('reference_id')}) matches no "
              f"recovery job; ignored", {**expired, "event_id": event_id, "matched": False})
        session.commit()
        return {"event": EVENT_LINK_EXPIRED, "matched": False, "link_id": expired["link_id"], "closed": False}
    attempt = job.attempt if job.attempt is not None else session.get(PaymentAttempt, job.attempt_id)
    existing = session.execute(select(Outcome).where(Outcome.job_id == job.id).order_by(Outcome.id.desc())).scalars().first()
    base = {"event": EVENT_LINK_EXPIRED, "matched": True, "link_id": expired["link_id"], "job_id": job.id,
            "attempt_id": attempt.id, "payment_id": attempt.razorpay_payment_id}
    if existing is not None:
        audit(session, attempt.id, "ingest",
              f"{EVENT_LINK_EXPIRED} for {expired['link_id']} redelivered: outcome already recorded on job#{job.id} "
              f"({'recovered' if existing.recovered else existing.note}); nothing changed",
              {**expired, "event_id": event_id, "outcome_id": existing.id, "redelivery": True})
        session.commit()
        return {**base, "closed": False, "already_closed": True, "recovered": bool(existing.recovered)}
    if job.razorpay_link_id is None:
        job.razorpay_link_id = expired["link_id"]  # matched by reference_id: the link existed, record its id
    audit(session, attempt.id, "ingest", f"{EVENT_LINK_EXPIRED} received for {expired['link_id']} (job#{job.id})",
          {**expired, "event_id": event_id, "job_id": job.id})
    when = expired.get("expired_at") or now
    outcome = executor.record_outcome(session, attempt, job, False, 0,
                                      f"payment link expired ({expired['link_id']})", now=min(when, now))
    cadence.void_reminders(session, attempt, f"link {expired['link_id']} expired", now=min(when, now), parent=job)
    follow = pipeline.schedule_expiry_followup(session, attempt, job, now=min(when, now))
    return {**base, "closed": True, "recovered": False, "outcome_id": outcome.id,
            "followup_job_id": follow.id if follow is not None else None,
            "followup_status": follow.status if follow is not None else None,
            "followup_scheduled_at": follow.scheduled_at if follow is not None else None,
            "followup_note": follow.schedule_note if follow is not None else None}


def _cancel_live_link(session: Session, attempt: PaymentAttempt, job: RecoveryJob, payment_id: str, rz_client,
                      now: datetime) -> str:
    """Cancel one live link because its order was paid. Returns "cancelled" or "parked". A failed
    cancel parks the job for a person with the reason: a live link for a paid order is never left
    silent."""
    reason = executor.order_paid_reason(payment_id)
    if offers.shadow_mode():
        # nothing leaves the process in shadow mode: the would-be cancel is audited, the job is left as it is
        audit(session, attempt.id, "cancel",
              f"shadow mode: would POST /payment_links/{job.razorpay_link_id}/cancel because the {reason}; nothing sent",
              {"job_id": job.id, "link_id": job.razorpay_link_id, "paid_by_payment_id": payment_id, "mode": offers.MODE_SHADOW})
        session.commit()
        return "shadow"
    try:
        cancel = getattr(rz_client, "cancel_payment_link", None)
        if cancel is None:
            raise RuntimeError(f"{getattr(rz_client, 'name', type(rz_client).__name__)} cannot cancel payment links")
        link = cancel(job.razorpay_link_id)
        status = str((link or {}).get("status") or "") if isinstance(link, dict) else ""
        if status and status != "cancelled":
            raise RuntimeError(f"cancel answered with status {status!r}, not 'cancelled'")
    except Exception as exc:  # RazorpayError, or a client bug: either way the job is parked, not left live
        job.status, job.executed_at = JobStatus.HUMAN_QUEUE.value, job.executed_at or now
        job.last_error = (f"{reason}, but cancelling live link {job.razorpay_link_id} failed ({exc}); a person cancels "
                          f"it at Razorpay before the customer can pay twice")
        audit(session, attempt.id, "cancel",
              f"human_queue: {job.last_error}",
              {"job_id": job.id, "link_id": job.razorpay_link_id, "paid_by_payment_id": payment_id, "error": str(exc)})
        session.commit()
        executor.record_outcome(session, attempt, job, False, 0, f"human_queue: {job.last_error}", now=now)
        return "parked"
    job.status, job.executed_at = JobStatus.CANCELLED.value, job.executed_at or now
    job.last_error = f"{reason}; link {job.razorpay_link_id} cancelled at Razorpay"
    audit(session, attempt.id, "cancel",
          f"cancelled: payment link {job.razorpay_link_id} cancelled at Razorpay because the {reason}; "
          f"the customer cannot pay twice",
          {"job_id": job.id, "link_id": job.razorpay_link_id, "paid_by_payment_id": payment_id, "link_status": "cancelled"})
    session.commit()
    executor.record_outcome(session, attempt, job, False, 0,
                            f"{executor.ORDER_PAID_NOTE} (link {job.razorpay_link_id} cancelled)", now=now)
    return "cancelled"


def record_order_paid(session: Session, paid: dict, *, rz_client=None, event_id: str | None = None,
                      now: datetime | None = None, source: str = "webhook", quiet: bool = False) -> dict:
    """The money-path stop. For every attempt on the order: record order_paid_at / paid_by_payment_id
    (first record wins), turn every pending job into no_action with an outcome, and cancel every
    live link (sent, no outcome yet) at Razorpay: cancelled -> job cancelled + outcome; the cancel
    failed -> job human_queue with the reason + outcome. Idempotent: a redelivery finds nothing
    pending and nothing live, and writes one audit row. `quiet` (from close_paid_link) skips the
    per-attempt "received" row when nothing needed doing."""
    now = now or utcnow()
    event = paid.get("event") or EVENT_PAYMENT_CAPTURED
    order_id, payment_id = paid.get("order_id"), paid.get("payment_id") or "?"
    if not order_id:
        audit(session, None, "ingest", f"{event} for {payment_id} carries no order_id; nothing to match, ignored",
              {**paid, "event_id": event_id, "matched": False})
        session.commit()
        return {"event": event, "matched": False, "order_id": None, "payment_id": payment_id}
    attempts = executor.mark_order_paid(session, order_id, payment_id, paid.get("paid_at"), now=now)
    out = {"event": event, "matched": bool(attempts), "order_id": order_id, "payment_id": payment_id,
           "attempt_ids": [a.id for a in attempts], "voided": [], "cancelled": [], "parked": [], "reminders_voided": [],
           "already_recorded": False}
    if not attempts:
        audit(session, None, "ingest", f"{event} for order {order_id} ({payment_id}) matches no attempt; ignored",
              {**paid, "event_id": event_id, "matched": False})
        session.commit()
        return out
    client = None
    for attempt in attempts:
        jobs = session.execute(with_reminders(select(RecoveryJob).where(RecoveryJob.attempt_id == attempt.id)
                                              .order_by(RecoveryJob.id))).scalars().all()
        reminders = [j for j in jobs if j.status == JobStatus.PENDING.value and j.action == Action.REMINDER.value]
        pending = [j for j in jobs if j.status == JobStatus.PENDING.value and j.action != Action.REMINDER.value]
        live = [j for j in jobs if j.status == JobStatus.SENT.value and j.razorpay_link_id
                and j.action in executor.LINK_ACTIONS
                and session.execute(select(Outcome.id).where(Outcome.job_id == j.id)).first() is None]
        if not pending and not live and not reminders:
            already = attempt.paid_by_payment_id != payment_id or any(
                (j.last_error or "").startswith(executor.ORDER_PAID_PREFIX) for j in jobs)
            out["already_recorded"] = out["already_recorded"] or already
            if not quiet or already:
                audit(session, attempt.id, "ingest",
                      f"{event}: order {order_id} paid by {payment_id}; "
                      + ("already recorded, nothing pending, no live link; nothing changed" if already
                         else "nothing pending and no live link for this attempt; recorded, nothing to stop"),
                      {**paid, "event_id": event_id, "source": source, "redelivery": already})
            continue
        audit(session, attempt.id, "ingest",
              f"{event}: order {order_id} paid by {payment_id}; {len(pending)} pending job(s) to void, "
              f"{len(live)} live link(s) to cancel"
              + (f", {len(reminders)} pending reminder(s) to void" if reminders else ""),
              {**paid, "event_id": event_id, "source": source, "pending_job_ids": [j.id for j in pending],
               "live_job_ids": [j.id for j in live], "reminder_job_ids": [j.id for j in reminders]})
        session.commit()
        if reminders:
            out["reminders_voided"].extend(j.id for j in cadence.void_reminders(
                session, attempt, executor.order_paid_reason(payment_id), now=now))
        for job in pending:
            job.status, job.executed_at, job.last_error = JobStatus.NO_ACTION.value, now, executor.order_paid_reason(payment_id)
            audit(session, attempt.id, "schedule",
                  f"no_action: job#{job.id} (seq {job.retry_seq}) voided before execution, {job.last_error}; nothing will be sent",
                  {"job_id": job.id, "paid_by_payment_id": payment_id})
            session.commit()
            executor.record_outcome(session, attempt, job, False, 0, executor.ORDER_PAID_NOTE, now=now)
            out["voided"].append(job.id)
        for job in live:
            client = client or rz_client or get_client()
            verdict = _cancel_live_link(session, attempt, job, payment_id, client, now)
            out.setdefault("shadow", [])
            out["cancelled" if verdict == "cancelled" else "shadow" if verdict == "shadow" else "parked"].append(job.id)
    return out


def ingest_event(session: Session, payload: dict, *, process: bool = False, execute_now: bool = False,
                 now: datetime | None = None, event_id: str | None = None, source: str = "webhook",
                 rz_client=None, llm_client=None, sleep: Callable[[float], None] = time.sleep) -> dict:
    """One webhook payload. payment.failed -> the attempt row (new, or the stored one on a
    redelivery) and, with process=True, pipeline.process_attempt on it; payment_link.paid ->
    close the outcome; payment_link.expired -> the outcome and the next attempt; payment.captured
    / order.paid -> the order is paid, void pending jobs and cancel live links; anything else ->
    {"ignored": <event>}. Raises IngestError for a payload
    that cannot be parsed and PipelineDBError when the database refuses a write (the caller must
    NOT acknowledge the event in that case)."""
    now = now or utcnow()
    if not isinstance(payload, dict):
        raise IngestError(f"payload must be a JSON object, got {type(payload).__name__}")
    event = _event_name(payload)
    if event is None and payload.get("entity") == "payment" and "id" in payload:
        event = EVENT_PAYMENT_FAILED  # the bare Payments API entity of a failed payment
    if event == EVENT_PAYMENT_FAILED or event in SUBSCRIPTION_EVENTS:
        fields = parse_payment_failed(payload) if event == EVENT_PAYMENT_FAILED else parse_subscription_event(payload)
        with pipeline.db_guarded(session):
            attempt, created = ingest_attempt(session, fields, source=source, event_id=event_id, now=now)
        out = {"event": event, "attempt_id": attempt.id, "payment_id": attempt.razorpay_payment_id,
               "created": created, "redelivery": not created, "amount_paise": attempt.amount_paise,
               "processed": False}
        if process:
            summary = pipeline.process_attempt(session, attempt, execute_now=execute_now, now=now,
                                               rz_client=rz_client, llm_client=llm_client, sleep=sleep)
            out.update({"processed": True, "summary": summary, "job_id": summary["job_id"],
                        "job_status": summary["job_status"], "action": summary["action"],
                        "failure_class": summary["failure_class"], "link_url": summary["link_url"]})
        return out
    if event == EVENT_LINK_PAID:
        paid = parse_payment_link_paid(payload)
        with pipeline.db_guarded(session):
            return close_paid_link(session, paid, event_id=event_id, now=now, rz_client=rz_client)
    if event == EVENT_LINK_EXPIRED:
        expired = parse_payment_link_expired(payload)
        with pipeline.db_guarded(session):
            return close_expired_link(session, expired, event_id=event_id, now=now)
    if event in ORDER_PAID_EVENTS:
        captured = parse_payment_captured(payload)
        with pipeline.db_guarded(session):
            return record_order_paid(session, captured, rz_client=rz_client, event_id=event_id, now=now, source=source)
    return {"ignored": event or "(no event field)", "event": event}


# ---- the dashboard CSV export --------------------------------------------------------------------

_HEADER_ALIASES = {
    "razorpay_payment_id": ("id", "payment_id", "razorpay_payment_id", "paymentid"),
    "amount": ("amount", "amount_inr", "amount_rs", "amount_rupees", "amount_in_rupees", "amountinr"),
    "amount_paise": ("amount_paise", "amount_in_paise", "amountpaise"),
    "currency": ("currency",),
    "status": ("status", "payment_status"),
    "order_id": ("order_id", "orderid", "razorpay_order_id"),
    "method": ("method", "payment_method"),
    "email": ("email", "customer_email"),
    "contact": ("contact", "phone", "mobile", "customer_contact"),
    "description": ("description",),
    "error_code": ("error_code", "errorcode", "code"),
    "error_description": ("error_description", "errordescription", "error_desc", "failure_reason"),
    "error_source": ("error_source", "errorsource"),
    "error_step": ("error_step", "errorstep"),
    "error_reason": ("error_reason", "errorreason"),
    "token_id": ("token_id", "token", "tokenid"),
    "created_at": ("created_at", "createdat", "created", "date", "timestamp", "created_on"),
    "customer_name": ("customer_name", "name", "customer", "customername"),
    "customer_language": ("customer_language", "language", "lang", "locale"),
    "merchant_id": ("merchant_id", "account_id", "merchantid"),
}


def _norm_header(h: str) -> str:
    h = (h or "").strip().lower().replace("-", "_").replace(" ", "_")
    h = re.sub(r"[^a-z0-9_]", "", h)
    return re.sub(r"_+", "_", h).strip("_")


def _column_map(headers: list[str]) -> dict[str, str]:
    """canonical name -> the actual header in this file (first alias that appears wins)."""
    normalised = {_norm_header(h): h for h in headers if h is not None}
    out: dict[str, str] = {}
    for canon, aliases in _HEADER_ALIASES.items():
        for alias in aliases:
            if alias in normalised:
                out[canon] = normalised[alias]
                break
    return out


_CURRENCY_MARKS = re.compile(r"(?i)(rs\.?|inr|₹)")


def parse_csv_amount(raw, *, header_is_paise: bool = False) -> int:
    """'2,499.00' / 'Rs 2499.50' -> rupees -> paise; '2499' -> rupees unless the header says paise.
    Raises IngestError for anything that is not a positive amount."""
    text = str(raw if raw is not None else "").strip()
    text = _CURRENCY_MARKS.sub("", text).replace(",", "").replace(" ", "")
    if not text:
        raise IngestError("amount is empty")
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise IngestError(f"amount {raw!r} is not a number") from None
    if value <= 0:
        raise IngestError(f"amount {raw!r} is not positive")
    if "." in text or not header_is_paise:
        paise = (value * 100).quantize(Decimal(1))
    else:
        paise = value.quantize(Decimal(1))
    return int(paise)


def parse_csv_timestamp(raw) -> datetime | None:
    """unix seconds, ISO 8601 (naive = UTC; an offset is normalised to UTC), or 'DD/MM/YYYY HH:MM[:SS]'."""
    text = str(raw if raw is not None else "").strip()
    if not text:
        return None
    if re.fullmatch(r"\d{9,11}", text):
        return _unix_to_naive_utc(int(text))
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt
    except ValueError:
        pass
    for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y", "%d-%m-%Y %H:%M:%S", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


@dataclass
class ParsedCSV:
    """The importable rows plus everything that was not: nothing is dropped silently."""
    rows: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)       # {"line": n, "reason": str, "id": str|None}
    not_failed: dict[str, int] = field(default_factory=dict)  # status -> count of rows not imported
    columns: dict[str, str] = field(default_factory=dict)   # canonical -> header actually used
    total_rows: int = 0

    def __iter__(self):
        return iter(self.rows)

    def __len__(self):
        return len(self.rows)

    def skipped_reasons(self) -> str:
        counts: dict[str, int] = {}
        for s in self.skipped:
            counts[s["reason"]] = counts.get(s["reason"], 0) + 1
        return "; ".join(f"{n} {r}" for r, n in counts.items()) or "-"


def parse_payments_csv(source: str | Path | IO) -> ParsedCSV:
    """The Razorpay dashboard payments export -> PaymentAttempt field dicts (only status=failed
    rows; a file with no status column is taken to be an export of failed payments). Headers are
    matched tolerantly after lowercasing and turning spaces/dashes into underscores. Rows with no
    id, a non pay_ id or an unparsable amount are reported in .skipped, never dropped silently."""
    if hasattr(source, "read"):
        text = source.read()
        if isinstance(text, bytes):
            text = text.decode("utf-8-sig")
        fh = io.StringIO(text)
    else:
        fh = open(source, "r", encoding="utf-8-sig", newline="")
    result = ParsedCSV()
    with fh:
        reader = csv.DictReader(fh)
        headers = reader.fieldnames or []
        cols = _column_map(headers)
        result.columns = dict(cols)
        if "razorpay_payment_id" not in cols:
            raise IngestError(f"CSV has no payment id column (looked for id / payment_id); headers: {headers}")
        if "amount" not in cols and "amount_paise" not in cols:
            raise IngestError(f"CSV has no amount column; headers: {headers}")
        get = lambda row, canon: _clean(row.get(cols[canon])) if canon in cols else None  # noqa: E731
        paise_header = "amount_paise" in cols and "amount" not in cols
        amount_col = "amount_paise" if paise_header else "amount"
        for row in reader:
            result.total_rows += 1
            line = reader.line_num
            pid = get(row, "razorpay_payment_id")
            status = (get(row, "status") or "failed").lower()
            if status != "failed":
                result.not_failed[status] = result.not_failed.get(status, 0) + 1
                continue
            if not pid:
                result.skipped.append({"line": line, "id": None, "reason": "missing payment id"})
                continue
            if not pid.startswith("pay_"):
                result.skipped.append({"line": line, "id": pid, "reason": "payment id does not start with pay_"})
                continue
            try:
                amount = parse_csv_amount(get(row, amount_col), header_is_paise=paise_header)
            except IngestError as exc:
                reason = "missing amount" if "empty" in str(exc) else f"bad amount ({exc})"
                result.skipped.append({"line": line, "id": pid, "reason": reason})
                continue
            email = get(row, "email")
            if email and email.lower() in PLACEHOLDER_EMAILS:
                email = None
            result.rows.append({
                "merchant_id": get(row, "merchant_id") or DEFAULT_MERCHANT,
                "order_id": get(row, "order_id") or "",
                "razorpay_payment_id": pid,
                "amount_paise": amount,
                "currency": (get(row, "currency") or "INR").upper()[:3],
                "method": (get(row, "method") or "unknown").lower()[:16],
                "error_code": get(row, "error_code"),
                "error_description": get(row, "error_description"),
                "error_source": get(row, "error_source"),
                "error_step": get(row, "error_step"),
                "error_reason": get(row, "error_reason"),
                "has_token": bool(get(row, "token_id")),
                "customer_name": get(row, "customer_name"),
                "customer_language": _language({"language": get(row, "customer_language")}),
                "customer_contact": get(row, "contact"),
                "customer_email": email,
                "failed_at": parse_csv_timestamp(get(row, "created_at")) or utcnow(),
            })
    return result


def import_rows(session: Session, rows: list[dict], *, now: datetime | None = None,
                source: str = "csv") -> tuple[list[PaymentAttempt], list[PaymentAttempt]]:
    """Insert every row whose payment id is new; return (created, already_present). Each new row
    gets its ingest audit line; an already-present id is left exactly as it was (no audit row:
    an export re-run is not a redelivery, and the trail should not fill up with it)."""
    now = now or utcnow()
    created: list[PaymentAttempt] = []
    present: list[PaymentAttempt] = []
    with pipeline.db_guarded(session):
        for fields in rows:
            pid = fields["razorpay_payment_id"]
            existing = session.execute(select(PaymentAttempt).where(
                PaymentAttempt.razorpay_payment_id == pid)).scalars().first()
            if existing is not None:
                present.append(existing)
                continue
            row = PaymentAttempt(**fields)
            session.add(row)
            session.flush()
            audit(session, row.id, "ingest", "failed payment event received",
                  {"razorpay_payment_id": pid, "method": row.method, "amount_paise": row.amount_paise,
                   "error": {"code": row.error_code, "description": row.error_description,
                             "source": row.error_source, "step": row.error_step, "reason": row.error_reason},
                   "has_token": row.has_token, "source": source})
            created.append(row)
        session.commit()
    return created, present


# ---- the webhook receiver ------------------------------------------------------------------------

def handle_webhook_request(body: bytes, signature: str | None, *, secret: str | None = None,
                           process: bool = False, execute_now: bool = False, event_id: str | None = None,
                           lock: "threading.Lock | None" = None, session_factory: Callable[[], Session] | None = None,
                           rz_client=None, llm_client=None, now: datetime | None = None) -> tuple[int, dict]:
    """The receiver's logic without the socket: (http_status, json_body).
      401  the secret is set and the signature is missing or wrong (nothing is read from the body)
      400  the body is not a JSON object, or is an event the parser rejects
      200  {"ok": true, ...summary}: the event is acknowledged (ignored events included)
      500  the database refused a write: NOT acknowledged, Razorpay redelivers
    Processing is serialised under `lock`: SQLite and the pipeline are single-writer."""
    secret = config.RAZORPAY_WEBHOOK_SECRET if secret is None else secret
    if secret:
        if not signature or not verify_signature(body, signature, secret):
            return 401, {"ok": False, "error": f"{SIGNATURE_HEADER} missing or invalid"}
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        return 400, {"ok": False, "error": f"body is not valid JSON: {exc}"}
    if not isinstance(payload, dict):
        return 400, {"ok": False, "error": "body must be a JSON object"}
    lock = lock or threading.Lock()
    factory = session_factory or db.session
    with lock:
        try:
            session = factory()
        except DBAPIError as exc:
            return 500, {"ok": False, "error": f"database unavailable: {pipeline._describe_db_error(exc)}",
                         "acknowledged": False}
        try:
            summary = ingest_event(session, payload, process=process, execute_now=execute_now, now=now,
                                   event_id=event_id, rz_client=rz_client, llm_client=llm_client)
        except IngestError as exc:
            return 400, {"ok": False, "error": str(exc), "event": _event_name(payload)}
        except pipeline.PipelineDBError as exc:
            return 500, {"ok": False, "error": str(exc), "acknowledged": False,
                         "link_may_exist": bool(getattr(exc, "link_may_exist", False))}
        except DBAPIError as exc:
            return 500, {"ok": False, "error": f"database unavailable: {pipeline._describe_db_error(exc)}",
                         "acknowledged": False}
        finally:
            try:
                session.close()
            except Exception:
                pass
    return 200, {"ok": True, **_jsonable(summary)}


def _jsonable(value):
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _summary_line(status: int, body: dict) -> str:
    if status != 200:
        return f"{status} {body.get('error', '')}"
    if "ignored" in body:
        return f"200 ignored {body['ignored']}"
    ev = body.get("event")
    if ev == EVENT_PAYMENT_FAILED:
        tail = " redelivery" if body.get("redelivery") else " new"
        if body.get("processed"):
            tail += f" -> {body.get('failure_class')} {body.get('action')} {body.get('job_status')}"
            if body.get("link_url"):
                tail += f" {body['link_url']}"
        return f"200 {ev} {body.get('payment_id')}{tail}"
    if ev == EVENT_LINK_PAID:
        if body.get("closed"):
            return f"200 {ev} {body.get('link_id')} -> recovered {body.get('amount_recovered_paise')} paise (job#{body.get('job_id')})"
        if body.get("already_closed"):
            return f"200 {ev} {body.get('link_id')} redelivery, outcome already recorded"
        return f"200 {ev} {body.get('link_id')} matched no job, ignored"
    if ev == EVENT_LINK_EXPIRED:
        if body.get("closed"):
            tail = (f"; follow-up job#{body['followup_job_id']} {body.get('followup_status')}"
                    if body.get("followup_job_id") else "")
            return f"200 {ev} {body.get('link_id')} -> not recovered (job#{body.get('job_id')}){tail}"
        if body.get("already_closed"):
            return f"200 {ev} {body.get('link_id')} redelivery, outcome already recorded"
        return f"200 {ev} {body.get('link_id')} matched no job, ignored"
    if ev in ORDER_PAID_EVENTS:
        if not body.get("matched"):
            return f"200 {ev} order {body.get('order_id')} matched no attempt, ignored"
        return (f"200 {ev} order {body.get('order_id')} paid by {body.get('payment_id')}: "
                f"{len(body.get('voided') or [])} voided, {len(body.get('cancelled') or [])} cancelled, "
                f"{len(body.get('parked') or [])} parked")
    return f"200 {ev}"


def make_handler(*, secret: str | None, process: bool, execute_now: bool, lock,
                 log: Callable[[str], None] = print, rz_client=None, llm_client=None):
    """A BaseHTTPRequestHandler class bound to this run's settings and shared clients."""

    class WebhookHandler(BaseHTTPRequestHandler):
        server_version = "payment-recovery-agent/0.1"

        def log_message(self, fmt, *args):  # one line per request comes from _reply instead
            pass

        def _reply(self, status: int, body: dict, summary: str | None = None) -> None:
            data = json.dumps(body, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            log(f"{utcnow():%Y-%m-%d %H:%M:%S}  {self.command} {self.path}  {summary or status}")

        def do_GET(self):
            if self.path.split("?")[0] == HEALTH_PATH:
                return self._reply(200, {"ok": True, "database": db.url(), "process": process})
            return self._reply(404, {"ok": False, "error": "not found"}, "404 not found")

        def do_POST(self):
            if self.path.split("?")[0] != WEBHOOK_PATH:
                return self._reply(404, {"ok": False, "error": f"POST {WEBHOOK_PATH}"}, "404 not found")
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._reply(400, {"ok": False, "error": "bad Content-Length"}, "400 bad Content-Length")
            if length <= 0 or length > MAX_BODY_BYTES:
                return self._reply(400, {"ok": False, "error": "empty or oversized body"}, "400 empty or oversized body")
            body = self.rfile.read(length)
            status, out = handle_webhook_request(
                body, self.headers.get(SIGNATURE_HEADER), secret=secret, process=process, execute_now=execute_now,
                event_id=self.headers.get(EVENT_ID_HEADER), lock=lock, rz_client=rz_client, llm_client=llm_client)
            return self._reply(status, out, _summary_line(status, out))

    return WebhookHandler


def serve(*, host: str = "0.0.0.0", port: int = 8080, secret: str | None = None, process: bool = False,
          execute_now: bool = False, log: Callable[[str], None] = print, rz_client=None,
          llm_client=None) -> ThreadingHTTPServer:
    """Build (not start) the server. serve_forever() it in the CLI, or in a thread in a test; port 0
    picks a free port (server.server_address[1])."""
    secret = config.RAZORPAY_WEBHOOK_SECRET if secret is None else secret
    handler = make_handler(secret=secret, process=process, execute_now=execute_now, lock=threading.Lock(), log=log,
                           rz_client=rz_client, llm_client=llm_client)
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def batch_totals(summaries: list[dict]) -> dict:
    """The 'measured money' line for a batch: amount at risk and where each event ended up."""
    at_risk = sum(int(s.get("amount_paise") or 0) for s in summaries)
    statuses = [s.get("job_status") for s in summaries]
    return {
        "events": len(summaries),
        "amount_at_risk_paise": at_risk,
        "linked": sum(1 for s in summaries if s.get("link_url") or s.get("job_status") == JobStatus.SENT.value),
        "pending": statuses.count(JobStatus.PENDING.value),
        "human_queue": statuses.count(JobStatus.HUMAN_QUEUE.value),
        "stubbed": statuses.count(JobStatus.STUBBED.value),
        "failed": statuses.count(JobStatus.FAILED.value),
        "no_action": statuses.count(JobStatus.NO_ACTION.value),
        "duplicates": statuses.count(JobStatus.SKIPPED_DUPLICATE.value),
    }

