"""Delivery receipts and the reminder cadence: a sent link becomes a campaign, not one message.

Delivery is Razorpay's own. A Payment Link created with notify.sms / notify.email is messaged by
Razorpay at creation, and POST /payment_links/{id}/notify_by/{sms|email} re-sends that message
later. Nothing here needs a third-party SMS or email provider, and nothing here sends the wording
app/llm.py drafts: the drafted nudge is stored on the job for a provider that is not in scope.
"Delivered" in the audit trail therefore means "Razorpay sent the customer its own link message on
this channel and answered success", never "the customer received our text".

The cadence is deterministic and per class (REMINDER_OFFSETS): after a link job is `sent`, one
RecoveryJob with Action.REMINDER per offset, keyed sha256(f"{payment_id}:{retry_seq}:reminder:{n}")
(UNIQUE like every job key), parent_job_id = the link job, scheduled_at = the send time + offset
moved out of the 21:00-09:00 IST quiet hours (app/scheduling.py). Offsets at or beyond the link's
expiry are skipped with a note; human-queue classes have no cadence. A reminder is refused into
human_queue by the weekly contact cap, at schedule time and again at execution (each reminder is a
contact), and skipped (no_action) when the link is no longer open or the order was paid elsewhere.
The scheduler executes reminders through executor.execute_job like any job; a reminder never
creates a link, so the one-link-per-payment invariant is untouched by it.

Subscriptions (subscription.pending / subscription.halted, app/ingest.py) go through the same
classify -> policy path; the two overrides below are the only subscription-specific rules, kept
out of app/policy.py on purpose: pending means Razorpay is still retrying the charge itself, so no
new link goes out (human_queue with a stated reason); halted means those retries are exhausted and
the mandate must be re-authorised, so a token retry is pointless and the customer gets the
subscription's own hosted re-authorisation page (link_source "subscription_url") rather than a new
Payment Link.
"""
import hashlib
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import config, executor, offers, scheduling
from .clock import utcnow
from .models import Outcome, PaymentAttempt, RecoveryDecision, RecoveryJob, audit, with_reminders
from .razorpay_client import NOTIFY_MEDIA, RazorpayClient, RazorpayError, get_client
from .taxonomy import Action, FailureClass, JobStatus, PolicyDecision

__all__ = ["REMINDER_OFFSETS", "LINK_SOURCE_PAYMENT_LINK", "LINK_SOURCE_SUBSCRIPTION", "SUBSCRIPTION_PENDING_REASON",
           "reminder_key", "reminders_for", "schedule_reminders", "execute_reminder", "void_reminders",
           "record_delivery", "delivery_media", "link_source_for", "subscription_override"]

# hours after the link went out; every offset must land before RECOVERY_LINK_EXPIRY_HOURS (72 by default)
REMINDER_OFFSETS: dict[str, tuple[timedelta, ...]] = {
    FailureClass.INSUFFICIENT_FUNDS.value: (timedelta(hours=24), timedelta(hours=60)),
    FailureClass.LIMIT_EXCEEDED.value: (timedelta(hours=24),),
    FailureClass.AUTH_ABANDONED.value: (timedelta(hours=2), timedelta(hours=24)),
    FailureClass.HARD_DECLINE.value: (timedelta(hours=24),),
    FailureClass.ISSUER_DOWN.value: (timedelta(hours=24),),
    FailureClass.NETWORK_TIMEOUT.value: (timedelta(hours=24),),
    # RISK_BLOCKED and UNKNOWN never reach a sent link (human queue); no cadence on purpose
}
LINK_SOURCE_PAYMENT_LINK = "payment_link"
LINK_SOURCE_SUBSCRIPTION = "subscription_url"
SUBSCRIPTION_PENDING = "pending"
SUBSCRIPTION_HALTED = "halted"
SUBSCRIPTION_PENDING_REASON = "subscription pending: Razorpay retry in progress"
NOT_DELIVERED_OFF = "not delivered: RAZORPAY_NOTIFY_CUSTOMER is off; message drafted and stored only"
CLOSED_LINK_STATUSES = frozenset({"paid", "expired", "cancelled"})
_SUBSCRIPTION_NORMAL_CLASSES = frozenset({FailureClass.HARD_DECLINE, FailureClass.RISK_BLOCKED})


# ---- keys and lookups ---------------------------------------------------------------------------

