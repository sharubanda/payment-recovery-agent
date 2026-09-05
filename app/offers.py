"""Deterministic offer intelligence and shadow mode: post-policy overrides, never in app/policy.py.

Four rules and one operating mode, every one a pure function of the row and the config, audited
by the caller (app/pipeline.py, app/executor.py, app/cadence.py, app/ingest.py):

  shadow mode        PRA_MODE=shadow: nothing leaves the process. A job that would have created a
                     link ends `shadow` with the exact payload in its audit row; reminders, the
                     cancel of a live link and every notify are recorded as "would have" too.
  partial offer      INSUFFICIENT_FUNDS / LIMIT_EXCEEDED, amount >= PARTIAL_OFFER_MIN_PAISE, first
                     link expired unpaid: the follow-up link accepts a partial payment of at least
                     max(PARTIAL_OFFER_MIN_SHARE x amount, Rs 100), whole rupees.
  repeat-failer      the same customer (phone or email) at the same merchant in the last
                     REPEAT_FAILER_WINDOW_DAYS days: 3+ INSUFFICIENT_FUNDS/LIMIT_EXCEEDED -> the
                     FIRST link is already the partial offer; 3+ HARD_DECLINE -> human_queue; any
                     RISK_BLOCKED -> human_queue for everything.
  rail preference    HARD_DECLINE's change-method link disables card at checkout and prefers UPI.

Razorpay fields relied on (ASSUMED, unverified offline; see docs/offers.md): Payment Links accept
`accept_partial` (bool) and `first_min_partial_amount` (paise) on a standard link; a partially paid
link reports status "partially_paid" with `amount_paid`; `options.checkout.method` with per-method
booleans restricts the hosted checkout. A live 400 whose description mentions the field falls back
to a plain link (app/executor.py), audited, so a wrong assumption costs one retry, never a lost link.
"""
import os
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from . import config
from .models import PaymentAttempt, RecoveryDecision, RecoveryJob
from .nudge_templates import format_rupees, preferred_language
from .taxonomy import Action, FailureClass, PolicyDecision

__all__ = ["OFFER_PARTIAL", "OFFER_RAIL", "MODE_SHADOW", "shadow_mode", "partial_min_paise", "partial_offer_applies",
           "link_payload_extras", "strip_offer_fields", "is_offer_rejection", "default_offer", "customer_history", "history_override",
           "nudge_suffix"]

OFFER_PARTIAL = "partial"        # RecoveryJob.offer: the link accepts a partial payment
OFFER_RAIL = "rail_upi"          # RecoveryJob.offer: card disabled at checkout, UPI preferred
MODE_SHADOW = "shadow"
PARTIAL_CLASSES = frozenset({FailureClass.INSUFFICIENT_FUNDS.value, FailureClass.LIMIT_EXCEEDED.value})
LINK_ACTIONS = frozenset({Action.RECOVERY_LINK.value, Action.NUDGE_CHANGE_METHOD.value})
PARTIAL_FLOOR_PAISE = 10000      # Rs 100: never ask for less than this as a first part
REPEAT_THRESHOLD = 3
# the assumed checkout restriction for a dead card: card off, the three Indian rails on
RAIL_CHECKOUT_METHODS = {"card": False, "upi": True, "netbanking": True, "wallet": True}
PREFERRED_RAIL = "upi"

# one sentence appended after the nudge is drafted (app/pipeline.attach_nudge); kept here rather
# than in the templates so the change to app/nudge_templates.py is zero lines
PARTIAL_SENTENCE = {
    "en": "You can also pay part of it now (at least {min}) and the rest later.",
    "hi": "आप अभी इसका एक हिस्सा (कम से कम {min}) भी दे सकते हैं और बाकी बाद में।",
    "hinglish": "Aap abhi iska ek hissa (kam se kam {min}) bhi de sakte hain aur baaki baad mein.",
}
RAIL_SENTENCE = {"en": "UPI works best.", "hi": "UPI सबसे आसान रहेगा।", "hinglish": "UPI sabse aasaan rahega."}


# ---- config (app/config.py is owned elsewhere: a declared attribute wins, then the env, then the default)

def _setting(name: str, default: str) -> str:
    value = getattr(config, name, None)
    if value in (None, ""):
        value = os.getenv(name, "")
    return str(value).strip() or default


def shadow_mode() -> bool:
    """True when PRA_MODE=shadow: decide, schedule and audit everything, send nothing."""
    return _setting("PRA_MODE", "live").lower() == MODE_SHADOW


