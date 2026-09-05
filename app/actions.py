"""Operator actions: the single place a person's intervention on an attempt or job lives, shared
by the operator view (app/web.py, POST /action/*) and, later, the CLI.

Every action:
  * checks its precondition first and REFUSES with a plain reason (never an exception to the
    caller: the browser and the CLI both show the message as-is),
  * writes one audit row with stage "operator" carrying the actor and the reason, before any
    state change, so the trail shows who asked for what even when the action then fails,
  * returns a small dict: {"ok": bool, "message": str, ...} plus the ids it touched.

    resolve             a person's outcome on a human-queued attempt (recovered N paise, or closed)
    cancel_link         cancel a live payment link at Razorpay: job cancelled + outcome; never a second link
    resend_notification re-notify a sent open link through Razorpay (sms|email), honouring the
                        contact cap and the RAZORPAY_NOTIFY_CUSTOMER flag
    retry_now           execute a pending job ahead of its schedule (executor.execute_job, every guard intact)
    apply_proposal      write an insights proposal into merchants/<id>.json with its evidence (rollback-able)

Duplication note: `resolve` re-implements the checks of app/main.py:cmd_resolve (which are inline
there and print to stderr); when the CLI is rewired it should call this function instead.
"""
from datetime import datetime

from sqlalchemy import select

from . import config, executor, insights, merchants, scheduling
from .clock import utcnow
from .models import Outcome, PaymentAttempt, RecoveryJob, audit, with_reminders
from .nudge_templates import format_rupees
from .razorpay_client import NOTIFY_MEDIA, RazorpayError, get_client
from .taxonomy import JobStatus

STAGE = "operator"
RESOLVED_PREFIX = "resolved by a person"  # == app.main.RESOLVED_PREFIX; a string here so main.py stays optional
ACTOR_MAX = 40
NOSLEEP = lambda s: None  # noqa: E731  an operator waits at the browser; no backoff sleeps


def _refuse(message: str, **extra) -> dict:
    return {"ok": False, "message": message, **extra}


def _ok(message: str, **extra) -> dict:
    return {"ok": True, "message": message, **extra}


def check_actor(actor) -> str | None:
    """The actor name a form or CLI supplied, or None when it is missing or too long."""
    name = (actor or "").strip()
    return name if 0 < len(name) <= ACTOR_MAX else None


def _audit_operator(session, attempt_id: int | None, action: str, actor: str, message: str, data: dict | None = None):
    audit(session, attempt_id, STAGE, f"{action} by {actor}: {message}", {"action": action, "actor": actor, **(data or {})})
    session.commit()


def _job(session, job_id) -> RecoveryJob | None:
    try:
        jid = int(job_id)
    except (TypeError, ValueError):
        return None
    return session.execute(with_reminders(select(RecoveryJob).where(RecoveryJob.id == jid))).scalars().first()


def _has_outcome(session, job: RecoveryJob) -> bool:
    return session.execute(select(Outcome.id).where(Outcome.job_id == job.id)).first() is not None


# ---- resolve --------------------------------------------------------------------------------------

