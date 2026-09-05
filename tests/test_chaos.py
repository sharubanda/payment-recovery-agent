"""The chaos harness itself: every fault passes its invariant in-process, the harness leaves the
database binding, the keys and the fault registry as it found them, and the CLI writes the same
report twice (the committed docs/failure_report.md must be reproducible)."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app import config, db, faults

ROOT = Path(__file__).resolve().parents[1]

from scripts import chaos  # noqa: E402


@pytest.mark.parametrize("fault", faults.FAULTS)
def test_each_fault_passes_its_invariant(fault, tmp_path):
    # the harness must neither create nor touch ./recovery.db, which a prior `make demo` may have left behind
    db_path = ROOT / "recovery.db"
    existed_before = db_path.exists()
    mtime_before = db_path.stat().st_mtime if existed_before else None
    url_before, key_before = db.url(), config.ANTHROPIC_API_KEY
    r = chaos.run_fault(fault, tmp_path)
    assert r.error is None, r.error
    assert r.passed, [what for ok, what in r.checks if not ok]
    assert r.fallback and r.final_state and r.traces  # the report has something to say for every fault
    assert db.url() == url_before and config.ANTHROPIC_API_KEY == key_before and not faults.active()
    assert db_path.exists() == existed_before and (not existed_before or db_path.stat().st_mtime == mtime_before)


def test_every_fault_in_the_registry_has_a_scenario_and_a_rationale():
    assert set(chaos.SCENARIOS) == set(faults.FAULTS)
    for name in faults.FAULTS:
        assert name in chaos.INJECT and name in chaos.WHY and name in chaos.INVARIANT and name in chaos.INJECTED_AT


def test_timestamps_render_as_offsets_from_t0():
    assert chaos._relative("due 2026-09-05T12:00:00") == "due T0"
    assert chaos._relative("for 2026-09-05T12:45:00 and 2026-09-07T12:05:00") == "for T0+45m and T0+48h5m"
    assert chaos._relative("at 2026-09-05T11:25:00") == "at T0-35m"


def test_cli_all_writes_an_identical_report_twice(tmp_path):
    run = lambda out: subprocess.run([sys.executable, "scripts/chaos.py", "--all", "--write", str(out)],  # noqa: E731
                                     cwd=ROOT, capture_output=True, text=True, timeout=300)
    first, second = run(tmp_path / "one.md"), run(tmp_path / "two.md")
    assert first.returncode == 0, first.stdout + first.stderr
    assert second.returncode == 0, second.stdout + second.stderr
    assert "9 faults: 9 PASS, 0 FAIL" in first.stdout and "CHECK FAIL" not in first.stdout
    text = (tmp_path / "one.md").read_text(encoding="utf-8")
    assert text == (tmp_path / "two.md").read_text(encoding="utf-8")
    assert text.count("**Verdict:** PASS") == 9 and "FAIL" not in text.replace("PASS, 0 FAIL", "")
    assert "re-run with `make chaos`" in text
    for i, name in enumerate(faults.FAULTS, 1):
        assert f"| {i} | `{name}` |" in text and f"## {i}. {name}\n" in text


def test_cli_single_fault_and_write_guard(tmp_path):
    one = subprocess.run([sys.executable, "scripts/chaos.py", "--fault", "duplicate_event"],
                         cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert one.returncode == 0, one.stdout + one.stderr
    assert "FAULT duplicate_event" in one.stdout and "verdict     : PASS" in one.stdout and "SUMMARY" not in one.stdout
    guarded = subprocess.run([sys.executable, "scripts/chaos.py", "--fault", "llm_timeout", "--write", str(tmp_path / "x.md")],
                             cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert guarded.returncode == 2 and "--write needs --all" in guarded.stderr and not (tmp_path / "x.md").exists()


def test_scenario_faults_bite_at_the_cli_through_pra_faults(tmp_path):
    """The registry docstring promises PRA_FAULTS injects any fault into a subprocess; the three
    scenario faults have to do something visible there, not only inside the harness."""
    env = dict(os.environ, DATABASE_URL=f"sqlite:///{tmp_path / 'cli.db'}")
    run = lambda fault, *cmd: subprocess.run([sys.executable, "-m", "app.main", *cmd], cwd=ROOT,  # noqa: E731
                                             env=dict(env, PRA_FAULTS=fault), capture_output=True, text=True, timeout=120)
    down = run("db_unavailable", "process", "--all")
    assert down.returncode == 2 and "database unavailable" in down.stderr and not (tmp_path / "cli.db").exists()

    seeded = run("", "seed")
    assert seeded.returncode == 0
    twice = run("duplicate_event", "process", "--all", "--execute-now")
    assert twice.returncode == 0, twice.stderr
    lines = [ln for ln in twice.stdout.splitlines() if ln.startswith("pay_")]
    assert len(lines) == 44 and sum("skipped_duplicate" in ln for ln in lines) == 22
    assert "13 payment links" not in twice.stdout  # process does not print totals; the second pass sent nothing
    again = run("", "process", "--all")
    assert "nothing to process" in again.stdout

    off = run("unknown_error_code", "demo")
    assert off.returncode == 0 and "forced off by the unknown_error_code fault" in off.stdout
    assert "faults   : unknown_error_code (" in off.stdout and "[llm_unavailable->human_queue]" not in off.stdout

    # order_paid_elsewhere on a fresh database: every link the demo sends is cancelled again
    fresh = dict(env, DATABASE_URL=f"sqlite:///{tmp_path / 'cli2.db'}", PRA_FAULTS="order_paid_elsewhere")
    paid = subprocess.run([sys.executable, "-m", "app.main", "demo"], cwd=ROOT, env=fresh, capture_output=True,
                          text=True, timeout=120)
    assert paid.returncode == 0, paid.stderr
    assert "faults   : order_paid_elsewhere (" in paid.stdout
    assert paid.stdout.count("cancelled live link job(s)") == 15 and "PARKED" not in paid.stdout
    assert "no customer payment simulated this run (0 links still open)" in paid.stdout
    assert "cancelled 15" in paid.stdout and "0 recovered" in paid.stdout
