"""The two LLM call sites, with no network ever: scripted doubles, the fault registry, and
a fake Anthropic client to pin the request shape. Every fallback name asserted here is
what the audit trail and the chaos harness show."""
import json
from types import SimpleNamespace

import anthropic
import pytest

from app import config, faults
from app import llm as llm_mod
from app.llm import (CLASSIFY_SCHEMA, CLASSIFY_SYSTEM, FEW_SHOT_EXAMPLES, NUDGE_SYSTEM, AnthropicLLM, FaultingLLM,
                     LLMResponse, ScriptedLLM, build_classify_system, classify_unmapped, draft_nudge, get_client,
                     last_call_usage, select_few_shot_examples, usage_log)
from app.nudge_templates import (SMS_MAX_CHARS, SMS_MAX_CHARS_UNICODE, SUBJECT_MAX_CHARS, channel_for, format_rupees,
                                 template_nudge)
from app.taxonomy import Action, FailureClass

NAME, CONTACT, EMAIL = "Priya Sharma", "+919876543210", "priya.sharma@example.com"
LINK = "https://rzp.io/i/AbCdEfGh"
GOOD = json.dumps({"failure_class": "INSUFFICIENT_FUNDS", "confidence": 0.86,
                   "rationale": "Hinglish for the account lacking balance."})
BAD_JSON = '{"failure_class": "INSUFFICIENT_FUNDS", "confidence": 0.8'
HALLUCINATED = json.dumps({"failure_class": "CUSTOMER_MOVED_ABROAD", "confidence": 0.97, "rationale": "moved"})
LINK_CLASSES = (FailureClass.INSUFFICIENT_FUNDS, FailureClass.ISSUER_DOWN, FailureClass.AUTH_ABANDONED,
                FailureClass.HARD_DECLINE, FailureClass.NETWORK_TIMEOUT, FailureClass.LIMIT_EXCEEDED)
NEUTRAL_CLASSES = (FailureClass.RISK_BLOCKED, FailureClass.UNKNOWN)


@pytest.fixture(autouse=True)
def _no_faults():
    faults.clear()
    yield
    faults.clear()


def attempt(**overrides):
    base = dict(razorpay_payment_id="pay_TESTllm0000001", order_id="order_ct86nidKocRa56", merchant_id="merchant_acme",
                amount_paise=249900, currency="INR", method="upi", has_token=False,
                error_code="BAD_REQUEST_ERROR", error_source="bank", error_step="payment_authorization",
                error_reason="payment_failed",
                error_description="Bhugtan asafal: khaate mein paryaapt raashi nahin hai",
                customer_name=NAME, customer_contact=CONTACT, customer_email=EMAIL)
    base.update(overrides)
    return SimpleNamespace(**base)


def sms_nudge(body, subject=""):
    return json.dumps({"channel": "sms", "subject": subject, "body": body})


def good_sms_body():
    return f"Hi Priya, your payment of Rs 2,499.00 for order order_ct86nidKocRa56 did not go through. Complete it here: {LINK}"


# ---- classify_unmapped -------------------------------------------------------------------

def test_scripted_happy_path_reports_llm_source_model_and_latency():
    client = ScriptedLLM([GOOD], latency_ms=42)
    c = classify_unmapped(attempt(), client=client)
    assert c.failure_class is FailureClass.INSUFFICIENT_FUNDS
    assert (c.source, c.confidence, c.fallback_taken) == ("llm", 0.86, None)
    assert c.reason == "Hinglish for the account lacking balance."
    assert c.llm_model == "scripted-model" and isinstance(c.llm_latency_ms, int) and c.llm_latency_ms >= 0
    assert len(client.calls) == 1
    assert client.calls[0]["schema"] is CLASSIFY_SCHEMA


def test_bad_json_gets_one_repair_call_then_falls_to_human_queue():
    client = ScriptedLLM([BAD_JSON, BAD_JSON])
    c = classify_unmapped(attempt(), client=client)
    assert len(client.calls) == 2, "exactly one repair attempt"
    assert BAD_JSON in client.calls[1]["user"] and "Return only the JSON object" in client.calls[1]["user"]
    assert c.failure_class is FailureClass.UNKNOWN and c.source == "fallback" and c.confidence == 0.0
    assert c.fallback_taken == "llm_bad_json->human_queue"
    assert c.reason.startswith("llm output rejected:")
    assert c.llm_model == "scripted-model" and c.llm_latency_ms is not None


def test_bad_json_repaired_on_second_call_is_accepted():
    client = ScriptedLLM([BAD_JSON, GOOD])
    c = classify_unmapped(attempt(), client=client)
    assert len(client.calls) == 2
    assert c.failure_class is FailureClass.INSUFFICIENT_FUNDS and c.source == "llm" and c.fallback_taken is None