def partial_min_amount_paise() -> int:
    return max(0, int(float(_setting("PARTIAL_OFFER_MIN_PAISE", "200000"))))


def partial_min_share() -> float:
    return min(1.0, max(0.0, float(_setting("PARTIAL_OFFER_MIN_SHARE", "0.5"))))


def repeat_window() -> timedelta:
    return timedelta(days=max(1, int(float(_setting("REPEAT_FAILER_WINDOW_DAYS", "60")))))


# ---- the partial offer ----------------------------------------------------------------------------

def partial_min_paise(amount_paise: int) -> int:
    """max(PARTIAL_OFFER_MIN_SHARE x amount rounded to whole rupees, Rs 100), never above the amount."""
    share = int(round(int(amount_paise) * partial_min_share() / 100.0)) * 100
    return min(int(amount_paise), max(PARTIAL_FLOOR_PAISE, share))


def partial_offer_applies(failure_class, amount_paise) -> bool:
    """The class and the amount qualify; whether the link is the first (repeat failer) or the
    follow-up after an expiry is the caller's business."""
    cls = failure_class.value if isinstance(failure_class, FailureClass) else str(failure_class or "")
    try:
        amount = int(amount_paise or 0)
    except (TypeError, ValueError):
        return False
    return cls in PARTIAL_CLASSES and amount >= partial_min_amount_paise() and amount > PARTIAL_FLOOR_PAISE


def default_offer(failure_class, action) -> str | None:
    """The offer a link job carries by construction: the rail preference on every HARD_DECLINE /
    change-method link. The partial offer is decided by history or an expiry, never here."""
    cls = failure_class.value if isinstance(failure_class, FailureClass) else str(failure_class or "")
    act = action.value if isinstance(action, Action) else str(action or "")
    if cls == FailureClass.HARD_DECLINE.value or act == Action.NUDGE_CHANGE_METHOD.value:
        return OFFER_RAIL
    return None


def link_payload_extras(attempt, job, failure_class=None) -> dict:
    """The Payment Link fields job.offer adds (merged over the base payload by the executor).
    partial: accept_partial + first_min_partial_amount (ASSUMED valid on a standard link).
    rail_upi: options.checkout.method with card off and notes.preferred_method=upi (ASSUMED: the
    hosted checkout honours per-method booleans). A job whose offer was dropped by the live-400
    fallback (offer None) gets a plain payload."""
    extras: dict = {}
    offer = getattr(job, "offer", None)
    if offer == OFFER_PARTIAL:
        extras["accept_partial"] = True
        extras["first_min_partial_amount"] = partial_min_paise(int(attempt.amount_paise))
    if offer == OFFER_RAIL:
        extras["options"] = {"checkout": {"method": dict(RAIL_CHECKOUT_METHODS)}}
        extras["notes"] = {"preferred_method": PREFERRED_RAIL}
    return extras


def apply_extras(payload: dict, extras: dict) -> dict:
    out = dict(payload)
    for k, v in extras.items():
        if k == "notes":
            out["notes"] = {**dict(out.get("notes") or {}), **v}
        else:
            out[k] = v
    return out


def strip_offer_fields(payload: dict) -> dict:
    """The plain link: the same payload without any offer field (the live-400 fallback)."""
    out = {k: v for k, v in payload.items() if k not in ("accept_partial", "first_min_partial_amount", "options")}
    notes = dict(out.get("notes") or {})
    notes.pop("preferred_method", None)
    out["notes"] = notes
    return out


def is_offer_rejection(status: int, description: str) -> bool:
    """A 400 whose wording points at an offer field: the API (or a stricter fixture) does not take it."""
    text = (description or "").lower()
    return int(status) == 400 and any(w in text for w in ("partial", "options", "checkout", "preferred_method"))


# ---- repeat-failer memory -------------------------------------------------------------------------