def reminder_key(razorpay_payment_id: str, retry_seq: int, n: int) -> str:
    return hashlib.sha256(f"{razorpay_payment_id}:{int(retry_seq)}:reminder:{int(n)}".encode("utf-8")).hexdigest()


def reminders_for(session: Session, attempt: PaymentAttempt) -> list[RecoveryJob]:
    """Every reminder job for the attempt, by due time then id (for `show` and the operator view)."""
    stmt = (select(RecoveryJob).where(RecoveryJob.attempt_id == attempt.id, RecoveryJob.action == Action.REMINDER.value)
            .order_by(RecoveryJob.scheduled_at, RecoveryJob.id))
    return list(session.execute(with_reminders(stmt)).scalars().all())


def get_job(session: Session, job_id: int | None) -> RecoveryJob | None:
    """session.get for a job that may be a reminder (plain job queries hide reminders, app/models.py)."""
    if job_id is None:
        return None
    return session.execute(with_reminders(select(RecoveryJob).where(RecoveryJob.id == job_id))).scalars().first()


def delivery_media(attempt: PaymentAttempt) -> list[str]:
    """The notify_by media this customer can be reached on, in Razorpay's order (sms, email)."""
    out = []
    if (getattr(attempt, "customer_contact", None) or "").strip():
        out.append("sms")
    if (getattr(attempt, "customer_email", None) or "").strip():
        out.append("email")
    return [m for m in out if m in NOTIFY_MEDIA]


def link_source_for(attempt: PaymentAttempt) -> str:
    """A halted subscription's link is its own hosted re-authorisation page; everything else is a Payment Link."""
    if (getattr(attempt, "subscription_id", None) and getattr(attempt, "subscription_status", None) == SUBSCRIPTION_HALTED
            and getattr(attempt, "subscription_url", None)):
        return LINK_SOURCE_SUBSCRIPTION
    return LINK_SOURCE_PAYMENT_LINK


def _reminder_no(job: RecoveryJob) -> int:
    try:
        return int((job.schedule_note or "").split("reminder ", 1)[1].split(" ", 1)[0].rstrip(":"))
    except (IndexError, ValueError):
        return 0


# ---- the deliver stage --------------------------------------------------------------------------

def record_delivery(session: Session, attempt: PaymentAttempt, job: RecoveryJob, *, now: datetime | None = None,
                    link: dict | None = None) -> list[dict]:
    """One `deliver` audit row per medium for a link that has just gone out, or one row saying why
    nothing was delivered. Commits. Returns the rows' data dicts.

    With RAZORPAY_NOTIFY_CUSTOMER on, the link was created with notify.sms / notify.email (see
    executor.build_payment_link_payload), so Razorpay sent its own link message at creation; the
    receipt recorded per medium is {"success": true}, the same shape notify_by answers, ASSUMED
    from the create call having been accepted with those flags (the create response echoes them
    under "notify"). With the flag off, the message is drafted and stored only."""
    now = now or utcnow()
    rows: list[dict] = []
    if job.link_source == LINK_SOURCE_SUBSCRIPTION:
        data = {"job_id": job.id, "delivered": False, "medium": None, "link_url": job.razorpay_link_url,
                "subscription_id": attempt.subscription_id,
                "reason": "subscription re-authorisation URL: no Payment Link to notify; Razorpay emails its own "
                          "re-authorisation link when a subscription halts (reported); drafted nudge stored only"}
        audit(session, attempt.id, "deliver", f"not delivered by the agent: {data['reason']}", data)
        rows.append(data)
    elif not config.RAZORPAY_NOTIFY_CUSTOMER:
        data = {"job_id": job.id, "delivered": False, "medium": None, "link_id": job.razorpay_link_id,
                "reason": NOT_DELIVERED_OFF}
        audit(session, attempt.id, "deliver", NOT_DELIVERED_OFF, data)
        rows.append(data)
    else:
        media = delivery_media(attempt)
        flags = (link or {}).get("notify") if isinstance(link, dict) else None
        if isinstance(flags, dict):
            media = [m for m in media if flags.get(m)]
        if not media:
            data = {"job_id": job.id, "delivered": False, "medium": None, "link_id": job.razorpay_link_id,
                    "reason": "no customer contact or email on the attempt; Razorpay had nowhere to send the link"}
            audit(session, attempt.id, "deliver", f"not delivered: {data['reason']}", data)
            rows.append(data)
        for medium in media:
            data = {"job_id": job.id, "delivered": True, "medium": medium, "link_id": job.razorpay_link_id,
                    "receipt": {"success": True},
                    "how": f"notify.{medium}=true on POST /payment_links; Razorpay sent its own link message at creation"}
            audit(session, attempt.id, "deliver",
                  f"delivered via Razorpay {medium}: link {job.razorpay_link_id} notified at creation "
                  f"(notify.{medium}=true); receipt {{\"success\": true}}; the drafted nudge text is stored, not sent", data)
            rows.append(data)
    session.commit()
    return rows


