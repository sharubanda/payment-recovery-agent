"""One failed-payment event through the five stages, in this order and no other:

  classify (rules) -> llm (only if UNKNOWN) -> policy -> persist decision
  -> schedule job (idempotency key, UNIQUE, committed) -> execute (outbound) -> nudge (drafted)

The order is the safety argument. Nothing leaves this process until the job row with its
idempotency key is durably committed, so a database outage means zero links created and
an event that is NOT acknowledged (PipelineDBError): the source redelivers it, the key
collides, and the second delivery is skipped_duplicate. If the database dies after a link
went out, the committed pending job stays pending; a redelivery is skipped_duplicate and
does not touch it, and run-due re-executes it once it is due, where Razorpay's reference_id
uniqueness answers "already exists" and the link is recovered by lookup instead of re-created.

A follow-up (the next retry_seq under a new key) is minted in exactly two situations. A failed
outbound call that provably did not reach Razorpay: a rate limit, or an ambiguous failure
(5xx, transport, client crash) that a reference_id lookup has shown created no link. And a
sent link that EXPIRED unpaid (seen by poll, or by a payment_link.expired webhook): the
customer never used it, so the class's next attempt is scheduled from the expiry time, with
the salary window and quiet hours applied, until max_attempts closes the payment no_action.
A lookup that finds the link marks the job sent; one that cannot answer parks the payment for
a person. Same-request failures (a final 4xx, a local guard) are parked too: repeating them is
noise, not recovery. A sent-and-still-open link is never re-issued.

The order being paid another way ends everything: a pending job becomes no_action, a live
link is cancelled at Razorpay (app/ingest.py, record_order_paid), and execute_job refuses to
send for a paid order however the job got there.

The customer nudge is drafted and stored, never sent: there is no SMS/email provider in
scope, and the audit row says so.
"""
import time
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from typing import Callable, Iterator

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from . import cadence, merchants, offers
from . import classify as classify_mod
from . import executor, llm
from . import policy as policy_mod
from .clock import utcnow
from .executor import LINK_ACTIONS
from .llm import LLMClient
from .models import Outcome, PaymentAttempt, RecoveryDecision, RecoveryJob, audit
from .policy import human_delay
from .razorpay_client import RazorpayClient, RazorpayError, get_client
from .taxonomy import Action, Classification, FailureClass, JobStatus, Nudge, PolicyDecision, coerce

__all__ = ["LINK_ACTIONS", "NUDGE_NOT_SENT", "PipelineDBError", "db_guarded", "process_attempt", "process_all",
           "finish_job", "attach_nudge", "schedule_followup", "schedule_expiry_followup", "unscheduled_attempts",
           "open_link_jobs", "poll_outcomes", "reachable", "reminders_for", "shadow_summary"]
reminders_for = cadence.reminders_for  # for `show` and the operator view (owned elsewhere)

# job statuses that are final at schedule time: the outcome is known before anything could go out
_PARKED_NOTES = {
    JobStatus.HUMAN_QUEUE.value: "human_queue: parked for a person; nothing sent",
    JobStatus.NO_ACTION.value: "no_action: policy said stop; nothing sent",
}
NUDGE_NOT_SENT = "NOT sent: no SMS/email provider in scope"
PARTIAL_NOTE = "partially paid"
STUB_NOTE = "token_retry stubbed: no real tokens in test mode; nothing charged, nothing recovered"
# a throttled (429) or undelivered send retries after a short API backoff, not the class recovery delay
DELIVERY_RETRY_BACKOFF_SECONDS = 300


class PipelineDBError(RuntimeError):
    """The database refused a write before the event was acknowledged. Redeliver the event.

    link_may_exist is True when the failure came AFTER a payment link was already committed (the
    nudge/outcome write is what failed): the redelivery is skipped_duplicate and no second link goes
    out, so the operator message must not claim "no link was created"."""

    def __init__(self, message: str, *, link_may_exist: bool = False):
        super().__init__(message)
        self.link_may_exist = link_may_exist


def _describe_db_error(exc: DBAPIError) -> str:
    orig = getattr(exc, "orig", None)
    return f"{type(exc).__name__}: {str(orig or exc).splitlines()[0][:200]}"


@contextmanager
def db_guarded(session: Session, progress: dict | None = None) -> Iterator[None]:
    """Turn any DB failure into PipelineDBError after a best-effort rollback. Razorpay and LLM
    failures never reach here: the executor and llm modules degrade instead of raising. `progress`
    (when passed) lets the caller record that a link was already committed, so the raised error
    can say a link may exist rather than that none was created."""
    try:
        yield
    except DBAPIError as exc:
        try:
            session.rollback()
        except Exception:  # the connection may be gone; the event is unacknowledged either way
            pass
        raise PipelineDBError(f"event not acknowledged: {_describe_db_error(exc)}",
                              link_may_exist=bool(progress and progress.get("link_created"))) from exc


