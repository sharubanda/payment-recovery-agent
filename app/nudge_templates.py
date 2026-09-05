"""Static customer messages: one per failure class per channel per language.

These are the floor under llm.draft_nudge: any failure there (timeout, bad JSON, a body
that broke a rule) lands here, and a recovery is never blocked on message generation.
They are also the ONLY message for RISK_BLOCKED, UNKNOWN and a human-queued action: the
customer must never be told about a risk flag, and when a person owns the next step the
honest message is "we will be in touch", with no link.

Every body is composed from a per-class intro plus one call-to-action, so a link that
was never created (executor failure) still yields a coherent message rather than a
dangling "click here:". Placeholders: {name}, {amount}, {order_id}, {link}.

Languages: "en" (Indian English), "hi" (Hindi, Devanagari) and "hinglish" (romanised
Hindi). Resolution is preferred_language(): an explicit argument, then the attempt's
customer_language, then NUDGE_LANGUAGE_DEFAULT. English is the fallback of last resort
for any class a language has no template for, per lookup, so a partial set never breaks
a message.

SMS length: carriers encode a message with only Latin characters in GSM-7 (160 chars per
segment, 153 when concatenated), so 300 characters is about two segments. Any character
outside Basic Latin / Latin-1 (Devanagari, the rupee sign, curly quotes) forces UCS-2 at
70 chars per segment (67 concatenated), so a Devanagari body is capped at 200 characters
(about three segments). sms_limit_for(body) applies whichever rule the body's script needs.
"""
import os

from . import config
from .taxonomy import Action, FailureClass, Nudge, coerce

SMS_MAX_CHARS = 300          # Latin-script body: GSM-7, ~2 concatenated segments
SMS_MAX_CHARS_UNICODE = 200  # any non-Latin character: UCS-2 at 70 chars/segment, ~3 segments
SUBJECT_MAX_CHARS = 60       # what mobile mail clients show before truncating

SUPPORTED_LANGUAGES = ("en", "hi", "hinglish")
DEFAULT_LANGUAGE = "en"
_LANGUAGE_ALIASES = {"english": "en", "en-in": "en", "en_in": "en", "hindi": "hi", "hi-in": "hi", "hi_in": "hi",
                     "hin": "hi", "romanised-hindi": "hinglish", "romanized-hindi": "hinglish", "hi-latn": "hinglish"}

# Actions that mean a person or nobody acts next: the customer gets the neutral message.
NEUTRAL_ACTIONS = frozenset({Action.HUMAN_QUEUE, Action.NO_ACTION, Action.TOKEN_RETRY})
# Classes the customer is never given a cause for.
NEUTRAL_CLASSES = frozenset({FailureClass.RISK_BLOCKED, FailureClass.UNKNOWN})

# What happened, in words a customer can read. Shared with llm.py so the model and the
# template say the same thing about the same failure. English; see plain_phrase() for others.
PLAIN_PHRASES: dict[FailureClass, str] = {
    FailureClass.INSUFFICIENT_FUNDS: "did not go through because the account did not have enough balance at the time",
    FailureClass.ISSUER_DOWN: "did not go through because your bank was temporarily unavailable",
    FailureClass.AUTH_ABANDONED: "was not completed because the OTP or approval step was left unfinished",
    FailureClass.HARD_DECLINE: "could not be completed because the card could not be used for this payment",
    FailureClass.NETWORK_TIMEOUT: "did not go through because the connection timed out before the bank could confirm it",
    FailureClass.LIMIT_EXCEEDED: "did not go through because a transaction limit on the account or card had been reached for the day",
    FailureClass.RISK_BLOCKED: "could not be completed",
    FailureClass.UNKNOWN: "could not be completed",
}