def test_hallucinated_class_is_rejected_not_coerced():
    client = ScriptedLLM([HALLUCINATED, HALLUCINATED])
    c = classify_unmapped(attempt(), client=client)
    assert len(client.calls) == 2
    assert c.failure_class is FailureClass.UNKNOWN and c.source == "fallback"
    assert c.fallback_taken == "llm_hallucinated_class->human_queue"
    assert "CUSTOMER_MOVED_ABROAD" in c.reason


@pytest.mark.parametrize("payload", [
    json.dumps({"failure_class": "ISSUER_DOWN", "confidence": 1.7, "rationale": "x"}),      # out of range
    json.dumps({"failure_class": "ISSUER_DOWN", "confidence": 0.9}),                        # missing field
    json.dumps({"failure_class": "ISSUER_DOWN", "confidence": 0.9, "rationale": "x", "extra": 1}),
    json.dumps(["ISSUER_DOWN"]),                                                            # not an object
])
def test_schema_violations_fall_to_human_queue(payload):
    c = classify_unmapped(attempt(), client=ScriptedLLM([payload, payload]))
    assert c.failure_class is FailureClass.UNKNOWN and c.fallback_taken == "llm_bad_json->human_queue"


def test_timeout_falls_to_human_queue_after_one_call():
    client = ScriptedLLM([TimeoutError("read timed out")])
    c = classify_unmapped(attempt(), client=client)
    assert len(client.calls) == 1, "no repair attempt after a timeout"
    assert c.failure_class is FailureClass.UNKNOWN and c.source == "fallback" and c.confidence == 0.0
    assert c.fallback_taken == "llm_timeout->human_queue"
    assert c.llm_model == "scripted-model" and isinstance(c.llm_latency_ms, int)


def test_sdk_timeout_and_api_errors_map_to_documented_fallbacks():
    api_timeout = anthropic.APITimeoutError(request=SimpleNamespace(url="https://api.anthropic.com/v1/messages"))
    assert classify_unmapped(attempt(), client=ScriptedLLM([api_timeout])).fallback_taken == "llm_timeout->human_queue"
    c = classify_unmapped(attempt(), client=ScriptedLLM([RuntimeError("boom")]))
    assert c.fallback_taken == "llm_error->human_queue" and "RuntimeError" in c.reason


@pytest.mark.parametrize("stop, expected", [("refusal", "llm_refusal->human_queue"), ("max_tokens", "llm_truncated->human_queue")])
def test_refusal_and_truncation_stop_reasons_fall_back(stop, expected):
    resp = LLMResponse(text=GOOD, model="scripted-model", latency_ms=3, stop_reason=stop)
    client = ScriptedLLM([resp])
    c = classify_unmapped(attempt(), client=client)
    assert c.failure_class is FailureClass.UNKNOWN and c.fallback_taken == expected and len(client.calls) == 1


def test_no_client_means_llm_unavailable_fallback():
    assert config.llm_live() is False and get_client() is None
    c = classify_unmapped(attempt())
    assert c.failure_class is FailureClass.UNKNOWN and c.source == "fallback"
    assert c.fallback_taken == "llm_unavailable->human_queue"
    assert c.llm_model is None and c.llm_latency_ms is None


def test_low_confidence_is_reported_honestly_not_rewritten():
    payload = json.dumps({"failure_class": "ISSUER_DOWN", "confidence": 0.4, "rationale": "maybe the bank"})
    c = classify_unmapped(attempt(), client=ScriptedLLM([payload]))
    assert c.failure_class is FailureClass.ISSUER_DOWN and c.source == "llm" and c.confidence == 0.4
    assert c.fallback_taken is None  # policy.decide turns this into human_queue, not the classifier


def test_classification_prompt_carries_error_fields_but_no_pii():
    client = ScriptedLLM([GOOD])
    a = attempt()
    classify_unmapped(a, client=client)
    sent = client.calls[0]["system"] + "\n" + client.calls[0]["user"]
    for secret in (NAME, "Priya", CONTACT, EMAIL, "9876543210"):
        assert secret not in sent
    for field in (a.error_code, a.error_source, a.error_step, a.error_reason, a.error_description, a.method, "Rs 2,499.00"):
        assert field in client.calls[0]["user"]
    for cls in FailureClass:
        assert cls.value in client.calls[0]["system"]


def test_classifier_never_raises_on_a_broken_client():
    class Broken:
        name = model = "broken"

        def complete_json(self, system, user, schema):
            return object()  # not an LLMResponse at all

    c = classify_unmapped(attempt(), client=Broken())
    assert c.failure_class is FailureClass.UNKNOWN and c.fallback_taken.endswith("->human_queue")
    c = classify_unmapped(None, client=ScriptedLLM([GOOD]))  # no attempt object at all
    assert c.failure_class is FailureClass.INSUFFICIENT_FUNDS