def reachable(attempt: PaymentAttempt) -> bool:
    """A link needs somewhere to go: a contact for SMS or an email address."""
    return bool(getattr(attempt, "customer_contact", None) or getattr(attempt, "customer_email", None))


# ---- the five stages ----------------------------------------------------------------------

def process_attempt(session: Session, attempt: PaymentAttempt, *, execute_now: bool = False,
                    now: datetime | None = None, rz_client: RazorpayClient | None = None,
                    llm_client: LLMClient | None = None, sleep: Callable[[float], None] = time.sleep) -> dict:
    """Run one event through classify -> policy -> decision -> schedule -> (execute -> nudge).

    execute_now runs a pending job immediately even if its delay has not elapsed (the demo
    cannot wait 48 hours); the audit trail records that it ran ahead of schedule. A job whose
    delay is 0 always runs now. A re-delivered event returns duplicate=True and executes nothing.
    Raises PipelineDBError (after rollback) if the database refuses a write; never raises for
    Razorpay or LLM failures.
    """
    now = now or utcnow()
    progress: dict = {}
    with db_guarded(session, progress):
        return _process(session, attempt, execute_now=execute_now, now=now, rz_client=rz_client,
                        llm_client=llm_client, sleep=sleep, progress=progress)


def _process(session: Session, attempt: PaymentAttempt, *, execute_now: bool, now: datetime, rz_client, llm_client,
             sleep, progress: dict | None = None) -> dict:
    progress = progress if progress is not None else {}
    if attempt.id is None:  # a raw event handed straight in (chaos harness): ingest is part of the ack
        existing = session.execute(
            select(PaymentAttempt).where(PaymentAttempt.razorpay_payment_id == attempt.razorpay_payment_id)
        ).scalars().first()
        if existing is not None:
            # a raw redelivery of an already-ingested payment id: continue with the stored row so the
            # idempotency key collides (skipped_duplicate), not the UNIQUE constraint on the insert.
            attempt = existing
        else:
            session.add(attempt)
            session.flush()
            audit(session, attempt.id, "ingest", "failed payment event received",
                  {"razorpay_payment_id": attempt.razorpay_payment_id, "method": attempt.method,
                   "amount_paise": attempt.amount_paise})

    cls = classify_mod.classify(attempt)
    audit(session, attempt.id, "classify", f"{cls.failure_class.value} ({cls.source}): {cls.reason}",
          {"failure_class": cls.failure_class.value, "source": cls.source, "confidence": cls.confidence,
           "fallback_taken": cls.fallback_taken, "error_code": attempt.error_code, "error_reason": attempt.error_reason})

    if cls.failure_class is FailureClass.UNKNOWN:
        cls = llm.classify_unmapped(attempt, client=llm_client)
        audit(session, attempt.id, "llm", _llm_message(cls),
              {"failure_class": cls.failure_class.value, "source": cls.source, "confidence": cls.confidence,
               "llm_model": cls.llm_model, "llm_latency_ms": cls.llm_latency_ms, "fallback_taken": cls.fallback_taken,
               "reason": cls.reason})

    pol = policy_mod.decide(cls.failure_class, has_token=bool(attempt.has_token), retry_seq=1, classification=cls,
                            reachable=reachable(attempt), overrides=merchants.for_attempt(attempt))
    audit(session, attempt.id, "policy", _policy_message(pol, retry_seq=1),
          {"action": pol.action.value, "delay_seconds": pol.delay_seconds, "max_attempts": pol.max_attempts,
           "backoff_multiplier": pol.backoff_multiplier, "nudge": pol.nudge, "has_token": bool(attempt.has_token),
           "reachable": reachable(attempt), "classification_source": cls.source, "confidence": cls.confidence})
    override = cadence.subscription_override(attempt, cls.failure_class, pol)
    if override is not None:  # a subscription event: pending -> no new link, halted -> its own re-authorisation URL
        audit(session, attempt.id, "policy", f"subscription override: {_policy_message(override, retry_seq=1)}",
              {"action": override.action.value, "delay_seconds": override.delay_seconds, "table_action": pol.action.value,
               "subscription_id": attempt.subscription_id, "subscription_status": attempt.subscription_status})
        pol = override
    # repeat-failer memory (app/offers.py): a post-policy override from this customer's own history
    # at this merchant; the counts it read are in the audit row either way
    history_pol, offer, counts = offers.history_override(session, attempt, cls.failure_class, pol, now=now)
    if history_pol is not None or offer is not None:
        what = (f"history override: {_policy_message(history_pol, retry_seq=1)}" if history_pol is not None
                else f"history override: first link carries the partial offer (min {offers.partial_min_paise(attempt.amount_paise)} paise)")
        audit(session, attempt.id, "policy", what,
              {"action": (history_pol or pol).action.value, "table_action": pol.action.value, "offer": offer, "counts": counts,
               "window_days": offers.repeat_window().days})
        if history_pol is not None:
            override, pol = history_pol, history_pol

    decision = RecoveryDecision(
        attempt_id=attempt.id, failure_class=cls.failure_class.value, action=pol.action.value,
        delay_seconds=pol.delay_seconds, max_attempts=pol.max_attempts,
        reason=f"{cls.reason} | {pol.rationale}", classified_by=cls.source,
        llm_used=cls.source == "llm" or cls.llm_model is not None,  # a model was consulted, even if it then failed
        llm_model=cls.llm_model, llm_latency_ms=cls.llm_latency_ms, confidence=cls.confidence,
        fallback_taken=cls.fallback_taken)
    session.add(decision)
    session.commit()  # the decision and its trail are durable before the job row is attempted

    job, created = executor.schedule_job(session, attempt, decision, pol, now=now, offer=offer)
    if not created:
        return _summary(session, attempt, cls, pol, job, duplicate=True)

    if job.status in _PARKED_NOTES:
        note = _parked_note(job)
        if override is not None and job.last_error is None and job.status == JobStatus.HUMAN_QUEUE.value:
            note = f"human_queue: {pol.rationale}"  # the subscription reason, not the generic parked note
        _close(session, attempt, job, note, now)  # schedule_job may have closed it already (order paid)
        return _summary(session, attempt, cls, pol, job)

    followup = None
    # Run now only when asked to (--execute-now) or when the job is actually due: a delay-0 class whose
    # send time quiet hours moved to 09:00 IST waits for run-due like any other scheduled job.
    if job.status == JobStatus.PENDING.value and (execute_now or job.scheduled_at <= now):
        if job.scheduled_at > now:
            audit(session, attempt.id, "execute",
                  f"execute_now: running ahead of schedule (was due {job.scheduled_at.isoformat()})",
                  {"job_id": job.id, "scheduled_at": job.scheduled_at.isoformat(), "delay_seconds": pol.delay_seconds})
            session.commit()
        job = executor.execute_job(session, job, client=rz_client, now=now, sleep=sleep)
        if job.razorpay_link_id:  # a link is committed; a later write failing must not say "no link created"
            progress["link_created"] = True
        followup = finish_job(session, attempt, job, decision, rz_client=rz_client, llm_client=llm_client, now=now)
    return _summary(session, attempt, cls, pol, job, followup=followup)


