"""Runs the jobs whose time has come. A poll loop over the recovery_jobs table, on purpose.

This is the stand-in for a real queue, and it is the honest answer to "what breaks at
10,000 merchants": this. One process scans one table for due rows and executes them in
order; there is no lease, no worker pool, no partitioning. The migration path is a queue
(SQS / Kafka / a Postgres SKIP LOCKED worker pool) fed by the same schedule_job rows, and
it is safe to make because the job row and its reference_id exist before any call: two
workers on one job are refused by the status re-read or, if they race past it, by
Razorpay's duplicate-reference_id rejection (assumed, not verified; a lease on execute_job
is part of the migration).

`now` is a parameter everywhere so `run-due --now <iso>` can replay a 48-hour delay in a
demo without waiting 48 hours.
"""
import time
from datetime import datetime
from typing import Callable

from sqlalchemy import select

from sqlalchemy.orm import Session

from . import executor, pipeline, razorpay_client
from .clock import utcnow
from .llm import LLMClient
from .models import PaymentAttempt, RecoveryDecision, RecoveryJob, with_reminders
from .razorpay_client import RazorpayClient
from .taxonomy import JobStatus


# pending is the normal due state; executing is a stale claim from a crashed sweep, reclaimed here
_DUE_STATUSES = (JobStatus.PENDING.value, JobStatus.EXECUTING.value)


def due_jobs(session: Session, now: datetime | None = None) -> list[RecoveryJob]:
    """Due jobs, oldest first (then by id for a stable order): pending ones whose scheduled_at has
    passed, plus any left 'executing' by a crash so a later sweep reclaims them rather than stranding
    the link they may have created. Reminder jobs (app/cadence.py) are included: they are executed
    here like any other job."""
    now = now or utcnow()
    stmt = (select(RecoveryJob)
            .where(RecoveryJob.status.in_(_DUE_STATUSES), RecoveryJob.scheduled_at <= now)
            .order_by(RecoveryJob.scheduled_at, RecoveryJob.id))
    return list(session.execute(with_reminders(stmt)).scalars().all())


def next_due(session: Session, now: datetime | None = None) -> RecoveryJob | None:
    """The earliest pending job still in the future (for the CLI to say when to come back)."""
    now = now or utcnow()
    stmt = (select(RecoveryJob)
            .where(RecoveryJob.status == JobStatus.PENDING.value, RecoveryJob.scheduled_at > now)
            .order_by(RecoveryJob.scheduled_at, RecoveryJob.id).limit(1))
    return session.execute(with_reminders(stmt)).scalars().first()


def run_due(session: Session, *, now: datetime | None = None, rz_client: RazorpayClient | None = None,
            llm_client: LLMClient | None = None, sleep: Callable[[float], None] = time.sleep) -> list[RecoveryJob]:
    """Execute every due job once, in order, then finish it: nudge for a sent link, reconcile
    or backoff follow-up for a failed one, an outcome for a stub or a parked job. Returns the
    executed jobs. One Razorpay client for the whole run, so the fault wrapper's "429 twice
    then success" counter spans the sweep (a real rate limit applies per request whatever
    client object sends it). Follow-ups scheduled during the sweep are never due in the same
    sweep (every backoff delay is > 0), so one call terminates. Raises PipelineDBError if the
    database goes away; never raises for Razorpay or LLM failures."""
    now = now or utcnow()
    executed: list[RecoveryJob] = []
    with pipeline.db_guarded(session):
        client = rz_client or razorpay_client.get_client()
        for job in due_jobs(session, now):
            attempt = job.attempt if job.attempt is not None else session.get(PaymentAttempt, job.attempt_id)
            decision = session.get(RecoveryDecision, job.decision_id) if job.decision_id else None
            if job.status == JobStatus.EXECUTING.value:
                # a prior sweep/process crashed after claiming this job: recover any link it made
                # before re-running it (the reference_id lookup answers whether the request landed).
                verdict = executor.recover_stale_execution(session, job, client=client, now=now)
                if verdict != "pending":  # "sent" (link recovered) or "unknown" (parked): finish as is
                    pipeline.finish_job(session, attempt, job, decision, rz_client=client,
                                        llm_client=llm_client, now=now)
                    executed.append(job)
                    continue
            job = executor.execute_job(session, job, client=client, now=now, sleep=sleep)
            pipeline.finish_job(session, attempt, job, decision, rz_client=client, llm_client=llm_client, now=now)
            executed.append(job)
    return executed
