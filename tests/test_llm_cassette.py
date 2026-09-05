"""Record/replay of the model path (app/llm_cassette.py) and the offline eval (scripts/eval_llm.py),
with no network: the recorder is fed a ScriptedLLM, so every cassette written here is SYNTHETIC.
tests/fixtures/llm_cassettes/synthetic.jsonl is the committed output of write_synthetic_cassette
below; the default cassette path must NOT exist in the repository (nothing real has been recorded)."""
import importlib.util
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import config, faults
from app import llm as llm_mod
from app.llm import AnthropicLLM, CassetteMiss, FaultingLLM, ScriptedLLM, classify_unmapped, draft_nudge, get_client
from app.llm_cassette import (DEFAULT_CASSETTE_PATH, CassetteLLM, NullLLM, cassette_key, cassette_mode, site_for,
                              wrap_for_cassette)
from app.taxonomy import Action, FailureClass

ROOT = config.BASE_DIR
CASES = ROOT / "tests" / "fixtures" / "failure_cases.json"
SYNTHETIC = ROOT / "tests" / "fixtures" / "llm_cassettes" / "synthetic.jsonl"
NAME, CONTACT, EMAIL = "Priya Sharma", "+919876543210", "priya.sharma@example.com"
LINK = "https://rzp.io/i/AbCdEfGh"
GOOD = json.dumps({"failure_class": "INSUFFICIENT_FUNDS", "confidence": 0.86, "rationale": "Hinglish for no balance."})
BAD_JSON = '{"failure_class": "INSUFFICIENT_FUNDS", "confidence": 0.8'


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    faults.clear()
    monkeypatch.delenv("LLM_CASSETTE_MODE", raising=False)
    monkeypatch.delenv("LLM_CASSETTE_PATH", raising=False)
    yield
    faults.clear()


def attempt(**overrides):
    base = dict(razorpay_payment_id="pay_TESTcassette01", order_id="order_ct86nidKocRa56", merchant_id="merchant_acme",
                amount_paise=249900, currency="INR", method="upi", has_token=False,
                error_code="BAD_REQUEST_ERROR", error_source="bank", error_step="payment_authorization",
                error_reason="payment_failed", error_description="Bhugtan asafal: khaate mein paryaapt raashi nahin hai",
                customer_name=NAME, customer_contact=CONTACT, customer_email=EMAIL)
    base.update(overrides)
    return SimpleNamespace(**base)


