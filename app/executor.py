"""The money path: schedule a recovery job under an idempotency key, then execute it.

Double-sending is the catastrophic failure of any retry system, so the guard is layered:
  1. idempotency_key = sha256(f"{razorpay_payment_id}:{retry_seq}") is UNIQUE in the DB and
     the INSERT happens in schedule_job, before anything is sent. A re-delivered event
     computes the same key and the INSERT fails; nothing is scheduled twice.
  2. execute_job re-reads the row and refuses to act on a job that is already sent/stubbed,
     before any outbound call.
  3. The key (first 40 chars) is echoed to Razorpay as the Payment Link reference_id, which
     we believe the API keeps unique per link, so even a crash between "HTTP call went out"
     and "row committed" cannot mint a second link on retry: the API says "already exists",
     the link is looked up by that reference_id and recorded, and nothing new is created.
  4. A failed job whose last error was ambiguous (transport failure, 5xx, a client crash: the
     request may have landed) is reconciled by the same reference_id lookup before the policy
     chain may mint a follow-up under a new key; if the lookup cannot answer, a person decides.
  5. An order that was paid another way (payment.captured / order.paid ingested, or found by
     GET /orders/{id}/payments when CHECK_ORDER_BEFORE_SEND is on) is refused at schedule time
     and again at execute time, before any outbound call: the job ends no_action with
     "order already paid by <payment_id>" and an outcome, and nothing is created or charged.
Link jobs are also timed here (app/scheduling.py): the salary window for INSUFFICIENT_FUNDS, the
21:00-09:00 IST quiet hours, and a per-contact weekly cap that parks the job for a person.
No function here raises on a Razorpay failure; the job is returned with status failed.
DB errors propagate on purpose (an unacknowledged event is safer than a lost one).
"""
import hashlib
import json
import time
from datetime import datetime, timedelta
from typing import Callable

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import config, merchants, offers, scheduling
from .clock import to_unix, utcnow
from .models import Outcome, PaymentAttempt, RecoveryDecision, RecoveryJob, audit
from .razorpay_client import RazorpayClient, RazorpayError, get_client
from .taxonomy import Action, JobStatus, PolicyDecision

# monkeypatchable: tests inject sleep and shrink these
BACKOFF_BASE_SECONDS = 0.5
MAX_HTTP_ATTEMPTS = 3
MIN_LINK_EXPIRY_HOURS = 1  # the API rejects an expire_by in the past; a pretend `now` must not produce one
UNSPECIFIED_CLASS = "UNSPECIFIED"  # a link job with no decision row to name its failure class

# nothing more may ever go out for a job in one of these states. EXECUTING is here so a second
# worker that races in after the atomic pending->executing claim is refused before any outbound call.
_TERMINAL = frozenset({JobStatus.SENT.value, JobStatus.STUBBED.value, JobStatus.SKIPPED_DUPLICATE.value,
                       JobStatus.HUMAN_QUEUE.value, JobStatus.NO_ACTION.value, JobStatus.EXECUTING.value,
                       JobStatus.CANCELLED.value, JobStatus.SHADOW.value})
LINK_ACTIONS = frozenset({Action.RECOVERY_LINK.value, Action.NUDGE_CHANGE_METHOD.value})
# the order was settled some other way: the reason on the job, and the outcome note that closes it
ORDER_PAID_PREFIX = "order already paid by"
ORDER_PAID_NOTE = "order paid elsewhere: no recovery needed"

# Why a link job failed, as the prefix of last_error ("<kind>: <detail>"). The pipeline reads it
# back to decide what may happen next: only a definite non-delivery earns an automatic follow-up.
LOCAL_GUARD = "local_guard"        # refused before any call (bad amount, unknown action)
FINAL_4XX = "final_4xx"            # Razorpay rejected the request; the same request would fail again
RATE_LIMITED = "rate_limited"      # 429 on every attempt: nothing was created
SERVER_ERROR = "server_error"      # 5xx on the last attempt: the request may have been processed
TRANSPORT = "transport"            # no HTTP answer: the request may have landed
CLIENT_ERROR = "client_error"      # the client raised something else: unknown whether it sent
FAILURE_KINDS = (LOCAL_GUARD, FINAL_4XX, RATE_LIMITED, SERVER_ERROR, TRANSPORT, CLIENT_ERROR)
AMBIGUOUS_KINDS = frozenset({SERVER_ERROR, TRANSPORT, CLIENT_ERROR})  # reconcile before any follow-up
RETRYABLE_KINDS = frozenset({RATE_LIMITED})                             # follow-up is safe as it stands
# A delivery failure never reached Razorpay's application layer (throttled, or a lost send that a
# reference_id lookup showed created nothing): the recovery was never delivered, so it is a delivery
# retry, not a recovery attempt. server_error (Razorpay answered) counts as a recovery attempt.
DELIVERY_FAILURE_KINDS = frozenset({RATE_LIMITED, TRANSPORT})
# a job that reached Razorpay's application layer counts against the recovery attempt budget
_REACHED_KINDS = frozenset({SERVER_ERROR, FINAL_4XX, CLIENT_ERROR})
_DUPLICATE_REFERENCE = "duplicate_reference"                            # internal: the loop saw layer 3 fire


