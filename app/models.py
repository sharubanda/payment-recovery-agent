"""Four tables plus an audit log. Together they ARE the audit trail the track asks for:
every event -> the decision taken (and why, and whether an LLM was involved) -> the job
(with its idempotency key) -> the outcome.
"""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, event
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship, with_loader_criteria

from .clock import utcnow
from .db import Base


class PaymentAttempt(Base):
    """One failed payment event, as Razorpay reports it (payment.failed webhook / Payments API)."""
    __tablename__ = "payment_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    merchant_id: Mapped[str] = mapped_column(String(64), index=True)
    order_id: Mapped[str] = mapped_column(String(64))
    razorpay_payment_id: Mapped[str] = mapped_column(String(64), unique=True)  # pay_XXXXXXXXXXXXXX
    amount_paise: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    method: Mapped[str] = mapped_column(String(16))  # card | upi | netbanking | wallet | emandate

    # Razorpay's error object, verbatim: {code, description, source, step, reason, metadata}
    error_code: Mapped[str | None] = mapped_column(String(32))         # BAD_REQUEST_ERROR | GATEWAY_ERROR | SERVER_ERROR
    error_source: Mapped[str | None] = mapped_column(String(32))       # customer | business | bank | gateway | internal | network
    error_step: Mapped[str | None] = mapped_column(String(32))         # payment_initiation | payment_authentication | payment_authorization | payment_capture
    error_reason: Mapped[str | None] = mapped_column(String(64))       # payment_failed | payment_cancelled | payment_timed_out | card_declined | ...
    error_description: Mapped[str | None] = mapped_column(Text)

    has_token: Mapped[bool] = mapped_column(Boolean, default=False)    # saved card token / active mandate exists
    customer_name: Mapped[str | None] = mapped_column(String(128))
    customer_contact: Mapped[str | None] = mapped_column(String(32))
    customer_email: Mapped[str | None] = mapped_column(String(128))
    customer_language: Mapped[str | None] = mapped_column(String(16))  # en | hi | hinglish; from notes.language or the CSV

    failed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    # The order was paid another way (payment.captured / order.paid webhook, or GET /orders/{id}/payments):
    # from then on nothing more may go out for this order, and any live link is cancelled. Set on every
    # attempt row sharing the order_id, so the guard is one column read, not a join.
    order_paid_at: Mapped[datetime | None] = mapped_column(DateTime)
    paid_by_payment_id: Mapped[str | None] = mapped_column(String(64))

    # Subscription lifecycle (subscription.pending / subscription.halted, app/ingest.py): the mandate
    # behind the failed charge, Razorpay's state for it at ingest, and its hosted re-authorisation page.
    subscription_id: Mapped[str | None] = mapped_column(String(64), index=True)   # sub_XXXXXXXXXXXXXX
    subscription_status: Mapped[str | None] = mapped_column(String(16))           # pending | halted
    subscription_url: Mapped[str | None] = mapped_column(String(256))             # the subscription's short_url

    decisions: Mapped[list["RecoveryDecision"]] = relationship(back_populates="attempt", cascade="all, delete-orphan")
    jobs: Mapped[list["RecoveryJob"]] = relationship(back_populates="attempt", cascade="all, delete-orphan")
    outcomes: Mapped[list["Outcome"]] = relationship(back_populates="attempt", cascade="all, delete-orphan")


class RecoveryDecision(Base):
    """What the agent decided for an attempt, and how it got there."""
    __tablename__ = "recovery_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    attempt_id: Mapped[int] = mapped_column(ForeignKey("payment_attempts.id"), index=True)
    failure_class: Mapped[str] = mapped_column(String(32))
    action: Mapped[str] = mapped_column(String(32))
    delay_seconds: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=1)
    reason: Mapped[str] = mapped_column(Text)                          # classification reason + policy rationale
    classified_by: Mapped[str] = mapped_column(String(16), default="rules")  # rules | llm | fallback
    llm_used: Mapped[bool] = mapped_column(Boolean, default=False)
    llm_model: Mapped[str | None] = mapped_column(String(64))
    llm_latency_ms: Mapped[int | None] = mapped_column(Integer)
    confidence: Mapped[float | None] = mapped_column(Float)
    fallback_taken: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    attempt: Mapped["PaymentAttempt"] = relationship(back_populates="decisions")