# ---- scheduling reminders -----------------------------------------------------------------------

def schedule_reminders(session: Session, attempt: PaymentAttempt, job: RecoveryJob, decision: RecoveryDecision | None,
                       *, now: datetime | None = None) -> list[RecoveryJob]:
    """Mint the class's reminder jobs for a link job that has just become `sent`. Idempotent: a
    reminder whose key exists already is left as it is. Each reminder is audited under stage
    "schedule" with its offset and any quiet-hours move; skipped offsets get a row saying why."""
    now = now or utcnow()
    shadow = job.status == JobStatus.SHADOW.value  # a would-have link gets would-have reminders (app/offers.py)
    if job.status not in (JobStatus.SENT.value, JobStatus.SHADOW.value) or job.action not in executor.LINK_ACTIONS:
        return []
    failure_class = decision.failure_class if decision is not None else executor.UNSPECIFIED_CLASS
    base = {"job_id": job.id, "failure_class": failure_class, "mode": offers.MODE_SHADOW if shadow else "live"}
    if not config.REMINDERS_ENABLED:
        audit(session, attempt.id, "schedule", "no reminders: REMINDERS_ENABLED is off", base)
        session.commit()
        return []
    if job.link_source == LINK_SOURCE_SUBSCRIPTION or (not job.razorpay_link_id and not shadow):
        audit(session, attempt.id, "schedule",
              "no reminders: the link is the subscription's own re-authorisation URL, not a Payment Link that "
              "notify_by can re-send", {**base, "link_url": job.razorpay_link_url})
        session.commit()
        return []
    offsets = REMINDER_OFFSETS.get(failure_class, ())
    if not offsets:
        audit(session, attempt.id, "schedule", f"no reminder cadence for {failure_class}", base)
        session.commit()
        return []
    sent_at = job.executed_at or now
    expiry_at = sent_at + timedelta(hours=max(executor.MIN_LINK_EXPIRY_HOURS, int(config.RECOVERY_LINK_EXPIRY_HOURS)))
    created: list[RecoveryJob] = []
    for n, offset in enumerate(offsets, 1):
        hours = offset.total_seconds() / 3600
        due = sent_at + offset
        if due >= expiry_at:
            audit(session, attempt.id, "schedule",
                  f"reminder {n} skipped: +{hours:g}h lands at or after the link expiry "
                  f"({expiry_at.isoformat()}, RECOVERY_LINK_EXPIRY_HOURS={config.RECOVERY_LINK_EXPIRY_HOURS})",
                  {**base, "reminder": n, "offset_hours": hours, "due": due.isoformat(), "expiry": expiry_at.isoformat()})
            session.commit()
            continue
        adjusted = scheduling.next_allowed_send(due)
        note = f"reminder {n}: +{hours:g}h after send"
        if adjusted != due:
            note += f"; quiet hours: {scheduling.format_ist(adjusted)}"
        if adjusted >= expiry_at:
            audit(session, attempt.id, "schedule",
                  f"reminder {n} skipped: +{hours:g}h moved out of quiet hours to {adjusted.isoformat()}, which is at "
                  f"or after the link expiry ({expiry_at.isoformat()})",
                  {**base, "reminder": n, "offset_hours": hours, "due": adjusted.isoformat(), "expiry": expiry_at.isoformat()})
            session.commit()
            continue
        status, last_error, scheduled_at = JobStatus.PENDING.value, None, adjusted
        allowed, cap_reason = scheduling.contact_cap(session, attempt, now)
        if not allowed:
            status, last_error, scheduled_at = JobStatus.HUMAN_QUEUE.value, cap_reason, now
        if shadow:
            status = JobStatus.SHADOW.value
        key = reminder_key(attempt.razorpay_payment_id, job.retry_seq, n)
        row = RecoveryJob(attempt_id=attempt.id, decision_id=job.decision_id, retry_seq=job.retry_seq,
                          action=Action.REMINDER.value, scheduled_at=scheduled_at, idempotency_key=key, status=status,
                          last_error=last_error, schedule_note=note[:160], parent_job_id=job.id,
                          link_source=job.link_source, razorpay_link_url=job.razorpay_link_url,
                          executed_at=now if shadow else None)
        session.add(row)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            existing = session.execute(with_reminders(select(RecoveryJob).where(RecoveryJob.idempotency_key == key))).scalar_one()
            audit(session, attempt.id, "schedule", f"reminder {n}: skipped_duplicate, key already present (job#{existing.id})",
                  {**base, "reminder": n, "idempotency_key": key, "existing_job_id": existing.id})
            session.commit()
            continue
        if status == JobStatus.SHADOW.value:
            message = (f"shadow mode: would schedule reminder {n} for {scheduled_at.isoformat()} (+{hours:g}h after send) and "
                       f"POST /payment_links/{{id}}/notify_by/{{{'|'.join(delivery_media(attempt)) or 'sms|email'}}}; nothing will be sent")
        elif status == JobStatus.HUMAN_QUEUE.value:
            message = f"reminder {n} parked human_queue: {cap_reason}; nothing will be sent"
        else:
            message = (f"reminder {n} of link {job.razorpay_link_id} scheduled for {scheduled_at.isoformat()} "
                       f"(+{hours:g}h after send{'; ' + note.split('; ', 1)[1] if '; ' in note else ''})")
        audit(session, attempt.id, "schedule", message,
              {**base, "reminder": n, "reminder_job_id": row.id, "idempotency_key": key, "offset_hours": hours,
               "scheduled_at": scheduled_at.isoformat(), "status": status, "schedule_note": note,
               "link_id": job.razorpay_link_id, "parent_job_id": job.id})
        session.commit()
        created.append(row)
    return created