def idempotency_key(razorpay_payment_id: str, retry_seq: int) -> str:
    return hashlib.sha256(f"{razorpay_payment_id}:{int(retry_seq)}".encode("utf-8")).hexdigest()


def reference_id_for(key: str) -> str:
    # Razorpay caps reference_id at 40 chars; 160 bits of the hash is still collision-proof here
    return key[:40]


def next_retry_seq(session: Session, attempt: PaymentAttempt) -> int:
    """For a scheduler-driven follow-up (backoff x3 etc.): one past the highest seq already used."""
    current = session.execute(
        select(func.max(RecoveryJob.retry_seq)).where(RecoveryJob.attempt_id == attempt.id)
    ).scalar_one()
    return int(current or 0) + 1


def reached_razorpay(session: Session, attempt: PaymentAttempt) -> bool:
    """True once any recovery attempt for this payment actually reached Razorpay's application layer:
    a link was created, a token stub ran, or Razorpay answered with a server/final/client error. A
    throttled (429) or never-landed (transport, reconciled absent) send has not reached it."""
    jobs = session.execute(select(RecoveryJob).where(RecoveryJob.attempt_id == attempt.id)).scalars().all()
    for j in jobs:
        if j.status in (JobStatus.SENT.value, JobStatus.STUBBED.value):
            return True
        if j.status == JobStatus.FAILED.value and failure_kind(j) in _REACHED_KINDS:
            return True
    return False


def paid_order(session: Session, attempt: PaymentAttempt) -> tuple[datetime, str] | None:
    """(paid_at, payment_id) when the attempt's order is recorded as paid another way, on this row
    or on any other attempt row sharing the order_id (a stale payment.failed can arrive after the
    capture). None when the order is not known to be paid, or the attempt has no order id."""
    if getattr(attempt, "order_paid_at", None):
        return attempt.order_paid_at, attempt.paid_by_payment_id or "?"
    order_id = getattr(attempt, "order_id", None)
    if not order_id:
        return None
    row = session.execute(
        select(PaymentAttempt).where(PaymentAttempt.order_id == order_id, PaymentAttempt.order_paid_at.is_not(None))
        .order_by(PaymentAttempt.id)).scalars().first()
    if row is None:
        return None
    return row.order_paid_at, row.paid_by_payment_id or "?"


def mark_order_paid(session: Session, order_id: str, payment_id: str, paid_at: datetime | None = None,
                    *, now: datetime | None = None) -> list[PaymentAttempt]:
    """Record on every attempt row sharing `order_id` that the order was paid by `payment_id`.
    Rows already marked keep their first record (the earliest capture is the truth). Returns every
    attempt row for the order, marked or not, and commits."""
    now = now or utcnow()
    rows = session.execute(select(PaymentAttempt).where(PaymentAttempt.order_id == order_id)
                           .order_by(PaymentAttempt.id)).scalars().all()
    for row in rows:
        if row.order_paid_at is None:
            row.order_paid_at, row.paid_by_payment_id = paid_at or now, payment_id
    session.commit()
    return list(rows)


def order_paid_reason(payment_id: str) -> str:
    return f"{ORDER_PAID_PREFIX} {payment_id}"


