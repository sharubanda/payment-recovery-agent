"""Evaluate the model path of classify_unmapped offline, from a recorded cassette.

The rules in app/classify.py score 100% on tests/fixtures/failure_cases.json (docs/classifier_eval.md);
the model only ever sees what the rules could not name. So the default run takes the UNKNOWN-by-design
cases (the ones a production event would actually send to the model) and, with --include-mapped, every
case, to measure how often the model agrees with the rules' labels. Each case goes through
classify_unmapped exactly as the pipeline calls it, against a CassetteLLM (app/llm_cassette.py):

  replay (default)   answers come from the cassette; a case with no recording is a miss (the fallback
                     llm_cassette_miss->human_queue), never a network call.
  --record           answers come from the real model (ANTHROPIC_API_KEY required) and are appended to
                     the cassette, so the next replay is this run, byte for byte.

Reported per case: expected label, the model's class, its confidence, whether the 0.7 gate (or a
fallback, or UNKNOWN) sends the event to a person; overall: agreement with the labels, the gate rate,
average latency and tokens as recorded in the cassette. The repository ships with NO cassette at the
default path: with none present this prints how to record one and exits 0, and --write produces a
document that says exactly that and claims nothing about the model.

Usage:  python scripts/eval_llm.py [--cassette PATH] [--write docs/llm_eval.md] [--include-mapped] [--record]
Record: LLM_CASSETTE_MODE=record ANTHROPIC_API_KEY=... python scripts/eval_llm.py --record
"""
import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # importable from a clean clone, any cwd

from app import config  # noqa: E402
from app import llm as llm_mod  # noqa: E402
from app.llm import AnthropicLLM, classify_unmapped  # noqa: E402
from app.llm_cassette import DEFAULT_CASSETTE_PATH, CassetteLLM, NullLLM  # noqa: E402
from app.taxonomy import FailureClass  # noqa: E402

DEFAULT_CASES = ROOT / "tests" / "fixtures" / "failure_cases.json"
UNKNOWN = FailureClass.UNKNOWN.value
EVAL_AMOUNT_PAISE = 249900  # the fixture has no amount; a fixed one keeps every prompt (and cassette key) stable
RECORD_COMMAND = "LLM_CASSETTE_MODE=record ANTHROPIC_API_KEY=... python scripts/eval_llm.py --record"
NOT_RECORDED = ("no cassette recorded yet; run with --record and a key: " + RECORD_COMMAND)
CAVEAT = ("The cases are hand-labelled and partly synthetic, not production traffic (docs/classifier_eval.md); "
          "agreement with these labels is agreement with the same rules the model exists to back up, not an "
          "estimate of production accuracy. The UNKNOWN-by-design cases are the ones a production event would "
          "actually send to the model, so the default run scores only those.")


def load_cases(path: Path) -> list[dict]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return payload["cases"] if isinstance(payload, dict) else payload


def to_attempt(case: dict) -> SimpleNamespace:
    """What _classify_user_content reads: the error fields, the method, a fixed amount. No customer, no ids."""
    return SimpleNamespace(error_code=case["error_code"], error_source=case["error_source"],
                           error_step=case["error_step"], error_reason=case["error_reason"],
                           error_description=case["error_description"], method=case["method"],
                           amount_paise=EVAL_AMOUNT_PAISE)


def select_cases(cases: list[dict], include_mapped: bool) -> list[dict]:
    return list(cases) if include_mapped else [c for c in cases if c["expected_class"] == UNKNOWN]


def make_client(mode: str, cassette: Path) -> CassetteLLM:
    if mode == "record":
        if not config.llm_live():
            raise SystemExit("--record needs ANTHROPIC_API_KEY (in the environment or .env); nothing was called")
        return CassetteLLM(AnthropicLLM(), "record", cassette)
    return CassetteLLM(NullLLM(), "replay", cassette)


def person_reason(c) -> str | None:
    """Why policy.decide would hand this event to a person, or None when the agent would act on the class."""
    if c.source != "llm":
        return "fallback"
    if c.failure_class is FailureClass.UNKNOWN:
        return "UNKNOWN"
    if c.confidence < config.LLM_MIN_CONFIDENCE:
        return f"<{config.LLM_MIN_CONFIDENCE}"
    return None