def _llm_message(cls: Classification) -> str:
    if cls.fallback_taken:
        return f"fallback {cls.fallback_taken}: {cls.reason}"
    return (f"classified as {cls.failure_class.value} with confidence {cls.confidence:.2f} "
            f"({cls.llm_model}, {cls.llm_latency_ms} ms): {cls.reason}")


def _policy_message(pol: PolicyDecision, *, retry_seq: int) -> str:
    prefix = f"follow-up seq {retry_seq}: " if retry_seq > 1 else ""
    return (f"{prefix}{pol.action.value} in {human_delay(pol.delay_seconds)} (max {pol.max_attempts} attempts, "
            f"backoff x{pol.backoff_multiplier:g}, nudge={'yes' if pol.nudge else 'no'}): {pol.rationale}")


def _latest_outcome(session: Session, job: RecoveryJob) -> Outcome | None:
    return session.execute(select(Outcome).where(Outcome.job_id == job.id).order_by(Outcome.id.desc())).scalars().first()


def _parked_note(job: RecoveryJob) -> str:
    """The outcome note for a job parked at schedule time: the reason schedule_job wrote (a contact
    cap, an order paid elsewhere) when there is one, else the generic note for the status."""
    if job.last_error and job.last_error.startswith(executor.ORDER_PAID_PREFIX):
        return executor.ORDER_PAID_NOTE
    if job.status == JobStatus.HUMAN_QUEUE.value and job.last_error:
        return f"human_queue: {job.last_error}"
    return _PARKED_NOTES[job.status]