def void_reminders(session: Session, attempt: PaymentAttempt, reason: str, *, now: datetime | None = None,
                   parent: RecoveryJob | None = None) -> list[RecoveryJob]:
    """Every pending reminder for the attempt (or for one parent link) ends no_action with the
    reason: the link was paid, expired, cancelled, or the order was paid elsewhere. No outcome row:
    the outcome belongs to the link job. Commits. Returns the voided jobs."""
    now = now or utcnow()
    voided = []
    for r in reminders_for(session, attempt):
        if r.status != JobStatus.PENDING.value or (parent is not None and r.parent_job_id != parent.id):
            continue
        r.status, r.executed_at, r.last_error = JobStatus.NO_ACTION.value, now, reason
        audit(session, attempt.id, "schedule",
              f"no_action: reminder job#{r.id} ({r.schedule_note or 'reminder'}) voided before execution, {reason}; "
              f"nothing will be sent", {"job_id": r.id, "parent_job_id": r.parent_job_id, "reason": reason})
        voided.append(r)
    if voided:
        session.commit()
    return voided


# ---- executing a reminder -----------------------------------------------------------------------

def _reload(session: Session, job: RecoveryJob) -> RecoveryJob:
    if job.id is None:
        raise ValueError("execute_reminder needs a persisted reminder job")
    if job in session:
        session.refresh(job)  # a column load: never filtered
        return job
    fresh = get_job(session, job.id)
    if fresh is None:
        raise ValueError(f"reminder job {job.id} no longer exists")
    return fresh


def _skip(session: Session, job: RecoveryJob, why: str, data: dict, now: datetime) -> RecoveryJob:
    job.last_error = why
    return executor._finish(session, job, JobStatus.NO_ACTION.value, f"no_action: reminder skipped, {why}; nothing sent",
                            data, now)