def run(cases: list[dict], client: CassetteLLM) -> list[dict]:
    rows = []
    for case in cases:
        before = len(llm_mod.usage_log)
        c = classify_unmapped(to_attempt(case), client=client)
        usage = llm_mod.usage_log[before:]  # this case's calls (attempt + optional repair)
        miss = (c.fallback_taken or "").startswith("llm_cassette_miss")
        rows.append({
            "id": case["id"], "expected": case["expected_class"], "note": case.get("note", ""),
            "description": case["error_description"] or "(empty)",
            "got": None if miss else c.failure_class.value, "confidence": None if c.source != "llm" else c.confidence,
            "rationale": c.reason, "fallback": c.fallback_taken, "miss": miss, "person": person_reason(c),
            "calls": len(usage), "model": c.llm_model,
            "latency_ms": sum(u["latency_ms"] for u in usage),
            "input_tokens": sum(u["input_tokens"] for u in usage),
            "output_tokens": sum(u["output_tokens"] for u in usage),
            "cache_read_input_tokens": sum(u["cache_read_input_tokens"] for u in usage),
        })
    return rows


def _cassette_latency(client: CassetteLLM) -> dict[str, dict]:
    """Recorded response fields by key (the wall-clock the replay measures is meaningless; the cassette's is real)."""
    return {e["key"]: e["response"] for e in client.entries()}


def metrics(rows: list[dict], client: CassetteLLM) -> dict:
    n = len(rows)
    answered = [r for r in rows if not r["miss"]]
    agree = sum(1 for r in answered if r["got"] == r["expected"])
    person = sum(1 for r in rows if r["person"])
    by_key = _cassette_latency(client)
    hits = [by_key[c["key"]] for c in client.calls if c["hit"] and c["key"] in by_key]
    avg = lambda f: (sum(int(h.get(f, 0) or 0) for h in hits) / len(hits)) if hits else None  # noqa: E731
    entries = client.entries()
    clients = sorted({e.get("client", "?") for e in entries})
    return {
        "n": n, "answered": len(answered), "misses": n - len(answered), "agree": agree,
        "agreement": agree / len(answered) if answered else None,
        "person": person, "gate_rate": person / n if n else None,
        "hits": len(hits), "avg_latency_ms": avg("latency_ms"), "avg_input_tokens": avg("input_tokens"),
        "avg_output_tokens": avg("output_tokens"), "avg_cache_read_input_tokens": avg("cache_read_input_tokens"),
        "records": len(entries), "clients": clients, "synthetic": bool(clients) and clients != ["anthropic"],
        "models": sorted({str(e.get("model", "?")) for e in entries}),
    }


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{100 * x:.1f}%"


def _num(x: float | None, unit: str = "") -> str:
    return "n/a" if x is None else f"{x:.0f}{unit}"


def _desc(text: str, width: int = 70) -> str:
    return text if len(text) <= width else text[: width - 3] + "..."


def provenance(m: dict, cassette: Path) -> str:
    if m["synthetic"]:
        return (f"SYNTHETIC cassette: recorded from {', '.join(m['clients'])} (a test double), not from a model; "
                f"the numbers below say nothing about any model")
    return f"recorded from {', '.join(m['clients'])}, model(s) {', '.join(m['models'])}"


def summary_text(rows: list[dict], m: dict, cassette: Path, mode: str, include_mapped: bool) -> str:
    scope = "every case (agreement with the rules' labels)" if include_mapped else "the UNKNOWN-by-design cases"
    lines = [f"eval_llm: {mode} against {cassette} ({m['records']} records; {provenance(m, cassette)})",
             f"{m['n']} cases: {scope}", "",
             f"{'id':<6} {'expected':<18} {'model':<18} {'conf':>5}  {'to a person':<12} {'fallback':<32} description"]
    for r in rows:
        got = r["got"] or "-"
        conf = "-" if r["confidence"] is None else f"{r['confidence']:.2f}"
        lines.append(f"{r['id']:<6} {r['expected']:<18} {got:<18} {conf:>5}  {(r['person'] or 'no'):<12} "
                     f"{(r['fallback'] or '-'):<32} {_desc(r['description'])}")
    lines += ["",
              f"agreement with labels {_pct(m['agreement'])} ({m['agree']}/{m['answered']} answered; "
              f"{m['misses']} cassette misses)",
              f"to a person (0.7 gate, UNKNOWN or a fallback) {_pct(m['gate_rate'])} ({m['person']}/{m['n']})",
              f"from the cassette over {m['hits']} replayed calls: avg latency {_num(m['avg_latency_ms'], ' ms')}, "
              f"avg tokens in {_num(m['avg_input_tokens'])} / out {_num(m['avg_output_tokens'])} / "
              f"cache read {_num(m['avg_cache_read_input_tokens'])}"]
    if m["misses"]:
        lines.append(f"misses: the cassette has no recording for {m['misses']} of these requests; record them with: "
                     + RECORD_COMMAND)
    lines.append(f"to record against the real model: {RECORD_COMMAND}")
    return "\n".join(lines)