# ---- draft_nudge -----------------------------------------------------------------------

def test_draft_nudge_happy_path_includes_link_and_amount():
    client = ScriptedLLM([sms_nudge(good_sms_body())])
    n = draft_nudge(attempt(), FailureClass.INSUFFICIENT_FUNDS, Action.RECOVERY_LINK, LINK, client=client)
    assert n.source == "llm" and n.fallback_taken is None
    assert n.channel == "sms" and n.subject == "" and LINK in n.body and "Rs 2,499.00" in n.body
    assert len(client.calls) == 1
    user = client.calls[0]["user"]
    assert "Priya" in user and LINK in user and "Rs 2,499.00" in user
    assert CONTACT not in user and EMAIL not in user  # the channel is chosen here, not by the model


def test_draft_nudge_email_channel_when_no_contact():
    payload = json.dumps({"channel": "email", "subject": "Complete your payment of Rs 2,499.00",
                          "body": f"Hi Priya,\n\nYour payment of Rs 2,499.00 did not go through.\n\nPay here: {LINK}"})
    n = draft_nudge(attempt(customer_contact=None), FailureClass.ISSUER_DOWN, Action.RECOVERY_LINK, LINK,
                    client=ScriptedLLM([payload]))
    assert n.channel == "email" and n.source == "llm" and n.subject.startswith("Complete")


@pytest.mark.parametrize("bad, expected", [
    (sms_nudge("Hi Priya, your payment of Rs 2,499.00 did not go through. Please retry."), "llm_output_rejected->template"),  # link missing
    (sms_nudge(f"Hi Priya, your payment of Rs 2,500.00 did not go through: {LINK}"), "llm_output_rejected->template"),        # wrong amount
    (sms_nudge("x" * 290 + f" Rs 2,499.00 {LINK}"), "llm_output_rejected->template"),                                        # too long
    (json.dumps({"channel": "email", "subject": "s", "body": good_sms_body()}), "llm_output_rejected->template"),           # wrong channel
    (sms_nudge(f"Hi Priya, a risk check failed on your Rs 2,499.00 payment: {LINK}"), "llm_output_rejected->template"),     # forbidden word
    (sms_nudge(good_sms_body() + " \U0001F389"), "llm_output_rejected->template"),                                          # emoji
    ('{"channel": "sms", "subject": "", "body": "Hi', "llm_bad_json->template"),                                             # malformed
    (json.dumps({"channel": "whatsapp", "subject": "", "body": good_sms_body()}), "llm_bad_json->template"),                 # schema
])
def test_draft_nudge_rejects_bad_output_then_uses_template(bad, expected):
    client = ScriptedLLM([bad, bad])
    n = draft_nudge(attempt(), FailureClass.INSUFFICIENT_FUNDS, Action.RECOVERY_LINK, LINK, client=client)
    assert len(client.calls) == 2, "exactly one repair attempt"
    assert n.source == "template" and n.fallback_taken == expected
    t = template_nudge(attempt(), FailureClass.INSUFFICIENT_FUNDS, Action.RECOVERY_LINK, LINK)
    assert (n.channel, n.subject, n.body) == (t.channel, t.subject, t.body)
    assert LINK in n.body and "Rs 2,499.00" in n.body


def test_draft_nudge_repair_can_succeed():
    client = ScriptedLLM(["not json", sms_nudge(good_sms_body())])
    n = draft_nudge(attempt(), FailureClass.AUTH_ABANDONED, Action.RECOVERY_LINK, LINK, client=client)
    assert n.source == "llm" and len(client.calls) == 2 and "rejected" in client.calls[1]["user"]


def test_draft_nudge_timeout_and_errors_use_template():
    n = draft_nudge(attempt(), FailureClass.ISSUER_DOWN, Action.RECOVERY_LINK, LINK, client=ScriptedLLM([TimeoutError()]))
    assert n.source == "template" and n.fallback_taken == "llm_timeout->template" and LINK in n.body
    n = draft_nudge(attempt(), FailureClass.ISSUER_DOWN, Action.RECOVERY_LINK, LINK, client=ScriptedLLM([ValueError("x")]))
    assert n.fallback_taken == "llm_error->template"
    refusal = LLMResponse(text=sms_nudge(good_sms_body()), model="m", latency_ms=1, stop_reason="refusal")
    n = draft_nudge(attempt(), FailureClass.ISSUER_DOWN, Action.RECOVERY_LINK, LINK, client=ScriptedLLM([refusal]))
    assert n.fallback_taken == "llm_refusal->template"


def test_draft_nudge_without_client_uses_template():
    n = draft_nudge(attempt(), FailureClass.HARD_DECLINE, Action.NUDGE_CHANGE_METHOD, LINK)
    assert n.source == "template" and n.fallback_taken == "llm_unavailable->template"
    assert "different" in n.body and LINK in n.body


