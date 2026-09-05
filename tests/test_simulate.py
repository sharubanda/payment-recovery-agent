"""The simulation is the one number in the README that a panel can poke at, so its shape is pinned:
deterministic under a seed, the stated event mix, the agent's zero double charges and zero risk
violations by construction, a gap that survives every seed, and a report whose caveat comes first."""
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

from app.taxonomy import FailureClass  # noqa: E402
from scripts import simulate as sim  # noqa: E402

N = 500


def test_generation_is_deterministic_and_matches_the_stated_mix():
    events = sim.generate_events(2000, 7)
    assert events == sim.generate_events(2000, 7)
    assert events != sim.generate_events(2000, 8)
    originals = [e for e in events if not e.duplicate]
    dups = [e for e in events if e.duplicate]
    assert len(events) == 2000 and len(dups) == round(0.04 * 2000)
    counts = Counter(e.failure_class for e in originals)
    for cls, share in sim.CLASS_MIX.items():
        assert abs(counts[cls] / len(originals) - share) < 0.03, (cls, counts[cls])
    assert all(e.method == "card" for e in originals if e.has_token)
    cards = [e for e in originals if e.method == "card"]
    assert abs(sum(e.has_token for e in cards) / len(cards) - 0.30) < 0.05
    assert all(19900 <= e.amount_paise <= 2499900 and e.amount_paise % 100 == 0 for e in originals)
    assert all(re.match(r"^pay_[0-9A-Za-z]{14}$", e.payment_id) for e in originals)
    assert len({e.payment_id for e in originals}) == len(originals)


def test_every_duplicate_follows_its_original_and_shares_its_identity():
    events = sim.generate_events(N, 42)
    seen = {}
    for pos, e in enumerate(events):
        if e.duplicate:
            original = seen[e.payment_id]
            assert original.index == e.index and original.failure_class is e.failure_class
            assert original.has_token == e.has_token and original.amount_paise == e.amount_paise
        else:
            assert e.payment_id not in seen
            seen[e.payment_id] = e


def test_coins_are_paired_across_policies_and_fixed_by_seed():
    assert sim.coin(42, 3, 1) == sim.coin(42, 3, 1)
    assert sim.coin(42, 3, 1) != sim.coin(43, 3, 1)
    assert sim.coin(42, 3, 1) != sim.coin(42, 3, 2)
    assert all(0.0 <= sim.coin(1, i, 1) < 1.0 for i in range(200))


def test_recovery_model_matches_the_docstring_table():
    table = sim.model_table_from_docstring().splitlines()[1:]
    parsed = {}
    for line in table:
        cls, kind, bucket, p = re.match(r"^(\w+)\s+(\w+)\s+(\w+)\s+(\d\.\d\d)\b", line).groups()
        parsed[(FailureClass(cls), kind, bucket)] = float(p)
    assert parsed == sim.RECOVERY_MODEL


def test_model_has_a_cell_for_every_action_either_policy_can_take():
    # the agent's cell for each class, with and without a token, and the baseline's 1h cell;
    # p_recover raises KeyError on a missing cell, and every present cell is a probability
    from app import policy
    for cls in FailureClass:
        cells = []
        for has_token in (False, True):
            d = policy.decide(cls, has_token=has_token)
            cells.append(sim.p_recover(cls, sim.KIND_OF_ACTION[d.action], policy.human_delay(d.delay_seconds)))
        cells += [sim.p_recover(cls, "token_retry", "1h"), sim.p_recover(cls, "link", "1h")]
        assert all(isinstance(p, float) and 0.0 <= p <= 1.0 for p in cells), (cls, cells)


def test_agent_never_double_charges_violates_risk_or_wastes_an_attempt():
    for seed in (1, 2, 3):
        events = sim.generate_events(N, seed)
        a = sim.run_agent(events, seed)
        assert (a.duplicate_charges, a.double_charged_paise, a.duplicate_links) == (0, 0, 0)
        assert a.risk_violations == 0 and a.wasted_attempts == 0 and a.duplicates_acted_on == 0
        assert a.duplicates_rejected == sum(1 for e in events if e.duplicate)
        parked = sum(1 for e in events if not e.duplicate
                     and e.failure_class in (FailureClass.RISK_BLOCKED, FailureClass.UNKNOWN))
        assert a.human_queued == parked
        assert a.payments == sum(1 for e in events if not e.duplicate) and a.events == N


def test_baseline_acts_on_every_delivery_including_the_damage():
    events = sim.generate_events(N, 42)
    b = sim.run_baseline(events, 42)
    assert b.attempts == N and b.human_queued == 0 and b.duplicates_rejected == 0
    assert b.risk_violations == sum(1 for e in events if e.failure_class is FailureClass.RISK_BLOCKED)
    dups = [e for e in events if e.duplicate]
    assert b.duplicates_acted_on == len(dups)
    assert b.duplicate_links == sum(1 for e in dups if not e.has_token)
    assert b.duplicate_charges <= sum(1 for e in dups if e.has_token)
    assert b.wasted_attempts >= len(dups)


