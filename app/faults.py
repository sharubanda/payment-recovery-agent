"""Fault injection registry for the chaos harness (scripts/chaos.py).

Faults are injected at the *client boundary* (the LLM client, the Razorpay client,
the DB engine) — never inside business logic — so a fault exercises the exact
fallback path production would take. Nothing in this module runs unless a fault
is activated explicitly (in-process) or via PRA_FAULTS=name1,name2 (subprocess).

Each fault name below MUST be reachable and demonstrated by `python scripts/chaos.py --all`.
"""
import os

FAULTS: tuple[str, ...] = (
    "llm_timeout",             # LLM call raises a timeout -> classification falls to human_queue; nudge falls to template
    "llm_bad_json",            # LLM returns malformed JSON -> one repair attempt -> human_queue / template
    "llm_hallucinated_class",  # LLM returns a class outside the enum -> rejected -> human_queue
    "razorpay_429",            # Payment Links API rate-limits twice, then succeeds -> exponential backoff, link created
    "razorpay_5xx",            # Payment Links API 5xx every time -> job marked failed after bounded retries, no duplicate link
    "duplicate_event",         # the same failed-payment event is delivered twice -> second is skipped_duplicate, one link total
    "unknown_error_code",      # error object matches no rule and LLM is unavailable -> human_queue, never a guess
    "db_unavailable",          # DB write fails before any outbound call -> event NOT acknowledged, zero links created
    "order_paid_elsewhere",    # the order is paid another way: before execution -> no_action, zero links; after a link
                               # was sent -> the link is cancelled, the job is cancelled, the outcome is written
)

_active: set[str] = set()


def _load_env() -> None:
    raw = os.getenv("PRA_FAULTS", "")
    for name in (n.strip() for n in raw.split(",")):
        if name:
            activate(name)


def activate(name: str) -> None:
    if name not in FAULTS:
        raise ValueError(f"unknown fault {name!r}; known: {', '.join(FAULTS)}")
    _active.add(name)


def deactivate(name: str) -> None:
    _active.discard(name)


def clear() -> None:
    _active.clear()


def is_active(name: str) -> bool:
    return name in _active


def active() -> frozenset[str]:
    return frozenset(_active)


_load_env()