def execute_reminder(session: Session, job: RecoveryJob, *, client: RazorpayClient | None = None,
                     now: datetime | None = None) -> RecoveryJob:
    """Re-notify the parent link through Razorpay, or record why not. Order of the guards, every
    one before any outbound call: already executed; order paid elsewhere; the link no longer open
    (parent not sent, its outcome written, or Razorpay reports paid/expired/cancelled); notifications
    off (no_action, audited); quiet hours (the job moves to 09:00 IST and stays pending); the contact
    cap (human_queue). Then one notify_by call per medium the customer has, each answered receipt
    stored in a `deliver` audit row. Never raises for Razorpay failures."""
    now = now or utcnow()
    job = _reload(session, job)
    if job.status in executor._TERMINAL:
        audit(session, job.attempt_id, "execute", f"already executed: status {job.status}; nothing sent",
              {"job_id": job.id, "idempotency_key": job.idempotency_key, "status": job.status})
        session.commit()
        return job
    attempt = job.attempt if job.attempt is not None else session.get(PaymentAttempt, job.attempt_id)
    parent = session.get(RecoveryJob, job.parent_job_id) if job.parent_job_id else None  # a link job, never hidden
    n = _reminder_no(job)
    data = {"job_id": job.id, "reminder": n, "parent_job_id": job.parent_job_id,
            "link_id": parent.razorpay_link_id if parent is not None else None}

    paid = executor.paid_order(session, attempt)
    if paid is not None:
        return _skip(session, job, f"{executor.order_paid_reason(paid[1])}", {**data, "paid_by_payment_id": paid[1]}, now)
    if parent is None or parent.status != JobStatus.SENT.value or not parent.razorpay_link_id:
        state = parent.status if parent is not None else "missing"
        return _skip(session, job, f"link no longer open: parent job#{job.parent_job_id} is {state}", {**data, "parent_status": state}, now)
    closed = session.execute(select(Outcome).where(Outcome.job_id == parent.id).order_by(Outcome.id.desc())).scalars().first()
    if closed is not None:
        return _skip(session, job, f"link {parent.razorpay_link_id} is closed ({closed.note})", {**data, "outcome_id": closed.id}, now)
    if offers.shadow_mode():
        job.last_error = None
        media = delivery_media(attempt)
        return executor._finish(session, job, JobStatus.SHADOW.value,
                                f"shadow mode: would POST /payment_links/{parent.razorpay_link_id}/notify_by/"
                                f"{{{'|'.join(media) or 'sms|email'}}} for reminder {n}; nothing sent",
                                {**data, "mode": offers.MODE_SHADOW, "media": media}, now)
    if not config.RAZORPAY_NOTIFY_CUSTOMER:
        job.last_error = "notifications off"
        return executor._finish(session, job, JobStatus.NO_ACTION.value,
                                f"no_action: notifications off (RAZORPAY_NOTIFY_CUSTOMER); reminder {n} of link "
                                f"{parent.razorpay_link_id} not sent, the drafted nudge stays stored", data, now)
    if scheduling.within_quiet_hours(now):
        moved = scheduling.next_allowed_send(now)
        job.scheduled_at = moved
        audit(session, attempt.id, "schedule",
              f"reminder {n} due inside quiet hours; moved to {moved.isoformat()} (quiet hours: {scheduling.format_ist(moved)})",
              {**data, "from": now.isoformat(), "to": moved.isoformat()})
        session.commit()
        return job
    allowed, cap_reason = scheduling.contact_cap(session, attempt, now)
    if not allowed:
        job.last_error = cap_reason
        return executor._finish(session, job, JobStatus.HUMAN_QUEUE.value,
                                f"human_queue: reminder {n} refused, {cap_reason}; nothing sent", data, now)
    media = delivery_media(attempt)
    if not media:
        return _skip(session, job, "no customer contact or email to notify", data, now)

    client = client or get_client()
    if not executor._claim_for_execution(session, job, now):
        job = _reload(session, job)
        audit(session, attempt.id, "execute", f"already claimed by another worker: status {job.status}; nothing sent", {"job_id": job.id})
        session.commit()
        return job
    data["client"] = executor._client_name(client)
    fetch = getattr(client, "fetch_payment_link", None)
    if fetch is not None:
        try:
            link = fetch(parent.razorpay_link_id)
            status = str((link or {}).get("status") or "") if isinstance(link, dict) else ""
            if status in CLOSED_LINK_STATUSES:
                return _skip(session, job, f"link {parent.razorpay_link_id} is {status} at Razorpay", {**data, "link_status": status}, now)
        except Exception as exc:  # RazorpayError or a client bug: the local record decides, the call is audited
            audit(session, attempt.id, "execute",
                  f"could not fetch link {parent.razorpay_link_id} before reminder {n} ({exc}); proceeding on the local record",
                  {**data, "error": str(exc)})
            session.commit()

    receipts: list[dict] = []
    errors: list[tuple[str, RazorpayError | Exception]] = []
    for medium in media:
        job.attempts_made = (job.attempts_made or 0) + 1
        session.commit()
        try:
            receipt = client.notify_payment_link(parent.razorpay_link_id, medium)
        except RazorpayError as exc:
            errors.append((medium, exc))
            audit(session, attempt.id, "deliver", f"not delivered via Razorpay {medium}: reminder {n} of link "
                  f"{parent.razorpay_link_id} refused ({exc})",
                  {**data, "delivered": False, "medium": medium, "http_status": exc.status, "code": exc.code, "error": str(exc)})
            session.commit()
            continue
        except Exception as exc:  # a client bug must not take the sweep down
            errors.append((medium, exc))
            audit(session, attempt.id, "deliver", f"not delivered via Razorpay {medium}: reminder {n} of link "
                  f"{parent.razorpay_link_id} failed ({type(exc).__name__}: {exc})",
                  {**data, "delivered": False, "medium": medium, "error": f"{type(exc).__name__}: {exc}"})
            session.commit()
            continue
        receipts.append({"medium": medium, "receipt": receipt})
        audit(session, attempt.id, "deliver",
              f"delivered via Razorpay {medium}: reminder {n} of link {parent.razorpay_link_id} re-sent "
              f"(POST /payment_links/{parent.razorpay_link_id}/notify_by/{medium}); receipt {receipt}",
              {**data, "delivered": True, "medium": medium, "receipt": receipt})
        session.commit()
    if not receipts:
        medium, exc = errors[-1]
        kind = executor._kind_of(exc) if isinstance(exc, RazorpayError) else executor.CLIENT_ERROR
        return executor._fail(session, job, kind, f"reminder {n} via {medium}: {exc}",
                              f"failed: reminder {n} of link {parent.razorpay_link_id} could not be delivered on any "
                              f"medium ({kind}: {exc}); no retry, the next reminder or a person follows",
                              {**data, "media": media}, now)
    job.last_error = None
    job.razorpay_link_url = parent.razorpay_link_url
    return executor._finish(session, job, JobStatus.SENT.value,
                            f"sent: reminder {n} of link {parent.razorpay_link_id} delivered via Razorpay on "
                            f"{', '.join(r['medium'] for r in receipts)}",
                            {**data, "receipts": receipts, "failed_media": [m for m, _ in errors]}, now)