def schedule_job(session: Session, attempt: PaymentAttempt, decision_row: RecoveryDecision, policy: PolicyDecision,
                 *, retry_seq: int = 1, now: datetime | None = None, offer: str | None = None) -> tuple[RecoveryJob, bool]:
    """Insert the job under its idempotency key and commit. Returns (job, created).

    `offer` (app/offers.py: "partial" / "rail_upi") is stored on a link job and shapes its payload;
    it never changes the schedule.

    retry_seq defaults to 1: an event delivery is always "the first recovery for this payment",
    however many times it is delivered. Only the scheduler passes a higher seq (next_retry_seq)
    for a follow-up, after calling policy.decide(..., retry_seq=seq) so policy.delay_seconds is
    already grown by the backoff for that attempt; it is used verbatim here. Never sends anything.

    Three more things decide the row before it is written. An order already paid another way is
    not scheduled at all: the row is no_action with the reason and gets its outcome here. A link
    job's send time is moved by the salary window (INSUFFICIENT_FUNDS) and the quiet hours
    (app/scheduling.py), with an audit row saying what moved and why. A link job for a contact
    that has already had its weekly quota of sends is parked human_queue with the reason.
    """
    now = now or utcnow()
    session.commit()  # the caller's attempt/decision rows survive a duplicate-key rollback below
    key = idempotency_key(attempt.razorpay_payment_id, retry_seq)
    action = policy.action.value
    last_error: str | None = None
    schedule_note: str | None = None
    shifted_from: datetime | None = None
    paid = None

    if action == Action.HUMAN_QUEUE.value:
        status, scheduled_at = JobStatus.HUMAN_QUEUE.value, now
    elif action == Action.NO_ACTION.value:
        status, scheduled_at = JobStatus.NO_ACTION.value, now
    elif retry_seq > policy.max_attempts:
        # stopping rule: the row still exists so the audit trail shows why nothing went out
        status, scheduled_at = JobStatus.NO_ACTION.value, now
    elif (paid := paid_order(session, attempt)) is not None:
        # the order was settled another way: nothing to recover, nothing to schedule
        status, scheduled_at, last_error = JobStatus.NO_ACTION.value, now, order_paid_reason(paid[1])
    else:
        status, scheduled_at = JobStatus.PENDING.value, now + timedelta(seconds=max(0, int(policy.delay_seconds)))
        if action in LINK_ACTIONS:
            failure_class = getattr(decision_row, "failure_class", None)
            adjusted, schedule_note = scheduling.adjust_send_time(scheduled_at, failure_class=failure_class)
            if adjusted != scheduled_at:
                shifted_from, scheduled_at = scheduled_at, adjusted
            allowed, cap_reason = scheduling.contact_cap(session, attempt, now)
            if not allowed:
                status, scheduled_at, last_error = JobStatus.HUMAN_QUEUE.value, now, cap_reason

    link_source = None
    if action in LINK_ACTIONS:
        from . import cadence  # local: cadence imports this module
        link_source = cadence.link_source_for(attempt)
        offer = offer or offers.default_offer(getattr(decision_row, "failure_class", None), action)
    job = RecoveryJob(attempt_id=attempt.id, decision_id=decision_row.id, retry_seq=retry_seq, action=action,
                      scheduled_at=scheduled_at, idempotency_key=key, status=status, last_error=last_error,
                      schedule_note=schedule_note, link_source=link_source,
                      offer=offer if action in LINK_ACTIONS else None)
    session.add(job)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        existing = session.execute(select(RecoveryJob).where(RecoveryJob.idempotency_key == key)).scalar_one()
        audit(session, attempt.id, "schedule", "skipped_duplicate: idempotency key already present",
              {"idempotency_key": key, "retry_seq": retry_seq, "existing_job_id": existing.id,
               "existing_status": existing.status})
        session.commit()
        return existing, False

    message = f"scheduled {action} (seq {retry_seq}) for {scheduled_at.isoformat()}"
    if paid is not None:
        message = f"no_action: {last_error}; nothing scheduled, nothing will be sent"
    elif status == JobStatus.HUMAN_QUEUE.value and last_error:
        message = f"human_queue: {last_error}; nothing will be sent"
    elif status == JobStatus.NO_ACTION.value and action != Action.NO_ACTION.value:
        message = f"stopping rule: seq {retry_seq} exceeds max_attempts {policy.max_attempts}; no_action"
    elif status != JobStatus.PENDING.value:
        message = f"{status}: nothing will be sent"
    audit(session, attempt.id, "schedule", message,
          {"job_id": job.id, "idempotency_key": key, "retry_seq": retry_seq, "action": action,
           "status": status, "delay_seconds": int((scheduled_at - now).total_seconds()),
           "schedule_note": schedule_note, "order_paid_by": paid[1] if paid else None, "link_source": link_source,
           "offer": job.offer})
    if shifted_from is not None:
        audit(session, attempt.id, "schedule",
              f"send time moved from {shifted_from.isoformat()} to {scheduled_at.isoformat()} ({schedule_note})",
              {"job_id": job.id, "from": shifted_from.isoformat(), "to": scheduled_at.isoformat(),
               "note": schedule_note, "failure_class": getattr(decision_row, "failure_class", None)})
    session.commit()
    if paid is not None:
        record_outcome(session, attempt, job, False, 0, ORDER_PAID_NOTE, now=now)
    return job, True


def _is_duplicate_reference(exc: RazorpayError) -> bool:
    # the real API's exact wording is unverified offline, so match the idea, not a sentence:
    # a 400 that talks about the reference and says it already exists / is not unique
    text = exc.description.lower().replace("_", " ").replace("-", " ")
    return exc.status == 400 and "reference" in text and any(
        w in text for w in ("already", "exist", "unique", "duplicate"))


def _offer_rejected(error: str | None) -> bool:
    """The final-4xx text names an offer field (the create loop keeps only the message, not the exception)."""
    return offers.is_offer_rejection(400, error or "") and "HTTP 400" in (error or "")


def _kind_of(exc: RazorpayError) -> str:
    if exc.status == 429:
        return RATE_LIMITED
    if exc.status >= 500:
        return SERVER_ERROR
    if exc.status == 0:
        return TRANSPORT
    return FINAL_4XX


def failure_kind(job: RecoveryJob) -> str | None:
    """The kind prefix execute_job wrote into last_error, or None when there is none to read."""
    head = (job.last_error or "").split(":", 1)[0].strip()
    return head if head in FAILURE_KINDS else None


