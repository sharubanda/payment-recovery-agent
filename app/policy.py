"""The policy table: failure class -> action, delay, backoff and stopping rules. Deterministic.

This is the policy table from README.md / ARCHITECTURE.md as code. Nothing here consults a
model: the LLM only ever picks a class (and only for unmapped descriptions); what to do
about that class is a table a payments reviewer can read in one screen. Three rules apply
across the table: a token retry is silent (no nudge: the customer has nothing to do), any
classification that did not come from the rules must clear config.LLM_MIN_CONFIDENCE or
the event goes to a person, and a link action needs a customer we can reach (a link nobody
receives is not a recovery). Never guess in a money path.
"""
from typing import TYPE_CHECKING

from . import config
from .taxonomy import Action, Classification, FailureClass, PolicyDecision, coerce

if TYPE_CHECKING:  # app/merchants.py imports this module; the annotation must not import it back at runtime
    from .merchants import MerchantPolicy

MINUTE, HOUR = 60, 3600

# action          : what to do when there is no saved token / mandate
# token_action    : what to do instead when has_token (None = same as action)
# delay_seconds   : wait before the first recovery attempt
# backoff         : delay multiplier per further attempt (1.0 = flat)
# max_attempts    : total automated recovery attempts allowed for this payment (0 = none)
# nudge           : draft/send a customer message with the action (never on a token retry)
POLICY: dict[FailureClass, dict] = {
    FailureClass.INSUFFICIENT_FUNDS: {
        "action": Action.RECOVERY_LINK, "token_action": None,
        "delay_seconds": 48 * HOUR, "backoff": 1.0, "max_attempts": 2, "nudge": True,
        "rationale": "Balance replenishes on salary cycles; a 10-minute retry burns an issuer attempt for nothing, so send a link and ask again in 48 hours.",
    },
    FailureClass.ISSUER_DOWN: {
        "action": Action.RECOVERY_LINK, "token_action": Action.TOKEN_RETRY,
        "delay_seconds": 15 * MINUTE, "backoff": 3.0, "max_attempts": 3, "nudge": True,
        "rationale": "The issuer outage is transient but without a saved token the customer must re-initiate, so send a fresh link after 15 minutes and back off threefold if it stays down.",
        "token_rationale": "The issuer outage is transient and the saved token allows a silent retry, so retry in 15 minutes and back off threefold if it stays down.",
    },
    FailureClass.AUTH_ABANDONED: {
        "action": Action.RECOVERY_LINK, "token_action": None,
        "delay_seconds": 10 * MINUTE, "backoff": 1.0, "max_attempts": 2, "nudge": True,
        "rationale": "Intent existed and friction at OTP or 3DS killed it; nothing can be retried without the customer, so send them back to checkout after 10 minutes.",
    },
    FailureClass.HARD_DECLINE: {
        "action": Action.NUDGE_CHANGE_METHOD, "token_action": None,
        "delay_seconds": 0, "backoff": 1.0, "max_attempts": 1, "nudge": True,
        "rationale": "A blocked, expired or invalid card can never authorise, so the only recovery is asking the customer for a different payment method right away.",
    },
    FailureClass.RISK_BLOCKED: {
        "action": Action.HUMAN_QUEUE, "token_action": None,
        "delay_seconds": 0, "backoff": 1.0, "max_attempts": 0, "nudge": False,
        "rationale": "A risk or fraud flag is never auto-retried; a person reviews it because a retry is fraud-adjacent and can get the merchant flagged.",
    },
    FailureClass.NETWORK_TIMEOUT: {
        "action": Action.RECOVERY_LINK, "token_action": Action.TOKEN_RETRY,
        "delay_seconds": 5 * MINUTE, "backoff": 2.0, "max_attempts": 2, "nudge": True,
        "rationale": "A gateway timeout is likely transient but without a saved token the customer must re-initiate, so send a fresh link after 5 minutes and double the wait if it recurs.",
        "token_rationale": "A gateway timeout is likely transient and the saved token allows a silent retry, so retry in 5 minutes and double the wait if it recurs.",
    },
    FailureClass.LIMIT_EXCEEDED: {
        "action": Action.RECOVERY_LINK, "token_action": None,
        "delay_seconds": 24 * HOUR, "backoff": 1.0, "max_attempts": 2, "nudge": True,
        "rationale": "A daily or per-transaction limit clears when its window resets, so an immediate retry (token or not) burns an issuer attempt against the same cap; send a link and ask again after 24 hours.",
    },
    FailureClass.UNKNOWN: {
        "action": Action.HUMAN_QUEUE, "token_action": None,
        "delay_seconds": 0, "backoff": 1.0, "max_attempts": 0, "nudge": False,
        "rationale": "No rule matched and the LLM did not resolve the cause with confidence, so a person decides rather than the agent guessing in a money path.",
    },
}

