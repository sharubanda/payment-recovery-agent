"""Drive the demo for a screen recording: type each command with a typewriter effect, run it,
then hold for the seconds the narration needs. Press record, run `python scripts/demo_video.py`,
and the terminal performs the five-minute demo in sync with docs/video (the narration file).

    python scripts/demo_video.py            # full run (~4 minutes of terminal time)
    python scripts/demo_video.py --fast     # rehearsal: no typing delay, short holds
    python scripts/demo_video.py --from 3   # start at scene 3

Runs against the fixture client unless .env has test keys; either way nothing here is different
from what `make demo` does. Uses the project's own venv python if present.
"""
import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "bin" / "python") if (ROOT / ".venv" / "bin" / "python").exists() else sys.executable
PRA = shutil.which("pra") or f"{PY} -m app.main"

# (scene, shell command, seconds to hold after the output, note printed dimly before the command)
SCENES = [
    (1, "clear", 1, "0:30  the problem is told over a blank terminal; then:"),
    (2, f"{PRA} doctor", 6, "twelve checks, a synthetic event through the real pipeline"),
    (3, f"PRA_MODE=shadow {PRA} import-csv docs/examples/failed_payments_export.csv --process", 10,
        "shadow mode: a merchant's export, every stage runs, nothing is sent"),
    (4, f"PRA_MODE=shadow {PRA} plan", 8, "the plan and its expected recovery, before anything goes out"),
    (5, "rm -f recovery.db; clear", 1, "now the live (fixture) run"),
    (6, f"make demo PY={PY}", 40, "22 events -> classify -> decide -> execute -> poll -> show"),
    (7, f"{PRA} audit --attempt 1 --no-data", 12, "one event's trail: ingest, classify, policy, schedule, execute, nudge, outcome"),
    (8, f"{PRA} ingest docs/examples/payment_failed_webhook.json --process --execute-now", 6, "a real payment.failed payload"),
    (9, f"{PRA} ingest docs/examples/payment_captured_webhook.json", 10, "the customer paid another way: link cancelled, reminders voided"),
    (10, f"make chaos PY={PY}", 25, "nine faults, each asserting an invariant"),
    (11, f"make simulate PY={PY}", 20, "500 synthetic events, baseline vs the table; a simulation, labelled"),
    (12, f"{PRA} queue --limit 3", 8, "the human queue, highest expected recovery first"),
]


def type_out(text: str, delay: float) -> None:
    for ch in text:
        sys.stdout.write(ch)
        sys.stdout.flush()
        time.sleep(delay)
    sys.stdout.write("\n")
    sys.stdout.flush()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fast", action="store_true", help="no typing delay, 1s holds (rehearsal)")
    ap.add_argument("--from", dest="start", type=int, default=1, metavar="SCENE")
    args = ap.parse_args(argv)
    os.chdir(ROOT)
    delay = 0 if args.fast else 0.035
    for scene, cmd, hold, note in SCENES:
        if scene < args.start:
            continue
        if note:
            sys.stdout.write(f"\033[2m# scene {scene}: {note}\033[0m\n")
        sys.stdout.write("\033[1;32m$\033[0m ")
        sys.stdout.flush()
        type_out(cmd, delay)
        subprocess.run(cmd, shell=True)
        time.sleep(1 if args.fast else hold)
    sys.stdout.write("\033[2m# end of demo\033[0m\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