# One template set per language. Keys: greeting / greeting_anon, phrases (per class), cta,
# sms / sms_compact / sms_minimal (progressively shorter, tried in order against the SMS
# budget), email_body, subjects (per class). HARD_DECLINE is the one class where retrying
# the same instrument is pointless, so its CTA asks for a different method.
_LANG: dict[str, dict] = {
    "en": {
        "greeting": "Hi {name},",
        "greeting_anon": "Hi,",
        "phrases": PLAIN_PHRASES,
        "cta": {
            "link": "You can complete it in a minute using this secure link: {link}",
            "no_link": "You can complete it by retrying from the checkout page.",
            "change_method_link": "Please use a different card or payment method via this secure link: {link}",
            "change_method_no_link": "Please retry from the checkout page with a different card or payment method.",
            "neutral": "Our team will get in touch with you shortly; no action is needed from you right now.",
        },
        "sms": "{greeting} your payment of {amount} for order {order_id} {what}. {cta}",
        "sms_compact": "Your payment of {amount} for order {order_id} {what}. {cta}",
        "sms_minimal": "Your payment of {amount} for order {order_id} could not be completed. {cta}",
        "email_body": (
            "{greeting}\n\n"
            "Your payment of {amount} for order {order_id} {what}.\n\n"
            "{cta}\n\n"
            "If you have already paid, please ignore this message.\n\n"
            "Thank you."
        ),
        "subjects": {
            FailureClass.INSUFFICIENT_FUNDS: "Complete your payment of {amount}",
            FailureClass.ISSUER_DOWN: "Complete your payment of {amount}",
            FailureClass.AUTH_ABANDONED: "Complete your payment of {amount}",
            FailureClass.HARD_DECLINE: "Action needed: payment of {amount} not completed",
            FailureClass.NETWORK_TIMEOUT: "Complete your payment of {amount}",
            FailureClass.LIMIT_EXCEEDED: "Complete your payment of {amount}",
            FailureClass.RISK_BLOCKED: "About your payment of {amount}",
            FailureClass.UNKNOWN: "About your payment of {amount}",
        },
    },
    # Hindi, Devanagari, आप register. Plain everyday words; the amount, order id and link
    # stay in Latin script exactly as given.
    "hi": {
        "greeting": "नमस्ते {name},",
        "greeting_anon": "नमस्ते,",
        "phrases": {
            FailureClass.INSUFFICIENT_FUNDS: "खाते में पर्याप्त राशि न होने से पूरा नहीं हो सका",
            FailureClass.ISSUER_DOWN: "आपके बैंक के कुछ समय के लिए अनुपलब्ध होने से पूरा नहीं हो सका",
            FailureClass.AUTH_ABANDONED: "OTP या स्वीकृति का चरण अधूरा रहने से पूरा नहीं हो सका",
            FailureClass.HARD_DECLINE: "इस कार्ड से नहीं हो सका",
            FailureClass.NETWORK_TIMEOUT: "बैंक की पुष्टि से पहले कनेक्शन टूटने से पूरा नहीं हो सका",
            FailureClass.LIMIT_EXCEEDED: "खाते या कार्ड की आज की लेन-देन सीमा पूरी होने से पूरा नहीं हो सका",
            FailureClass.RISK_BLOCKED: "पूरा नहीं हो सका",
            FailureClass.UNKNOWN: "पूरा नहीं हो सका",
        },
        "cta": {
            "link": "इस सुरक्षित लिंक से पूरा करें: {link}",
            "no_link": "आप चेकआउट पेज से दोबारा प्रयास करके इसे पूरा कर सकते हैं।",
            "change_method_link": "कृपया दूसरे कार्ड या भुगतान माध्यम से यहाँ पूरा करें: {link}",
            "change_method_no_link": "कृपया चेकआउट पेज से दूसरे कार्ड या भुगतान माध्यम से दोबारा प्रयास करें।",
            "neutral": "हमारी टीम जल्द ही आपसे संपर्क करेगी; अभी आपको कुछ करने की ज़रूरत नहीं है।",
        },
        "sms": "{greeting} ऑर्डर {order_id} के लिए आपका {amount} का भुगतान {what}। {cta}",
        "sms_compact": "ऑर्डर {order_id} के लिए आपका {amount} का भुगतान {what}। {cta}",
        "sms_minimal": "ऑर्डर {order_id} के लिए आपका {amount} का भुगतान पूरा नहीं हो सका। {cta}",
        "email_body": (
            "{greeting}\n\n"
            "ऑर्डर {order_id} के लिए आपका {amount} का भुगतान {what}।\n\n"
            "{cta}\n\n"
            "यदि आपने भुगतान पहले ही कर दिया है, तो कृपया इस संदेश को अनदेखा करें।\n\n"
            "धन्यवाद।"
        ),
        "subjects": {
            FailureClass.INSUFFICIENT_FUNDS: "अपना {amount} का भुगतान पूरा करें",
            FailureClass.ISSUER_DOWN: "अपना {amount} का भुगतान पूरा करें",
            FailureClass.AUTH_ABANDONED: "अपना {amount} का भुगतान पूरा करें",
            FailureClass.HARD_DECLINE: "ज़रूरी: {amount} का भुगतान पूरा नहीं हुआ",
            FailureClass.NETWORK_TIMEOUT: "अपना {amount} का भुगतान पूरा करें",
            FailureClass.LIMIT_EXCEEDED: "अपना {amount} का भुगतान पूरा करें",
            FailureClass.RISK_BLOCKED: "आपके {amount} के भुगतान के बारे में",
            FailureClass.UNKNOWN: "आपके {amount} के भुगतान के बारे में",
        },
    },
    # Hinglish: romanised Hindi in Latin script, aap register, the everyday English loanwords
    # (payment, order, card, link) kept as people actually say them.
    "hinglish": {
        "greeting": "Namaste {name},",
        "greeting_anon": "Namaste,",
        "phrases": {
            FailureClass.INSUFFICIENT_FUNDS: "poora nahi ho saka kyunki us samay account mein paryapt balance nahi tha",
            FailureClass.ISSUER_DOWN: "poora nahi ho saka kyunki aapka bank kuchh samay ke liye uplabdh nahi tha",
            FailureClass.AUTH_ABANDONED: "poora nahi ho saka kyunki OTP ya approval ka step adhoora reh gaya",
            FailureClass.HARD_DECLINE: "poora nahi ho saka kyunki yeh card is payment ke liye istemaal nahi ho saka",
            FailureClass.NETWORK_TIMEOUT: "poora nahi ho saka kyunki bank ki pushti se pehle connection toot gaya",
            FailureClass.LIMIT_EXCEEDED: "poora nahi ho saka kyunki account ya card ki aaj ki transaction limit poori ho chuki thi",
            FailureClass.RISK_BLOCKED: "poora nahi ho saka",
            FailureClass.UNKNOWN: "poora nahi ho saka",
        },
        "cta": {
            "link": "Aap is secure link se ise ek minute mein poora kar sakte hain: {link}",
            "no_link": "Aap checkout page se dobara try karke ise poora kar sakte hain.",
            "change_method_link": "Kripya is secure link se kisi doosre card ya payment method ka istemaal karein: {link}",
            "change_method_no_link": "Kripya checkout page se kisi doosre card ya payment method se dobara try karein.",
            "neutral": "Hamari team jald hi aapse sampark karegi; abhi aapko kuchh karne ki zaroorat nahi hai.",
        },
        "sms": "{greeting} order {order_id} ke liye aapka {amount} ka payment {what}. {cta}",
        "sms_compact": "Order {order_id} ke liye aapka {amount} ka payment {what}. {cta}",
        "sms_minimal": "Order {order_id} ke liye aapka {amount} ka payment poora nahi ho saka. {cta}",
        "email_body": (
            "{greeting}\n\n"
            "Order {order_id} ke liye aapka {amount} ka payment {what}.\n\n"
            "{cta}\n\n"
            "Agar aapne payment pehle hi kar diya hai, to kripya is message ko ignore karein.\n\n"
            "Dhanyavaad."
        ),
        "subjects": {
            FailureClass.INSUFFICIENT_FUNDS: "Apna {amount} ka payment poora karein",
            FailureClass.ISSUER_DOWN: "Apna {amount} ka payment poora karein",
            FailureClass.AUTH_ABANDONED: "Apna {amount} ka payment poora karein",
            FailureClass.HARD_DECLINE: "Zaroori: {amount} ka payment poora nahi hua",
            FailureClass.NETWORK_TIMEOUT: "Apna {amount} ka payment poora karein",
            FailureClass.LIMIT_EXCEEDED: "Apna {amount} ka payment poora karein",
            FailureClass.RISK_BLOCKED: "Aapke {amount} ke payment ke baare mein",
            FailureClass.UNKNOWN: "Aapke {amount} ke payment ke baare mein",
        },
    },
}