def resolve(session, attempt_id, *, recovered_paise: int | None = None, closed: bool = False, note: str = "",
            actor: str, force: bool = False, now: datetime | None = None) -> dict:
    """A person's decision on a human-queued attempt: exactly one of recovered_paise / closed.
    Refused when the attempt is not human-queued (latest job status), or already has a decided
    outcome (recovered, or an earlier resolve), unless `force`."""
    name = check_actor(actor)
    if name is None:
        return _refuse(f"actor is required (1..{ACTOR_MAX} characters)")
    if (recovered_paise is None) == (not closed):
        return _refuse("say exactly one of: recovered <amount in paise>, or closed")
    if recovered_paise is not None and int(recovered_paise) <= 0:
        return _refuse("recovered takes a positive amount in paise (e.g. 249900 for Rs 2,499.00)")
    try:
        a = session.get(PaymentAttempt, int(attempt_id))
    except (TypeError, ValueError):
        a = None
    if a is None:
        return _refuse(f"no attempt {attempt_id!r}")
    # fresh reads, not the relationships: the same session may have added rows since they loaded
    job = session.execute(select(RecoveryJob).where(RecoveryJob.attempt_id == a.id)
                          .order_by(RecoveryJob.id.desc())).scalars().first()
    outcomes = session.execute(select(Outcome).where(Outcome.attempt_id == a.id).order_by(Outcome.id)).scalars().all()
    decided = [o for o in outcomes if o.recovered or (o.note or "").startswith(RESOLVED_PREFIX)]
    if decided and not force:
        last = decided[-1]
        what = f"recovered {format_rupees(last.amount_recovered_paise)}" if last.recovered else last.note
        return _refuse(f"attempt {a.id} already has an outcome: {what} (outcome#{last.id}); force to record another",
                       attempt_id=a.id)
    if job is not None and job.status != JobStatus.HUMAN_QUEUE.value and not force:
        return _refuse(f"attempt {a.id} is not human-queued (latest job#{job.id} is {job.status}); "
                       f"force to record a person's outcome anyway", attempt_id=a.id)
    suffix = f" ({note.strip()})" if note and note.strip() else ""
    if recovered_paise is not None:
        text = f"{RESOLVED_PREFIX}: recovered {format_rupees(int(recovered_paise))} outside the agent{suffix}"
        _audit_operator(session, a.id, "resolve", name, text,
                        {"attempt_id": a.id, "recovered_paise": int(recovered_paise), "note": note, "force": force})
        o = executor.record_outcome(session, a, job, True, int(recovered_paise), text, now=now)
        return _ok(f"attempt {a.id} recovered {format_rupees(o.amount_recovered_paise)} (outcome#{o.id})",
                   attempt_id=a.id, outcome_id=o.id)
    text = f"{RESOLVED_PREFIX}: closed, not recovered{suffix}"
    _audit_operator(session, a.id, "resolve", name, text, {"attempt_id": a.id, "note": note, "force": force})
    o = executor.record_outcome(session, a, job, False, 0, text, now=now)
    return _ok(f"attempt {a.id} closed, not recovered (outcome#{o.id})", attempt_id=a.id, outcome_id=o.id)


# ---- cancel a live link ---------------------------------------------------------------------------

def cancel_link(session, job_id, actor: str, rz_client=None, *, reason: str = "", now: datetime | None = None) -> dict:
    """Cancel the payment link of a SENT link job at Razorpay (the same call record_order_paid makes
    when an order is paid elsewhere): the job becomes cancelled with an outcome. A failed cancel
    changes nothing but the audit trail; never a second link. Refused for anything but a sent,
    outcome-less link job."""
    name = check_actor(actor)
    if name is None:
        return _refuse(f"actor is required (1..{ACTOR_MAX} characters)")
    job = _job(session, job_id)
    if job is None:
        return _refuse(f"no job {job_id!r}")
    if job.status != JobStatus.SENT.value or not job.razorpay_link_id or job.action not in executor.LINK_ACTIONS:
        return _refuse(f"job#{job.id} is not a sent payment-link job (status {job.status}, action {job.action}); "
                       f"only a live link can be cancelled", job_id=job.id)
    if _has_outcome(session, job):
        return _refuse(f"job#{job.id} already has an outcome; the link is no longer live", job_id=job.id)
    now = now or utcnow()
    why = reason.strip() or "cancelled by a person"
    _audit_operator(session, job.attempt_id, "cancel", name, f"cancelling link {job.razorpay_link_id} ({why})",
                    {"job_id": job.id, "link_id": job.razorpay_link_id, "reason": why})
    client = rz_client or get_client()
    try:
        link = client.cancel_payment_link(job.razorpay_link_id)
        status = str((link or {}).get("status") or "") if isinstance(link, dict) else ""
        if status and status != "cancelled":
            raise RuntimeError(f"cancel answered with status {status!r}, not 'cancelled'")
    except Exception as exc:  # RazorpayError or a client bug: the job stays as it is, the trail says why
        audit(session, job.attempt_id, "cancel", f"cancel of link {job.razorpay_link_id} failed ({exc}); job#{job.id} unchanged",
              {"job_id": job.id, "link_id": job.razorpay_link_id, "error": str(exc), "actor": name})
        session.commit()
        return _refuse(f"Razorpay refused to cancel {job.razorpay_link_id}: {exc}; job#{job.id} unchanged", job_id=job.id)
    job.status, job.executed_at = JobStatus.CANCELLED.value, job.executed_at or now
    job.last_error = f"link {job.razorpay_link_id} cancelled at Razorpay by {name}: {why}"
    audit(session, job.attempt_id, "cancel",
          f"cancelled: payment link {job.razorpay_link_id} cancelled at Razorpay by a person ({why})",
          {"job_id": job.id, "link_id": job.razorpay_link_id, "actor": name, "link_status": "cancelled"})
    session.commit()
    attempt = job.attempt or session.get(PaymentAttempt, job.attempt_id)
    o = executor.record_outcome(session, attempt, job, False, 0, f"{RESOLVED_PREFIX}: link {job.razorpay_link_id} cancelled ({why})",
                                now=now)
    return _ok(f"link {job.razorpay_link_id} cancelled; job#{job.id} cancelled (outcome#{o.id})", job_id=job.id, outcome_id=o.id)