# ---- subscription rules -------------------------------------------------------------------------

def subscription_override(attempt: PaymentAttempt, failure_class: FailureClass, pol: PolicyDecision) -> PolicyDecision | None:
    """The policy adjustment for an attempt that came from a subscription event, or None when the
    table's decision stands. pending: Razorpay retries the charge itself (T+1, T+2, T+3), so the
    action becomes human_queue with SUBSCRIPTION_PENDING_REASON, unless the class is HARD_DECLINE or
    RISK_BLOCKED (their normal action already stops the retrying). halted: Razorpay's retries are
    exhausted and the mandate must be re-authorised, so a token_retry becomes recovery_link now,
    which executor.execute_job serves as the subscription's own re-authorisation URL."""
    sub_id, status = getattr(attempt, "subscription_id", None), getattr(attempt, "subscription_status", None)
    if not sub_id or not status:
        return None
    if status == SUBSCRIPTION_PENDING:
        if failure_class in _SUBSCRIPTION_NORMAL_CLASSES or pol.action in (Action.HUMAN_QUEUE, Action.NO_ACTION):
            return None
        return PolicyDecision(
            action=Action.HUMAN_QUEUE, delay_seconds=0, max_attempts=pol.max_attempts, backoff_multiplier=1.0, nudge=False,
            rationale=(f"{SUBSCRIPTION_PENDING_REASON}: subscription {sub_id} is pending, Razorpay retries the charge "
                       f"itself (T+1, T+2, T+3); no new link while it does, a person watches for subscription.halted "
                       f"(table said {pol.action.value})."))
    if status == SUBSCRIPTION_HALTED and pol.action is Action.TOKEN_RETRY:
        return PolicyDecision(
            action=Action.RECOVERY_LINK, delay_seconds=0, max_attempts=pol.max_attempts, backoff_multiplier=1.0, nudge=True,
            rationale=(f"subscription {sub_id} halted: Razorpay's own retries are exhausted and the mandate needs "
                       f"re-authorisation, so a token retry cannot succeed; the customer gets the subscription's "
                       f"re-authorisation page now (table said token_retry)."))
    return None
