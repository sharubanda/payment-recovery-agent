"""Deterministic classification of Razorpay's error object. No LLM in this file, on purpose.

Razorpay's error codes, sources, steps and reasons are a finite enumerated set, so a
lookup beats a model: tens of microseconds (measured once, see ARCHITECTURE.md D1), $0, and the
same answer every time. Rules key on the
structured fields first and on description keywords second, and they are ORDERED:
a risk flag must win over anything else, and a hard decline must win over a soft
signal in the same description. Anything unmatched is UNKNOWN by design; the LLM and
then a human take it from there. A generic "declined by the bank" is deliberately
UNKNOWN: soft declines are often insufficient funds or issuer flakiness, and guessing
either way in a money path is worse than asking.

The rules are scored against the hand-labelled set in tests/fixtures/failure_cases.json by
scripts/eval_classify.py (report: docs/classifier_eval.md); tests/test_eval_classify.py
asserts that no case there lands on a wrong class (UNKNOWN is allowed, wrong is not).
"""
import re
from typing import Callable, NamedTuple

from .taxonomy import Classification, FailureClass


class Signals(NamedTuple):
    """The five fields every rule sees, lowercased and whitespace-normalised."""
    code: str
    source: str
    step: str
    reason: str
    desc: str

    def has(self, *patterns: str) -> bool:
        return any(re.search(p, self.desc) for p in patterns)


# Keyword groups are regexes anchored at a word start so "otp" cannot match inside
# "footprint" and "risk" still matches "risky" but not "brisk". Stems ("expir", "block") are
# intentional. NOT_ is a negation guard: "card is not blocked" must not read as a blocked card.
NOT_ = r"(?<!not )"
RISK_WORDS = (r"\brisk", r"\bfraud", r"\bsuspic", r"\bsuspect", r"\bchargeback", r"\bvelocity\b", r"\bblacklist")
HARD_WORDS = (NOT_ + r"\bblock", NOT_ + r"\bexpir", NOT_ + r"\binvalid\b", r"\bnot enabled\b", r"\bnot activated\b",
              NOT_ + r"\brestrict", NOT_ + r"\blost\b", NOT_ + r"\bstolen\b", r"\binternational\b", r"\bnot permitted\b")
HARD_CARD_PHRASES = (
    r"\bcard\b.{0,40}" + NOT_ + r"\b(?:blocked|expired|invalid|restricted|lost|stolen|not enabled|not activated)\b",
    NOT_ + r"\b(?:blocked|expired|invalid|restricted|lost|stolen)\b.{0,10}\bcard\b",
    r"\bnot enabled for\b",
    r"\binternational (?:transactions?|usage|payments?)\b",
    r"\binternational cards?\b.{0,20}\bnot (?:supported|accepted|allowed|enabled)\b",  # merchant has no international
    r"\bnot permitted to (?:the )?cardholder\b",                                        # ISO 8583 code 57
    r"\bmandate\b.{0,30}\b(?:cancelled|canceled|revoked|expired|inactive|not active|rejected)\b",  # a dead mandate is a dead instrument
    r"\b(?:cancelled|canceled|revoked|expired|inactive)\b.{0,10}\bmandate\b",
)
# "balance" alone is not enough ("balance confirmation could not be obtained" is the bank, not the customer):
# it needs a quantity word next to it. A credit limit is the card's balance (ISO 8583 code 51 is literally
# "insufficient funds / over credit limit") and recovers on the billing cycle, so it lands here, not on LIMIT_EXCEEDED.
INSUFFICIENT_WORDS = (r"\binsufficient\b", r"\bnot enough (?:funds|money|balance)\b",
                      r"\b(?:low|zero|nil) (?:\w+ )?balance\b",
                      r"\bbalance\b.{0,25}\b(?:insufficient|too low|not enough|not sufficient|exhausted|nahi)\b",
                      r"\bcredit limit\b",
                      r"\baparyapt\b", r"\bparyapt\b.{0,30}\bnahi\b")  # Hinglish: "paryapt ... nahi" = not enough
# A customer-side transaction limit: daily / per-transaction / spending / withdrawal caps, ISO 8583 codes 61 and 65.
LIMIT_PHRASES = (
    r"\b(?:transaction|txn|daily|per[- ]transaction|spending|spend|withdrawal|transfer|card|upi|monthly|weekly|"
    r"purchase|debit|amount|payment) (?:amount )?limit\b",
    r"\blimit (?:has been |is |was )?(?:exceeded|reached|crossed|exhausted|breached|khatam)\b",
    r"\bexceeds? (?:the |your )?(?:\w+ ){0,3}limit\b",
    r"\b(?:over|above) (?:the |your )?(?:\w+ ){0,2}limit\b",
)
NOT_A_CUSTOMER_LIMIT = (r"\brate[ -]?limit", r"\btime limit\b", r"\bcredit limit\b")
AUTH_STEP_WORDS = (r"\bcancel", r"\botp\b", r"\b3ds\b", r"\bnot completed\b", r"\btimed?[ -]?out\b",
                   r"\babandon", r"\bnot approved\b", r"\bclosed\b", r"\bdid not (?:complete|approve|enter)\b",
                   r"\bincorrect\b", r"\bwrong\b", r"\binvalid\b")