def build_payment_link_payload(attempt: PaymentAttempt, job: RecoveryJob, failure_class: str,
                               *, now: datetime | None = None) -> dict:
    now = now or utcnow()
    customer = {k: v for k, v in (("name", attempt.customer_name), ("contact", attempt.customer_contact),
                                  ("email", attempt.customer_email)) if v}
    # `now` may be a replayed clock (run-due --now); the link must still expire in the real future
    hours = max(MIN_LINK_EXPIRY_HOURS, int(config.RECOVERY_LINK_EXPIRY_HOURS))
    expire_at = max(now, utcnow()) + timedelta(hours=hours)
    merchant_notify = getattr(merchants.for_attempt(attempt), "notify_customer", None)
    notify_on = bool(config.RAZORPAY_NOTIFY_CUSTOMER) and merchant_notify is not False  # a file can only turn it off
    notify = {"sms": bool(attempt.customer_contact) and notify_on,
              "email": bool(attempt.customer_email) and notify_on}
    payload = {
        "amount": int(attempt.amount_paise),
        "currency": attempt.currency or "INR",
        "description": f"Retry payment for order {attempt.order_id}",
        "reference_id": reference_id_for(job.idempotency_key),
        "customer": customer,
        "notify": notify,
        "reminder_enable": notify["sms"] or notify["email"],  # a reminder needs a channel to go out on
        "expire_by": to_unix(expire_at),
        "notes": {
            "payment_id": str(attempt.razorpay_payment_id),
            "order_id": str(attempt.order_id),
            "failure_class": str(failure_class),
            "retry_seq": str(job.retry_seq),
            "agent": "payment-recovery-agent",
        },
    }
    if config.CALLBACK_URL:
        payload["callback_url"] = config.CALLBACK_URL
        payload["callback_method"] = "get"
    # the offer fields (app/offers.py): partial payment on a follow-up / repeat failer, rail preference
    # on a dead card. Assumed API fields; a live 400 naming them falls back to the plain payload.
    return offers.apply_extras(payload, offers.link_payload_extras(attempt, job, failure_class))


def _finish_shadow(session: Session, job: RecoveryJob, what: str, payload: dict | None, data: dict,
                   now: datetime) -> RecoveryJob:
    """PRA_MODE=shadow: the job ends `shadow` with the exact request it would have made in its audit
    row (the merchant's own data, PII included), executed_at set, no link id, no outcome. Nothing
    is called. A shadow job stays visibly "would have"; poll never asks about it."""
    job.last_error = None
    body = f" with {json.dumps(payload, sort_keys=True, default=str)}" if payload is not None else ""
    return _finish(session, job, JobStatus.SHADOW.value, f"shadow mode: would {what}{body}",
                   {**data, "mode": offers.MODE_SHADOW, "payload": payload}, now)


def _reload(session: Session, job: RecoveryJob) -> RecoveryJob:
    if job.id is None:
        raise ValueError("execute_job needs a persisted job (schedule_job commits one)")
    if job in session:
        session.refresh(job)
        return job
    fresh = session.get(RecoveryJob, job.id)
    if fresh is None:
        raise ValueError(f"recovery job {job.id} no longer exists")
    return fresh


def _finish(session: Session, job: RecoveryJob, status: str, message: str, data: dict | None,
            now: datetime) -> RecoveryJob:
    job.status = status
    job.executed_at = now
    audit(session, job.attempt_id, "execute", message, data)
    session.commit()
    return job


def _fail(session: Session, job: RecoveryJob, kind: str, detail: str, message: str, data: dict,
          now: datetime) -> RecoveryJob:
    job.last_error = f"{kind}: {detail}"
    return _finish(session, job, JobStatus.FAILED.value, message, {**data, "failure_kind": kind}, now)


def _client_name(client) -> str:
    return getattr(client, "name", type(client).__name__)


def _lookup_by_reference(client, reference_id: str) -> tuple[dict | None, str | None]:
    """(link, error): the link Razorpay holds under reference_id, or None with error=None when
    none exists, or None with the reason when the lookup itself could not answer."""
    lookup = getattr(client, "list_payment_links", None)
    if lookup is None:
        return None, f"{_client_name(client)} cannot list links by reference_id"
    try:
        links = lookup(reference_id=reference_id)
    except RazorpayError as exc:
        return None, str(exc)
    except Exception as exc:  # a client bug is still "could not answer"
        return None, f"{type(exc).__name__}: {exc}"
    for link in links or []:
        if isinstance(link, dict) and link.get("id") and link.get("reference_id") == reference_id:
            return link, None
    return None, None


def _create_link_with_backoff(session: Session, job: RecoveryJob, client, payload: dict,
                              sleep: Callable[[float], None]) -> tuple[dict | None, str | None, str | None]:
    """Up to MAX_HTTP_ATTEMPTS create calls, backing off on 429 / 5xx / transport failures.
    Returns (link, kind, error): the link on success; otherwise kind is one of FAILURE_KINDS
    (an ambiguous kind wins over a later rate limit, because it might have landed) or
    _DUPLICATE_REFERENCE when Razorpay said the reference_id is already taken."""
    kinds: list[str] = []
    last_error: str | None = None
    for n in range(MAX_HTTP_ATTEMPTS):
        # persist the counter before the call so a crash mid-request leaves evidence a request went out
        job.attempts_made = (job.attempts_made or 0) + 1
        session.commit()
        try:
            return client.create_payment_link(payload), None, None
        except RazorpayError as exc:
            last_error = str(exc)
            if _is_duplicate_reference(exc):
                return None, _DUPLICATE_REFERENCE, last_error
            kind = _kind_of(exc)
            kinds.append(kind)
            if kind == FINAL_4XX:
                break
            if n < MAX_HTTP_ATTEMPTS - 1:
                delay = BACKOFF_BASE_SECONDS * (2 ** n)
                audit(session, job.attempt_id, "execute",
                      f"retryable error on attempt {n + 1}/{MAX_HTTP_ATTEMPTS}: {exc}; backing off {delay}s",
                      {"job_id": job.id, "http_status": exc.status, "code": exc.code, "sleep_seconds": delay,
                       "client": _client_name(client)})
                session.commit()
                sleep(delay)
        except Exception as exc:  # a client bug must not take the pipeline down with it
            last_error = f"{type(exc).__name__}: {exc}"
            kinds.append(CLIENT_ERROR)
            break
    kind = next((k for k in reversed(kinds) if k in AMBIGUOUS_KINDS), kinds[-1] if kinds else CLIENT_ERROR)
    return None, kind, last_error or "no response"


