"""Timing intelligence for customer-facing sends: pure functions, every one taking `now`.

Three rules, all deterministic, all applied by executor.schedule_job to LINK jobs only (a
token retry is silent, so nothing here touches it):

  next_salary_window   INSUFFICIENT_FUNDS: a send that would land on the 24th..31st of a month
                       moves to config.SALARY_WINDOW_DAY (2nd) of the next month at 10:00 IST
  next_allowed_send    no customer message between 21:00 and 09:00 IST; shift to 09:00 IST
  contact_cap          at most MAX_CONTACTS_PER_CUSTOMER_PER_WEEK link/nudge sends per phone or
                       email in a rolling 7 days, counted across every attempt sharing the contact

Times are naive UTC, as everywhere else in the agent; IST is UTC+05:30 with no daylight saving,
so the conversion is a fixed offset. The salary-cycle rule is a HEURISTIC: most Indian salaried
accounts are credited in the last days of the month or the first days of the next, so a balance
that was short on the 27th is most likely to be there on the 2nd. It is not a fact about any
customer, which is why it is one number in config and one function here.
"""
from datetime import date, datetime, time, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from . import config, merchants
from .models import PaymentAttempt, RecoveryJob, with_reminders
from .taxonomy import Action, JobStatus

IST_OFFSET = timedelta(hours=5, minutes=30)
QUIET_START_HOUR = 21          # 21:00 IST: no customer message from here...
QUIET_END_HOUR = 9             # ...until 09:00 IST (TRAI-style courtesy window; a 2am link reads as spam)
SALARY_CYCLE_FROM_DAY = 24     # the 24th..31st is "waiting for salary"
SALARY_WINDOW_HOUR = 10        # 10:00 IST on the salary-window day
CONTACT_WINDOW = timedelta(days=7)
LINK_ACTIONS = frozenset({Action.RECOVERY_LINK.value, Action.NUDGE_CHANGE_METHOD.value})
# everything that reaches the customer's phone or email counts against the cap: a link, and each
# reminder of it (app/cadence.py re-notifies the same link through Razorpay)
CONTACT_ACTIONS = LINK_ACTIONS | {Action.REMINDER.value}


def to_ist(dt: datetime) -> datetime:
    """Naive UTC -> naive IST wall-clock time."""
    return dt + IST_OFFSET


def from_ist(dt: datetime) -> datetime:
    """Naive IST wall-clock time -> naive UTC."""
    return dt - IST_OFFSET


def format_ist(dt: datetime) -> str:
    """'2 Oct 10:00 IST' for audit rows and the CLI (day without a leading zero)."""
    ist = to_ist(dt)
    return f"{ist.day} {ist:%b %H:%M} IST"


# ---- salary window ------------------------------------------------------------------------------

def _first_of_next_month(d: date) -> date:
    return date(d.year + 1, 1, 1) if d.month == 12 else date(d.year, d.month + 1, 1)


def within_salary_wait(dt: datetime) -> bool:
    """True when `dt` (UTC) falls on the 24th..31st of a month in IST."""
    return to_ist(dt).day >= SALARY_CYCLE_FROM_DAY


def next_salary_window(now: datetime) -> datetime:
    """The time an INSUFFICIENT_FUNDS send should happen, given it would otherwise happen at `now`.

    Heuristic (Indian salary cycle): if `now` falls on the 24th..31st of a month in IST, the
    balance is most likely waiting for the month's salary credit, so the send moves to
    config.SALARY_WINDOW_DAY (default the 2nd) of the NEXT month at 10:00 IST. Any other day,
    including the 1st..23rd (already inside the window), is returned unchanged; with the default
    SALARY_WINDOW_DAY of 2 that covers the window day itself (a day of 24..28 is allowed by the
    clamp and then shifts a send on that day to the same day next month). February and December are just months: 24 Feb -> 2 Mar, 27 Dec -> 2 Jan.
    Pure and monotone: the result is never earlier than `now`.
    """
    ist = to_ist(now)
    if ist.day < SALARY_CYCLE_FROM_DAY:
        return now
    day = max(1, min(int(config.SALARY_WINDOW_DAY), 28))  # every month has a 28th
    target = _first_of_next_month(ist.date()) + timedelta(days=day - 1)
    shifted = from_ist(datetime.combine(target, time(SALARY_WINDOW_HOUR, 0)))
    return max(now, shifted)