def _summary(session: Session, attempt: PaymentAttempt, cls: Classification, pol: PolicyDecision, job: RecoveryJob,
             *, duplicate: bool = False, followup: RecoveryJob | None = None) -> dict:
    outcome = None if duplicate else _latest_outcome(session, job)
    return {
        "attempt_id": attempt.id, "payment_id": attempt.razorpay_payment_id, "method": attempt.method,
        "amount_paise": attempt.amount_paise,
        "failure_class": cls.failure_class.value, "classified_by": cls.source, "confidence": cls.confidence,
        "fallback_taken": cls.fallback_taken, "llm_model": cls.llm_model,
        "action": pol.action.value, "delay_seconds": pol.delay_seconds,
        "job_id": job.id, "retry_seq": job.retry_seq,
        "job_status": JobStatus.SKIPPED_DUPLICATE.value if duplicate else job.status,
        "existing_job_status": job.status if duplicate else None,
        "scheduled_at": job.scheduled_at, "attempts_made": job.attempts_made, "last_error": job.last_error,
        "link_id": job.razorpay_link_id, "link_url": job.razorpay_link_url,
        "nudge_source": None if duplicate else job.nudge_source,
        "nudge_channel": None if duplicate else job.nudge_channel,
        "schedule_note": None if duplicate else job.schedule_note,
        "duplicate": duplicate,
        "outcome_note": outcome.note if outcome is not None else None,
        "followup_job_id": followup.id if followup is not None else None,
        "followup_status": followup.status if followup is not None else None,
        "followup_scheduled_at": followup.scheduled_at if followup is not None else None,
    }


# ---- after execution: nudge, reconcile, follow-up or a closed outcome --------------------------

def finish_job(session: Session, attempt: PaymentAttempt, job: RecoveryJob, decision: RecoveryDecision | None, *,
               rz_client: RazorpayClient | None = None, llm_client: LLMClient | None = None,
               now: datetime | None = None) -> RecoveryJob | None:
    """What happens once execute_job has returned. A sent link job gets its nudge drafted. A
    stubbed token retry and a job parked at execution get their outcome written so the payment
    is visibly closed. A failed job is routed by executor.failure_kind: rate-limited -> the next
    attempt from the policy table; ambiguous (5xx, transport, client crash) -> reconcile by
    reference_id first (found: sent, no follow-up; absent: follow-up; unknown: a person);
    same-request failures (final 4xx, local guard) -> a person. Returns the follow-up job when
    one was scheduled. Shared with the scheduler."""
    now = now or utcnow()
    failure_class = decision.failure_class if decision is not None else FailureClass.UNKNOWN.value
    if job.action == Action.REMINDER.value:
        return None  # a reminder has no nudge, no outcome and no follow-up chain of its own (app/cadence.py)
    if job.status == JobStatus.SENT.value:
        if job.action in LINK_ACTIONS and not job.nudge_body:
            _after_sent(session, attempt, job, decision, failure_class, llm_client=llm_client, now=now)
        return None
    if job.status == JobStatus.SHADOW.value:
        # would have sent: the nudge is drafted from the template (no model call either), delivery
        # is recorded as "would have", the reminders are minted as shadow rows; no outcome
        if job.action in LINK_ACTIONS and not job.nudge_body:
            attach_nudge(session, attempt, job, failure_class, job.action, llm_client=None, shadow=True)
            audit(session, attempt.id, "deliver", "shadow mode: nothing delivered; the link was never created",
                  {"job_id": job.id, "delivered": False, "mode": offers.MODE_SHADOW})
            session.commit()
            cadence.schedule_reminders(session, attempt, job, decision, now=now)
        return None
    if job.status == JobStatus.STUBBED.value:
        _close(session, attempt, job, STUB_NOTE, now)
        return None
    if job.status == JobStatus.HUMAN_QUEUE.value:
        _close(session, attempt, job, f"human_queue: {job.last_error or 'parked for a person; nothing sent'}", now)
        return None
    if job.status != JobStatus.FAILED.value:
        return None

    kind = executor.failure_kind(job)
    if kind in executor.RETRYABLE_KINDS:
        if decision is not None:
            return schedule_followup(session, attempt, decision, now=now)
        _close(session, attempt, job, f"human_queue: rate limited and no decision row to derive a follow-up "
                                      f"from; a person re-runs it once the limit clears ({job.last_error})", now)
        return None
    if kind is None or kind in executor.AMBIGUOUS_KINDS:
        verdict = executor.reconcile_failed_job(session, job, client=rz_client, now=now)
        if verdict == "sent":
            _after_sent(session, attempt, job, decision, failure_class, llm_client=llm_client, now=now)
            return None
        if verdict == "absent" and decision is not None:
            return schedule_followup(session, attempt, decision, now=now)
        _close(session, attempt, job, "human_queue: the failed request may have reached Razorpay and the lookup "
                                      "could not tell; a person reconciles before anything more goes out", now)
        return None
    _close(session, attempt, job, f"human_queue: {kind} would fail the same way again ({job.last_error}); "
                                  f"no automatic retry, a person decides", now)
    return None