AUTH_ANY_STEP_PHRASES = (r"\botp\b", r"\b3ds\b", r"\babandon", r"\bcancel+ed by (?:the )?(?:customer|user)\b",
                         r"\bnot approved by (?:the )?(?:customer|user)\b", r"\bdid not approve\b",
                         r"\bcollect request (?:expired|declined|timed out)\b",
                         r"\bdeclined by (?:the )?(?:customer|user|payer)\b")
ISSUER_WORDS = (r"\bdown\b", r"\bunavailable\b", r"\btechnical\b", r"\boutage\b", r"\bmaintenance\b",
                r"\bnot (?:responding|reachable|available)\b", r"\bunreachable\b", r"\bfailed to respond\b",
                r"\binoperative\b",                                                 # ISO 8583 code 91; 96 "system malfunction" stays UNKNOWN on purpose
                r"\bunable to process\b", r"\btemporarily (?:unavailable|unable|down|not available|out of service)\b",
                r"\bkaam nahi kar rah[ae]\b")                                        # Hinglish: "is not working"
BANK_WORDS = (r"\bbank\b", r"\bissuer\b", r"\bnet ?banking\b")
TIMEOUT_WORDS = (r"\btime[ -]?out\b", r"\btimed[ -]?out\b", r"\bno response\b", r"\bnetwork\b",
                 r"\bconnection (?:reset|refused|error|failed)\b", r"\btemporary (?:issue|error|problem|glitch)\b")

AUTH_STEP = "payment_authentication"


class Rule(NamedTuple):
    name: str
    failure_class: FailureClass
    predicate: Callable[[Signals], bool]
    description: str                  # what the predicate checks, in words, for the audit trail and docs


def _risk(s: Signals) -> bool:
    return "risk" in s.reason or "fraud" in s.reason or s.has(*RISK_WORDS)


def _hard_decline_structured(s: Signals) -> bool:
    return s.reason == "international_transaction_not_allowed"


def _hard_decline_reason(s: Signals) -> bool:
    return s.reason == "card_declined" and s.has(*HARD_WORDS)


def _hard_decline_description(s: Signals) -> bool:
    # A hard decline is an authorization-stage verdict; at authentication the customer
    # is still in the loop ("invalid OTP" is abandonment, not a dead card).
    return s.step != AUTH_STEP and s.has(*HARD_CARD_PHRASES)


def _insufficient_funds(s: Signals) -> bool:
    return "insufficient" in s.reason or s.has(*INSUFFICIENT_WORDS)


def _limit_exceeded_structured(s: Signals) -> bool:
    return s.reason == "transaction_limit_exceeded"


def _limit_exceeded_description(s: Signals) -> bool:
    return s.has(*LIMIT_PHRASES) and not s.has(*NOT_A_CUSTOMER_LIMIT)


def _auth_abandoned_structured(s: Signals) -> bool:
    return s.reason == "payment_cancelled" or (s.step == AUTH_STEP and s.reason == "payment_timed_out")


def _auth_abandoned_description(s: Signals) -> bool:
    return (s.step == AUTH_STEP and s.has(*AUTH_STEP_WORDS)) or s.has(*AUTH_ANY_STEP_PHRASES)


def _issuer_down_structured(s: Signals) -> bool:
    return s.reason in ("bank_technical_error", "bank_failure")


def _issuer_down_description(s: Signals) -> bool:
    return (s.source == "bank" or s.has(*BANK_WORDS)) and s.has(*ISSUER_WORDS)


def _network_timeout_structured(s: Signals) -> bool:
    return (s.source == "network"
            or s.reason == "payment_timed_out"
            or (s.code == "gateway_error" and s.reason == "gateway_technical_error")
            or (s.code == "server_error" and s.reason == "server_error"))


def _network_timeout_description(s: Signals) -> bool:
    return s.has(*TIMEOUT_WORDS)