def test_baseline_double_charges_exactly_when_the_original_token_charge_recovered():
    e = sim.Event(0, "pay_ABCDEFGHIJKLMN", FailureClass.ISSUER_DOWN, "card", True, 100000)
    dup = sim.Event(0, e.payment_id, e.failure_class, e.method, e.has_token, e.amount_paise, duplicate=True)
    p = sim.p_recover(FailureClass.ISSUER_DOWN, "token_retry", "1h")
    winning = next(s for s in range(1000) if sim.coin(s, 0, 1) < p)
    losing = next(s for s in range(1000) if sim.coin(s, 0, 1) >= p)
    hit = sim.run_baseline([e, dup], winning)
    assert (hit.recovered, hit.duplicate_charges, hit.double_charged_paise, hit.attempts) == (1, 1, 100000, 2)
    miss = sim.run_baseline([e, dup], losing)
    assert (miss.recovered, miss.duplicate_charges, miss.attempts) == (0, 0, 2)
    assert sim.run_agent([e, dup], winning).duplicates_rejected == 1
    assert sim.run_agent([e, dup], winning).attempts <= 3  # ISSUER_DOWN token chain: max 3 per the policy table


def test_agent_honours_the_policy_table_per_class():
    def one(cls, has_token, seed=0):
        return sim.run_agent([sim.Event(0, "pay_00000000000000", cls, "card", has_token, 50000)], seed)

    assert one(FailureClass.HARD_DECLINE, True).attempts == 1  # change-method link, never a token retry
    assert one(FailureClass.INSUFFICIENT_FUNDS, True).attempts == 1  # link on the salary cycle, no token chain
    assert one(FailureClass.RISK_BLOCKED, True).attempts == 0 and one(FailureClass.RISK_BLOCKED, True).human_queued == 1
    assert one(FailureClass.UNKNOWN, False).attempts == 0 and one(FailureClass.UNKNOWN, False).human_queued == 1
    # a token chain stops at max_attempts: pick a seed where every coin loses
    p_each = 1 - (1 - 0.70) ** (1 / 3)
    unlucky = next(s for s in range(5000) if all(sim.coin(s, 0, k) >= p_each for k in (1, 2, 3)))
    chain = one(FailureClass.ISSUER_DOWN, True, unlucky)
    assert chain.attempts == 3 and chain.recovered == 0
    lucky = next(s for s in range(5000) if sim.coin(s, 0, 1) < p_each)
    assert one(FailureClass.ISSUER_DOWN, True, lucky).attempts == 1
    assert one(FailureClass.NETWORK_TIMEOUT, True, unlucky).attempts <= 2


def test_agent_recovers_more_on_every_seed_and_the_sensitivity_line_says_so():
    deltas = sim.sensitivity(N)
    assert len(deltas) == len(sim.SENSITIVITY_SEEDS) and min(deltas) > 0
    line = sim.sensitivity_line(deltas)
    assert line.startswith("sensitivity:") and "never negative" in line
    assert f"min {min(deltas):+d}" in line and f"max {max(deltas):+d}" in line


def test_report_leads_with_the_exact_caveat_and_carries_the_model_table(tmp_path, capsys):
    out = tmp_path / "docs" / "simulation_report.md"
    assert sim.main(["--n", "200", "--seed", "3", "--write", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "simulate: 200 deliveries" in printed and "sensitivity:" in printed and f"wrote {out}" in printed
    text = out.read_text(encoding="utf-8")
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    assert paragraphs[0].startswith("# ")
    assert paragraphs[1] == sim.CAVEAT.format(n=200)
    assert "Measured on 200 synthetic failure events with a hand-specified recovery model." in paragraphs[1]
    assert sim.model_table_from_docstring() in text
    assert "| duplicate charges | " in text and "| risk-block violations | " in text
    assert "## Where the simulation is wrong" in text


def test_cli_runs_from_a_clean_clone_without_keys_or_a_database(tmp_path):
    # run from an empty cwd: the script must find the repo by itself and must not create a database there
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "simulate.py"), "--n", "60", "--seed", "5"],
                          cwd=tmp_path, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert "recovery rate" in proc.stdout and "duplicate charges" in proc.stdout
    assert not list(tmp_path.glob("*.db*"))
    bad = subprocess.run([sys.executable, str(ROOT / "scripts" / "simulate.py"), "--n", "0"],
                         cwd=tmp_path, capture_output=True, text=True, timeout=120)
    assert bad.returncode == 1