def _setting(name: str, default: str) -> str:
    """config.py is read-only for this module: a declared attribute wins, then the environment
    (so the documented env name works before config.py declares it), then the default."""
    value = getattr(config, name, None)
    if value in (None, ""):
        value = os.getenv(name, "")
    return str(value).strip() or default


def normalise_language(value) -> str | None:
    """"hi-IN" -> "hi", "Hindi" -> "hi", "en_IN" -> "en"; None for anything unsupported."""
    key = str(value or "").strip().lower().replace("_", "-")
    if not key:
        return None
    key = _LANGUAGE_ALIASES.get(key, key)
    if key in SUPPORTED_LANGUAGES:
        return key
    base = key.split("-")[0]
    return base if base in SUPPORTED_LANGUAGES else None


def preferred_language(attempt: object, language: str | None = None) -> str:
    """Explicit argument > attempt.customer_language > NUDGE_LANGUAGE_DEFAULT > "en".
    Unsupported values at any level fall through to the next, never to an error."""
    for candidate in (language, getattr(attempt, "customer_language", None),
                      _setting("NUDGE_LANGUAGE_DEFAULT", DEFAULT_LANGUAGE)):
        resolved = normalise_language(candidate)
        if resolved:
            return resolved
    return DEFAULT_LANGUAGE


def is_latin_script(text: str) -> bool:
    """True when every character is Basic Latin or Latin-1 (what fits GSM-7, near enough)."""
    return all(ord(ch) <= 0xFF for ch in text)