@pytest.mark.parametrize("failure_class, action", [
    (FailureClass.RISK_BLOCKED, Action.HUMAN_QUEUE),
    (FailureClass.RISK_BLOCKED, Action.RECOVERY_LINK),   # even if a caller asked for a link message
    (FailureClass.UNKNOWN, Action.HUMAN_QUEUE),
    (FailureClass.INSUFFICIENT_FUNDS, Action.HUMAN_QUEUE),
    (FailureClass.ISSUER_DOWN, Action.NO_ACTION),
    (FailureClass.ISSUER_DOWN, Action.TOKEN_RETRY),
])
def test_risk_blocked_and_human_queue_never_call_the_model(failure_class, action):
    client = ScriptedLLM([sms_nudge(good_sms_body())])
    n = draft_nudge(attempt(), failure_class, action, LINK, client=client)
    assert client.calls == []
    assert n.source == "template" and n.fallback_taken is None
    assert LINK not in n.body and "get in touch" in n.body
    for word in ("risk", "fraud", "block"):
        assert word not in n.body.lower()


def test_draft_nudge_never_raises():
    n = draft_nudge(attempt(), FailureClass.ISSUER_DOWN, Action.RECOVERY_LINK, LINK, client=ScriptedLLM([object()]))
    assert n.source == "template"
    n = draft_nudge(None, "not-a-class", "not-an-action", None, client=ScriptedLLM([GOOD]))
    assert n.source == "template" and n.channel == "email"


# ---- templates -------------------------------------------------------------------------

def test_format_rupees_indian_grouping():
    assert format_rupees(149900) == "Rs 1,499.00"
    assert format_rupees(5) == "Rs 0.05"
    assert format_rupees(100000) == "Rs 1,000.00"
    assert format_rupees(12345678900) == "Rs 12,34,56,789.00"
    assert format_rupees(10000000) == "Rs 1,00,000.00"
    assert format_rupees(None) == "Rs 0.00" and format_rupees("abc") == "Rs 0.00"
    assert format_rupees(-250) == "-Rs 2.50"


@pytest.mark.parametrize("failure_class", list(FailureClass))
@pytest.mark.parametrize("channel", ["sms", "email"])
def test_templates_format_for_every_class_and_channel(failure_class, channel):
    a = attempt(customer_contact=CONTACT if channel == "sms" else None)
    action = Action.NUDGE_CHANGE_METHOD if failure_class is FailureClass.HARD_DECLINE else Action.RECOVERY_LINK
    n = template_nudge(a, failure_class, action, LINK)
    assert n.channel == channel and n.source == "template" and n.fallback_taken is None
    assert "Rs 2,499.00" in n.body and "order_ct86nidKocRa56" in n.body and "Priya" in n.body
    assert "{" not in n.body and "}" not in n.body and "{" not in n.subject
    if channel == "sms":
        assert n.subject == "" and len(n.body) <= SMS_MAX_CHARS
    else:
        assert 0 < len(n.subject) <= SUBJECT_MAX_CHARS and "Rs 2,499.00" in n.subject
    if failure_class in NEUTRAL_CLASSES:
        assert LINK not in n.body and "get in touch" in n.body
        for word in ("risk", "fraud", "suspic"):
            assert word not in n.body.lower()
    else:
        assert LINK in n.body
    if failure_class is FailureClass.HARD_DECLINE:
        assert "different card or payment method" in n.body


def test_template_without_link_or_name_still_reads_cleanly():
    a = attempt(customer_name=None, customer_contact=None)
    n = template_nudge(a, FailureClass.ISSUER_DOWN, Action.RECOVERY_LINK, None)
    assert n.channel == "email" and n.body.startswith("Hi,\n") and "checkout page" in n.body and "None" not in n.body
    n = template_nudge(a, FailureClass.HARD_DECLINE, Action.NUDGE_CHANGE_METHOD, None)
    assert "different card or payment method" in n.body and "http" not in n.body
    n = template_nudge(SimpleNamespace(), FailureClass.INSUFFICIENT_FUNDS, Action.RECOVERY_LINK, LINK)
    assert n.channel == "email" and "Rs 0.00" in n.body and LINK in n.body


def test_sms_template_compacts_rather_than_overflowing():
    a = attempt(customer_name="A" * 200)
    n = template_nudge(a, FailureClass.NETWORK_TIMEOUT, Action.RECOVERY_LINK, "https://rzp.io/i/" + "x" * 60)
    assert n.channel == "sms" and len(n.body) <= SMS_MAX_CHARS and "rzp.io" in n.body


def test_channel_follows_contact():
    assert channel_for(attempt()) == "sms"
    assert channel_for(attempt(customer_contact=None)) == "email"
    assert channel_for(attempt(customer_contact=None, customer_email=None)) == "email"