def _conf(row: dict) -> str:
    return "-" if row["confidence"] is None else f"{row['confidence']:.2f}"


def _md_cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def not_recorded_markdown(cassette: Path) -> str:
    return f"""# LLM classifier evaluation

**Not recorded.** No cassette exists at `{_rel(cassette)}`: no call has been made against the real model
from this checkout, so this document makes no claim about the model's accuracy, confidence, latency or
token usage. That is the state the repository ships in.

To record one (one command, needs a key; every call is stored under `tests/fixtures/llm_cassettes/` and
replayed byte for byte afterwards, see docs/llm.md "Recording and replaying real calls"):

```
{RECORD_COMMAND}
```

then regenerate this page offline with `python scripts/eval_llm.py --write docs/llm_eval.md`.

{CAVEAT}
"""


def _rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def report_markdown(rows: list[dict], m: dict, cassette: Path, mode: str, include_mapped: bool) -> str:
    banner = ("> **SYNTHETIC.** " + provenance(m, cassette) + ". This page exists to prove the eval runs; "
              "record a real cassette to replace it.\n\n") if m["synthetic"] else ""
    scope = "every case, so agreement is agreement with the rules' labels" if include_mapped \
        else "the UNKNOWN-by-design cases only (what a production event would actually send to the model)"
    head = ["| id | expected | model | confidence | to a person | fallback | description | rationale |",
            "|---|---|---|---:|---|---|---|---|"]
    body = [f"| {r['id']} | {r['expected']} | {r['got'] or '-'} | {_conf(r)} | {r['person'] or 'no'} | "
            f"{r['fallback'] or '-'} | {_md_cell(_desc(r['description'], 90))} | "
            f"{_md_cell(_desc(r['rationale'] or '', 120))} |" for r in rows]
    return f"""# LLM classifier evaluation

{banner}Generated by `python scripts/eval_llm.py --write docs/llm_eval.md` in `{mode}` mode against `{_rel(cassette)}`
({m['records']} records; {provenance(m, cassette)}) over {scope}: {m['n']} cases through `classify_unmapped`, exactly as the pipeline calls it.

{CAVEAT}

## Headline

| metric | value |
|---|---:|
| agreement with the labels (over answered cases) | {_pct(m['agreement'])} ({m['agree']}/{m['answered']}) |
| cassette misses (no recording for the request; `llm_cassette_miss->human_queue`) | {m['misses']} |
| sent to a person (confidence below {config.LLM_MIN_CONFIDENCE}, UNKNOWN, or a fallback) | {_pct(m['gate_rate'])} ({m['person']}/{m['n']}) |
| replayed calls (attempt + repair) | {m['hits']} |
| average latency per call, as recorded | {_num(m['avg_latency_ms'], ' ms')} |
| average input / output / cache-read tokens per call, as recorded | {_num(m['avg_input_tokens'])} / {_num(m['avg_output_tokens'])} / {_num(m['avg_cache_read_input_tokens'])} |

## Per case

{chr(10).join(head + body)}

## Reproduce

```
{RECORD_COMMAND}                      # record (appends to the cassette; last record per request wins)
LLM_CASSETTE_MODE=replay python -m app.cli ...                       # replay the same answers in the pipeline, no network
python scripts/eval_llm.py --write docs/llm_eval.md                  # this page, offline
```
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="evaluate classify_unmapped from a recorded LLM cassette, offline")
    p.add_argument("--cassette", default=str(DEFAULT_CASSETTE_PATH), help="cassette path (JSON Lines)")
    p.add_argument("--cases", default=str(DEFAULT_CASES), help="path to the cases JSON")
    p.add_argument("--write", metavar="PATH", help="also write the Markdown report to this path")
    p.add_argument("--include-mapped", action="store_true", help="every case, not only the UNKNOWN-by-design ones")
    p.add_argument("--record", action="store_true", help="call the real model (needs a key) and append to the cassette")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    cassette = Path(args.cassette)
    mode = "record" if args.record else "replay"
    if mode == "replay" and not cassette.exists():
        print(NOT_RECORDED)
        if args.write:
            out = Path(args.write)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(not_recorded_markdown(cassette), encoding="utf-8")
            print(f"wrote {out}")
        return 0
    cases = select_cases(load_cases(Path(args.cases)), args.include_mapped)
    client = make_client(mode, cassette)
    rows = run(cases, client)
    m = metrics(rows, client)
    print(summary_text(rows, m, cassette, mode, args.include_mapped))
    if args.write:
        out = Path(args.write)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report_markdown(rows, m, cassette, mode, args.include_mapped), encoding="utf-8")
        print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
