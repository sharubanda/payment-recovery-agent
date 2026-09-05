"""Per-merchant overrides: loading and validation (file and field named), the invariants a file
cannot break (RISK_BLOCKED/UNKNOWN stay human_queue; action only ever moves to human_queue; delays
>= 0; max_attempts 0..5), decide() with overrides, the CLI refusing a malformed file, and the
default behaviour staying byte-identical when no override is passed."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app import config, merchants, policy
from app.merchants import MerchantConfigError, MerchantPolicy
from app.taxonomy import Action, FailureClass

ROOT = Path(__file__).resolve().parent.parent
H = 3600


def write(directory: Path, name: str, data) -> Path:
    path = directory / name
    path.write_text(json.dumps(data) if not isinstance(data, str) else data, encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _fresh_registry():
    yield
    merchants.reload(ROOT / "merchants")  # never leave a test's directory cached for the next test


# ---- loading -----------------------------------------------------------------------------------

def test_loads_per_merchant_and_default_and_skips_underscore_files(tmp_path):
    write(tmp_path, "merchant_acme.json", {"nudge_language": "hi", "classes": {"ISSUER_DOWN": {"action": "human_queue"}}})
    write(tmp_path, "_default.json", {"max_contacts_per_week": 2, "disabled_actions": ["token_retry"]})
    write(tmp_path, "_example.json", {"this is": "never parsed", "so garbage": "is fine"})
    reg = merchants.reload(tmp_path)
    assert reg.count == 2 and reg.skipped == ["_example.json"]
    acme = merchants.get("merchant_acme")
    assert acme.nudge_language == "hi" and acme.max_contacts_per_week == 2 and acme.disabled_actions == ["token_retry"]
    assert acme.classes["ISSUER_DOWN"].action == "human_queue"
    other = merchants.get("merchant_other")  # no file: the default alone
    assert other.max_contacts_per_week == 2 and other.nudge_language is None and other.classes == {}
    assert merchants.get(None) is other
    assert "2 merchant override file(s)" in merchants.header_line() and "_example.json" in merchants.header_line()


def test_missing_directory_means_no_overrides(tmp_path):
    reg = merchants.reload(tmp_path / "nope")
    assert reg.count == 0 and merchants.get("anyone") is None and merchants.for_attempt(None) is None
    assert "does not exist" in merchants.header_line()


def test_shipped_example_is_documentation_and_not_loaded():
    reg = merchants.reload(ROOT / "merchants")
    assert "_example.json" in reg.skipped and "_example" not in reg.merchants
    raw = json.loads((ROOT / "merchants" / "_example.json").read_text(encoding="utf-8"))
    raw.pop("_comment")  # the only key a real file may not carry
    m = MerchantPolicy.model_validate(raw)  # the example, minus its comment, is a valid file
    assert m.classes["INSUFFICIENT_FUNDS"].delay_seconds == 86400 and m.disabled_actions == ["token_retry"]


def test_merchant_file_layers_over_default_field_by_field(tmp_path):
    write(tmp_path, "_default.json", {"classes": {"INSUFFICIENT_FUNDS": {"delay_seconds": 24 * H, "nudge": False}},
                                      "human_queue_all": False, "nudge_language": "en"})
    write(tmp_path, "m.json", {"classes": {"INSUFFICIENT_FUNDS": {"max_attempts": 1}}, "nudge_language": "hinglish"})
    merchants.reload(tmp_path)
    m = merchants.get("m")
    o = m.classes["INSUFFICIENT_FUNDS"]
    assert (o.delay_seconds, o.nudge, o.max_attempts) == (24 * H, False, 1)
    assert m.nudge_language == "hinglish"


def test_cache_and_reload(tmp_path):
    write(tmp_path, "m.json", {"nudge_language": "hi"})
    merchants.reload(tmp_path)
    assert merchants.get("m").nudge_language == "hi"
    write(tmp_path, "m.json", {"nudge_language": "en"})
    assert merchants.get("m").nudge_language == "hi"  # read once per process
    merchants.reload()
    assert merchants.get("m").nudge_language == "en"


def test_merchants_dir_from_env_when_config_does_not_declare_it(monkeypatch, tmp_path):
    monkeypatch.delattr(config, "MERCHANTS_DIR", raising=False)
    monkeypatch.setenv("MERCHANTS_DIR", str(tmp_path))
    assert merchants.merchants_dir() == tmp_path
    monkeypatch.setenv("MERCHANTS_DIR", "rel/dir")
    assert merchants.merchants_dir() == config.BASE_DIR / "rel" / "dir"


# ---- validation: every error names the file and the field ----------------------------------------

@pytest.mark.parametrize("data, needle", [
    ({"classes": {"RISK_BLOCKED": {"action": "recovery_link"}}}, "classes.RISK_BLOCKED.action"),
    ({"classes": {"UNKNOWN": {"delay_seconds": 60}}}, "always human_queue"),
    ({"classes": {"HARD_DECLINE": {"action": "token_retry"}}}, "more conservative, never less"),
    ({"classes": {"ISSUER_DOWN": {"action": "recovery_link"}}}, "classes.ISSUER_DOWN.action"),
    ({"classes": {"ISSUER_DOWN": {"delay_seconds": -1}}}, "classes.ISSUER_DOWN.delay_seconds"),
    ({"classes": {"ISSUER_DOWN": {"max_attempts": 6}}}, "classes.ISSUER_DOWN.max_attempts"),
    ({"classes": {"ISSUER_DOWN": {"max_attempts": -1}}}, "classes.ISSUER_DOWN.max_attempts"),
    ({"classes": {"ISSUER_DOWN": {"retries": 2}}}, "classes.ISSUER_DOWN.retries"),
    ({"classes": {"NOT_A_CLASS": {"nudge": False}}}, "unknown failure class"),
    ({"nudge_language": "fr"}, "nudge_language"),
    ({"max_contacts_per_week": 99}, "max_contacts_per_week"),
    ({"disabled_actions": ["human_queue"]}, "cannot disable"),
    ({"disabled_actions": "token_retry"}, "disabled_actions"),
    ({"human_queue_all": "yes please"}, "human_queue_all"),
    ({"typo_field": 1}, "typo_field"),
], ids=lambda v: v if isinstance(v, str) else "")
def test_invalid_files_are_rejected_naming_file_and_field(tmp_path, data, needle):
    path = write(tmp_path, "merchant_x.json", data)
    with pytest.raises(MerchantConfigError) as exc:
        merchants.reload(tmp_path)
    assert str(path) in str(exc.value) and needle in str(exc.value), str(exc.value)


def test_not_json_and_not_an_object_are_rejected(tmp_path):
    path = write(tmp_path, "m.json", "{not json")
    with pytest.raises(MerchantConfigError, match="not valid JSON"):
        merchants.reload(tmp_path)
    path.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(MerchantConfigError, match=r"\(root\)"):
        merchants.reload(tmp_path)


def test_empty_file_is_a_valid_no_op():
    m = MerchantPolicy.model_validate({})
    assert m.is_empty() and m.describe() == "(no overrides)"
    for cls in FailureClass:
        for token in (True, False):
            assert policy.decide(cls, has_token=token, overrides=m) == policy.decide(cls, has_token=token)


# ---- decide() with overrides -------------------------------------------------------------------

def over(**kw) -> MerchantPolicy:
    return MerchantPolicy.model_validate(kw)


def test_no_overrides_is_byte_identical_to_the_table():
    for cls in FailureClass:
        for token in (True, False):
            for seq in (1, 2, 4):
                assert policy.decide(cls, has_token=token, retry_seq=seq, overrides=None) == \
                       policy.decide(cls, has_token=token, retry_seq=seq)
    assert policy.effective_entry(FailureClass.ISSUER_DOWN) is policy.POLICY[FailureClass.ISSUER_DOWN]
    assert all(r["override"] == "" for r in policy.policy_table())


def test_delay_override_moves_first_attempt_and_the_backoff_chain():
    m = over(classes={"INSUFFICIENT_FUNDS": {"delay_seconds": 24 * H}, "ISSUER_DOWN": {"delay_seconds": 600}})
    d = policy.decide(FailureClass.INSUFFICIENT_FUNDS, has_token=False, overrides=m)
    assert d.action is Action.RECOVERY_LINK and d.delay_seconds == 24 * H and d.max_attempts == 2 and d.nudge is True
    assert "[merchant override]" in d.rationale and d.rationale.endswith(".")
    assert [policy.delay_for(FailureClass.ISSUER_DOWN, n, m) for n in (1, 2, 3)] == [600, 1800, 5400]
    assert policy.decide(FailureClass.ISSUER_DOWN, has_token=True, retry_seq=2, overrides=m).delay_seconds == 1800


def test_max_attempts_override_changes_the_stop_rule_both_ways():
    m = over(classes={"ISSUER_DOWN": {"max_attempts": 1}, "AUTH_ABANDONED": {"max_attempts": 5}})
    assert policy.decide(FailureClass.ISSUER_DOWN, has_token=False, retry_seq=2, overrides=m).action is Action.NO_ACTION
    assert policy.decide(FailureClass.AUTH_ABANDONED, has_token=False, retry_seq=5, overrides=m).action is Action.RECOVERY_LINK
    assert policy.decide(FailureClass.AUTH_ABANDONED, has_token=False, retry_seq=6, overrides=m).action is Action.NO_ACTION
    zero = over(classes={"HARD_DECLINE": {"max_attempts": 0}})
    assert policy.decide(FailureClass.HARD_DECLINE, has_token=False, overrides=zero).action is Action.NO_ACTION


def test_nudge_override_silences_a_link_but_never_makes_a_token_retry_talk():
    m = over(classes={"INSUFFICIENT_FUNDS": {"nudge": False}, "ISSUER_DOWN": {"nudge": True}})
    assert policy.decide(FailureClass.INSUFFICIENT_FUNDS, has_token=False, overrides=m).nudge is False
    assert policy.decide(FailureClass.ISSUER_DOWN, has_token=True, overrides=m).nudge is False  # token retry is silent
    assert policy.decide(FailureClass.ISSUER_DOWN, has_token=False, overrides=m).nudge is True


def test_action_override_can_only_route_to_a_person():
    m = over(classes={"HARD_DECLINE": {"action": "human_queue"}})
    d = policy.decide(FailureClass.HARD_DECLINE, has_token=False, overrides=m)
    assert d.action is Action.HUMAN_QUEUE and d.max_attempts == 0 and d.nudge is False and d.delay_seconds == 0
    assert "for this merchant" in d.rationale
    # a human_queue override is terminal: it ignores retry_seq like the table's own human_queue rows
    assert policy.decide(FailureClass.HARD_DECLINE, has_token=False, retry_seq=7, overrides=m).action is Action.HUMAN_QUEUE
    for action in ("token_retry", "recovery_link", "nudge_change_method", "no_action", "anything"):
        with pytest.raises(ValueError):
            over(classes={"HARD_DECLINE": {"action": action}})


def test_disabled_actions_drop_the_token_path_or_park_the_class():
    m = over(disabled_actions=["token_retry"])
    d = policy.decide(FailureClass.ISSUER_DOWN, has_token=True, overrides=m)
    assert d.action is Action.RECOVERY_LINK and d.nudge is True  # the class's own link action, not a new one
    assert policy.decide(FailureClass.NETWORK_TIMEOUT, has_token=True, overrides=m).action is Action.RECOVERY_LINK
    m = over(disabled_actions=["recovery_link", "nudge_change_method"])
    for cls in (FailureClass.INSUFFICIENT_FUNDS, FailureClass.HARD_DECLINE, FailureClass.AUTH_ABANDONED):
        assert policy.decide(cls, has_token=False, overrides=m).action is Action.HUMAN_QUEUE
    assert policy.decide(FailureClass.ISSUER_DOWN, has_token=True, overrides=m).action is Action.TOKEN_RETRY
    d = policy.decide(FailureClass.ISSUER_DOWN, has_token=False, overrides=m)
    assert d.action is Action.HUMAN_QUEUE and "disabled" in d.rationale
    row = {r["failure_class"]: r for r in policy.policy_table(m)}["ISSUER_DOWN"]
    assert (row["action"], row["action_with_token"], row["override"]) == ("human_queue", "token_retry", "action")


def test_human_queue_all_is_a_kill_switch():
    m = over(human_queue_all=True, classes={"ISSUER_DOWN": {"delay_seconds": 1}})
    for cls in FailureClass:
        for token in (True, False):
            d = policy.decide(cls, has_token=token, overrides=m)
            assert d.action is Action.HUMAN_QUEUE and d.nudge is False and d.max_attempts == 0, cls


def test_risk_blocked_and_unknown_stay_human_queue_whatever_the_file_says():
    # a file may only name action=human_queue for them (a no-op); the entry is returned untouched
    m = over(classes={"RISK_BLOCKED": {"action": "human_queue"}, "UNKNOWN": {"action": "human_queue"}},
             disabled_actions=["recovery_link", "token_retry", "nudge_change_method"])
    for cls in (FailureClass.RISK_BLOCKED, FailureClass.UNKNOWN):
        assert policy.effective_entry(cls, m) is policy.POLICY[cls]
        assert policy.decide(cls, has_token=True, overrides=m) == policy.decide(cls, has_token=True)
    # even a hand-built object that bypasses validation cannot move them (enforced in policy, not only in the schema)
    forged = MerchantPolicy()
    forged.classes["RISK_BLOCKED"] = merchants.ClassOverride.model_construct(action="recovery_link", max_attempts=5, delay_seconds=0)
    forged.classes["UNKNOWN"] = merchants.ClassOverride.model_construct(action="token_retry", max_attempts=5)
    for cls in (FailureClass.RISK_BLOCKED, FailureClass.UNKNOWN):
        d = policy.decide(cls, has_token=True, retry_seq=1, overrides=forged)
        assert d.action is Action.HUMAN_QUEUE and d.max_attempts == 0


def test_forged_less_conservative_action_is_ignored_by_policy():
    forged = MerchantPolicy()
    forged.classes["INSUFFICIENT_FUNDS"] = merchants.ClassOverride.model_construct(action="token_retry", delay_seconds=0)
    d = policy.decide(FailureClass.INSUFFICIENT_FUNDS, has_token=True, overrides=forged)
    assert d.action is Action.RECOVERY_LINK  # the table's action; a file never invents a token charge


def test_gates_still_run_before_the_overridden_table():
    m = over(classes={"INSUFFICIENT_FUNDS": {"delay_seconds": 60}})
    assert policy.decide(FailureClass.INSUFFICIENT_FUNDS, has_token=False, reachable=False, overrides=m).action is Action.HUMAN_QUEUE
    from app.taxonomy import Classification
    low = Classification(FailureClass.INSUFFICIENT_FUNDS, "model", "llm", 0.2)
    assert policy.decide(FailureClass.INSUFFICIENT_FUNDS, has_token=False, classification=low, overrides=m).action is Action.HUMAN_QUEUE


def test_effective_table_marks_overridden_fields(tmp_path):
    write(tmp_path, "acme.json", {"classes": {"INSUFFICIENT_FUNDS": {"delay_seconds": 24 * H, "nudge": False},
                                              "ISSUER_DOWN": {"action": "human_queue"}}, "disabled_actions": ["token_retry"]})
    merchants.reload(tmp_path)
    rows = {r["failure_class"]: r for r in merchants.effective_table("acme")}
    assert rows["INSUFFICIENT_FUNDS"]["delay"] == "24h" and rows["INSUFFICIENT_FUNDS"]["override"] == "delay_seconds, nudge"
    assert rows["ISSUER_DOWN"]["action"] == "human_queue" and "action" in rows["ISSUER_DOWN"]["override"]
    assert rows["NETWORK_TIMEOUT"]["action_with_token"] == "recovery_link" and rows["NETWORK_TIMEOUT"]["override"] == "token_action"
    assert rows["RISK_BLOCKED"]["override"] == "" and rows["AUTH_ABANDONED"]["override"] == ""
    assert [r["failure_class"] for r in merchants.effective_table("nobody")] == [c.value for c in FailureClass]


def test_for_attempt_uses_the_merchant_id(tmp_path):
    write(tmp_path, "merchant_acme.json", {"nudge_language": "hi"})
    merchants.reload(tmp_path)

    class A:
        merchant_id = "merchant_acme"

    class B:
        merchant_id = "merchant_bolt"

    assert merchants.for_attempt(A()).nudge_language == "hi"
    assert merchants.for_attempt(B()) is None
    assert merchants.for_attempt(object()) is None


# ---- the CLI: fail loudly, never fall back -----------------------------------------------------

def test_cli_exits_1_on_a_malformed_merchant_file_and_prints_the_effective_table(tmp_path):
    mdir = tmp_path / "merchants"
    mdir.mkdir()
    env = dict(os.environ, DATABASE_URL=f"sqlite:///{tmp_path / 'cli.db'}", MERCHANTS_DIR=str(mdir))
    run = lambda *cmd: subprocess.run([sys.executable, "-m", "app.main", *cmd], cwd=ROOT, env=env,  # noqa: E731
                                      capture_output=True, text=True, timeout=120)
    write(mdir, "merchant_acme.json", {"classes": {"ISSUER_DOWN": {"action": "human_queue"}}, "nudge_language": "hi"})
    r = run("policy", "--merchant", "merchant_acme")
    assert r.returncode == 0, r.stderr
    assert "ISSUER_DOWN         human_queue" in r.stdout and "language=hi" in r.stdout
    assert "1 merchant override file(s)" in r.stdout
    r = run("policy")
    assert r.returncode == 0 and "per-merchant overrides: docs/merchants.md" in r.stdout

    write(mdir, "merchant_bad.json", {"classes": {"HARD_DECLINE": {"action": "token_retry"}}})
    r = run("show")
    assert r.returncode == 1 and "merchant_bad.json" in r.stderr and "classes.HARD_DECLINE.action" in r.stderr
    assert "does not fall back" in r.stderr and r.stdout == ""
    r = run("demo")
    assert r.returncode == 1 and "merchant_bad.json" in r.stderr and "seeded" not in r.stdout


# ---- written overrides (write_override / rollback_override, docs/web.md) ------------------------

def test_loader_ignores_the_history_trail_but_still_rejects_unknown_keys(tmp_path):
    write(tmp_path, "m.json", {"classes": {"INSUFFICIENT_FUNDS": {"delay_seconds": 4 * 86400}},
                               "_history": [{"when": "2026-09-05T12:00:00Z", "actor": "asha", "evidence": "OPS-12",
                                             "class": "INSUFFICIENT_FUNDS", "previous": None}]})
    merchants.reload(tmp_path)
    assert merchants.get("m").classes["INSUFFICIENT_FUNDS"].delay_seconds == 4 * 86400
    write(tmp_path, "bad.json", {"_history": [], "histroy": []})
    with pytest.raises(MerchantConfigError, match="histroy"):
        merchants.reload(tmp_path)


def test_write_override_round_trips_through_the_loader(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MERCHANTS_DIR", str(tmp_path), raising=False)
    merchants.reload(tmp_path)
    path = merchants.write_override("merchant_acme", "ISSUER_DOWN", {"action": "human_queue"}, evidence="OPS-7: bank outage",
                                    actor="asha")
    assert path == tmp_path / "merchant_acme.json"
    assert merchants.get("merchant_acme").classes["ISSUER_DOWN"].action == "human_queue"  # reloaded already
    assert merchants.Registry.load(tmp_path).merchants["merchant_acme"].classes["ISSUER_DOWN"].action == "human_queue"
    with pytest.raises(MerchantConfigError, match="human_queue"):  # the schema's own rule still holds
        merchants.write_override("merchant_acme", "ISSUER_DOWN", {"action": "token_retry"}, evidence="x", actor="asha")
    merchants.rollback_override("merchant_acme", "ISSUER_DOWN")
    assert merchants.get("merchant_acme").classes == {}