# ---- resend a notification ------------------------------------------------------------------------

def resend_notification(session, job_id, medium: str, actor: str, rz_client=None, *, now: datetime | None = None) -> dict:
    """Ask Razorpay to re-send its link message (sms|email) for a sent, still-open payment link.
    Refused when notifications are off (RAZORPAY_NOTIFY_CUSTOMER, or the merchant file), when the
    customer has no such contact, or when the weekly contact cap is hit (a person may still
    decide, but not by pressing this button a fourth time)."""
    name = check_actor(actor)
    if name is None:
        return _refuse(f"actor is required (1..{ACTOR_MAX} characters)")
    if medium not in NOTIFY_MEDIA:
        return _refuse(f"medium must be one of {', '.join(NOTIFY_MEDIA)}, got {medium!r}")
    job = _job(session, job_id)
    if job is None:
        return _refuse(f"no job {job_id!r}")
    if job.status != JobStatus.SENT.value or not job.razorpay_link_id or job.action not in executor.LINK_ACTIONS:
        return _refuse(f"job#{job.id} is not a sent payment-link job (status {job.status}); nothing to re-notify", job_id=job.id)
    if getattr(job, "link_source", None) == "subscription_url":
        return _refuse(f"job#{job.id} carries a subscription re-authorisation URL, not a Payment Link; Razorpay sends "
                       f"its own message for those", job_id=job.id)
    if _has_outcome(session, job):
        return _refuse(f"job#{job.id} already has an outcome; the link is no longer open", job_id=job.id)
    attempt = job.attempt or session.get(PaymentAttempt, job.attempt_id)
    if not config.RAZORPAY_NOTIFY_CUSTOMER:
        return _refuse("notifications are off (RAZORPAY_NOTIFY_CUSTOMER=0): the agent never messages a customer "
                       "with the flag off, an operator included", job_id=job.id)
    if getattr(merchants.for_attempt(attempt), "notify_customer", None) is False:
        return _refuse(f"merchant {attempt.merchant_id} turned notifications off in its override file", job_id=job.id)
    contact, email = scheduling.contact_keys(attempt)
    if (medium == "sms" and not contact) or (medium == "email" and not email):
        return _refuse(f"the customer has no {medium} contact on this attempt", job_id=job.id)
    now = now or utcnow()
    allowed, why = scheduling.contact_cap(session, attempt, now)
    if not allowed:
        return _refuse(why, job_id=job.id)
    _audit_operator(session, job.attempt_id, "resend", name, f"re-notifying link {job.razorpay_link_id} by {medium} ({why})",
                    {"job_id": job.id, "link_id": job.razorpay_link_id, "medium": medium})
    client = rz_client or get_client()
    try:
        receipt = client.notify_payment_link(job.razorpay_link_id, medium)
    except RazorpayError as exc:
        audit(session, job.attempt_id, "deliver", f"not delivered: notify_by/{medium} for {job.razorpay_link_id} failed ({exc})",
              {"job_id": job.id, "link_id": job.razorpay_link_id, "medium": medium, "delivered": False, "error": str(exc), "actor": name})
        session.commit()
        return _refuse(f"Razorpay refused notify_by/{medium} for {job.razorpay_link_id}: {exc}", job_id=job.id)
    audit(session, job.attempt_id, "deliver", f"delivered: link {job.razorpay_link_id} re-notified by {medium} at a person's request",
          {"job_id": job.id, "link_id": job.razorpay_link_id, "medium": medium, "delivered": True, "receipt": receipt, "actor": name})
    session.commit()
    return _ok(f"link {job.razorpay_link_id} re-notified by {medium}", job_id=job.id, receipt=receipt)