TERMINAL_ACTIONS = (Action.HUMAN_QUEUE, Action.NO_ACTION)
_LINK_ACTIONS = (Action.RECOVERY_LINK, Action.NUDGE_CHANGE_METHOD)


def _coerce_class(failure_class) -> FailureClass:
    return coerce(failure_class, FailureClass, FailureClass.UNKNOWN)  # unrecognised is, by definition, unknown -> human


def _human_queue(rationale: str) -> PolicyDecision:
    return PolicyDecision(action=Action.HUMAN_QUEUE, delay_seconds=0, max_attempts=0,
                          backoff_multiplier=1.0, rationale=rationale, nudge=False)


OVERRIDE_SUFFIX = " [merchant override]"


def _human_queue_entry(entry: dict, why: str) -> dict:
    """A table entry turned terminal by an override: human_queue, no attempts, no nudge."""
    return {**entry, "action": Action.HUMAN_QUEUE, "token_action": None, "delay_seconds": 0, "backoff": 1.0,
            "max_attempts": 0, "nudge": False, "rationale": f"{why}; a person decides for this merchant.",
            "overridden": entry.get("overridden", []) + ["action"]}


def effective_entry(failure_class: FailureClass, overrides: "MerchantPolicy | None" = None) -> dict:
    """The table entry for a class after a merchant's overrides (app/merchants.py). With no
    overrides this is the POLICY row itself, untouched. The invariants: a row whose table action
    is human_queue (RISK_BLOCKED, UNKNOWN) is returned unchanged whatever the file says; an override
    can only move an action to human_queue (or drop token_retry back to the class's link action via
    disabled_actions); delays and attempts arrive validated (>= 0, 0..5). The merged entry carries
    an "overridden" list naming the fields a file changed, for policy_table() and the audit trail."""
    cls = _coerce_class(failure_class)
    entry = POLICY[cls]
    if overrides is None:
        return entry
    if entry["action"] in TERMINAL_ACTIONS:
        return entry  # invariant: RISK_BLOCKED and UNKNOWN stay human_queue
    merged = {**entry, "overridden": []}
    if getattr(overrides, "human_queue_all", False):
        return _human_queue_entry(merged, "human_queue_all is set for this merchant")
    disabled = set(getattr(overrides, "disabled_actions", ()) or ())
    if merged["token_action"] is not None and merged["token_action"].value in disabled:
        merged["token_action"] = None
        merged["overridden"].append("token_action")
    if merged["action"].value in disabled:
        if merged["token_action"] is None:
            return _human_queue_entry(merged, f"{merged['action'].value} is disabled for this merchant")
        # the token path survives: only the no-token branch goes to a person (decide checks the flag)
        merged["no_token_action_disabled"] = True
        merged["overridden"].append("action")
    per_class = (getattr(overrides, "classes", None) or {}).get(cls.value)
    if per_class is None:
        return merged
    if per_class.action == Action.HUMAN_QUEUE.value:
        return _human_queue_entry(merged, f"{cls.value} is routed to a person for this merchant")
    for name in ("delay_seconds", "max_attempts", "nudge"):
        value = getattr(per_class, name, None)
        if value is not None and value != merged[name]:
            merged[name] = int(value) if name != "nudge" else bool(value)
            merged["overridden"].append(name)
    if merged["max_attempts"] < 0 or merged["delay_seconds"] < 0:  # belt and braces: the file was validated already
        return _human_queue_entry(merged, "override out of range")
    if merged["overridden"]:
        merged["rationale"] = merged["rationale"].rstrip(".") + OVERRIDE_SUFFIX + "."
        if "token_rationale" in merged:
            merged["token_rationale"] = merged["token_rationale"].rstrip(".") + OVERRIDE_SUFFIX + "."
    return merged


def delay_for(failure_class: FailureClass, retry_seq: int = 1, overrides: "MerchantPolicy | None" = None) -> int:
    """Delay in seconds before attempt number `retry_seq` (1-based): base * backoff ** (n - 1)."""
    entry = effective_entry(failure_class, overrides)
    n = max(1, int(retry_seq))
    return int(round(entry["delay_seconds"] * entry["backoff"] ** (n - 1)))