def customer_history(session: Session, attempt: PaymentAttempt, *, now: datetime | None = None,
                     window: timedelta | None = None) -> dict[str, int]:
    """Failed attempts by class for this customer (same merchant_id, same phone or email, case-
    insensitive email) in the window before `now`, excluding `attempt` itself. The class is the
    one the agent decided (recovery_decisions.failure_class); an attempt not yet decided is not
    counted. Empty when the attempt carries no contact at all."""
    from .clock import utcnow
    now = now or utcnow()
    window = window or repeat_window()
    contact = (getattr(attempt, "customer_contact", None) or "").strip() or None
    email = (getattr(attempt, "customer_email", None) or "").strip().lower() or None
    clauses = []
    if contact:
        clauses.append(PaymentAttempt.customer_contact == contact)
    if email:
        clauses.append(func.lower(PaymentAttempt.customer_email) == email)
    if not clauses:
        return {}
    stmt = (select(RecoveryDecision.failure_class, func.count(func.distinct(PaymentAttempt.id)))
            .join(PaymentAttempt, PaymentAttempt.id == RecoveryDecision.attempt_id)
            .where(PaymentAttempt.merchant_id == attempt.merchant_id, or_(*clauses),
                   PaymentAttempt.failed_at > now - window, PaymentAttempt.failed_at <= now)
            .group_by(RecoveryDecision.failure_class))
    if getattr(attempt, "id", None) is not None:
        stmt = stmt.where(PaymentAttempt.id != attempt.id)
    return {str(cls): int(n) for cls, n in session.execute(stmt).all() if n}


def history_override(session: Session, attempt: PaymentAttempt, failure_class, pol: PolicyDecision,
                     *, now: datetime | None = None) -> tuple[PolicyDecision | None, str | None, dict]:
    """(policy override or None, offer for the first job or None, the counts the rule read).
    Counts include this attempt. Rules, in order: any RISK_BLOCKED in history -> human_queue for
    everything; 3+ HARD_DECLINE -> human_queue (the instrument is dead); 3+ INSUFFICIENT_FUNDS /
    LIMIT_EXCEEDED on a link action for a qualifying amount -> the first link is the partial offer."""
    cls = failure_class.value if isinstance(failure_class, FailureClass) else str(failure_class or "")
    history = customer_history(session, attempt, now=now)
    counts = dict(history)
    counts[cls] = counts.get(cls, 0) + 1
    risk = history.get(FailureClass.RISK_BLOCKED.value, 0)
    hard = counts.get(FailureClass.HARD_DECLINE.value, 0)
    soft = counts.get(FailureClass.INSUFFICIENT_FUNDS.value, 0) + counts.get(FailureClass.LIMIT_EXCEEDED.value, 0)
    days = repeat_window().days
    if pol.action in (Action.HUMAN_QUEUE, Action.NO_ACTION):
        return None, None, counts
    if risk:
        return PolicyDecision(action=Action.HUMAN_QUEUE, delay_seconds=0, max_attempts=pol.max_attempts,
                              backoff_multiplier=1.0, nudge=False,
                              rationale=(f"repeat-failer memory: {risk} RISK_BLOCKED failure(s) for this customer in the last "
                                         f"{days} days; nothing goes out automatically, a person reviews (table said {pol.action.value}).")), None, counts
    if cls == FailureClass.HARD_DECLINE.value and hard >= REPEAT_THRESHOLD:
        return PolicyDecision(action=Action.HUMAN_QUEUE, delay_seconds=0, max_attempts=pol.max_attempts,
                              backoff_multiplier=1.0, nudge=False,
                              rationale=(f"repeat hard declines: {hard} HARD_DECLINE failures in {days} days, the instrument "
                                         f"is dead, a person reaches out (table said {pol.action.value}).")), None, counts
    if cls in PARTIAL_CLASSES and soft >= REPEAT_THRESHOLD and pol.action.value in LINK_ACTIONS \
            and partial_offer_applies(cls, attempt.amount_paise):
        return None, OFFER_PARTIAL, counts
    return None, None, counts


# ---- the nudge --------------------------------------------------------------------------------------

def nudge_suffix(attempt, job, failure_class, source: str, *, language: str | None = None) -> str | None:
    """The sentence appended to a drafted nudge: the partial offer (both sources), or "UPI works
    best" on a rail-preferred link when the wording came from the template (the LLM variant is left
    as drafted so the amount/link validators it passed still hold)."""
    lang = preferred_language(attempt, language)
    offer = getattr(job, "offer", None)
    if offer == OFFER_PARTIAL:
        text = PARTIAL_SENTENCE.get(lang, PARTIAL_SENTENCE["en"])
        return text.format(min=format_rupees(partial_min_paise(int(attempt.amount_paise))))
    if offer == OFFER_RAIL and source == "template" and job.razorpay_link_url:
        return RAIL_SENTENCE.get(lang, RAIL_SENTENCE["en"])
    return None