# ---- retry now ------------------------------------------------------------------------------------

def retry_now(session, job_id, actor: str, rz_client=None, *, now: datetime | None = None) -> dict:
    """Execute a PENDING job ahead of its scheduled time through executor.execute_job, so every
    guard (already executed, order paid elsewhere, idempotency, backoff) still applies. Refused
    for any job that is not pending."""
    name = check_actor(actor)
    if name is None:
        return _refuse(f"actor is required (1..{ACTOR_MAX} characters)")
    job = _job(session, job_id)
    if job is None:
        return _refuse(f"no job {job_id!r}")
    if job.status != JobStatus.PENDING.value:
        return _refuse(f"job#{job.id} is {job.status}, not pending; only a pending job can be run early", job_id=job.id)
    now = now or utcnow()
    _audit_operator(session, job.attempt_id, "retry", name,
                    f"running job#{job.id} (seq {job.retry_seq}, {job.action}) now, ahead of its schedule "
                    f"{job.scheduled_at.strftime('%Y-%m-%d %H:%MZ') if job.scheduled_at else '-'}",
                    {"job_id": job.id, "scheduled_at": job.scheduled_at})
    done = executor.execute_job(session, job, client=rz_client, now=now, sleep=NOSLEEP)
    return _ok(f"job#{done.id} executed now: {done.status}" + (f" ({done.last_error})" if done.last_error else ""),
               job_id=done.id, status=done.status)


# ---- apply an insights proposal ------------------------------------------------------------------

def apply_proposal(session, proposal_id: str, actor: str, *, merchant_id: str | None = None,
                   min_samples: int = insights.MIN_SAMPLES) -> dict:
    """Write the proposal's target delay into merchants/<merchant_id>.json (or _default.json for
    every merchant) with the evidence stored next to it. The report is recomputed here, scoped to
    the merchant when one is named, so the evidence is what the database says NOW, not what a
    page showed earlier; a proposal that no longer holds is refused. Both buckets must carry
    n >= insights.MIN_SAMPLES (30) whatever --min-samples the page used: that is the bar for
    making the agent less conservative than the code-reviewed table."""
    name = check_actor(actor)
    if name is None:
        return _refuse(f"actor is required (1..{ACTOR_MAX} characters)")
    report = insights.compute(session, min_samples=max(1, int(min_samples)), merchant=merchant_id or None)
    p = insights.proposal_by_id(report, proposal_id)
    if p is None:
        return _refuse(f"no current proposal {proposal_id!r}" + (f" for merchant {merchant_id}" if merchant_id else "")
                       + "; the evidence may have changed since the page was rendered")
    if not p.is_proposal:
        return _refuse(f"{proposal_id} is not a proposal ({p.kind}): {p.text}")
    if min(p.n_current or 0, p.n_other or 0) < insights.MIN_SAMPLES:
        return _refuse(f"{proposal_id}: both buckets need n >= {insights.MIN_SAMPLES} to apply (have {p.n_current} and "
                       f"{p.n_other}); a lower --min-samples is fine for reading, not for writing")
    fields = {"delay_seconds": insights.bucket_delay_seconds(p.to_bucket)}
    evidence = f"insights proposal {p.id} (n={p.n_current} vs n={p.n_other}, min-samples {min_samples}): {p.text}"
    try:
        path = merchants.write_override(merchant_id, p.failure_class, fields, evidence=evidence, actor=name,
                                        proposal_n=(p.n_current, p.n_other))
    except (merchants.MerchantConfigError, merchants.OverrideRefused) as exc:
        return _refuse(str(exc))
    _audit_operator(session, None, "apply_proposal", name, f"{p.id} applied to {path.name}: {p.failure_class} {fields}",
                    {"proposal_id": p.id, "merchant_id": merchant_id or merchants.ALL_MERCHANTS, "fields": fields,
                     "evidence": evidence, "file": str(path)})
    return _ok(f"{p.id} applied: {path.name} now sets {p.failure_class} delay_seconds={fields['delay_seconds']} "
               f"(rollback: python -m app.merchants rollback --merchant {merchant_id or 'all'} --class {p.failure_class})",
               proposal_id=p.id, file=str(path), fields=fields)
