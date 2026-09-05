"""The vocabulary every module shares. Enums are closed sets on purpose:
an LLM answer that is not one of these values is rejected, not coerced.
"""
from dataclasses import dataclass
from enum import Enum


class FailureClass(str, Enum):
    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"  # balance will replenish; retry on a salary cycle, not in 10 minutes
    ISSUER_DOWN = "ISSUER_DOWN"                # bank/issuer outage; transient, retry soon with backoff
    AUTH_ABANDONED = "AUTH_ABANDONED"          # customer dropped at OTP/3DS/UPI approval; intent existed, friction killed it
    HARD_DECLINE = "HARD_DECLINE"              # card blocked / expired / invalid / not enabled; a retry can never succeed
    RISK_BLOCKED = "RISK_BLOCKED"              # risk or fraud flag from issuer or Razorpay; never auto-retry
    NETWORK_TIMEOUT = "NETWORK_TIMEOUT"        # timeout at gateway/network; likely transient
    LIMIT_EXCEEDED = "LIMIT_EXCEEDED"          # daily / per-transaction limit hit; recovers once the limit window resets
    UNKNOWN = "UNKNOWN"                        # matched no rule; falls through to the LLM, then to a human


class Action(str, Enum):
    TOKEN_RETRY = "token_retry"                # charge the saved token/mandate again (STUBBED: see executor.py)
    RECOVERY_LINK = "recovery_link"            # create a fresh Razorpay Payment Link and nudge the customer
    NUDGE_CHANGE_METHOD = "nudge_change_method"  # link + message asking for a different payment method
    HUMAN_QUEUE = "human_queue"                # stop; a person decides
    NO_ACTION = "no_action"                    # stop; nothing to recover
    REMINDER = "reminder"                      # re-notify an open Payment Link via Razorpay (app/cadence.py); never a new link


class JobStatus(str, Enum):
    PENDING = "pending"                        # decided and scheduled; nothing sent yet
    EXECUTING = "executing"                    # claimed for an outbound call (atomic pending->executing); a second worker is refused
    SENT = "sent"                              # payment link created (or token retry stub executed)
    FAILED = "failed"                          # outbound call failed after bounded retries
    SKIPPED_DUPLICATE = "skipped_duplicate"    # idempotency key already used; nothing sent
    HUMAN_QUEUE = "human_queue"                # parked for a person
    NO_ACTION = "no_action"                    # policy said stop
    STUBBED = "stubbed"                        # token_retry path: recorded, not executed (no real tokens in test mode)
    CANCELLED = "cancelled"                    # a sent link cancelled at Razorpay because the order was paid another way
    SHADOW = "shadow"                          # PRA_MODE=shadow: would have sent; the payload is in the audit row, nothing left the process


@dataclass(frozen=True)
class Classification:
    """Output of classify.py (rules) or llm.classify_unmapped (LLM)."""
    failure_class: FailureClass
    reason: str                       # one sentence a reviewer can read in the audit trail
    source: str                       # "rules" | "llm" | "fallback"
    confidence: float                 # 1.0 for rules; the model's own number for llm; 0.0 for fallback
    llm_model: str | None = None
    llm_latency_ms: int | None = None
    fallback_taken: str | None = None  # e.g. "llm_timeout->human_queue"; None when no fallback fired


@dataclass(frozen=True)
class PolicyDecision:
    """Output of policy.decide(). Deterministic given (failure_class, has_token, attempt history)."""
    action: Action
    delay_seconds: int                # 0 = act now
    max_attempts: int                 # stopping rule: total recovery attempts allowed for this payment
    backoff_multiplier: float         # delay grows by this factor per attempt (1.0 = flat)
    rationale: str                    # why, in one sentence, for the audit trail
    nudge: bool                       # whether to draft/send a customer message with the action


@dataclass(frozen=True)
class Nudge:
    """Output of llm.draft_nudge() or the template fallback."""
    channel: str                      # "sms" | "email"
    subject: str                      # empty for sms
    body: str
    source: str                       # "llm" | "template"
    fallback_taken: str | None = None
    reason: str | None = None          # for a fallback: the exception type + message the model failed with, for the audit trail


def coerce(value, enum, default):
    """Turn a str/enum/garbage value into a member of `enum`, or `default`. One helper so that
    every caller degrades the same way: an unrecognised class is UNKNOWN, an unrecognised action
    is HUMAN_QUEUE, and nobody invents a nearest neighbour."""
    try:
        return enum(value.value if isinstance(value, enum) else value)
    except Exception:
        return default