def _eval_module():
    spec = importlib.util.spec_from_file_location("eval_llm", ROOT / "scripts" / "eval_llm.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["eval_llm"] = module
    spec.loader.exec_module(module)
    return module


def _synthetic_answer(case: dict) -> str:
    """Deterministic per case id: mostly UNKNOWN at low confidence, one confident class, one below the gate."""
    n = int(case["id"].split("-")[1])
    if n % 5 == 0:
        return json.dumps({"failure_class": "HARD_DECLINE", "confidence": 0.91, "rationale": "synthetic: confident"})
    if n % 5 == 3:
        return json.dumps({"failure_class": "ISSUER_DOWN", "confidence": 0.55, "rationale": "synthetic: below the gate"})
    return json.dumps({"failure_class": "UNKNOWN", "confidence": 0.3, "rationale": "synthetic: stays UNKNOWN"})


def write_synthetic_cassette(path: Path) -> list[dict]:
    """Record a ScriptedLLM over the UNKNOWN-by-design cases through the real eval plumbing."""
    ev = _eval_module()
    cases = ev.select_cases(ev.load_cases(CASES), include_mapped=False)
    if path.exists():
        path.unlink()
    client = CassetteLLM(ScriptedLLM([_synthetic_answer(c) for c in cases], latency_ms=123), "record", path)
    ev.run(cases, client)
    return cases


# ---- keys and sites ---------------------------------------------------------------------

def test_key_is_stable_exact_and_site_comes_from_the_schema():
    k = cassette_key("classify_unmapped", "sys", "user", {"b": 1, "a": [1, 2]})
    assert k == cassette_key("classify_unmapped", "sys", "user", {"a": [1, 2], "b": 1}), "schema key order is irrelevant"
    assert len(k) == 64 and k != cassette_key("classify_unmapped", "sys", "user ", {"b": 1, "a": [1, 2]})
    assert k != cassette_key("draft_nudge", "sys", "user", {"b": 1, "a": [1, 2]})
    assert site_for(llm_mod.CLASSIFY_SCHEMA) == "classify_unmapped"
    assert site_for(llm_mod.NUDGE_SCHEMA) == "draft_nudge"
    assert site_for({"type": "object"}) == "unknown"


# ---- record -> replay --------------------------------------------------------------------

def test_record_then_replay_round_trip_without_touching_the_inner_client(tmp_path):
    path = tmp_path / "c.jsonl"
    rec = CassetteLLM(ScriptedLLM([GOOD], latency_ms=321), "record", path)
    live = classify_unmapped(attempt(), client=rec)
    assert live.source == "llm" and live.failure_class is FailureClass.INSUFFICIENT_FUNDS
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert set(entry) == {"key", "site", "client", "model", "request", "response", "recorded_at"}
    assert entry["site"] == "classify_unmapped" and entry["client"] == "scripted" and entry["model"] == "scripted-model"
    assert set(entry["request"]) == {"system", "user", "schema"} and entry["request"]["system"] == llm_mod.CLASSIFY_SYSTEM
    assert entry["response"]["text"] == GOOD and entry["response"]["latency_ms"] == 321
    assert entry["response"]["stop_reason"] == "end_turn" and entry["response"]["input_tokens"] == 0

    inner = NullLLM()
    rep = CassetteLLM(inner, "replay", path)
    again = classify_unmapped(attempt(), client=rep)
    assert (again.failure_class, again.source, again.confidence, again.reason) == \
        (live.failure_class, "llm", live.confidence, live.reason)
    assert again.llm_model == "scripted-model" and again.fallback_taken is None
    assert inner.calls == 0, "replay never calls the inner client"
    assert rep.calls == [{"key": entry["key"], "site": "classify_unmapped", "hit": True}]
    assert llm_mod.last_call_usage()["client"] == "cassette-replay"


def test_record_captures_the_repair_call_and_replay_reproduces_it(tmp_path):
    path = tmp_path / "c.jsonl"
    rec = CassetteLLM(ScriptedLLM([BAD_JSON, GOOD]), "record", path)
    live = classify_unmapped(attempt(), client=rec)
    assert live.source == "llm" and len(path.read_text().splitlines()) == 2
    rep = CassetteLLM(NullLLM(), "replay", path)
    again = classify_unmapped(attempt(), client=rep)
    assert again.source == "llm" and again.failure_class is live.failure_class
    assert [c["hit"] for c in rep.calls] == [True, True], "attempt and repair both replayed"


def test_record_does_not_store_a_failed_call_and_the_error_still_propagates(tmp_path):
    path = tmp_path / "c.jsonl"
    rec = CassetteLLM(ScriptedLLM([TimeoutError("slow")]), "record", path)
    assert classify_unmapped(attempt(), client=rec).fallback_taken == "llm_timeout->human_queue"
    assert not path.exists()


def test_last_record_for_a_key_wins_on_replay(tmp_path):
    path = tmp_path / "c.jsonl"
    other = json.dumps({"failure_class": "ISSUER_DOWN", "confidence": 0.8, "rationale": "second take"})
    classify_unmapped(attempt(), client=CassetteLLM(ScriptedLLM([GOOD]), "record", path))
    classify_unmapped(attempt(), client=CassetteLLM(ScriptedLLM([other]), "record", path))
    assert len(path.read_text().splitlines()) == 2
    c = classify_unmapped(attempt(), client=CassetteLLM(NullLLM(), "replay", path))
    assert c.failure_class is FailureClass.ISSUER_DOWN and c.reason == "second take"


# ---- misses ------------------------------------------------------------------------------

def test_replay_miss_is_its_own_fallback_at_both_sites_and_never_reaches_the_network(tmp_path):
    path = tmp_path / "c.jsonl"
    classify_unmapped(attempt(), client=CassetteLLM(ScriptedLLM([GOOD]), "record", path))
    inner = NullLLM()
    rep = CassetteLLM(inner, "replay", path)
    c = classify_unmapped(attempt(error_description="a different description"), client=rep)
    assert c.fallback_taken == "llm_cassette_miss->human_queue" and c.source == "fallback"
    assert c.failure_class is FailureClass.UNKNOWN and "CassetteMiss" in c.reason
    n = draft_nudge(attempt(), FailureClass.INSUFFICIENT_FUNDS, Action.RECOVERY_LINK, LINK, client=rep)
    assert n.fallback_taken == "llm_cassette_miss->template" and n.source == "template"
    assert "Rs 2,499.00" in n.body and LINK in n.body
    assert inner.calls == 0
    assert [x["hit"] for x in rep.calls] == [False, False]
    assert llm_mod.last_call_usage()["error"] == "CassetteMiss"


def test_missing_cassette_file_is_a_miss_not_a_crash(tmp_path):
    rep = CassetteLLM(NullLLM(), "replay", tmp_path / "nowhere.jsonl")
    with pytest.raises(CassetteMiss) as exc:
        rep.complete_json("s", "u", llm_mod.CLASSIFY_SCHEMA)
    assert "does not exist" in str(exc.value) and exc.value.site == "classify_unmapped" and len(exc.value.key) == 64
    assert classify_unmapped(attempt(), client=rep).fallback_taken == "llm_cassette_miss->human_queue"


def test_null_llm_raises_and_off_mode_is_passthrough(tmp_path):
    with pytest.raises(RuntimeError):
        NullLLM().complete_json("s", "u", {})
    off = CassetteLLM(ScriptedLLM([GOOD]), "off", tmp_path / "never.jsonl")
    assert classify_unmapped(attempt(), client=off).source == "llm"
    assert not (tmp_path / "never.jsonl").exists() and off.calls == []


# ---- get_client wiring --------------------------------------------------------------------

def test_get_client_wraps_a_null_client_in_replay_even_without_a_key(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_CASSETTE_MODE", "replay")
    monkeypatch.setenv("LLM_CASSETTE_PATH", str(tmp_path / "c.jsonl"))
    assert not config.llm_live()
    client = get_client()
    assert isinstance(client, CassetteLLM) and client.mode == "replay" and isinstance(client.inner, NullLLM)
    assert client.path == tmp_path / "c.jsonl"
    assert classify_unmapped(attempt()).fallback_taken == "llm_cassette_miss->human_queue"
    assert client.inner.calls == 0


def test_get_client_records_only_when_a_real_key_exists(monkeypatch):
    monkeypatch.setenv("LLM_CASSETTE_MODE", "record")
    assert get_client() is None, "no key: nothing to record, the usual llm_unavailable path"
    monkeypatch.setattr(config, "llm_live", lambda: True)
    client = get_client()
    assert isinstance(client, CassetteLLM) and client.mode == "record"
    assert isinstance(client.inner, AnthropicLLM) and client.inner._client is None  # lazy: nothing constructed
    assert client.path == DEFAULT_CASSETTE_PATH


def test_get_client_off_is_the_old_behaviour_and_faults_beat_the_cassette(monkeypatch):
    assert cassette_mode() == "off" and get_client() is None
    monkeypatch.setattr(config, "llm_live", lambda: True)
    assert isinstance(get_client(), AnthropicLLM)
    monkeypatch.setenv("LLM_CASSETTE_MODE", "replay")
    assert isinstance(get_client(), CassetteLLM)
    faults.activate("llm_timeout")
    assert isinstance(get_client(), FaultingLLM), "a fault beats replay"
    faults.clear()
    faults.activate("unknown_error_code")
    assert get_client() is None, "no-model fault beats replay"


def test_config_attribute_beats_env_and_an_unknown_mode_degrades_to_a_person(monkeypatch):
    monkeypatch.setenv("LLM_CASSETTE_MODE", "replay")
    monkeypatch.setattr(config, "LLM_CASSETTE_MODE", "off", raising=False)
    assert cassette_mode() == "off"
    monkeypatch.setattr(config, "LLM_CASSETTE_MODE", "sideways", raising=False)
    with pytest.raises(ValueError):
        cassette_mode()
    with pytest.raises(ValueError):
        wrap_for_cassette(None)
    c = classify_unmapped(attempt())  # get_client raised inside the outer try: still never raises
    assert c.fallback_taken == "llm_error->human_queue" and "LLM_CASSETTE_MODE" in c.reason


# ---- PII ---------------------------------------------------------------------------------

def test_recorded_prompts_carry_no_contact_details(tmp_path):
    path = tmp_path / "c.jsonl"
    rec = CassetteLLM(ScriptedLLM([GOOD, json.dumps({"channel": "sms", "subject": "", "body":
                                                      f"Hi Priya, your payment of Rs 2,499.00 did not go through. Complete it here: {LINK}"})]),
                      "record", path)
    classify_unmapped(attempt(), client=rec)
    n = draft_nudge(attempt(), FailureClass.INSUFFICIENT_FUNDS, Action.RECOVERY_LINK, LINK, client=rec)
    assert n.source == "llm"
    text = path.read_text(encoding="utf-8")
    assert len(text.splitlines()) == 2 and "draft_nudge" in text
    for secret in (CONTACT, CONTACT[3:], EMAIL, NAME, "pay_TESTcassette01", "merchant_acme"):
        assert secret not in text, secret
    assert "Priya" in text, "the first name alone reaches the nudge prompt by design"
    assert "@" not in text.replace("@example", "")  # no email anywhere; belt and braces


# ---- the shipped state and the synthetic cassette --------------------------------------------

def test_default_cassette_is_not_shipped():
    assert DEFAULT_CASSETTE_PATH == ROOT / "tests" / "fixtures" / "llm_cassettes" / "default.jsonl"
    assert not DEFAULT_CASSETTE_PATH.exists(), "nothing real has been recorded; the repo must say so"


def test_committed_synthetic_cassette_is_marked_as_a_test_double():
    assert SYNTHETIC.exists()
    entries = CassetteLLM(NullLLM(), "replay", SYNTHETIC).entries()
    assert len(entries) == 15
    assert {e["client"] for e in entries} == {"scripted"} and {e["model"] for e in entries} == {"scripted-model"}
    assert {e["site"] for e in entries} == {"classify_unmapped"}
    assert len({e["key"] for e in entries}) == 15


def test_synthetic_cassette_regenerates_from_the_scripted_double(tmp_path):
    path = tmp_path / "synthetic.jsonl"
    cases = write_synthetic_cassette(path)
    entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(cases) == len(entries) == 15 and {e["client"] for e in entries} == {"scripted"}
    committed = {e["key"] for e in CassetteLLM(NullLLM(), "replay", SYNTHETIC).entries()}
    # same fixture, same prompt bytes -> same keys; if the fixture's UNKNOWN cases change, regenerate synthetic.jsonl
    assert {e["key"] for e in entries} == committed, "run write_synthetic_cassette(SYNTHETIC) to refresh the committed file"


# ---- scripts/eval_llm.py -------------------------------------------------------------------

def test_eval_runs_against_a_synthetic_cassette_and_labels_it_as_such(tmp_path, capsys):
    path = tmp_path / "synthetic.jsonl"
    write_synthetic_cassette(path)
    doc = tmp_path / "llm_eval.md"
    assert _eval_module().main(["--cassette", str(path), "--write", str(doc)]) == 0
    out = capsys.readouterr().out
    assert "SYNTHETIC" in out and "15 cases" in out and "0 cassette misses" in out
    assert "agreement with labels 60.0% (9/15 answered" in out  # 9 UNKNOWN, 3 HARD_DECLINE@0.91, 3 ISSUER_DOWN@0.55
    assert "to a person (0.7 gate, UNKNOWN or a fallback) 80.0% (12/15)" in out
    assert "avg latency 123 ms" in out and "LLM_CASSETTE_MODE=record ANTHROPIC_API_KEY=... python scripts/eval_llm.py --record" in out
    text = doc.read_text(encoding="utf-8")
    assert text.startswith("# LLM classifier evaluation") and "SYNTHETIC" in text
    assert "| UK-05 | UNKNOWN | HARD_DECLINE | 0.91 | no |" in text
    assert "| UK-03 | UNKNOWN | ISSUER_DOWN | 0.55 | <0.7 |" in text
    assert "| UK-01 | UNKNOWN | UNKNOWN | 0.30 | UNKNOWN |" in text


def test_eval_include_mapped_reports_misses_for_unrecorded_cases(tmp_path, capsys):
    path = tmp_path / "synthetic.jsonl"
    write_synthetic_cassette(path)
    assert _eval_module().main(["--cassette", str(path), "--include-mapped"]) == 0
    out = capsys.readouterr().out
    assert "118 cases" in out and "103 cassette misses" in out and "llm_cassette_miss->human_queue" in out


def test_eval_without_a_cassette_exits_zero_and_claims_nothing(tmp_path, capsys):
    missing = tmp_path / "default.jsonl"
    doc = tmp_path / "llm_eval.md"
    assert _eval_module().main(["--cassette", str(missing), "--write", str(doc)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("no cassette recorded yet; run with --record and a key")
    text = doc.read_text(encoding="utf-8")
    assert "**Not recorded.**" in text and "LLM_CASSETTE_MODE=record ANTHROPIC_API_KEY=... python scripts/eval_llm.py --record" in text
    assert "## Headline" not in text and not re.search(r"\d+(\.\d+)?%", text), "no accuracy number of any kind"
    assert not missing.exists()


def test_eval_record_without_a_key_refuses_before_any_call(tmp_path):
    with pytest.raises(SystemExit) as exc:
        _eval_module().main(["--cassette", str(tmp_path / "c.jsonl"), "--record"])
    assert "ANTHROPIC_API_KEY" in str(exc.value) and not (tmp_path / "c.jsonl").exists()