def _finish_duplicate_reference(session: Session, job: RecoveryJob, client, reference_id: str, said: str,
                                now: datetime) -> RecoveryJob:
    """Layer 3 fired: an earlier attempt created this link. Recover its id so poll/mark-paid can
    close the loop; if that is impossible, park the job for a person rather than guess."""
    link, error = _lookup_by_reference(client, reference_id)
    data = {"job_id": job.id, "reference_id": reference_id, "attempts_made": job.attempts_made,
            "client": _client_name(client)}
    if link is not None:
        job.razorpay_link_id, job.razorpay_link_url = link.get("id"), link.get("short_url")
        job.last_error = (f"reference_id already exists at Razorpay; link {job.razorpay_link_id} recovered by "
                          f"lookup, nothing re-created ({said})")
        return _finish(session, job, JobStatus.SENT.value,
                       f"sent (already existed): Razorpay rejected the duplicate reference_id, so an earlier "
                       f"attempt created this link; recovered {job.razorpay_link_id} ({job.razorpay_link_url}), "
                       f"nothing new sent", {**data, "link_id": job.razorpay_link_id, "short_url": job.razorpay_link_url,
                                            "link_status": link.get("status")}, now)
    why = error or "no link is listed under that reference_id"
    job.last_error = (f"reference_id {reference_id} already exists at Razorpay but its link could not be fetched "
                      f"({why}); a person reconciles before anything more goes out")
    return _finish(session, job, JobStatus.HUMAN_QUEUE.value,
                   f"human_queue: Razorpay says reference_id {reference_id} already exists, lookup could not "
                   f"recover the link ({why}); nothing sent, a person reconciles", {**data, "lookup_error": error}, now)


def _claim_for_execution(session: Session, job: RecoveryJob, now: datetime) -> bool:
    """Atomic compare-and-set: pending -> executing. Returns True to the one caller that won it.
    A second caller's UPDATE matches no row (status is already 'executing'), so it returns False and
    makes no outbound call. Committed so the claim is visible to any other session immediately."""
    won = session.execute(
        update(RecoveryJob).where(RecoveryJob.id == job.id, RecoveryJob.status == JobStatus.PENDING.value)
        .values(status=JobStatus.EXECUTING.value, executed_at=now)).rowcount
    session.commit()
    if won:
        session.refresh(job)
    return bool(won)


def _earlier_landed(session: Session, job: RecoveryJob, client, now: datetime) -> RecoveryJob | None:
    """Before a follow-up (retry_seq > 1) creates a link, check whether an earlier ambiguous FAILED
    attempt for the same payment has since landed at Razorpay. If a link now exists under that
    attempt's reference_id, mark that attempt sent (recover its id/url) and return it, so the caller
    creates nothing. This closes the window where an attempt's request completes after its own
    reconcile answered 'absent' and a second link was about to be minted."""
    prevs = session.execute(
        select(RecoveryJob).where(RecoveryJob.attempt_id == job.attempt_id, RecoveryJob.id != job.id,
                                  RecoveryJob.status == JobStatus.FAILED.value,
                                  RecoveryJob.retry_seq < job.retry_seq)
        .order_by(RecoveryJob.retry_seq)).scalars().all()
    for prev in prevs:
        if failure_kind(prev) not in AMBIGUOUS_KINDS:
            continue
        reference_id = reference_id_for(prev.idempotency_key)
        link, _error = _lookup_by_reference(client, reference_id)
        if link is not None and link.get("id"):
            prev.razorpay_link_id, prev.razorpay_link_url = link.get("id"), link.get("short_url")
            prev.last_error = (f"reconciled before seq {job.retry_seq}: this earlier ambiguous attempt had "
                               f"landed at Razorpay after all; link {prev.razorpay_link_id} recovered, no "
                               f"second link created")
            prev.status, prev.executed_at = JobStatus.SENT.value, now
            audit(session, prev.attempt_id, "reconcile",
                  f"link {prev.razorpay_link_id} exists under reference_id {reference_id} from earlier attempt "
                  f"seq {prev.retry_seq}: it had landed; marked sent, follow-up seq {job.retry_seq} will not "
                  f"create a second ({prev.razorpay_link_url})",
                  {"job_id": prev.id, "reference_id": reference_id, "link_id": prev.razorpay_link_id,
                   "short_url": prev.razorpay_link_url, "followup_job_id": job.id, "client": _client_name(client)})
            session.commit()
            return prev
    return None


def _finish_order_paid(session: Session, attempt: PaymentAttempt, job: RecoveryJob, payment_id: str,
                       now: datetime, *, how: str) -> RecoveryJob:
    """The order was settled another way: close the job no_action with the reason, write the
    outcome, send nothing. Zero Razorpay calls happen after this returns."""
    job.last_error = order_paid_reason(payment_id)
    job = _finish(session, job, JobStatus.NO_ACTION.value,
                  f"no_action: {job.last_error} ({how}); nothing sent, nothing charged",
                  {"job_id": job.id, "order_id": attempt.order_id, "paid_by_payment_id": payment_id}, now)
    record_outcome(session, attempt, job, False, 0, ORDER_PAID_NOTE, now=now)
    return job


