"""app/scheduling.py: the salary window, the quiet hours and the contact cap, all in naive UTC
with IST = UTC+05:30. Dates below are chosen so the IST and UTC calendar days differ where it
matters (18:30 UTC is 00:00 IST the next day)."""
from datetime import datetime, timedelta

import pytest

from app import config, scheduling
from app.models import PaymentAttempt, RecoveryJob
from app.taxonomy import Action, JobStatus


def ist(y, m, d, hh=12, mm=0):
    """A naive-UTC datetime for the given IST wall-clock time."""
    return scheduling.from_ist(datetime(y, m, d, hh, mm))


# ---- salary window --------------------------------------------------------------------------

@pytest.mark.parametrize("day", [1, 2, 3, 15, 23])
def test_days_before_the_24th_are_left_alone(day):
    now = ist(2026, 9, day, 14)
    assert scheduling.next_salary_window(now) == now


@pytest.mark.parametrize("day", [24, 27, 30])
def test_late_month_moves_to_the_2nd_of_next_month_at_10_ist(day):
    now = ist(2026, 9, day, 14)
    assert scheduling.next_salary_window(now) == ist(2026, 10, 2, 10)


def test_february_and_december_roll_over_correctly():
    assert scheduling.next_salary_window(ist(2026, 2, 26, 9)) == ist(2026, 3, 2, 10)
    assert scheduling.next_salary_window(ist(2026, 12, 29, 9)) == ist(2027, 1, 2, 10)


def test_the_ist_day_decides_not_the_utc_day():
    # 23 Sep 19:00 UTC is 24 Sep 00:30 IST: inside the wait window.
    now = datetime(2026, 9, 23, 19, 0)
    assert scheduling.within_salary_wait(now)
    assert scheduling.next_salary_window(now) == ist(2026, 10, 2, 10)


def test_salary_window_is_never_earlier_than_now(monkeypatch):
    monkeypatch.setattr(config, "SALARY_WINDOW_DAY", 1)
    now = ist(2026, 9, 30, 23, 30)
    assert scheduling.next_salary_window(now) >= now


def test_salary_window_day_is_clamped_to_a_day_every_month_has(monkeypatch):
    monkeypatch.setattr(config, "SALARY_WINDOW_DAY", 31)
    assert scheduling.next_salary_window(ist(2026, 1, 25)) == ist(2026, 2, 28, 10)


# ---- quiet hours ----------------------------------------------------------------------------

@pytest.mark.parametrize("hour", [9, 12, 17, 20])
def test_daytime_ist_sends_are_not_shifted(hour):
    now = ist(2026, 9, 5, hour, 30)
    assert not scheduling.within_quiet_hours(now)
    assert scheduling.next_allowed_send(now) == now


@pytest.mark.parametrize("hour", [21, 22, 23])
def test_evening_ist_moves_to_9am_the_next_morning(hour):
    now = ist(2026, 9, 5, hour, 15)
    assert scheduling.within_quiet_hours(now)
    assert scheduling.next_allowed_send(now) == ist(2026, 9, 6, 9)


@pytest.mark.parametrize("hour", [0, 3, 8])
def test_early_morning_ist_moves_to_9am_the_same_day(hour):
    now = ist(2026, 9, 6, hour, 59)
    assert scheduling.next_allowed_send(now) == ist(2026, 9, 6, 9)


def test_quiet_hours_boundaries_are_inclusive_at_21_and_exclusive_at_9():
    assert scheduling.within_quiet_hours(ist(2026, 9, 5, 21, 0))
    assert not scheduling.within_quiet_hours(ist(2026, 9, 5, 9, 0))
    assert scheduling.within_quiet_hours(ist(2026, 9, 5, 8, 59))


def test_utc_midnight_is_5_30am_ist_and_therefore_quiet():
    now = datetime(2026, 9, 5, 0, 0)
    assert scheduling.within_quiet_hours(now)
    assert scheduling.next_allowed_send(now) == ist(2026, 9, 5, 9)


# ---- adjust_send_time ----------------------------------------------------------------------