# ---- faults and client selection -----------------------------------------------------

@pytest.mark.parametrize("fault, classify_fb, nudge_fb", [
    ("llm_timeout", "llm_timeout->human_queue", "llm_timeout->template"),
    ("llm_bad_json", "llm_bad_json->human_queue", "llm_bad_json->template"),
    ("llm_hallucinated_class", "llm_hallucinated_class->human_queue", "llm_bad_json->template"),
])
def test_each_llm_fault_produces_its_documented_fallback(fault, classify_fb, nudge_fb):
    faults.activate(fault)
    client = get_client()
    assert isinstance(client, FaultingLLM) and client.fault == fault
    c = classify_unmapped(attempt())
    assert c.failure_class is FailureClass.UNKNOWN and c.source == "fallback" and c.fallback_taken == classify_fb
    assert c.llm_model == f"faulting-{fault}"
    n = draft_nudge(attempt(), FailureClass.INSUFFICIENT_FUNDS, Action.RECOVERY_LINK, LINK)
    assert n.source == "template" and n.fallback_taken == nudge_fb and LINK in n.body


def test_faulting_llm_fails_every_call_so_the_repair_fails_too():
    client = FaultingLLM("llm_bad_json")
    classify_unmapped(attempt(), client=client)
    assert len(client.calls) == 2
    with pytest.raises(ValueError):
        FaultingLLM("razorpay_429")


def test_get_client_honours_faults_over_config(monkeypatch):
    assert get_client() is None
    monkeypatch.setattr(config, "llm_live", lambda: True)
    live = get_client()
    assert isinstance(live, AnthropicLLM) and live._client is None  # lazy: nothing constructed, no key needed
    faults.activate("llm_timeout")
    assert isinstance(get_client(), FaultingLLM)


def test_scripted_llm_records_calls_and_a_dry_client_degrades_to_a_person():
    client = ScriptedLLM([GOOD])
    client.complete_json("s", "u", {})
    assert client.calls == [{"system": "s", "user": "u", "schema": {}}]
    # a double that runs dry mid-interaction is an exception like any client bug: the money path
    # degrades to a human review, never a guess.
    c = classify_unmapped(attempt(), client=ScriptedLLM([]))
    assert c.failure_class is FailureClass.UNKNOWN and c.source == "fallback"
    assert c.fallback_taken == "llm_error->human_queue" and "RuntimeError" in c.reason


# ---- the live client's request shape, without the network -------------------------------

class _FakeMessages:
    def __init__(self, log):
        self.log = log

    def create(self, **kwargs):
        self.log.append(kwargs)
        block = SimpleNamespace(type="text", text=GOOD)
        usage = SimpleNamespace(input_tokens=1200, output_tokens=40, cache_read_input_tokens=1100,
                                cache_creation_input_tokens=0)
        return SimpleNamespace(content=[SimpleNamespace(type="thinking", thinking=""), block],
                               model="claude-test-served", stop_reason="end_turn", usage=usage)


def test_anthropic_llm_request_shape_and_response_parsing():
    log = []
    fake = SimpleNamespace(messages=_FakeMessages(log))
    client = AnthropicLLM(model="claude-test", client=fake)
    resp = client.complete_json("SYS", "USER", CLASSIFY_SCHEMA)
    assert (resp.text, resp.model, resp.stop_reason) == (GOOD, "claude-test-served", "end_turn")
    assert isinstance(resp.latency_ms, int)
    kw = log[0]
    assert kw["model"] == "claude-test" and kw["max_tokens"] == llm_mod.MAX_TOKENS
    # the whole system text is ONE cached block; only the user turn varies between calls
    assert kw["system"] == [{"type": "text", "text": "SYS", "cache_control": {"type": "ephemeral"}}]
    assert kw["messages"] == [{"role": "user", "content": "USER"}]
    assert kw["output_config"]["format"] == {"type": "json_schema", "schema": CLASSIFY_SCHEMA}
    assert kw["output_config"]["effort"] == "low"
    assert "thinking" not in kw and "temperature" not in kw
    assert (resp.input_tokens, resp.output_tokens, resp.cache_read_input_tokens) == (1200, 40, 1100)
    c = classify_unmapped(attempt(), client=client)
    assert c.source == "llm" and c.llm_model == "claude-test-served"
    u = llm_mod.last_call_usage()
    assert u["call_site"] == "classify_unmapped" and u["model"] == "claude-test-served" and u["client"] == "anthropic"
    assert (u["input_tokens"], u["output_tokens"], u["cache_read_input_tokens"]) == (1200, 40, 1100)


