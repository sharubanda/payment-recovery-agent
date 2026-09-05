"""The template floor in every language: every class x channel x language renders with the
placeholders filled, the amount and link present, and the SMS budget its script allows."""
import re
from types import SimpleNamespace

import pytest

from app import nudge_templates as t
from app.nudge_templates import (SMS_MAX_CHARS, SMS_MAX_CHARS_UNICODE, SUBJECT_MAX_CHARS, is_latin_script,
                                 normalise_language, plain_phrase, preferred_language, sms_limit_for, template_nudge)
from app.taxonomy import Action, FailureClass

LINK = "https://rzp.io/i/AbCdEfGh"
AMOUNT = "Rs 2,499.00"
ORDER = "order_ct86nidKocRa56"
DEVANAGARI = re.compile(r"[ऀ-ॿ]")
FORBIDDEN = ("risk", "fraud", "suspic", "block", "refund", "guarantee", "within 24")


def attempt(**overrides):
    base = dict(order_id=ORDER, amount_paise=249900, customer_name="Priya Sharma", customer_contact="+919876543210",
                customer_email="priya.sharma@example.com")
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.mark.parametrize("language", t.SUPPORTED_LANGUAGES)
@pytest.mark.parametrize("channel", ["sms", "email"])
@pytest.mark.parametrize("failure_class", list(FailureClass))
def test_every_class_channel_language_renders_within_budget(failure_class, channel, language):
    a = attempt(customer_contact="+919876543210" if channel == "sms" else None)
    action = Action.NUDGE_CHANGE_METHOD if failure_class is FailureClass.HARD_DECLINE else Action.RECOVERY_LINK
    n = template_nudge(a, failure_class, action, LINK, language=language)
    assert n.channel == channel and n.source == "template" and n.fallback_taken is None
    assert "{" not in n.body and "}" not in n.body and "{" not in n.subject and "None" not in n.body
    assert AMOUNT in n.body and ORDER in n.body and "Priya" in n.body
    for word in FORBIDDEN:
        assert word not in n.body.lower()
    if channel == "sms":
        assert n.subject == ""
        assert len(n.body) <= sms_limit_for(n.body)
        assert len(n.body) <= (SMS_MAX_CHARS_UNICODE if language == "hi" else SMS_MAX_CHARS)
    else:
        assert 0 < len(n.subject) <= SUBJECT_MAX_CHARS and AMOUNT in n.subject
        assert n.body.count("\n\n") >= 3  # greeting / cause / cta / already-paid / thanks
    if language == "hi":
        assert DEVANAGARI.search(n.body) and DEVANAGARI.search(n.subject or "नमस्ते")
        assert not is_latin_script(n.body)
    else:
        assert is_latin_script(n.body) and not DEVANAGARI.search(n.body)
    if failure_class in (FailureClass.RISK_BLOCKED, FailureClass.UNKNOWN):
        assert LINK not in n.body
    else:
        assert LINK in n.body
        if failure_class is FailureClass.HARD_DECLINE:
            assert "card" in n.body.lower() or "कार्ड" in n.body


def test_hindi_sms_keeps_the_cause_for_a_normal_order_id_and_link():
    for cls in (FailureClass.INSUFFICIENT_FUNDS, FailureClass.LIMIT_EXCEEDED, FailureClass.NETWORK_TIMEOUT):
        n = template_nudge(attempt(), cls, Action.RECOVERY_LINK, LINK, language="hi")
        assert n.body.startswith("नमस्ते Priya,") and plain_phrase(cls, "hi") in n.body and len(n.body) <= 200