def test_adjust_applies_salary_window_then_quiet_hours_for_insufficient_funds():
    when, note = scheduling.adjust_send_time(ist(2026, 9, 28, 22), failure_class="INSUFFICIENT_FUNDS")
    assert when == ist(2026, 10, 2, 10)      # 10:00 IST is already outside quiet hours
    assert note and "salary window" in note and "2 Oct 10:00 IST" in note


def test_adjust_applies_only_quiet_hours_for_other_classes():
    when, note = scheduling.adjust_send_time(ist(2026, 9, 28, 22), failure_class="AUTH_ABANDONED")
    assert when == ist(2026, 9, 29, 9)
    assert note and "quiet hours" in note and "salary" not in note


def test_adjust_returns_none_note_when_nothing_moves():
    when, note = scheduling.adjust_send_time(ist(2026, 9, 10, 11), failure_class="INSUFFICIENT_FUNDS")
    assert when == ist(2026, 9, 10, 11) and note is None


# ---- contact cap ----------------------------------------------------------------------------

def _attempt(session, pid, contact="+919999900001", email=None):
    a = PaymentAttempt(merchant_id="m", order_id=f"order_{pid}", razorpay_payment_id=pid, amount_paise=10000,
                       method="card", customer_contact=contact, customer_email=email)
    session.add(a)
    session.commit()
    return a


_seq = iter(range(1, 10_000))


def _sent_link(session, attempt, executed_at, status=JobStatus.SENT.value, action=Action.RECOVERY_LINK.value):
    j = RecoveryJob(attempt_id=attempt.id, action=action, idempotency_key=f"k-{attempt.id}-{next(_seq)}",
                    status=status, executed_at=executed_at, scheduled_at=executed_at)
    session.add(j)
    session.commit()
    return j


def test_cap_counts_sends_across_attempts_sharing_the_phone(session):
    now = ist(2026, 9, 5, 12)
    a1 = _attempt(session, "pay_a1")
    a2 = _attempt(session, "pay_a2")
    a3 = _attempt(session, "pay_a3")
    for a in (a1, a2):
        _sent_link(session, a, now - timedelta(days=1))
    allowed, reason = scheduling.contact_cap(session, a3, now)
    assert allowed and "2 of 3" in reason
    _sent_link(session, a3, now - timedelta(hours=1))
    allowed, reason = scheduling.contact_cap(session, a3, now)
    assert not allowed and "contact cap" in reason and "4th" in reason


def test_cap_matches_email_case_insensitively_and_ignores_old_sends(session):
    now = ist(2026, 9, 5, 12)
    a1 = _attempt(session, "pay_e1", contact=None, email="Asha@Example.com")
    a2 = _attempt(session, "pay_e2", contact=None, email="asha@example.com")
    _sent_link(session, a1, now - timedelta(days=8))       # outside the rolling week
    _sent_link(session, a1, now - timedelta(days=2))
    assert scheduling.contact_sends(session, a2, now) == 1


def test_cancelled_links_count_but_token_retries_and_pending_jobs_do_not(session):
    now = ist(2026, 9, 5, 12)
    a = _attempt(session, "pay_c1")
    _sent_link(session, a, now - timedelta(days=1), status=JobStatus.CANCELLED.value)
    _sent_link(session, a, now - timedelta(days=1), status=JobStatus.STUBBED.value, action=Action.TOKEN_RETRY.value)
    _sent_link(session, a, now - timedelta(days=1), status=JobStatus.PENDING.value)
    assert scheduling.contact_sends(session, a, now) == 1


def test_no_contact_means_no_cap(session):
    a = _attempt(session, "pay_n1", contact=None, email=None)
    allowed, reason = scheduling.contact_cap(session, a, ist(2026, 9, 5, 12))
    assert allowed and "nothing to cap" in reason


def test_cap_of_zero_parks_the_first_send(session, monkeypatch):
    monkeypatch.setattr(config, "MAX_CONTACTS_PER_CUSTOMER_PER_WEEK", 0)
    a = _attempt(session, "pay_z1")
    allowed, _ = scheduling.contact_cap(session, a, ist(2026, 9, 5, 12))
    assert not allowed