def test_anthropic_llm_usage_defaults_to_zero_when_the_response_has_none():
    class NoUsage:
        def create(self, **kwargs):
            return SimpleNamespace(content=[SimpleNamespace(type="text", text=GOOD)], model="m", stop_reason="end_turn",
                                   usage=SimpleNamespace(input_tokens=None, output_tokens=12))

    resp = AnthropicLLM(model="claude-test", client=SimpleNamespace(messages=NoUsage())).complete_json("s", "u", {})
    assert (resp.input_tokens, resp.output_tokens, resp.cache_read_input_tokens) == (0, 12, 0)


def test_anthropic_llm_errors_propagate_to_the_caller_which_degrades():
    class Boom:
        def create(self, **kwargs):
            raise anthropic.APITimeoutError(request=SimpleNamespace(url="https://api.anthropic.com/v1/messages"))

    client = AnthropicLLM(client=SimpleNamespace(messages=Boom()))
    with pytest.raises(anthropic.APITimeoutError):
        client.complete_json("s", "u", {})
    assert classify_unmapped(attempt(), client=client).fallback_taken == "llm_timeout->human_queue"
    assert draft_nudge(attempt(), FailureClass.ISSUER_DOWN, Action.RECOVERY_LINK, LINK, client=client).fallback_taken \
        == "llm_timeout->template"


def test_classify_schema_is_closed_and_bound_free():
    assert CLASSIFY_SCHEMA["additionalProperties"] is False
    assert set(CLASSIFY_SCHEMA["required"]) == {"failure_class", "confidence", "rationale"}
    assert CLASSIFY_SCHEMA["properties"]["failure_class"]["enum"] == [c.value for c in FailureClass]
    # numeric bounds are unsupported by the API's structured-output schema; pydantic enforces 0..1 instead
    assert "minimum" not in CLASSIFY_SCHEMA["properties"]["confidence"]


def test_unknown_error_code_fault_forces_no_client_even_with_a_key(monkeypatch):
    from app import config, faults
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-ant-test")
    assert isinstance(get_client(), AnthropicLLM)
    faults.activate("unknown_error_code")
    try:
        assert get_client() is None
        assert classify_unmapped(attempt()).fallback_taken == "llm_unavailable->human_queue"
    finally:
        faults.clear()


# ---- few-shot calibration: a byte-stable cached prefix ------------------------------------

def _fixture_cases():
    with open(config.BASE_DIR / "tests" / "fixtures" / "failure_cases.json", encoding="utf-8") as fh:
        return json.load(fh)["cases"]


def test_classify_system_prompt_is_byte_stable_and_has_examples_for_every_class():
    cases = _fixture_cases()
    once = build_classify_system(select_few_shot_examples(cases))
    twice = build_classify_system(select_few_shot_examples(list(reversed(cases))))  # file order must not matter
    assert once == twice == CLASSIFY_SYSTEM
    assert len(FEW_SHOT_EXAMPLES) == 2 * len(FailureClass)
    for cls in FailureClass:
        lines = [ln for ln in CLASSIFY_SYSTEM.splitlines() if ln.endswith(f" -> {cls.value}")]
        assert len(lines) == 2, cls
        assert all(ln.startswith('- "') for ln in lines)
    # the rule: first ids per class, sorted, never random
    assert FEW_SHOT_EXAMPLES[0][1] == "INSUFFICIENT_FUNDS" and FEW_SHOT_EXAMPLES[-1][1] == "UNKNOWN"
    by_id = {c["id"]: c["error_description"] for c in cases}
    assert FEW_SHOT_EXAMPLES[0][0] == by_id["IF-01"] and FEW_SHOT_EXAMPLES[-2][0] == by_id["UK-01"]


def test_few_shot_selection_skips_empty_descriptions_and_honours_per_class():
    cases = [{"id": "UK-00", "expected_class": "UNKNOWN", "error_description": "   "},
             {"id": "UK-02", "expected_class": "UNKNOWN", "error_description": "b"},
             {"id": "UK-01", "expected_class": "UNKNOWN", "error_description": "a\nwith newline"},
             {"id": "IF-01", "expected_class": "INSUFFICIENT_FUNDS", "error_description": "no money"},
             {"id": "XX-01", "expected_class": "NOT_A_CLASS", "error_description": "ignored"}]
    assert select_few_shot_examples(cases, per_class=1) == [("no money", "INSUFFICIENT_FUNDS"), ("a with newline", "UNKNOWN")]
    assert select_few_shot_examples(cases, per_class=0) == []
    assert build_classify_system([]) == CLASSIFY_SYSTEM.split("\n\nLabelled examples")[0]