def decide(failure_class: FailureClass, *, has_token: bool, retry_seq: int = 1,
           classification: Classification | None = None, reachable: bool = True,
           overrides: "MerchantPolicy | None" = None) -> PolicyDecision:
    """Pure and never raises. Same inputs -> same decision, every time.

    Checks run in this order: confidence gate (a non-rules classification below the
    threshold goes to a person), terminal actions (human_queue is not an "attempt",
    so it ignores retry_seq), the max-attempts stop rule, then the table; finally a link
    action for a customer with no contact and no email (reachable=False) goes to a person,
    because a link nobody receives is not a recovery. A token retry needs no channel.

    `overrides` (a merchants.MerchantPolicy, from merchants.for_attempt(attempt)) swaps the
    table row for effective_entry(); with None the behaviour is exactly the table's.
    """
    try:
        cls = _coerce_class(failure_class)
        try:
            seq = max(1, int(retry_seq))
        except Exception:
            seq = 1
        entry = effective_entry(cls, overrides)

        if classification is not None and getattr(classification, "source", "rules") != "rules":
            confidence = float(getattr(classification, "confidence", 0.0) or 0.0)
            if confidence < config.LLM_MIN_CONFIDENCE:
                return _human_queue(f"LLM confidence {confidence:.2f} is below the {config.LLM_MIN_CONFIDENCE} threshold; "
                                    "a person decides rather than the agent guessing in a money path.")

        if entry["action"] in TERMINAL_ACTIONS:
            return PolicyDecision(action=entry["action"], delay_seconds=0, max_attempts=entry["max_attempts"],
                                  backoff_multiplier=1.0, rationale=entry["rationale"], nudge=False)

        if seq > entry["max_attempts"]:
            return PolicyDecision(action=Action.NO_ACTION, delay_seconds=0, max_attempts=entry["max_attempts"],
                                  backoff_multiplier=entry["backoff"], nudge=False,
                                  rationale=f"stop rule: max attempts reached (attempt {seq} of {entry['max_attempts']} "
                                            f"allowed for {cls.value}); further retries burn issuer trust for nothing.")

        use_token = bool(has_token) and entry["token_action"] is not None
        if not use_token and entry.get("no_token_action_disabled"):
            return _human_queue(f"{entry['action'].value} is disabled for this merchant and no saved token allows a "
                                "silent retry; a person decides for this merchant.")
        action = entry["token_action"] if use_token else entry["action"]
        rationale = entry.get("token_rationale", entry["rationale"]) if use_token else entry["rationale"]
        if action in _LINK_ACTIONS and not reachable:
            return _human_queue("No customer contact or email on the event, so a payment link would reach nobody; "
                                "a person finds a channel rather than the agent creating a link for nobody.")
        return PolicyDecision(action=action, delay_seconds=delay_for(cls, seq, overrides), max_attempts=entry["max_attempts"],
                              backoff_multiplier=entry["backoff"], rationale=rationale,
                              nudge=bool(entry["nudge"]) and action is not Action.TOKEN_RETRY)
    except Exception as exc:  # never raises: an unexpected input in a money path goes to a person, not to a retry
        return _human_queue(f"policy error ({type(exc).__name__}); a person decides rather than the agent guessing.")


def human_delay(seconds: int) -> str:
    if seconds <= 0:
        return "now"
    if seconds % HOUR == 0:
        return f"{seconds // HOUR}h"
    if seconds % MINUTE == 0:
        return f"{seconds // MINUTE}m"
    return f"{seconds}s"


def policy_table(overrides: "MerchantPolicy | None" = None) -> list[dict]:
    """The table in spec order, for README/ARCHITECTURE docs. With `overrides` it is one merchant's
    effective table (merchants.effective_table(merchant_id)); "override" names the fields a file changed."""
    rows = []
    for cls in POLICY:
        e = effective_entry(cls, overrides)
        rows.append({
            "failure_class": cls.value,
            "action": (Action.HUMAN_QUEUE if e.get("no_token_action_disabled") else e["action"]).value,
            "action_with_token": (e["token_action"] or e["action"]).value,
            "delay": human_delay(e["delay_seconds"]),
            "delay_seconds": e["delay_seconds"],
            "backoff_multiplier": e["backoff"],
            "max_attempts": e["max_attempts"],
            "nudge": e["nudge"],
            "rationale": e["rationale"],
            "override": ", ".join(e.get("overridden", [])),
        })
    return rows