def _after_sent(session: Session, attempt: PaymentAttempt, job: RecoveryJob, decision: RecoveryDecision | None,
                failure_class, *, llm_client, now: datetime) -> None:
    """The three things a link that has just gone out earns, in order: the drafted nudge (stored),
    the `deliver` rows saying what Razorpay sent and on which channel (or why nothing went), and
    the class's reminder jobs (app/cadence.py). All idempotent behind the nudge_body guard."""
    attach_nudge(session, attempt, job, failure_class, job.action, llm_client=llm_client)
    cadence.record_delivery(session, attempt, job, now=now)
    cadence.schedule_reminders(session, attempt, job, decision, now=now)


def _close(session: Session, attempt: PaymentAttempt, job: RecoveryJob, note: str, now: datetime) -> None:
    """One not-recovered outcome per job, so a second finish never writes a second row."""
    if _latest_outcome(session, job) is None:
        executor.record_outcome(session, attempt, job, False, 0, note, now=now)


def attach_nudge(session: Session, attempt: PaymentAttempt, job: RecoveryJob, failure_class: FailureClass | str,
                 action: Action | str, *, llm_client: LLMClient | None = None, shadow: bool = False) -> Nudge:
    """Draft the customer message for a job that has its link (or lack of one) and store it on the
    job. Drafted, not sent: no SMS/email provider is in scope. Never raises: llm.draft_nudge degrades
    to the per-class template on any model failure. It runs AFTER the link exists, so it never blocks
    a recovery, but with a key the call itself is synchronous (bounded by LLM_TIMEOUT_SECONDS)."""
    cls, act = coerce(failure_class, FailureClass, FailureClass.UNKNOWN), coerce(action, Action, Action.HUMAN_QUEUE)
    # the customer's own language wins; then the merchant's configured default; then NUDGE_LANGUAGE_DEFAULT
    language = getattr(attempt, "customer_language", None) or getattr(merchants.for_attempt(attempt), "nudge_language", None)
    if shadow:  # no model call in shadow mode: the template is the draft
        from .nudge_templates import template_nudge
        nudge = template_nudge(attempt, cls, act, job.razorpay_link_url, language=language)
    else:
        nudge = llm.draft_nudge(attempt, cls, act, job.razorpay_link_url, client=llm_client, language=language)
    # the offer sentence (app/offers.py): partial payment on both sources, "UPI works best" on the template only
    extra = offers.nudge_suffix(attempt, job, cls, nudge.source, language=language)
    if extra:
        nudge = replace(nudge, body=f"{nudge.body.rstrip()} {extra}")
    job.nudge_channel, job.nudge_subject, job.nudge_body, job.nudge_source = (
        nudge.channel, nudge.subject, nudge.body, nudge.source)
    suffix = f" (fallback: {nudge.fallback_taken})" if nudge.fallback_taken else ""
    prefix = "shadow mode: " if shadow else ""
    audit(session, attempt.id, "nudge", f"{prefix}nudge drafted via {nudge.source} for {nudge.channel}; {NUDGE_NOT_SENT}{suffix}",
          {"job_id": job.id, "channel": nudge.channel, "subject": nudge.subject, "body": nudge.body,
           "source": nudge.source, "fallback_taken": nudge.fallback_taken, "reason": nudge.reason,
           "link_url": job.razorpay_link_url, "offer": job.offer, "offer_sentence": extra})
    session.commit()
    return nudge