# ---- quiet hours --------------------------------------------------------------------------------

def within_quiet_hours(dt: datetime) -> bool:
    """True when `dt` (UTC) is between 21:00 and 09:00 IST, the window no customer message goes out in."""
    hour = to_ist(dt).hour
    return hour >= QUIET_START_HOUR or hour < QUIET_END_HOUR


def next_allowed_send(dt: datetime) -> datetime:
    """`dt` itself when it is outside quiet hours; otherwise the next 09:00 IST (the same
    morning for 00:00..08:59 IST, the next morning for 21:00..23:59 IST). Never earlier than `dt`."""
    if not within_quiet_hours(dt):
        return dt
    ist = to_ist(dt)
    day = ist.date() + timedelta(days=1) if ist.hour >= QUIET_START_HOUR else ist.date()
    return from_ist(datetime.combine(day, time(QUIET_END_HOUR, 0)))


# ---- contact cap --------------------------------------------------------------------------------

def contact_keys(attempt: PaymentAttempt) -> tuple[str | None, str | None]:
    contact = (getattr(attempt, "customer_contact", None) or "").strip() or None
    email = (getattr(attempt, "customer_email", None) or "").strip().lower() or None
    return contact, email


def contact_sends(session: Session, attempt: PaymentAttempt, now: datetime) -> int:
    """Link/nudge jobs and reminders SENT in the last 7 days to this customer's phone or email, across
    every attempt that shares either. A cancelled link counts too: the customer was still messaged."""
    contact, email = contact_keys(attempt)
    clauses = []
    if contact:
        clauses.append(PaymentAttempt.customer_contact == contact)
    if email:
        clauses.append(func.lower(PaymentAttempt.customer_email) == email)
    if not clauses:
        return 0
    sharing = select(PaymentAttempt.id).where(or_(*clauses))
    stmt = (select(func.count(RecoveryJob.id))
            .where(RecoveryJob.attempt_id.in_(sharing),
                   RecoveryJob.action.in_(tuple(CONTACT_ACTIONS)),
                   RecoveryJob.status.in_((JobStatus.SENT.value, JobStatus.CANCELLED.value)),
                   RecoveryJob.executed_at.is_not(None),
                   RecoveryJob.executed_at > now - CONTACT_WINDOW,
                   RecoveryJob.executed_at <= now))
    return int(session.execute(with_reminders(stmt)).scalar_one() or 0)


def contact_cap(session: Session, attempt: PaymentAttempt, now: datetime) -> tuple[bool, str]:
    """(allowed, reason). Allowed while fewer than config.MAX_CONTACTS_PER_CUSTOMER_PER_WEEK
    link/nudge sends reached this phone or email in the rolling 7 days before `now`. When the
    cap is hit the caller parks the job for a person (human_queue) with the reason: a customer
    messaged three times this week about failed payments is a support conversation, not a fourth
    link. An attempt with no contact and no email is not capped (there is nobody to spam)."""
    cap = getattr(merchants.for_attempt(attempt), "max_contacts_per_week", None)  # a merchant file may lower it
    limit = max(0, int(cap if cap is not None else config.MAX_CONTACTS_PER_CUSTOMER_PER_WEEK))
    contact, email = contact_keys(attempt)
    if not contact and not email:
        return True, "no customer contact on the attempt; nothing to cap"
    sent = contact_sends(session, attempt, now)
    who = contact or email
    if sent >= limit:
        return False, (f"contact cap: {sent} link/nudge send(s) to {who} in the last 7 days "
                       f"(max {limit} per week); a person decides rather than the agent sending a {sent + 1}th")
    return True, f"contact cap ok: {sent} of {limit} sends to {who} this week"


def adjust_send_time(scheduled_at: datetime, *, failure_class: str | None) -> tuple[datetime, str | None]:
    """The send time for a link job after the salary window (INSUFFICIENT_FUNDS only) and quiet
    hours, with a one-line note saying what moved and why, or None when nothing did."""
    notes = []
    when = scheduled_at
    if failure_class == "INSUFFICIENT_FUNDS":
        shifted = next_salary_window(when)
        if shifted != when:
            notes.append(f"salary window: {format_ist(shifted)}")
            when = shifted
    shifted = next_allowed_send(when)
    if shifted != when:
        notes.append(f"quiet hours: {format_ist(shifted)}")
        when = shifted
    return when, "; ".join(notes) or None
