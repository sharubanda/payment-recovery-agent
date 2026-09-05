"""Environment-driven configuration. Read once at import; tests set env before importing."""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


RAZORPAY_KEY_ID = _env("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = _env("RAZORPAY_KEY_SECRET")

ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY")
LLM_MODEL = _env("LLM_MODEL", "claude-opus-5")
LLM_TIMEOUT_SECONDS = float(_env("LLM_TIMEOUT_SECONDS", "8"))

DATABASE_URL = _env("DATABASE_URL", "sqlite:///./recovery.db")

RECOVERY_LINK_EXPIRY_HOURS = int(_env("RECOVERY_LINK_EXPIRY_HOURS", "72"))
CALLBACK_URL = _env("CALLBACK_URL")
# Off by default: with real test keys, notify.sms/email would make Razorpay message the seed
# contacts, which are realistic-looking numbers and addresses, not reserved ones.
RAZORPAY_NOTIFY_CUSTOMER = _env("RAZORPAY_NOTIFY_CUSTOMER", "0").lower() in ("1", "true", "yes")

# The LLM classifier must clear this or the event goes to a human. Never guess in a money path.
LLM_MIN_CONFIDENCE = 0.7


def razorpay_live() -> bool:
    """True when real test-mode keys are present. Refuses live keys outright."""
    if RAZORPAY_KEY_ID and not RAZORPAY_KEY_ID.startswith("rzp_test_"):
        raise RuntimeError("Refusing to run with a non-test Razorpay key. This agent is test-mode only.")
    return bool(RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET)


def llm_live() -> bool:
    return bool(ANTHROPIC_API_KEY)

# Dashboard -> Settings -> Webhooks -> the secret you typed when creating the webhook. When set,
# `ingest --signature` and `serve-webhook` verify X-Razorpay-Signature (HMAC-SHA256 of the raw body).
RAZORPAY_WEBHOOK_SECRET = _env("RAZORPAY_WEBHOOK_SECRET")

# ---- the money path (docs/money_path.md) --------------------------------------------------------
# Before a link is created, ask Razorpay whether the order has a captured payment already
# (GET /orders/{order_id}/payments). "auto" = on when real keys are present, off against the
# fixture (which answers from its own memory when forced on with 1); "1"/"0" force it.
CHECK_ORDER_BEFORE_SEND = _env("CHECK_ORDER_BEFORE_SEND", "auto")
# Indian salary-cycle heuristic for INSUFFICIENT_FUNDS: a send that would land on the 24th..31st
# moves to this day of the next month, 10:00 IST (app/scheduling.py, next_salary_window).
SALARY_WINDOW_DAY = int(_env("SALARY_WINDOW_DAY", "2"))
# Link/nudge sends per customer contact (phone or email) in a rolling 7 days; beyond it a person decides.
MAX_CONTACTS_PER_CUSTOMER_PER_WEEK = int(_env("MAX_CONTACTS_PER_CUSTOMER_PER_WEEK", "3"))
# Reminder cadence (app/cadence.py): 0 disables the per-class reminders after a sent link; 1 (default)
# schedules them. Reminders are Razorpay re-notifications of the same link, never a new link.
REMINDERS_ENABLED = _env("REMINDERS_ENABLED", "1").lower() in ("1", "true", "yes", "on")


def check_order_before_send() -> bool:
    """True when the executor should GET /orders/{id}/payments before creating a link."""
    value = CHECK_ORDER_BEFORE_SEND.strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    return razorpay_live()


# ---- running it as a product (docs/ops.md) -------------------------------------------------------
# live   : the executor makes the outbound Razorpay calls (test mode only, as always).
# shadow : the whole pipeline runs and records what it WOULD do, but the executor makes NO outbound
#          call and marks the job `shadow`. The guard lives in app/executor.py and reads this value.
PRA_MODE = _env("PRA_MODE", "live").lower() or "live"
if PRA_MODE not in ("live", "shadow"):
    raise RuntimeError(f"PRA_MODE must be 'live' or 'shadow', not {PRA_MODE!r}")
# Bearer token the operator view (app/web.py) requires when set; blank = loopback-only, no auth.
OPERATOR_TOKEN = _env("OPERATOR_TOKEN")
# Slack-compatible incoming webhook: `pra digest --post`, `pra alert-test` and the human-queue alerts
# from `pra serve` POST {"text": ...} here. Blank = alerts are printed, never sent.
ALERT_WEBHOOK_URL = _env("ALERT_WEBHOOK_URL")
# Timezone name printed on digests. Only Asia/Kolkata (a fixed +05:30, no zoneinfo lookup) is
# rendered as local time; anything else falls back to UTC with a note.
DIGEST_TIMEZONE = _env("DIGEST_TIMEZONE", "Asia/Kolkata") or "Asia/Kolkata"


def shadow_mode() -> bool:
    return PRA_MODE == "shadow"