def schedule_followup(session: Session, attempt: PaymentAttempt, decision: RecoveryDecision, *,
                      now: datetime | None = None, trigger: str = "delivery failure", offer: str | None = None) -> RecoveryJob:
    """The backoff chain from the policy table: seq n+1 at base * backoff**n, or the stop rule.

    Two triggers earn a follow-up: a FAILED job whose request provably did not land (finish_job
    checks the failure kind and reconciles ambiguous ones by reference_id first), and a sent link
    that expired unpaid (schedule_expiry_followup, from poll or the payment_link.expired webhook).
    A sent link that is still open is never re-issued: the customer holds it, Razorpay's
    reminder_enable re-nudges, and a second live link for one order is a double-payment risk.
    The confidence gate is not re-applied: a class that produced a link job already cleared it at
    seq 1. schedule_job applies the order-paid guard, the send-time rules and the contact cap.

    A delivery failure (a 429, or a send that never landed) is separated from a recovery attempt:
    the class backoff and stop rule are for the outage the class recovers from (an ISSUER_DOWN 5xx
    chain), not for the transport. So when the last failure was a delivery failure and nothing has
    ever reached Razorpay, the re-send waits only a short API backoff, and a stop rule that would
    otherwise close the payment `no_action` parks it for a person instead, because "max attempts
    reached" is the wrong close for a recovery that was never delivered.
    """
    now = now or utcnow()
    seq = executor.next_retry_seq(session, attempt)
    pol = policy_mod.decide(decision.failure_class, has_token=bool(attempt.has_token), retry_seq=seq,
                            reachable=reachable(attempt), overrides=merchants.for_attempt(attempt))
    last_failed = session.execute(
        select(RecoveryJob).where(RecoveryJob.attempt_id == attempt.id, RecoveryJob.status == JobStatus.FAILED.value)
        .order_by(RecoveryJob.id.desc())).scalars().first()
    delivery_retry = (last_failed is not None
                      and executor.failure_kind(last_failed) in executor.DELIVERY_FAILURE_KINDS
                      and not executor.reached_razorpay(session, attempt))
    if delivery_retry and pol.action is Action.NO_ACTION:
        pol = _delivery_exhausted_human_queue(seq)
    elif delivery_retry and pol.action not in (Action.HUMAN_QUEUE, Action.NO_ACTION):
        pol = replace(pol, delay_seconds=DELIVERY_RETRY_BACKOFF_SECONDS)
    audit(session, attempt.id, "policy", _policy_message(pol, retry_seq=seq),
          {"action": pol.action.value, "delay_seconds": pol.delay_seconds, "max_attempts": pol.max_attempts,
           "backoff_multiplier": pol.backoff_multiplier, "retry_seq": seq, "delivery_retry": delivery_retry,
           "trigger": trigger, "offer": offer})
    job, created = executor.schedule_job(session, attempt, decision, pol, retry_seq=seq, now=now, offer=offer)
    if created and job.status in _PARKED_NOTES:
        note = _parked_note(job)
        if job.last_error is None and pol.action is Action.NO_ACTION:
            note = f"no_action: {pol.rationale}"
        elif job.last_error is None and pol.action is Action.HUMAN_QUEUE:
            note = f"human_queue: {pol.rationale}"
        _close(session, attempt, job, note, now)
    return job


def schedule_expiry_followup(session: Session, attempt: PaymentAttempt, job: RecoveryJob, *,
                             now: datetime | None = None) -> RecoveryJob | None:
    """A sent link expired unpaid (its outcome is already written): mint the next attempt, or the
    stop rule. `now` is the expiry time, so the class delay counts from when the link died, not
    from when the agent noticed. Returns the follow-up job (pending, no_action by the stop rule,
    human_queue by the contact cap, or no_action because the order was paid meanwhile), or None
    when there is nothing to derive it from or one is already pending. Audits every branch."""
    now = now or utcnow()
    decision = session.get(RecoveryDecision, job.decision_id) if job.decision_id else None
    if decision is None:
        audit(session, attempt.id, "policy", f"link {job.razorpay_link_id} expired but job#{job.id} has no decision "
                                             f"row to derive a follow-up from; a person decides", {"job_id": job.id})
        session.commit()
        return None
    pending = session.execute(select(RecoveryJob).where(RecoveryJob.attempt_id == attempt.id,
                                                        RecoveryJob.status == JobStatus.PENDING.value)).scalars().first()
    if pending is not None:
        audit(session, attempt.id, "policy", f"link {job.razorpay_link_id} expired; follow-up job#{pending.id} "
                                             f"(seq {pending.retry_seq}) is already pending, nothing more minted",
              {"job_id": job.id, "pending_job_id": pending.id})
        session.commit()
        return None
    offer = None
    if offers.partial_offer_applies(decision.failure_class, attempt.amount_paise):
        # the first link lapsed unpaid on a balance/limit failure worth the offer: the next one
        # accepts a part payment (app/offers.py), audited on the policy row below
        offer = offers.OFFER_PARTIAL
        audit(session, attempt.id, "policy",
              f"partial offer: link {job.razorpay_link_id} expired unpaid on {decision.failure_class} for "
              f"{attempt.amount_paise} paise; the follow-up link accepts a partial payment of at least "
              f"{offers.partial_min_paise(attempt.amount_paise)} paise",
              {"job_id": job.id, "offer": offer, "first_min_partial_amount": offers.partial_min_paise(attempt.amount_paise),
               "min_amount_paise": offers.partial_min_amount_paise(), "min_share": offers.partial_min_share()})
        session.commit()
    return schedule_followup(session, attempt, decision, now=now, trigger=f"link expired ({job.razorpay_link_id})", offer=offer)