def test_sms_compacts_by_dropping_greeting_then_cause_before_the_hard_cut():
    long_link = "https://rzp.io/i/" + "x" * 40
    n = template_nudge(attempt(customer_name="Priyamvada"), FailureClass.LIMIT_EXCEEDED, Action.RECOVERY_LINK, long_link,
                       language="hi")
    assert len(n.body) <= SMS_MAX_CHARS_UNICODE and long_link in n.body and "नमस्ते" not in n.body
    n = template_nudge(attempt(order_id="order_" + "y" * 40), FailureClass.LIMIT_EXCEEDED, Action.RECOVERY_LINK, long_link,
                       language="hi")
    assert len(n.body) <= SMS_MAX_CHARS_UNICODE and long_link in n.body and "पूरा नहीं हो सका" in n.body
    n = template_nudge(attempt(customer_name="A" * 200), FailureClass.NETWORK_TIMEOUT, Action.RECOVERY_LINK,
                       "https://rzp.io/i/" + "x" * 60, language="hinglish")
    assert len(n.body) <= SMS_MAX_CHARS and "rzp.io" in n.body


def test_english_is_the_fallback_per_piece_when_a_language_lacks_a_class(monkeypatch):
    partial = dict(t._LANG["hi"])
    partial["phrases"] = {k: v for k, v in t._LANG["hi"]["phrases"].items() if k is not FailureClass.ISSUER_DOWN}
    partial["subjects"] = {}
    monkeypatch.setitem(t._LANG, "hi", partial)
    n = template_nudge(attempt(customer_contact=None), FailureClass.ISSUER_DOWN, Action.RECOVERY_LINK, LINK, language="hi")
    assert t.PLAIN_PHRASES[FailureClass.ISSUER_DOWN] in n.body and "नमस्ते Priya," in n.body  # cause: en; greeting: hi
    assert n.subject == "Complete your payment of Rs 2,499.00"
    assert plain_phrase(FailureClass.ISSUER_DOWN, "hi") == t.PLAIN_PHRASES[FailureClass.ISSUER_DOWN]
    n = template_nudge(attempt(), FailureClass.ISSUER_DOWN, Action.RECOVERY_LINK, LINK, language="xx")  # unknown -> en
    assert n.body.startswith("Hi Priya,")


def test_preferred_language_resolution_order(monkeypatch):
    monkeypatch.delenv("NUDGE_LANGUAGE_DEFAULT", raising=False)
    assert preferred_language(attempt()) == "en"
    assert preferred_language(attempt(customer_language="hi")) == "hi"
    assert preferred_language(attempt(customer_language="hi"), "hinglish") == "hinglish"
    assert preferred_language(attempt(customer_language="klingon"), "martian") == "en"
    assert preferred_language(None, "HI-in") == "hi" and preferred_language(SimpleNamespace(), "Hindi") == "hi"
    monkeypatch.setenv("NUDGE_LANGUAGE_DEFAULT", "hinglish")
    assert preferred_language(attempt()) == "hinglish"
    assert preferred_language(attempt(customer_language="en_IN")) == "en"
    monkeypatch.setattr(t.config, "NUDGE_LANGUAGE_DEFAULT", "hi", raising=False)
    assert preferred_language(attempt()) == "hi"  # a declared config attribute beats the environment
    assert normalise_language("hi-Latn") == "hinglish" and normalise_language("") is None and normalise_language("fr") is None


def test_sms_limit_follows_the_script():
    assert sms_limit_for("Hi Priya, Rs 2,499.00") == SMS_MAX_CHARS
    assert sms_limit_for("café") == SMS_MAX_CHARS            # Latin-1 still GSM-7 territory
    assert sms_limit_for("नमस्ते") == SMS_MAX_CHARS_UNICODE
    assert sms_limit_for("₹ 2,499") == SMS_MAX_CHARS_UNICODE   # the rupee sign alone forces UCS-2
    assert sms_limit_for("don’t") == SMS_MAX_CHARS_UNICODE     # so does a curly quote
    assert is_latin_script("") is True


def test_plain_phrase_matches_what_the_template_says():
    for lang in t.SUPPORTED_LANGUAGES:
        for cls in FailureClass:
            n = template_nudge(attempt(customer_contact=None), cls, Action.RECOVERY_LINK, LINK, language=lang)
            assert plain_phrase(cls, lang) in n.body