def _captured_payment_on_order(client, order_id: str) -> tuple[str | None, str | None]:
    """(payment_id, error): the id of a captured payment on the order, or None with error=None when
    there is none, or None with the reason when the lookup could not answer (or the client cannot)."""
    lookup = getattr(client, "list_order_payments", None)
    if lookup is None:
        return None, f"{_client_name(client)} cannot list payments by order"
    try:
        payments = lookup(order_id)
    except RazorpayError as exc:
        return None, str(exc)
    except Exception as exc:  # a client bug is still "could not answer"
        return None, f"{type(exc).__name__}: {exc}"
    for p in payments or []:
        if isinstance(p, dict) and str(p.get("status") or "").lower() == "captured":
            return str(p.get("id") or "?"), None
    return None, None


def recover_stale_execution(session: Session, job: RecoveryJob, *, client: RazorpayClient | None = None,
                            now: datetime | None = None) -> str:
    """A job left in 'executing' by a crash after it claimed the row. The outbound call may have
    landed, so reconcile by reference_id before anything else, exactly like a failed ambiguous job:
      "sent"    a link exists: mark the job sent with it, nothing re-created
      "pending" no link: reset to pending so it re-executes cleanly under a fresh claim
      "unknown" the lookup could not answer: park for a person
    Audits under stage "reconcile". Never raises for Razorpay failures."""
    now = now or utcnow()
    job = _reload(session, job)
    client = client or get_client()
    reference_id = reference_id_for(job.idempotency_key)
    link, error = _lookup_by_reference(client, reference_id)
    data = {"job_id": job.id, "reference_id": reference_id, "client": _client_name(client)}
    if link is not None and link.get("id"):
        job.razorpay_link_id, job.razorpay_link_url = link.get("id"), link.get("short_url")
        job.last_error = f"reconciled: a crashed execution had landed; link {job.razorpay_link_id} recovered by lookup"
        job.status, job.executed_at = JobStatus.SENT.value, now
        audit(session, job.attempt_id, "reconcile",
              f"stale 'executing' job: link {job.razorpay_link_id} exists under reference_id {reference_id}, the "
              f"crashed request had landed; marked sent ({job.razorpay_link_url})",
              {**data, "link_id": job.razorpay_link_id, "short_url": job.razorpay_link_url})
        session.commit()
        return "sent"
    if error is None:
        job.status = JobStatus.PENDING.value
        job.last_error = "reconciled: a crashed execution left no link under this reference_id; reset to pending to re-execute"
        audit(session, job.attempt_id, "reconcile",
              f"stale 'executing' job: no link under reference_id {reference_id}, the crashed request did not "
              f"land; reset to pending for a clean re-execution", data)
        session.commit()
        return "pending"
    job.status = JobStatus.HUMAN_QUEUE.value
    job.last_error = (f"a crashed execution may have reached Razorpay and reference_id {reference_id} could not be "
                      f"looked up ({error}); a person reconciles before anything more goes out")
    audit(session, job.attempt_id, "reconcile",
          f"stale 'executing' job: could not tell whether reference_id {reference_id} has a link ({error}); "
          f"parked for a person", {**data, "lookup_error": error})
    session.commit()
    return "unknown"