def sms_limit_for(body: str) -> int:
    """300 characters for a Latin-script body (GSM-7), 200 as soon as any character needs
    UCS-2 (Devanagari, the rupee sign, curly quotes): see the module docstring."""
    return SMS_MAX_CHARS if is_latin_script(body) else SMS_MAX_CHARS_UNICODE


def _lookup(language: str, key: str, cls: FailureClass | None = None):
    """A template piece for the language, falling back to English per piece (never per message)."""
    for lang in (language, DEFAULT_LANGUAGE):
        table = _LANG.get(lang, {}).get(key)
        if table is None:
            continue
        if cls is None:
            return table
        if cls in table:
            return table[cls]
    return _LANG[DEFAULT_LANGUAGE][key] if cls is None else _LANG[DEFAULT_LANGUAGE][key][cls]


def plain_phrase(failure_class: FailureClass | str, language: str | None = None) -> str:
    """The customer-facing cause for a class in the language (English if missing)."""
    cls = coerce(failure_class, FailureClass, FailureClass.UNKNOWN)
    return _lookup(normalise_language(language) or DEFAULT_LANGUAGE, "phrases", cls)


def format_rupees(paise) -> str:
    """249900 -> "Rs 2,499.00"; 12345678900 -> "Rs 12,34,56,789.00" (Indian grouping).

    "Rs" rather than the rupee sign because not every SMS gateway carries it intact (and
    the sign alone would push a message from GSM-7 to UCS-2). Never raises: a bad amount
    renders as Rs 0.00 rather than blocking a message.
    """
    try:
        total = int(paise or 0)
    except (TypeError, ValueError):
        total = 0
    sign = "-" if total < 0 else ""
    rupees, p = divmod(abs(total), 100)
    digits = str(rupees)
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        digits = ",".join(groups + [tail])
    return f"{sign}Rs {digits}.{p:02d}"


def first_name(name: str | None) -> str:
    return (str(name or "").strip().split() or [""])[0]


def channel_for(attempt: object) -> str:
    """SMS when we have a phone number (opened faster, no spam folder), else email."""
    return "sms" if getattr(attempt, "customer_contact", None) else "email"


def is_neutral(failure_class: FailureClass | str, action: Action | str) -> bool:
    """True when the customer must not be told a cause: no link, no LLM, the neutral message."""
    return failure_class in NEUTRAL_CLASSES or action in NEUTRAL_ACTIONS


def template_nudge(attempt: object, failure_class: FailureClass | str, action: Action | str,
                   link_url: str | None, *, language: str | None = None) -> Nudge:
    """Deterministic message for (class, action, channel, language). Never raises.

    HARD_DECLINE asks for a different payment method; RISK_BLOCKED, UNKNOWN and any
    human-queued / no-action / token-retry action get the neutral message with no link.
    Everything else explains the cause in plain words and carries the link if there is one.
    An SMS is tried full, then without the greeting, then without the cause, against the
    budget its script allows; only an absurd order id or link reaches the final hard cut.
    """
    try:
        cls = coerce(failure_class, FailureClass, FailureClass.UNKNOWN)
        act = coerce(action, Action, Action.HUMAN_QUEUE)
        lang = preferred_language(attempt, language)
        channel = channel_for(attempt)
        name = first_name(getattr(attempt, "customer_name", None))
        greeting = _lookup(lang, "greeting") if name else _lookup(lang, "greeting_anon")
        fields = {
            "name": name,
            "greeting": greeting.format(name=name),
            "amount": format_rupees(getattr(attempt, "amount_paise", 0)),
            "order_id": str(getattr(attempt, "order_id", None) or "your order"),
            "what": _lookup(lang, "phrases", cls),
        }
        cta = _lookup(lang, "cta")
        if is_neutral(cls, act):
            text = cta["neutral"]
        elif cls is FailureClass.HARD_DECLINE or act is Action.NUDGE_CHANGE_METHOD:
            text = cta["change_method_link"] if link_url else cta["change_method_no_link"]
        else:
            text = cta["link"] if link_url else cta["no_link"]
        fields["cta"] = text.format(link=link_url or "")

        if channel == "sms":
            body = ""
            for key in ("sms", "sms_compact", "sms_minimal"):
                body = _lookup(lang, key).format(**fields)
                if len(body) <= sms_limit_for(body):
                    break
            return Nudge(channel="sms", subject="", body=body[:sms_limit_for(body)], source="template")

        subject = _lookup(lang, "subjects", cls).format(**fields)[:SUBJECT_MAX_CHARS]
        return Nudge(channel="email", subject=subject, body=_lookup(lang, "email_body").format(**fields),
                     source="template")
    except Exception as exc:  # never raises: a message must never block a recovery
        return Nudge(channel="email", subject="About your payment",
                     body="Your payment could not be completed. Our team will get in touch with you shortly.",
                     source="template", fallback_taken=f"template_error({type(exc).__name__})->neutral")