def test_cached_prompts_carry_no_pii_and_only_the_user_turn_varies():
    import re
    for prompt in (CLASSIFY_SYSTEM, NUDGE_SYSTEM):
        for secret in (NAME, "Priya", CONTACT, EMAIL):
            assert secret not in prompt
        assert not re.search(r"\d{10}", prompt) and "@" not in prompt
    client = ScriptedLLM([GOOD, GOOD])
    classify_unmapped(attempt(error_description="first"), client=client)
    classify_unmapped(attempt(error_description="second", amount_paise=100), client=client)
    assert client.calls[0]["system"] == client.calls[1]["system"] == CLASSIFY_SYSTEM
    assert client.calls[0]["user"] != client.calls[1]["user"]


# ---- usage accounting ---------------------------------------------------------------------

def test_usage_log_records_every_call_with_zero_tokens_for_doubles(caplog):
    usage_log.clear()
    with caplog.at_level("INFO", logger="app.llm"):
        classify_unmapped(attempt(), client=ScriptedLLM([BAD_JSON, GOOD], latency_ms=3))
    assert len(usage_log) == 2 and [e["attempt"] for e in usage_log] == [1, 2]
    u = last_call_usage()
    assert u["call_site"] == "classify_unmapped" and u["client"] == "scripted" and u["model"] == "scripted-model"
    assert (u["input_tokens"], u["output_tokens"], u["cache_read_input_tokens"]) == (0, 0, 0)
    assert u["stop_reason"] == "end_turn" and u["error"] is None and isinstance(u["latency_ms"], int)
    assert u is not usage_log[-1]  # a copy: callers cannot corrupt the log
    lines = [r.getMessage() for r in caplog.records if r.name == "app.llm"]
    assert len(lines) == 2 and "site=classify_unmapped" in lines[0] and "attempt=2" in lines[1]
    draft_nudge(attempt(), FailureClass.ISSUER_DOWN, Action.RECOVERY_LINK, LINK, client=ScriptedLLM([TimeoutError("t")]))
    u = last_call_usage()
    assert u["call_site"] == "draft_nudge" and u["error"] == "TimeoutError" and u["stop_reason"] == ""
    faults.activate("llm_bad_json")
    classify_unmapped(attempt())
    assert last_call_usage()["client"] == "faulting" and last_call_usage()["input_tokens"] == 0