def execute_job(session: Session, job: RecoveryJob, *, client: RazorpayClient | None = None,
                now: datetime | None = None, sleep: Callable[[float], None] = time.sleep) -> RecoveryJob:
    """Perform the job's action. Re-reads the row first and refuses anything already executed,
    before any outbound call. Never raises for Razorpay failures; a failed link job carries
    "<kind>: <detail>" in last_error (see FAILURE_KINDS) so the pipeline knows what is safe next."""
    now = now or utcnow()
    if job.action == Action.REMINDER.value:  # before the reload: plain job lookups hide reminders (app/models.py)
        from . import cadence  # local: cadence imports this module
        return cadence.execute_reminder(session, job, client=client, now=now)
    job = _reload(session, job)

    if job.status in _TERMINAL:
        audit(session, job.attempt_id, "execute", f"already executed: status {job.status}; nothing sent",
              {"job_id": job.id, "idempotency_key": job.idempotency_key, "status": job.status})
        session.commit()
        return job

    if job.action == Action.HUMAN_QUEUE.value:
        return _finish(session, job, JobStatus.HUMAN_QUEUE.value, "human_queue: parked for a person; nothing sent",
                       {"job_id": job.id}, now)
    if job.action == Action.NO_ACTION.value:
        return _finish(session, job, JobStatus.NO_ACTION.value, "no_action: policy said stop; nothing sent",
                       {"job_id": job.id}, now)

    attempt = job.attempt if job.attempt is not None else session.get(PaymentAttempt, job.attempt_id)
    # Guard 5, before ANY outbound call (a token retry included: charging a token for an order the
    # customer has since paid is the double charge this agent exists to avoid).
    paid = paid_order(session, attempt)
    if paid is not None:
        return _finish_order_paid(session, attempt, job, paid[1], now, how="recorded at ingest")

    if job.action == Action.TOKEN_RETRY.value:
        job.last_error = None
        return _finish(session, job, JobStatus.STUBBED.value,
                       "STUB: would charge saved token via Orders+Payments API (recurring); "
                       "no real tokens in test mode", {"job_id": job.id, "idempotency_key": job.idempotency_key}, now)
    if job.action not in LINK_ACTIONS:
        detail = f"unknown action {job.action!r}"
        return _fail(session, job, LOCAL_GUARD, detail, f"failed: {detail}; nothing sent", {"job_id": job.id}, now)

    decision = session.get(RecoveryDecision, job.decision_id) if job.decision_id else None
    failure_class = decision.failure_class if decision else UNSPECIFIED_CLASS
    amount = attempt.amount_paise
    if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
        detail = f"refusing to create a link for amount_paise={amount!r}"
        return _fail(session, job, LOCAL_GUARD, detail, f"failed: {detail}", {"job_id": job.id}, now)

    client = client or get_client()

    # Atomic claim before any outbound call: UPDATE ... WHERE status='pending' is a compare-and-set,
    # so two workers on one pending job cannot both POST /payment_links (the second sees 'executing',
    # which is terminal above, and refuses). run-due reclaims a job left 'executing' by a crash.
    if not _claim_for_execution(session, job, now):
        job = _reload(session, job)
        audit(session, job.attempt_id, "execute",
              f"already claimed by another worker: status {job.status}; nothing sent", {"job_id": job.id})
        session.commit()
        return job

    # PRA_MODE=shadow: everything above (guards, claim, idempotency) ran for real; from here nothing
    # leaves the process. The order check GET is skipped too: shadow means zero outbound calls.
    if offers.shadow_mode():
        if job.link_source == "subscription_url" and getattr(attempt, "subscription_url", None):
            return _finish_shadow(session, job, f"record the subscription re-authorisation URL {attempt.subscription_url} "
                                  f"as the link (no Payment Link created)", None,
                                  {"job_id": job.id, "link_source": job.link_source, "subscription_id": attempt.subscription_id}, now)
        payload = build_payment_link_payload(attempt, job, failure_class, now=now)
        return _finish_shadow(session, job, "POST /payment_links", payload,
                              {"job_id": job.id, "reference_id": payload["reference_id"], "offer": job.offer,
                               "client": _client_name(client)}, now)

    # A halted subscription: the recovery link is the subscription's own hosted re-authorisation page
    # from the webhook payload. Nothing is created at Razorpay, so there is no link id to poll; the
    # loop closes when the subscription re-activates (not in scope) or the order is paid another way.
    if job.link_source == "subscription_url" and getattr(attempt, "subscription_url", None):
        job.razorpay_link_url = attempt.subscription_url
        job.last_error = None
        return _finish(session, job, JobStatus.SENT.value,
                       f"sent: subscription re-authorisation URL from the payload; no Payment Link created "
                       f"({job.razorpay_link_url}, subscription {attempt.subscription_id})",
                       {"job_id": job.id, "link_source": job.link_source, "subscription_id": attempt.subscription_id,
                        "short_url": job.razorpay_link_url, "attempts_made": job.attempts_made,
                        "client": _client_name(client)}, now)

    # Optional belt and braces (CHECK_ORDER_BEFORE_SEND): ask Razorpay whether the order already has
    # a captured payment. The webhook is the primary record; a failed check is audited, not fatal.
    if config.check_order_before_send() and attempt.order_id:
        pid, error = _captured_payment_on_order(client, attempt.order_id)
        if pid is not None:
            mark_order_paid(session, attempt.order_id, pid, now, now=now)
            audit(session, job.attempt_id, "execute",
                  f"order check: GET /orders/{attempt.order_id}/payments shows a captured payment {pid}; "
                  f"recorded on the attempt, nothing will be sent",
                  {"job_id": job.id, "order_id": attempt.order_id, "payment_id": pid, "client": _client_name(client)})
            return _finish_order_paid(session, attempt, job, pid, now, how="found by the order check before sending")
        if error is not None:
            audit(session, job.attempt_id, "execute",
                  f"order check: GET /orders/{attempt.order_id}/payments could not answer ({error}); proceeding, "
                  f"the payment.captured webhook is the primary record",
                  {"job_id": job.id, "order_id": attempt.order_id, "error": error, "client": _client_name(client)})
        else:
            audit(session, job.attempt_id, "execute",
                  f"order check: no captured payment on order {attempt.order_id}; sending",
                  {"job_id": job.id, "order_id": attempt.order_id, "client": _client_name(client)})
        session.commit()

    # A follow-up must not create a second live link if an earlier ambiguous attempt has since
    # landed at Razorpay (its request may have completed after that attempt's reconcile answered).
    if job.retry_seq > 1:
        landed = _earlier_landed(session, job, client, now)
        if landed is not None:
            job.last_error = (f"an earlier attempt (seq {landed.retry_seq}) already created link "
                              f"{landed.razorpay_link_id}; not creating a second live link for this payment")
            return _finish(session, job, JobStatus.NO_ACTION.value,
                           f"no_action: an earlier attempt (seq {landed.retry_seq}) had landed at Razorpay "
                           f"(link {landed.razorpay_link_id} recovered by lookup); a second link is a "
                           f"double-payment risk, nothing created",
                           {"job_id": job.id, "recovered_job_id": landed.id,
                            "link_id": landed.razorpay_link_id, "client": _client_name(client)}, now)

    payload = build_payment_link_payload(attempt, job, failure_class, now=now)
    link, kind, error = _create_link_with_backoff(session, job, client, payload, sleep)

    plain = offers.strip_offer_fields(payload)
    if link is None and kind == FINAL_4XX and plain != payload and _offer_rejected(error):
        # The API refused the offer field (accept_partial / options), not the link: send the plain
        # link under the same reference_id and say so. The offer is dropped from the job.
        audit(session, job.attempt_id, "execute",
              f"offer fields refused by Razorpay ({error}); falling back to a plain link without them "
              f"(offer {job.offer or 'rail'} dropped)",
              {"job_id": job.id, "offer": job.offer, "error": error, "dropped": sorted(set(payload) - set(plain)),
               "client": _client_name(client)})
        job.offer = None
        session.commit()
        payload = plain
        link, kind, error = _create_link_with_backoff(session, job, client, payload, sleep)

    if kind == _DUPLICATE_REFERENCE:
        return _finish_duplicate_reference(session, job, client, payload["reference_id"], error or "", now)
    if link is None:
        return _fail(session, job, kind or CLIENT_ERROR, error or "no response",
                     f"failed after {job.attempts_made} attempt(s): {kind}: {error}",
                     {"job_id": job.id, "attempts_made": job.attempts_made, "client": _client_name(client)}, now)
    if not (isinstance(link, dict) and link.get("id")):
        # a 2xx with no link id (an empty 200/204 from a proxy, an error-shaped body): treat as
        # ambiguous, not sent, so finish_job reconciles by reference_id rather than marking a job
        # sent with no link (which poll would never close).
        return _fail(session, job, CLIENT_ERROR, f"2xx response without a link id: {link!r}",
                     f"failed after {job.attempts_made} attempt(s): a create call returned no link id",
                     {"job_id": job.id, "attempts_made": job.attempts_made, "client": _client_name(client)}, now)

    job.razorpay_link_id = link.get("id")
    job.razorpay_link_url = link.get("short_url")
    job.last_error = None
    return _finish(session, job, JobStatus.SENT.value,
                   f"sent: payment link {job.razorpay_link_id} created ({job.razorpay_link_url})",
                   {"job_id": job.id, "link_id": job.razorpay_link_id, "short_url": job.razorpay_link_url,
                    "reference_id": payload["reference_id"], "attempts_made": job.attempts_made,
                    "client": _client_name(client), "link_status": link.get("status")}, now)