def _delivery_exhausted_human_queue(seq: int) -> PolicyDecision:
    """A follow-up policy for a recovery that never reached the customer (throttled or undelivered on
    every send): a person retries once the limit clears, rather than the agent recording it as
    recovery exhausted."""
    return PolicyDecision(
        action=Action.HUMAN_QUEUE, delay_seconds=0, max_attempts=0, backoff_multiplier=1.0, nudge=False,
        rationale=(f"every recovery send was throttled or never reached Razorpay (through attempt {seq}); "
                   "nothing was delivered to the customer, so a person retries once the rate limit clears "
                   "rather than the agent recording the recovery as exhausted."))


# ---- batch entry points ----------------------------------------------------------------------

def unscheduled_attempts(session: Session) -> list[PaymentAttempt]:
    """Attempts with no recovery job yet, in id order. Keyed on the job, not the decision: a crash
    between the decision commit and the job commit must not strand an event as "already done"."""
    scheduled = select(RecoveryJob.id).where(RecoveryJob.attempt_id == PaymentAttempt.id).exists()
    return list(session.execute(select(PaymentAttempt).where(~scheduled).order_by(PaymentAttempt.id)).scalars().all())


def process_all(session: Session, *, execute_now: bool = False, now: datetime | None = None,
                rz_client: RazorpayClient | None = None, llm_client: LLMClient | None = None,
                sleep: Callable[[float], None] = time.sleep) -> list[dict]:
    """Every attempt without a job yet, in id order. An attempt that already has a job is not
    re-run: re-processing is only ever a re-delivery, which schedule_job turns into skipped_duplicate.
    An attempt with a decision but no job (crash between the two commits) is decided again, so the
    trail shows both, and scheduled once."""
    now = now or utcnow()
    with db_guarded(session):
        pending = unscheduled_attempts(session)
    return [process_attempt(session, a, execute_now=execute_now, now=now, rz_client=rz_client,
                            llm_client=llm_client, sleep=sleep) for a in pending]


def open_link_jobs(session: Session) -> list[RecoveryJob]:
    """Sent jobs with a link id and no outcome yet: the only ones whose result is still unknown.
    A sent job always has a link id (created, or recovered by lookup); the filter is belt and braces."""
    closed = select(Outcome.id).where(Outcome.job_id == RecoveryJob.id).exists()
    stmt = (select(RecoveryJob)
            .where(RecoveryJob.status == JobStatus.SENT.value, RecoveryJob.razorpay_link_id.is_not(None), ~closed)
            .order_by(RecoveryJob.id))
    return list(session.execute(stmt).scalars().all())


def _expiry_time(link: dict, now: datetime) -> datetime:
    """When the link died, from expired_at or expire_by (unix), else now. Never later than now."""
    for key in ("expired_at", "expire_by"):
        value = link.get(key)
        try:
            if value and not isinstance(value, bool) and int(value) > 0:
                when = datetime.fromtimestamp(int(value), tz=timezone.utc).replace(tzinfo=None)
                return min(when, now)
        except (TypeError, ValueError, OverflowError, OSError):
            continue
    return now