def test_usage_log_is_capped():
    usage_log.clear()
    for _ in range(llm_mod.USAGE_LOG_MAX // 2 + 10):
        classify_unmapped(attempt(), client=ScriptedLLM([BAD_JSON, BAD_JSON]))  # 2 calls each
    assert len(usage_log) == llm_mod.USAGE_LOG_MAX
    assert last_call_usage()["attempt"] == 2
    llm_mod.clear_usage_log()
    assert last_call_usage() is None


# ---- multilingual nudges through the model --------------------------------------------------

HINDI_BODY = f"नमस्ते Priya, ऑर्डर order_ct86nidKocRa56 के लिए आपका Rs 2,499.00 का भुगतान पूरा नहीं हो सका। यहाँ पूरा करें: {LINK}"
HINGLISH_BODY = f"Namaste Priya, aapka Rs 2,499.00 ka payment order order_ct86nidKocRa56 ke liye poora nahi ho saka. Yahan poora karein: {LINK}"


def test_draft_nudge_in_hindi_tells_the_model_and_accepts_a_devanagari_body():
    client = ScriptedLLM([sms_nudge(HINDI_BODY)])
    n = draft_nudge(attempt(), FailureClass.INSUFFICIENT_FUNDS, Action.RECOVERY_LINK, LINK, client=client, language="hi")
    assert n.source == "llm" and n.body == HINDI_BODY and len(n.body) <= SMS_MAX_CHARS_UNICODE
    user = client.calls[0]["user"]
    assert "language: hi (Hindi, Devanagari script)" in user and "what_happened_in_hi: " in user and "पर्याप्त" in user
    assert client.calls[0]["system"] == NUDGE_SYSTEM  # language rules live in the cached block, not the user turn


def test_language_resolution_explicit_then_attempt_then_default(monkeypatch):
    client = ScriptedLLM([sms_nudge(HINGLISH_BODY)] * 3)
    draft_nudge(attempt(customer_language="hi"), FailureClass.ISSUER_DOWN, Action.RECOVERY_LINK, LINK, client=client,
                language="hinglish")
    assert "language: hinglish" in client.calls[0]["user"]
    draft_nudge(attempt(customer_language="hinglish"), FailureClass.ISSUER_DOWN, Action.RECOVERY_LINK, LINK, client=client)
    assert "language: hinglish" in client.calls[1]["user"]
    monkeypatch.setenv("NUDGE_LANGUAGE_DEFAULT", "hinglish")
    draft_nudge(attempt(), FailureClass.ISSUER_DOWN, Action.RECOVERY_LINK, LINK, client=client)
    assert "language: hinglish" in client.calls[2]["user"]


@pytest.mark.parametrize("language, bad, why", [
    ("hi", HINDI_BODY[:-len(LINK)] + "और भी बहुत कुछ " * 8 + LINK, "limit 200"),                    # Devanagari over UCS-2 budget
    ("hi", good_sms_body(), "must be in Hindi"),                                                          # asked for Hindi, got English
    ("hinglish", HINDI_BODY, "Latin script"),                                                             # asked for Hinglish, got Devanagari
    ("en", HINDI_BODY, "Latin script"),
])
def test_language_rules_are_enforced_then_the_template_answers_in_the_same_language(language, bad, why):
    client = ScriptedLLM([sms_nudge(bad), sms_nudge(bad)])
    n = draft_nudge(attempt(), FailureClass.INSUFFICIENT_FUNDS, Action.RECOVERY_LINK, LINK, client=client, language=language)
    assert len(client.calls) == 2 and why in client.calls[1]["user"]
    assert n.source == "template" and n.fallback_taken == "llm_output_rejected->template" and why in n.reason
    t = template_nudge(attempt(), FailureClass.INSUFFICIENT_FUNDS, Action.RECOVERY_LINK, LINK, language=language)
    assert n.body == t.body and LINK in n.body and "Rs 2,499.00" in n.body


def test_latin_sms_keeps_the_300_budget_and_unicode_gets_200():
    tail = f" Rs 2,499.00 {LINK}"
    long_latin = "x" * (SMS_MAX_CHARS - len(tail)) + tail
    assert len(long_latin) == 300
    n = draft_nudge(attempt(), FailureClass.ISSUER_DOWN, Action.RECOVERY_LINK, LINK, client=ScriptedLLM([sms_nudge(long_latin)]))
    assert n.source == "llm"
    curly = long_latin.replace("x", "\u2019", 1)  # one curly quote: UCS-2, so 300 is now too long
    n = draft_nudge(attempt(), FailureClass.ISSUER_DOWN, Action.RECOVERY_LINK, LINK, client=ScriptedLLM([sms_nudge(curly)] * 2))
    assert n.fallback_taken == "llm_output_rejected->template" and "UCS-2" in n.reason


# ---- stricter content rules: no other amount, no promises ----------------------------------

@pytest.mark.parametrize("body", [
    f"Hi Priya, your payment of Rs 2,499.00 failed. Pay Rs 2,500.00 here: {LINK}",
    f"Hi Priya, your payment of Rs 2,499.00 failed; a fee of Rs. 49 applies: {LINK}",
    f"Hi Priya, your payment of Rs 2,499.00 (₹2,499) failed: {LINK}",          # same number, different rendering: still a second amount
    f"Hi Priya, your payment of Rs 2,499.00 failed. Pay INR 2499 here: {LINK}",
    f"Hi Priya, your payment of Rs 2,499.00 failed. Any deduction will be refunded: {LINK}",
    f"Hi Priya, we guarantee your Rs 2,499.00 payment will work now: {LINK}",
    f"Hi Priya, your Rs 2,499.00 payment will be fixed within 24 hours: {LINK}",
    f"नमस्ते Priya, आपका Rs 2,499.00 का भुगतान पूरा नहीं हो सका; रिफंड 24 घंटे में: {LINK}",
])
def test_other_amounts_and_promises_are_rejected_then_template(body):
    language = "hi" if "नमस्ते" in body else "en"
    client = ScriptedLLM([sms_nudge(body), sms_nudge(body)])
    n = draft_nudge(attempt(), FailureClass.INSUFFICIENT_FUNDS, Action.RECOVERY_LINK, LINK, client=client, language=language)
    assert len(client.calls) == 2
    assert n.source == "template" and n.fallback_taken == "llm_output_rejected->template"
    assert "different amount" in n.reason or "must not promise" in n.reason


def test_same_amount_repeated_or_in_the_subject_is_not_a_different_amount():
    payload = json.dumps({"channel": "email", "subject": "Complete your payment of Rs 2,499.00",
                          "body": f"Hi Priya,\n\nYour payment of Rs 2,499.00 did not go through.\n\nPay Rs 2,499.00 here: {LINK}"})
    n = draft_nudge(attempt(customer_contact=None), FailureClass.ISSUER_DOWN, Action.RECOVERY_LINK, LINK,
                    client=ScriptedLLM([payload]))
    assert n.source == "llm"
    assert llm_mod._other_amounts("order order_ct86nidKocRa56 at 10:30, Rs 2,499.00", "Rs 2,499.00") == []
    assert llm_mod._other_amounts("Rs 2,499.00 and rs 2,499.00 and Rs 2,499", "Rs 2,499.00") == ["Rs 2,499"]


def test_llm_response_token_fields_default_to_zero():
    resp = LLMResponse(text="x", model="m", latency_ms=1, stop_reason="end_turn")
    assert (resp.input_tokens, resp.output_tokens, resp.cache_read_input_tokens) == (0, 0, 0)