def reconcile_failed_job(session: Session, job: RecoveryJob, *, client: RazorpayClient | None = None,
                         now: datetime | None = None) -> str:
    """After a FAILED link job whose last error was ambiguous (the request may have landed), ask
    Razorpay whether a link exists under this job's reference_id before anything else is minted.
      "sent"    a link exists: the job becomes sent with that id/url, nothing re-created
      "absent"  no link: a follow-up under the next retry_seq is safe
      "unknown" the lookup could not answer: nothing more may go out automatically
    Audits under stage "reconcile". Never raises for Razorpay failures."""
    now = now or utcnow()
    job = _reload(session, job)
    client = client or get_client()
    reference_id = reference_id_for(job.idempotency_key)
    link, error = _lookup_by_reference(client, reference_id)
    data = {"job_id": job.id, "reference_id": reference_id, "failure_kind": failure_kind(job),
            "client": _client_name(client)}
    if link is not None:
        job.razorpay_link_id, job.razorpay_link_url = link.get("id"), link.get("short_url")
        job.last_error = f"reconciled: the failed request had landed; link {job.razorpay_link_id} recovered by lookup"
        job.status, job.executed_at = JobStatus.SENT.value, now
        audit(session, job.attempt_id, "reconcile",
              f"link {job.razorpay_link_id} exists at Razorpay under reference_id {reference_id}: the failed "
              f"request had landed; job marked sent ({job.razorpay_link_url}), no follow-up",
              {**data, "link_id": job.razorpay_link_id, "short_url": job.razorpay_link_url,
               "link_status": link.get("status")})
        session.commit()
        return "sent"
    if error is None:
        audit(session, job.attempt_id, "reconcile",
              f"no link at Razorpay under reference_id {reference_id}: the failed request did not land; "
              f"a follow-up under the next retry_seq is safe", data)
        session.commit()
        return "absent"
    audit(session, job.attempt_id, "reconcile",
          f"could not tell whether reference_id {reference_id} has a link ({error}); nothing more goes out "
          f"automatically, a person reconciles", {**data, "lookup_error": error})
    session.commit()
    return "unknown"


def record_outcome(session: Session, attempt: PaymentAttempt, job: RecoveryJob | None, recovered: bool,
                   amount_paise: int, note: str, now: datetime | None = None) -> Outcome:
    """Close the loop for one attempt (recovered / not / human-queued) and commit."""
    now = now or utcnow()
    row = Outcome(attempt_id=attempt.id, job_id=job.id if job is not None else None, recovered=bool(recovered),
                  recovered_at=now if recovered else None,
                  amount_recovered_paise=int(amount_paise) if recovered else 0, note=note)
    session.add(row)
    audit(session, attempt.id, "outcome", f"{'recovered' if recovered else 'not recovered'}: {note}",
          {"job_id": row.job_id, "amount_recovered_paise": row.amount_recovered_paise})
    session.commit()
    return row