def poll_outcomes(session: Session, *, rz_client: RazorpayClient | None = None,
                  now: datetime | None = None) -> list[Outcome]:
    """Ask Razorpay what became of each open link and close the loop. paid -> recovered with the
    amount actually paid; expired -> not recovered, then the next attempt from the policy table
    (schedule_expiry_followup) or the stop rule; cancelled -> not recovered (a cancel is a
    decision, not a lapse: no follow-up); anything else stays open. A fetch failure is audited
    and skipped, never raised: the next poll asks again."""
    now = now or utcnow()
    recorded: list[Outcome] = []
    with db_guarded(session):
        client = rz_client or get_client()
        for job in open_link_jobs(session):
            attempt = job.attempt if job.attempt is not None else session.get(PaymentAttempt, job.attempt_id)
            try:
                link = client.fetch_payment_link(job.razorpay_link_id)
            except RazorpayError as exc:
                audit(session, job.attempt_id, "poll", f"could not fetch {job.razorpay_link_id}: {exc}; still open",
                      {"job_id": job.id, "link_id": job.razorpay_link_id, "http_status": exc.status, "code": exc.code})
                session.commit()
                continue
            except Exception as exc:  # a client bug must not stop the sweep
                audit(session, job.attempt_id, "poll", f"could not fetch {job.razorpay_link_id}: "
                      f"{type(exc).__name__}: {exc}; still open", {"job_id": job.id})
                session.commit()
                continue
            status = str(link.get("status") or "")
            if status == "paid":
                paid = int(link.get("amount_paid") or attempt.amount_paise)
                recorded.append(executor.record_outcome(session, attempt, job, True, paid,
                                                        f"payment_link.paid via poll ({job.razorpay_link_id})", now=now))
                cadence.void_reminders(session, attempt, f"link {job.razorpay_link_id} paid", now=now, parent=job)
            elif status == "partially_paid" and int(link.get("amount_paid") or 0) > 0:
                # the partial offer was taken: recovered for what was paid; the link stays open at
                # Razorpay for the balance (no order-paid record, no cancel of anything)
                paid = int(link.get("amount_paid"))
                recorded.append(executor.record_outcome(session, attempt, job, True, paid,
                                                        f"{PARTIAL_NOTE} via poll ({job.razorpay_link_id}): {paid} of "
                                                        f"{attempt.amount_paise} paise", now=now))
                cadence.void_reminders(session, attempt, f"link {job.razorpay_link_id} partially paid", now=now, parent=job)
            elif status == "expired":
                recorded.append(executor.record_outcome(session, attempt, job, False, 0,
                                                        f"payment link expired ({job.razorpay_link_id})", now=now))
                cadence.void_reminders(session, attempt, f"link {job.razorpay_link_id} expired", now=now, parent=job)
                schedule_expiry_followup(session, attempt, job, now=_expiry_time(link, now))
            elif status == "cancelled":
                recorded.append(executor.record_outcome(session, attempt, job, False, 0,
                                                        f"payment link cancelled ({job.razorpay_link_id})", now=now))
                cadence.void_reminders(session, attempt, f"link {job.razorpay_link_id} cancelled", now=now, parent=job)
            else:
                audit(session, job.attempt_id, "poll", f"link {job.razorpay_link_id} still open: status {status or '?'}",
                      {"job_id": job.id, "link_id": job.razorpay_link_id, "link_status": status,
                       "amount_paid": link.get("amount_paid")})
                session.commit()
    return recorded


# ---- shadow mode ------------------------------------------------------------------------------------

def shadow_summary(session: Session) -> dict:
    """What shadow mode would have done: jobs with status `shadow` grouped by their would-be action,
    with the amount at stake and the expected recovery from app.priority when it is importable
    (the simulation prior or the merchant's own empirical rate). Reminders are counted separately.
    {"jobs": n, "amount_paise": total, "expected_paise": total | None, "by_action": {action: {...}},
     "reminders": n}"""
    stmt = select(RecoveryJob).where(RecoveryJob.status == JobStatus.SHADOW.value).order_by(RecoveryJob.id)
    from .models import with_reminders
    rows = list(session.execute(with_reminders(stmt)).scalars().all())
    try:
        from . import priority
        empirical = priority.empirical_rates(session)
    except Exception:  # priority is owned elsewhere and optional here
        priority, empirical = None, None
    by_action: dict[str, dict] = {}
    total = expected = 0
    reminders = 0
    have_expected = priority is not None
    for job in rows:
        if job.action == Action.REMINDER.value:
            reminders += 1
            continue
        attempt = job.attempt if job.attempt is not None else session.get(PaymentAttempt, job.attempt_id)
        decision = session.get(RecoveryDecision, job.decision_id) if job.decision_id else None
        amount = int(attempt.amount_paise or 0)
        cell = by_action.setdefault(job.action, {"jobs": 0, "amount_paise": 0, "expected_paise": 0 if have_expected else None,
                                                 "offers": {}})
        cell["jobs"] += 1
        cell["amount_paise"] += amount
        if job.offer:
            cell["offers"][job.offer] = cell["offers"].get(job.offer, 0) + 1
        total += amount
        if have_expected:
            try:
                est = priority.expected_recovery(attempt, decision.failure_class if decision else FailureClass.UNKNOWN,
                                                 job.action, delay_seconds=decision.delay_seconds if decision else None,
                                                 empirical=empirical)
                cell["expected_paise"] += est.expected_paise
                expected += est.expected_paise
            except Exception:
                have_expected = False
    if not have_expected:
        expected = None
        for cell in by_action.values():
            cell["expected_paise"] = None
    return {"jobs": sum(c["jobs"] for c in by_action.values()), "amount_paise": total, "expected_paise": expected,
            "by_action": by_action, "reminders": reminders, "mode": offers.MODE_SHADOW if offers.shadow_mode() else "live"}