class RecoveryJob(Base):
    """One scheduled recovery action. The idempotency key is the guard against double-sending:
    sha256(f"{razorpay_payment_id}:{retry_seq}"), UNIQUE in the database, checked BEFORE any
    outbound call, and echoed to Razorpay as the Payment Link reference_id."""
    __tablename__ = "recovery_jobs"
    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_recovery_jobs_idempotency_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    attempt_id: Mapped[int] = mapped_column(ForeignKey("payment_attempts.id"), index=True)
    decision_id: Mapped[int | None] = mapped_column(ForeignKey("recovery_decisions.id"))
    retry_seq: Mapped[int] = mapped_column(Integer, default=1)          # 1 = first recovery attempt for this payment
    action: Mapped[str] = mapped_column(String(32))
    scheduled_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)  # JobStatus values

    razorpay_link_id: Mapped[str | None] = mapped_column(String(64))
    razorpay_link_url: Mapped[str | None] = mapped_column(String(256))
    attempts_made: Mapped[int] = mapped_column(Integer, default=0)      # outbound HTTP attempts (backoff counter)
    last_error: Mapped[str | None] = mapped_column(Text)

    nudge_channel: Mapped[str | None] = mapped_column(String(8))
    nudge_subject: Mapped[str | None] = mapped_column(String(160))
    nudge_body: Mapped[str | None] = mapped_column(Text)
    nudge_source: Mapped[str | None] = mapped_column(String(16))        # llm | template
    # why scheduled_at is not now + delay: "salary window: 2 Oct 10:00 IST" / "quiet hours: 6 Sep 09:00 IST" (app/scheduling.py)
    schedule_note: Mapped[str | None] = mapped_column(String(160))
    # A reminder (Action.REMINDER, app/cadence.py) re-notifies the link its parent job created; the
    # parent is the LINK job row. Its key is sha256(f"{payment_id}:{retry_seq}:reminder:{n}"), still UNIQUE.
    parent_job_id: Mapped[int | None] = mapped_column(ForeignKey("recovery_jobs.id"), index=True)
    # where the link on this job comes from: a Payment Link the agent created ("payment_link") or the
    # halted subscription's own hosted re-authorisation page ("subscription_url"; nothing is created)
    link_source: Mapped[str | None] = mapped_column(String(24))
    # the offer this link carries (app/offers.py): "partial" (accept_partial + first_min_partial_amount)
    # or "rail_upi" (card disabled at checkout, UPI preferred); None for a plain link
    offer: Mapped[str | None] = mapped_column(String(24))

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime)

    attempt: Mapped["PaymentAttempt"] = relationship(back_populates="jobs")


class Outcome(Base):
    __tablename__ = "outcomes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    attempt_id: Mapped[int] = mapped_column(ForeignKey("payment_attempts.id"), index=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("recovery_jobs.id"))
    recovered: Mapped[bool] = mapped_column(Boolean, default=False)
    recovered_at: Mapped[datetime | None] = mapped_column(DateTime)
    amount_recovered_paise: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str | None] = mapped_column(Text)                      # e.g. "payment_link.paid via poll" / "human_queue"
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    attempt: Mapped["PaymentAttempt"] = relationship(back_populates="outcomes")


class AuditEvent(Base):
    """Append-only trail: one row per stage per attempt (classified, decided, scheduled, sent, ...)."""
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    attempt_id: Mapped[int | None] = mapped_column(ForeignKey("payment_attempts.id"), index=True)
    stage: Mapped[str] = mapped_column(String(32))                      # classify | llm | policy | schedule | execute | outcome | fault
    message: Mapped[str] = mapped_column(Text)
    data_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


def audit(session, attempt_id: int | None, stage: str, message: str, data: dict | None = None) -> AuditEvent:
    """Write one audit row. Caller commits."""
    import json
    row = AuditEvent(attempt_id=attempt_id, stage=stage, message=message,
                     data_json=json.dumps(data, default=str) if data is not None else None)
    session.add(row)
    return row


# ---- reminders are hidden from job queries unless asked for --------------------------------------
# A reminder row (RecoveryJob.action == "reminder", app/cadence.py) re-notifies a link its parent job
# created; it is not a recovery state of the attempt. Every reader that takes "the attempt's latest
# job" as its status (the CLI totals, the operator view, the insights report) would otherwise see a
# pending or voided reminder where the sent link is. So a SELECT of RecoveryJob, including the
# PaymentAttempt.jobs relationship, excludes reminders unless the statement carries the execution
# option include_reminders=True (with_reminders(stmt)); cadence, the scheduler, the contact cap and
# the paid-elsewhere stop ask for them explicitly. Column refreshes are never filtered.
INCLUDE_REMINDERS = "include_reminders"
REMINDER_ACTION = "reminder"  # == taxonomy.Action.REMINDER.value; a string here to keep this module import-free


def with_reminders(stmt):
    """`stmt` with reminder jobs included in its results (see the note above)."""
    return stmt.execution_options(**{INCLUDE_REMINDERS: True})


@event.listens_for(Session, "do_orm_execute")
def _hide_reminder_jobs(state) -> None:
    if state.is_select and not state.is_column_load and not state.execution_options.get(INCLUDE_REMINDERS, False):
        state.statement = state.statement.options(
            with_loader_criteria(RecoveryJob, lambda cls: cls.action != REMINDER_ACTION, include_aliases=True))