# Order is the policy: first match wins. Do not sort or "tidy" this list.
RULES: list[Rule] = [
    Rule("risk_flag", FailureClass.RISK_BLOCKED, _risk,
         "reason mentions risk/fraud (e.g. payment_risk_check_failed) or the description carries a risk, fraud, suspicious, velocity, blacklist or chargeback flag"),
    Rule("hard_decline_structured", FailureClass.HARD_DECLINE, _hard_decline_structured,
         "reason is international_transaction_not_allowed"),
    Rule("hard_decline_reason", FailureClass.HARD_DECLINE, _hard_decline_reason,
         "reason is card_declined and the description says blocked, expired, invalid, not enabled, restricted, lost, stolen, international or not permitted (and not 'not blocked')"),
    Rule("hard_decline_description", FailureClass.HARD_DECLINE, _hard_decline_description,
         "outside the authentication step, the description names a blocked, expired, invalid, restricted, lost or stolen card, a card not enabled for the transaction, international cards not supported, a transaction not permitted to the cardholder, or a cancelled/expired/revoked mandate"),
    Rule("insufficient_funds", FailureClass.INSUFFICIENT_FUNDS, _insufficient_funds,
         "reason mentions insufficient funds or the description says insufficient, not enough funds, low balance, balance too low, credit limit, or the Hinglish paryapt/aparyapt"),
    Rule("limit_exceeded_structured", FailureClass.LIMIT_EXCEEDED, _limit_exceeded_structured,
         "reason is transaction_limit_exceeded"),
    Rule("limit_exceeded_description", FailureClass.LIMIT_EXCEEDED, _limit_exceeded_description,
         "the description names a transaction, daily, per-transaction, spending, withdrawal, transfer, card or UPI limit, or says a limit was exceeded/reached/exhausted or the amount exceeds a limit; never a rate limit, time limit or credit limit"),
    Rule("auth_abandoned_structured", FailureClass.AUTH_ABANDONED, _auth_abandoned_structured,
         "reason is payment_cancelled, or the payment timed out at the authentication step"),
    Rule("auth_abandoned_description", FailureClass.AUTH_ABANDONED, _auth_abandoned_description,
         "at the authentication step the description says cancelled, OTP, 3DS, not completed, timed out or similar; at any step it says the customer or payer cancelled, declined or did not approve, or the collect request expired"),
    Rule("issuer_down_structured", FailureClass.ISSUER_DOWN, _issuer_down_structured,
         "reason is bank_technical_error or bank_failure"),
    Rule("issuer_down_description", FailureClass.ISSUER_DOWN, _issuer_down_description,
         "source is bank (or the description names the bank/issuer) and it says down, unavailable, technical, outage, maintenance, not responding, inoperative, unable to process, or the Hinglish kaam nahi kar raha"),
    Rule("network_timeout_structured", FailureClass.NETWORK_TIMEOUT, _network_timeout_structured,
         "source is network, or reason is payment_timed_out outside authentication, or a GATEWAY_ERROR with reason gateway_technical_error, or a SERVER_ERROR with reason server_error"),
    Rule("network_timeout_description", FailureClass.NETWORK_TIMEOUT, _network_timeout_description,
         "the description says timeout, timed out, no response, network, connection error or a temporary issue"),
]

NO_RULE_MATCHED = "no rule matched"


def _text(value: object) -> str:
    return " ".join(str(value if value is not None else "").split()).lower()


def signals_of(code: str | None, source: str | None, step: str | None, reason: str | None,
               description: str | None) -> Signals:
    return Signals(_text(code), _text(source), _text(step), _text(reason), _text(description))


def classify_error(code: str | None, source: str | None, step: str | None, reason: str | None,
                   description: str | None) -> Classification:
    """Classify Razorpay's error object fields directly (no ORM row needed; simulate.py uses this)."""
    try:
        s = signals_of(code, source, step, reason, description)
        for rule in RULES:
            if rule.predicate(s):
                return Classification(failure_class=rule.failure_class, reason=f"rule {rule.name}: {rule.description}",
                                      source="rules", confidence=1.0)
        return Classification(failure_class=FailureClass.UNKNOWN, reason=NO_RULE_MATCHED, source="rules", confidence=1.0)
    except Exception as exc:  # never raises: a classifier crash must not stall a money path; UNKNOWN routes to a human
        return Classification(failure_class=FailureClass.UNKNOWN,
                              reason=f"classifier error ({type(exc).__name__}); treated as no rule matched",
                              source="rules", confidence=1.0, fallback_taken="classify_error->unknown")


def classify(attempt: object) -> Classification:
    """Pure and never raises. `attempt` is anything with Razorpay's error_* attributes (a PaymentAttempt row)."""
    get = lambda name: getattr(attempt, name, None)  # noqa: E731
    return classify_error(get("error_code"), get("error_source"), get("error_step"),
                          get("error_reason"), get("error_description"))


def is_mapped(attempt: object) -> bool:
    """True when a rule fired; False means the LLM (and then a human) has to look at it."""
    return classify(attempt).failure_class is not FailureClass.UNKNOWN


def rules_table() -> list[dict]:
    """The rule list in evaluation order, for README/ARCHITECTURE docs and the chaos report."""
    return [{"order": i, "rule": r.name, "failure_class": r.failure_class.value, "matches": r.description}
            for i, r in enumerate(RULES, start=1)]
