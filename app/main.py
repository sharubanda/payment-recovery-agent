"""CLI entrypoint: python -m app.main <command>. No server, no UI: a reviewer runs `make demo`.

Commands
  seed                              22 realistic failed-payment events (idempotent)
  process [--all | --attempt ID] [--execute-now]
  run-due [--now ISO]               execute pending jobs whose time has come
  poll                              ask Razorpay what became of each open link
  show [--attempt ID] [--by-value]  one aligned row per attempt: decision, job, link, outcome
  queue [--limit N]                 the human queue, highest expected recovery first (docs/merchants.md)
  policy [--merchant ID]            the policy table, or one merchant's effective table after overrides
  audit --attempt ID                the audit trail for one attempt, one line per stage
  mark-paid --job ID                record the outcome for a link you paid in test mode
  demo                              init + seed + process --all --execute-now + poll + show
  ingest FILE.json [--process] [--execute-now] [--signature HEX]
                                    one Razorpay webhook payload (payment.failed / payment_link.paid /
                                    payment_link.expired / payment.captured / order.paid)
  import-csv FILE.csv [--process] [--execute-now]
                                    the dashboard payments export, failed rows only, batch summary
  serve-webhook [--port 8080] [--process] [--execute-now]
                                    POST /razorpay/webhook receiver (needs a public URL, e.g. ngrok)
  resolve --attempt ID (--recovered PAISE | --closed) [--note TEXT] [--force]
                                    a person's decision on a human-queued attempt
  run-due --loop [--interval SECONDS]
                                    keep sweeping until Ctrl-C, a fresh session per sweep
  init / doctor / serve / digest / alert-test
                                    run it like a product: guided setup, health checks, ONE process
                                    (receiver + scheduler + operator view), a period summary (app/ops.py, docs/ops.md)

Exit codes: 0 ok; 2 the database refused a write (the event was NOT acknowledged: redeliver
it); 1 anything else. The header printed by `demo` says which Razorpay client and which LLM
are real, so nobody has to guess what they are watching.
"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from . import config, db, executor, faults, ingest, llm, merchants, ops, pipeline, priority, razorpay_client, scheduler
from .clock import utcnow
from .models import AuditEvent, Outcome, PaymentAttempt, RecoveryDecision, RecoveryJob
from .nudge_templates import format_rupees
from .policy import human_delay
from .razorpay_client import FaultingRazorpayClient, FixtureRazorpayClient, RazorpayError
from .taxonomy import JobStatus

EXIT_OK, EXIT_OTHER, EXIT_DB = 0, 1, 2
# What the three scenario faults do to a CLI run (PRA_FAULTS=...); the llm_*/razorpay_* faults act
# inside their clients. A path SQLite cannot create is what a dead database looks like at connect.
UNOPENABLE_DB_URL = "sqlite:////nonexistent/dir/recovery.db"
FAULT_NOTES = {
    "duplicate_event": "every event processed this run is delivered a second time",
    "order_paid_elsewhere": "every order that got a link this run is then paid another way, so the link is cancelled",
    "unknown_error_code": "the LLM is forced off, so every unmapped event goes to the human queue",
    "db_unavailable": f"the database is bound to {UNOPENABLE_DB_URL}",
}


# ---- clients and the honesty header --------------------------------------------------------

def _clients():
    """One client each per command: a Razorpay rate limit is then hit once per run, not per job."""
    return razorpay_client.get_client(), llm.get_client()


def _fixture_under(client) -> FixtureRazorpayClient | None:
    if isinstance(client, FixtureRazorpayClient):
        return client
    if isinstance(client, FaultingRazorpayClient) and isinstance(client.inner, FixtureRazorpayClient):
        return client.inner
    return None


def _describe(rz, lm) -> list[str]:
    if _fixture_under(rz) is not None:
        rz_line = "fixture client (no RAZORPAY_KEY_ID): in-memory Payment Links, nothing leaves this machine"
        if isinstance(rz, FaultingRazorpayClient):
            rz_line += f" [fault {rz.fault} injected]"
    else:
        rz_line = f"live TEST MODE ({config.RAZORPAY_KEY_ID[:12]}...): real Payment Links at {razorpay_client.API_BASE_URL}"
    if lm is None and faults.is_active("unknown_error_code"):
        lm_line = "none (forced off by the unknown_error_code fault): unmapped failures -> human queue, nudges -> templates"
    elif lm is None:
        lm_line = "none (ANTHROPIC_API_KEY unset): unmapped failures -> human queue, nudges -> templates"
    elif isinstance(lm, llm.FaultingLLM):
        lm_line = f"fault {lm.fault} injected: every model call degrades as the chaos harness expects"
    else:
        lm_line = f"anthropic {lm.model} (structured JSON, effort low, timeout {config.LLM_TIMEOUT_SECONDS:g}s)"
    active = ", ".join(f"{f} ({FAULT_NOTES[f]})" if f in FAULT_NOTES else f for f in sorted(faults.active())) or "none"
    return [f"razorpay : {rz_line}", f"llm      : {lm_line}", f"database : {db.url()}", f"faults   : {active}",
            f"merchants: {merchants.header_line()}", f"mode     : {ops.mode_line()}"]


# ---- formatting ------------------------------------------------------------------------------

def _ts(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d %H:%MZ") if dt else "-"


def _event_line(s: dict) -> str:
    by = s["classified_by"]
    when = "now" if s["delay_seconds"] <= 0 else f"in {human_delay(s['delay_seconds'])}"
    if s.get("schedule_note"):  # e.g. "in 48h -> 2 Oct 10:00 IST (salary window)"
        when = f"{when} -> " + "; ".join(f"{v.strip()} ({k.strip()})" for k, v in
                                        (part.split(":", 1) for part in s["schedule_note"].split(";") if ":" in part))
    head = (f"{s['payment_id']:<18} {s['method']:<10} {format_rupees(s['amount_paise']):>15}  "
            f"{s['failure_class'] + ' (' + by + ')':<30} -> {s['action']:<20} {when:<7} job#{s['job_id']:<3}")
    status = s["job_status"]
    if status == JobStatus.SENT.value:
        tail = f"sent {s['link_url'] or '(link already existed at Razorpay)'}  nudge:{s['nudge_source'] or '-'}"
    elif status == JobStatus.PENDING.value:
        tail = f"pending until {_ts(s['scheduled_at'])}"
    elif status == JobStatus.STUBBED.value:
        tail = "stubbed (token_retry: no real tokens in test mode)"
    elif status == JobStatus.FAILED.value:
        tail = f"failed after {s['attempts_made']} attempts: {s['last_error']}"
        if s.get("followup_job_id"):
            tail += (f"; retry job#{s['followup_job_id']} {s['followup_status']}"
                     + (f" at {_ts(s['followup_scheduled_at'])}" if s["followup_status"] == JobStatus.PENDING.value else ""))
        elif s.get("outcome_note"):
            tail += f"; {s['outcome_note'].split(':')[0]} (no automatic retry)"
    elif status == JobStatus.SKIPPED_DUPLICATE.value:
        tail = f"skipped_duplicate (idempotency key already present; existing job is {s['existing_job_status']}; nothing sent)"
    elif status in (JobStatus.HUMAN_QUEUE.value, JobStatus.NO_ACTION.value) and s.get("last_error"):
        tail = f"{status}: {s['last_error']}"
    else:
        tail = status
    if s["fallback_taken"]:
        tail += f"  [{s['fallback_taken']}]"
    return f"{head} {tail}"


def _table(headers: list[str], rows: list[list[str]], right: set[int] = frozenset()) -> str:
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]
    fmt = lambda cells: "  ".join((c.rjust(w) if i in right else c.ljust(w))  # noqa: E731
                                  for i, (c, w) in enumerate(zip(cells, widths))).rstrip()
    return "\n".join([fmt(headers), fmt(["-" * w for w in widths])] + [fmt(r) for r in rows])


def _latest(items):
    return max(items, key=lambda x: x.id) if items else None


def _outcome_text(o: Outcome | None, job: RecoveryJob | None) -> str:
    if o is not None:
        return f"recovered {format_rupees(o.amount_recovered_paise)}" if o.recovered else (o.note or "not recovered").split(":")[0]
    if job is not None and job.status == JobStatus.SENT.value:
        return "open"
    return "-"


def _show(session, attempt_id: int | None, by_value: bool = False) -> str:
    stmt = select(PaymentAttempt).order_by(PaymentAttempt.id)
    if attempt_id is not None:
        stmt = stmt.where(PaymentAttempt.id == attempt_id)
    attempts = session.execute(stmt).scalars().all()
    estimates = {}
    if by_value:  # highest expected recovery first; ties keep id order
        rates = priority.empirical_rates(session)
        for a in attempts:
            d = _latest(a.decisions)
            estimates[a.id] = priority.expected_recovery(a, d.failure_class if d else "UNKNOWN", d.action if d else "human_queue",
                                                         delay_seconds=d.delay_seconds if d else None, empirical=rates)
        attempts.sort(key=lambda a: (-estimates[a.id].expected_paise, a.id))
    rows = []
    for a in attempts:
        d, j, o = _latest(a.decisions), _latest(a.jobs), _latest(a.outcomes)
        rows.append(([format_rupees(estimates[a.id].expected_paise), estimates[a.id].source.split(" (")[0]] if by_value else []) + [
            str(a.id), a.razorpay_payment_id, a.method, format_rupees(a.amount_paise),
            d.failure_class if d else "-", d.classified_by if d else "-", d.action if d else "-",
            (human_delay(d.delay_seconds) + (f" -> {j.schedule_note}" if j is not None and j.schedule_note else "")) if d else "-",
            f"#{j.id}/{j.retry_seq}" if j else "-", j.status if j else "-",
            (j.razorpay_link_url or ("(exists)" if j.status == JobStatus.SENT.value else "-")) if j else "-",
            (j.nudge_source or "-") if j else "-", _outcome_text(o, j),
        ])
    headers = ["id", "payment_id", "method", "amount", "class", "by", "action", "delay", "job", "status", "link",
               "nudge", "outcome"]
    if by_value:
        return (_table(["expected", "source"] + headers, rows, right={0, 5})
                + f"\n  expected: {priority.PRIOR_NOTE}")
    return _table(headers, rows, right={3})


def _queue(session, limit: int | None) -> str:
    items = priority.human_queue(session)
    shown = items[:limit] if limit else items
    rows = [[str(it.attempt.id), it.attempt.razorpay_payment_id, it.attempt.method, format_rupees(it.attempt.amount_paise),
             format_rupees(it.estimate.expected_paise), f"{it.estimate.probability:.2f}", it.estimate.source,
             it.failure_class, it.classified_by, it.reason, f"#{it.job.id}/{it.job.retry_seq}", _ts(it.queued_at)]
            for it in shown]
    headers = ["id", "payment_id", "method", "amount", "expected", "p", "source", "class", "by", "reason", "job", "queued at"]
    head = (f"human queue: {len(items)} attempt(s) parked for a person, highest expected recovery first"
            + (f" (top {len(shown)})" if limit and len(items) > len(shown) else "")
            + f"; {format_rupees(sum(it.estimate.expected_paise for it in items))} expected in total")
    tail = (f"  expected: {priority.PRIOR_NOTE}\n"
            "  resolve one with: python -m app.main resolve --attempt ID (--recovered PAISE | --closed)")
    return head + "\n" + (_table(headers, rows, right={3, 4, 5}) if rows else "(nothing parked)") + "\n" + tail


def _policy_text(merchant_id: str | None) -> str:
    rows = merchants.effective_table(merchant_id)
    ov = merchants.get(merchant_id)
    head = (f"policy table for merchant {merchant_id}: {ov.describe() if ov else '(no override file; table defaults)'}"
            if merchant_id else "policy table (app/policy.py); per-merchant overrides: docs/merchants.md")
    table = _table(["class", "action", "with token", "delay", "backoff", "max attempts", "nudge", "override"],
                   [[r["failure_class"], r["action"], r["action_with_token"], r["delay"], f"x{r['backoff_multiplier']:g}",
                     str(r["max_attempts"]), "yes" if r["nudge"] else "no", r["override"] or "-"] for r in rows],
                   right={5})
    return f"{head}\n{table}\n  {merchants.header_line()}"


def _resolve_attempt(session, ref: str) -> PaymentAttempt | None:
    if ref.startswith("pay_"):
        return session.execute(select(PaymentAttempt).where(PaymentAttempt.razorpay_payment_id == ref)).scalars().first()
    try:
        return session.get(PaymentAttempt, int(ref))
    except ValueError:
        return None


# ---- fixture-only helpers, always printed as such ----------------------------------------------

def _fixture_rehydrate(session, fx: FixtureRazorpayClient) -> int:
    """The fixture is in-memory, so a new process has forgotten links created by an earlier one.
    Re-create them from the job rows (ids derive from reference_id, so they come back identical)."""
    known = {link["id"] for link in fx.links()}
    restored = 0
    for job in session.execute(select(RecoveryJob).where(RecoveryJob.status == JobStatus.SENT.value,
                                                         RecoveryJob.razorpay_link_id.is_not(None))).scalars().all():
        if job.razorpay_link_id in known:
            continue
        decision = session.get(RecoveryDecision, job.decision_id) if job.decision_id else None
        payload = executor.build_payment_link_payload(job.attempt, job,
                                                      decision.failure_class if decision else executor.UNSPECIFIED_CLASS)
        try:
            if fx.create_payment_link(payload)["id"] == job.razorpay_link_id:
                restored += 1
        except RazorpayError:
            pass
    return restored


def _fixture_simulate_payments(session, fx: FixtureRazorpayClient) -> list[RecoveryJob]:
    """Deterministic subset: every other sent link, by job id, over ALL sent links (not just the
    open ones) so a re-run picks the same subset again and simulates nothing new. Never silent."""
    sent = session.execute(select(RecoveryJob).where(RecoveryJob.status == JobStatus.SENT.value,
                                                     RecoveryJob.razorpay_link_id.is_not(None))
                           .order_by(RecoveryJob.id)).scalars().all()
    still_open = {job.id for job in pipeline.open_link_jobs(session)}
    known = {link["id"] for link in fx.links()}
    chosen = [job for i, job in enumerate(sent)
              if i % 2 == 0 and job.id in still_open and job.razorpay_link_id in known]
    for job in chosen:
        fx.mark_paid(job.razorpay_link_id)
    return chosen


def _print_outcomes(outcomes: list[Outcome], session) -> None:
    if not outcomes:
        print("poll: no new outcomes")
        return
    for o in outcomes:
        a = session.get(PaymentAttempt, o.attempt_id)
        verdict = f"recovered {format_rupees(o.amount_recovered_paise)}" if o.recovered else "not recovered"
        print(f"{a.razorpay_payment_id:<18} {verdict:<22} {o.note}")


# ---- commands --------------------------------------------------------------------------------

def cmd_seed(args, session) -> int:
    if str(config.BASE_DIR) not in sys.path:  # scripts/ is a sibling of app/, not a dependency of it
        sys.path.insert(0, str(config.BASE_DIR))
    from scripts.seed import existing_ids, seed
    before = len(existing_ids(session))
    rows = seed(session)
    print(f"seeded {len(rows) - before} events ({before} already present)")
    return EXIT_OK


def _redeliver(session, summaries: list[dict], rz, lm, *, execute_now: bool, now: datetime) -> list[dict]:
    """The duplicate_event fault at the CLI: every event just processed arrives a second time."""
    if not faults.is_active("duplicate_event"):
        return []
    again = []
    for s in summaries:
        attempt = session.get(PaymentAttempt, s["attempt_id"])
        again.append(pipeline.process_attempt(session, attempt, execute_now=execute_now, now=now, rz_client=rz,
                                              llm_client=lm))
    return again


def _pay_elsewhere(session, summaries: list[dict], rz, *, now: datetime) -> list[str]:
    """The order_paid_elsewhere fault at the CLI: every order that just got a link is paid another
    way (a checkout retry), which must cancel the link. Returns one printable line per order."""
    if not faults.is_active("order_paid_elsewhere"):
        return []
    lines = []
    for s in summaries:
        if s.get("job_status") != JobStatus.SENT.value or not s.get("link_id"):
            continue
        attempt = session.get(PaymentAttempt, s["attempt_id"])
        if not attempt.order_id:
            continue
        out = ingest.record_order_paid(session, {"event": ingest.EVENT_PAYMENT_CAPTURED, "order_id": attempt.order_id,
                                                 "payment_id": f"pay_elsewhere{attempt.id:06d}",
                                                 "amount_paise": attempt.amount_paise, "paid_at": now},
                                       rz_client=rz, now=now, source="fault:order_paid_elsewhere")
        lines.append(_ingest_line(out))
    return lines


def cmd_process(args, session) -> int:
    rz, lm = _clients()
    now = utcnow()
    if args.attempt:
        attempt = _resolve_attempt(session, args.attempt)
        if attempt is None:
            print(f"no attempt {args.attempt!r}", file=sys.stderr)
            return EXIT_OTHER
        summaries = [pipeline.process_attempt(session, attempt, execute_now=args.execute_now, now=now,
                                              rz_client=rz, llm_client=lm)]
    else:
        summaries = pipeline.process_all(session, execute_now=args.execute_now, now=now, rz_client=rz, llm_client=lm)
        if not summaries:
            total = session.execute(select(PaymentAttempt.id)).all()
            print(f"nothing to process: all {len(total)} attempts already decided and scheduled")
    summaries += _redeliver(session, summaries, rz, lm, execute_now=args.execute_now, now=now)
    for s in summaries:
        print(_event_line(s))
    for line in _pay_elsewhere(session, summaries, rz, now=now):
        print(line)
    if not args.attempt and summaries:
        print(_batch_line(summaries))
        for line in priority.batch_lines(session, summaries):
            print(line)
    return EXIT_OK


def _parse_now(text: str) -> datetime | None:
    try:
        now = datetime.fromisoformat(text)
    except ValueError:
        return None
    if now.tzinfo is not None:  # the agent speaks naive UTC everywhere; normalise before comparing
        now = now.astimezone(timezone.utc).replace(tzinfo=None)
    return now


def _sweep_line(jobs: list[RecoveryJob], now: datetime, session) -> str:
    counts: dict[str, int] = {}
    for j in jobs:
        counts[j.status] = counts.get(j.status, 0) + 1
    parts = ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "none"
    nxt = scheduler.next_due(session, now)
    hint = f"; next due job#{nxt.id} at {_ts(nxt.scheduled_at)}" if nxt else "; nothing pending"
    return f"sweep at {_ts(now)}: {len(jobs)} job(s) executed ({parts}){hint}"


def cmd_run_due(args, session) -> int:
    rz, lm = _clients()
    now = utcnow()
    if args.now:
        now = _parse_now(args.now)
        if now is None:
            print(f"error: --now {args.now!r} is not an ISO 8601 datetime (e.g. 2026-09-08T12:00:00)", file=sys.stderr)
            return EXIT_OTHER
    if getattr(args, "loop", False):
        if args.now:
            print("error: --loop follows the wall clock; it cannot be combined with --now", file=sys.stderr)
            return EXIT_OTHER
        interval = max(1, int(args.interval))
        print(f"run-due --loop: sweeping every {interval}s until Ctrl-C (database {db.url()})")
        try:
            while True:
                sweep = db.session()  # a fresh session per sweep: nothing stale survives a long-running loop
                try:
                    sweep_now = utcnow()
                    jobs = scheduler.run_due(sweep, now=sweep_now, rz_client=rz, llm_client=lm)
                    print(_sweep_line(jobs, sweep_now, sweep), flush=True)
                finally:
                    sweep.close()
                time.sleep(interval)
        except KeyboardInterrupt:
            print("\nrun-due --loop: stopped")
            return EXIT_OK
    jobs = scheduler.run_due(session, now=now, rz_client=rz, llm_client=lm)
    if not jobs:
        nxt = scheduler.next_due(session, now)
        hint = f"; next due job#{nxt.id} at {_ts(nxt.scheduled_at)}" if nxt else ""
        print(f"no jobs due at {_ts(now)}{hint}")
        return EXIT_OK
    for j in jobs:
        a = j.attempt
        extra = f" {j.razorpay_link_url}  nudge:{j.nudge_source or '-'}" if j.status == JobStatus.SENT.value else (
            f": {j.last_error}" if j.last_error else "")
        print(f"job#{j.id:<3} seq {j.retry_seq}  {a.razorpay_payment_id:<18} {j.action:<20} {j.status}{extra}")
    return EXIT_OK


def cmd_poll(args, session) -> int:
    rz, _ = _clients()
    fx = _fixture_under(rz)
    if fx is not None and (restored := _fixture_rehydrate(session, fx)):
        print(f"[fixture] restored {restored} in-memory links from the database (the fixture forgets them between runs)")
    _print_outcomes(pipeline.poll_outcomes(session, rz_client=rz), session)
    return EXIT_OK


def cmd_show(args, session) -> int:
    attempt_id = None
    if args.attempt:
        a = _resolve_attempt(session, args.attempt)
        if a is None:
            print(f"no attempt {args.attempt!r}", file=sys.stderr)
            return EXIT_OTHER
        attempt_id = a.id
    print(_show(session, attempt_id, by_value=bool(getattr(args, "by_value", False))))
    return EXIT_OK


def cmd_queue(args, session) -> int:
    print(_queue(session, args.limit))
    return EXIT_OK


def cmd_policy(args, session) -> int:
    print(_policy_text(args.merchant))
    return EXIT_OK


def cmd_audit(args, session) -> int:
    a = _resolve_attempt(session, args.attempt)
    if a is None:
        print(f"no attempt {args.attempt!r}", file=sys.stderr)
        return EXIT_OTHER
    rows = session.execute(select(AuditEvent).where(AuditEvent.attempt_id == a.id).order_by(AuditEvent.id)).scalars().all()
    print(f"audit trail for attempt {a.id} ({a.razorpay_payment_id}, {a.method}, {format_rupees(a.amount_paise)}): {len(rows)} rows")
    for r in rows:
        print(f"{r.created_at:%Y-%m-%d %H:%M:%S}  {r.stage:<9} {r.message}")
        if r.data_json and not args.no_data:
            try:
                data = json.dumps(json.loads(r.data_json), sort_keys=True)
            except ValueError:
                data = r.data_json
            print(f"{'':30}{data[:300] + ('...' if len(data) > 300 else '')}")
    return EXIT_OK


def cmd_mark_paid(args, session) -> int:
    rz, _ = _clients()
    job = session.get(RecoveryJob, args.job)
    if job is None or job.status != JobStatus.SENT.value or not job.razorpay_link_id:
        print(f"job {args.job} is not a sent job with a payment link", file=sys.stderr)
        return EXIT_OTHER
    if session.execute(select(Outcome.id).where(Outcome.job_id == job.id)).first():
        print(f"job#{job.id}: outcome already recorded")
        return EXIT_OK
    fx = _fixture_under(rz)
    if fx is not None:
        _fixture_rehydrate(session, fx)
        fx.mark_paid(job.razorpay_link_id)
        print(f"[fixture] simulating customer payment on {job.razorpay_link_id}")
    try:
        link = rz.fetch_payment_link(job.razorpay_link_id)
    except RazorpayError as exc:
        print(f"could not fetch {job.razorpay_link_id}: {exc}", file=sys.stderr)
        return EXIT_OTHER
    if link.get("status") != "paid":
        print(f"{job.razorpay_link_id} status is {link.get('status')!r}, not paid; nothing recorded")
        return EXIT_OTHER
    paid = int(link.get("amount_paid") or job.attempt.amount_paise)
    o = executor.record_outcome(session, job.attempt, job, True, paid, f"payment_link.paid via mark-paid ({job.razorpay_link_id})")
    print(f"{job.attempt.razorpay_payment_id} recovered {format_rupees(o.amount_recovered_paise)} (job#{job.id})")
    return EXIT_OK


def cmd_demo(args, session) -> int:
    rz, lm = _clients()
    print("payment-recovery-agent demo")
    for line in _describe(rz, lm):
        print(f"  {line}")
    print("  note     : --execute-now runs each job immediately instead of at its scheduled time; the audit trail says so")
    print("\n== seed")
    cmd_seed(args, session)

    print("\n== process --all --execute-now")
    now = utcnow()
    summaries = pipeline.process_all(session, execute_now=True, now=now, rz_client=rz, llm_client=lm)
    if not summaries:
        total = session.execute(select(PaymentAttempt.id)).all()
        print(f"nothing to process: all {len(total)} attempts already decided and scheduled "
              "(re-run is idempotent; no new links)")
    summaries += _redeliver(session, summaries, rz, lm, execute_now=True, now=now)
    for s in summaries:
        print(_event_line(s))
    for line in _pay_elsewhere(session, summaries, rz, now=now):
        print(line)

    fx = _fixture_under(rz)
    if fx is not None:
        restored = _fixture_rehydrate(session, fx)
        if restored:
            print(f"\n[fixture] restored {restored} in-memory links from the database (the fixture forgets them between runs)")
        paid = _fixture_simulate_payments(session, fx)
        open_count = len(pipeline.open_link_jobs(session))
        if paid:
            print(f"\n[fixture] simulating customer payment on {len(paid)} of {open_count} open links "
                  f"(every other one, by job id): {', '.join(j.razorpay_link_id for j in paid)}")
        else:
            print(f"\n[fixture] no customer payment simulated this run ({open_count} links still open)")
    else:
        print("\nlive test mode: pay a link from the table below, then `python -m app.main poll` or `mark-paid --job ID`")

    print("\n== poll")
    _print_outcomes(pipeline.poll_outcomes(session, rz_client=rz, now=utcnow()), session)

    print("\n== show")
    print(_show(session, None))
    _print_totals(session)
    return EXIT_OK


def _print_totals(session) -> None:
    jobs = session.execute(select(RecoveryJob)).scalars().all()
    latest = {}
    for j in jobs:  # one status per attempt: its latest job
        if j.attempt_id not in latest or j.id > latest[j.attempt_id].id:
            latest[j.attempt_id] = j
    counts: dict[str, int] = {}
    for j in latest.values():
        counts[j.status] = counts.get(j.status, 0) + 1
    outcomes = session.execute(select(Outcome).where(Outcome.recovered.is_(True))).scalars().all()
    total = sum(o.amount_recovered_paise for o in outcomes)
    links = sum(1 for j in jobs if j.razorpay_link_id)
    parts = ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
    print(f"\ntotals: {len(latest)} attempts -> {parts}; {links} payment links created; "
          f"{len(outcomes)} recovered for {format_rupees(total)}")


# ---- real Razorpay inputs: webhook files, the dashboard export, the receiver -------------------

def _read_json_file(path: str) -> tuple[bytes, dict] | None:
    try:
        raw = open(path, "rb").read()
    except OSError as exc:
        print(f"error: cannot read {path}: {exc}", file=sys.stderr)
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        print(f"error: {path} is not valid JSON: {exc}", file=sys.stderr)
        return None
    if not isinstance(payload, dict):
        print(f"error: {path} must contain a JSON object", file=sys.stderr)
        return None
    return raw, payload


def _ingest_line(out: dict) -> str:
    if "ignored" in out:
        return f"ignored event {out['ignored']!r}: the agent acts on {', '.join(ingest.HANDLED_EVENTS)} only"
    if out.get("event") == ingest.EVENT_LINK_EXPIRED:
        if out.get("closed"):
            tail = ""
            if out.get("followup_job_id"):
                tail = f"; follow-up job#{out['followup_job_id']} {out['followup_status']}"
                if out["followup_status"] == JobStatus.PENDING.value:
                    tail += f" at {_ts(out['followup_scheduled_at'])}" + (f" ({out['followup_note']})" if out.get("followup_note") else "")
            return f"{out['payment_id']:<18} not recovered: payment link expired ({out['link_id']}, job#{out['job_id']}){tail}"
        if out.get("already_closed"):
            return f"{out['payment_id']:<18} payment_link.expired redelivered for {out['link_id']}: outcome already recorded, nothing changed"
        return f"payment_link.expired for {out['link_id']} matches no recovery job here; ignored"
    if out.get("event") in ingest.ORDER_PAID_EVENTS or (out.get("event") == ingest.EVENT_LINK_PAID and "cancelled" in out):
        if not out.get("matched"):
            return f"{out['event']} for order {out.get('order_id')} matches no attempt here; ignored"
        parts = []
        if out.get("voided"):
            parts.append(f"voided pending job(s) {', '.join('#' + str(i) for i in out['voided'])}")
        if out.get("cancelled"):
            parts.append(f"cancelled live link job(s) {', '.join('#' + str(i) for i in out['cancelled'])}")
        if out.get("parked"):
            parts.append(f"PARKED job(s) {', '.join('#' + str(i) for i in out['parked'])} (cancel failed; a person cancels the link)")
        what = "; ".join(parts) or ("already recorded, nothing changed" if out.get("already_recorded") else "nothing pending, no live link")
        return f"order {out['order_id']:<20} paid elsewhere by {out['payment_id']}: {what}"
    if out.get("event") == ingest.EVENT_LINK_PAID:
        if out.get("closed"):
            return (f"{out['payment_id']:<18} recovered {format_rupees(out['amount_recovered_paise']):<15} "
                    f"payment_link.paid via webhook ({out['link_id']}, job#{out['job_id']})")
        if out.get("already_closed"):
            return f"{out['payment_id']:<18} payment_link.paid redelivered for {out['link_id']}: outcome already recorded, nothing changed"
        return f"payment_link.paid for {out['link_id']} matches no recovery job here; ignored"
    head = f"{out['payment_id']:<18} {'redelivery' if out.get('redelivery') else 'ingested'} as attempt {out['attempt_id']}"
    if not out.get("processed"):
        return f"{head} (not processed; run `process --attempt {out['payment_id']}` or pass --process)"
    return f"{head}\n{_event_line(out['summary'])}"


def cmd_ingest(args, session) -> int:
    loaded = _read_json_file(args.file)
    if loaded is None:
        return EXIT_OTHER
    raw, payload = loaded
    secret = config.RAZORPAY_WEBHOOK_SECRET
    if secret and args.signature:
        if not ingest.verify_signature(raw, args.signature, secret):
            print("error: X-Razorpay-Signature does not match the file's HMAC-SHA256 under RAZORPAY_WEBHOOK_SECRET; "
                  "refusing to ingest", file=sys.stderr)
            return EXIT_OTHER
        print("signature ok")
    elif secret:
        print("warning: RAZORPAY_WEBHOOK_SECRET is set but no --signature was given; a file carries no headers, "
              "so the payload is ingested unverified", file=sys.stderr)
    elif args.signature:
        print("warning: --signature given but RAZORPAY_WEBHOOK_SECRET is unset; nothing to verify against",
              file=sys.stderr)
    rz, lm = _clients()
    fx = _fixture_under(rz)
    if fx is not None and (restored := _fixture_rehydrate(session, fx)):
        print(f"[fixture] restored {restored} in-memory links from the database (the fixture forgets them between runs)")
    try:
        out = ingest.ingest_event(session, payload, process=args.process, execute_now=args.execute_now, now=utcnow(),
                                  source=f"file:{args.file}", rz_client=rz, llm_client=lm)
    except ingest.IngestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_OTHER
    print(_ingest_line(out))
    return EXIT_OK


def _batch_line(summaries: list[dict]) -> str:
    t = ingest.batch_totals(summaries)
    return (f"batch: {t['events']} events, {format_rupees(t['amount_at_risk_paise'])} at risk -> "
            f"{t['linked']} got a link, {t['pending']} pending, {t['human_queue']} human-queued, "
            f"{t['stubbed']} stubbed, {t['failed']} failed, {t['no_action']} no_action, {t['duplicates']} duplicates")


def cmd_import_csv(args, session) -> int:
    try:
        parsed = ingest.parse_payments_csv(args.file)
    except OSError as exc:
        print(f"error: cannot read {args.file}: {exc}", file=sys.stderr)
        return EXIT_OTHER
    except ingest.IngestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_OTHER
    created, present = ingest.import_rows(session, parsed.rows, now=utcnow(), source=f"csv:{args.file}")
    skipped = f"{len(parsed.skipped)} skipped: {parsed.skipped_reasons()}" if parsed.skipped else "0 skipped"
    print(f"imported {len(created)} failed payments ({len(present)} already present, {skipped})")
    for s in parsed.skipped:
        print(f"  line {s['line']}: {s['reason']}" + (f" ({s['id']})" if s.get("id") else ""))
    if parsed.not_failed:
        print("  not imported (status is not failed): " + ", ".join(f"{k} {v}" for k, v in sorted(parsed.not_failed.items())))
    if not args.process:
        if created or present:
            print("run `process --all` (or re-run with --process) to classify, decide and schedule them")
        return EXIT_OK
    rz, lm = _clients()
    now = utcnow()
    unscheduled = {a.id for a in pipeline.unscheduled_attempts(session)}
    todo = created + [a for a in present if a.id in unscheduled]
    summaries = [pipeline.process_attempt(session, a, execute_now=args.execute_now, now=now, rz_client=rz, llm_client=lm)
                 for a in todo]
    summaries += _redeliver(session, summaries, rz, lm, execute_now=args.execute_now, now=now)
    for s in summaries:
        print(_event_line(s))
    print(_batch_line(summaries))
    for line in priority.batch_lines(session, summaries):
        print(line)
    return EXIT_OK


def cmd_serve_webhook(args, session) -> int:
    session.close()  # the receiver opens one session per request, under a lock
    rz, lm = _clients()
    secret = config.RAZORPAY_WEBHOOK_SECRET
    try:
        server = ingest.serve(host=args.host, port=args.port, secret=secret, process=args.process,
                              execute_now=args.execute_now, rz_client=rz, llm_client=lm)
    except OSError as exc:
        print(f"error: cannot listen on {args.host}:{args.port}: {exc}", file=sys.stderr)
        return EXIT_OTHER
    host, port = server.server_address[:2]
    print("payment-recovery-agent webhook receiver")
    for line in _describe(rz, lm):
        print(f"  {line}")
    print(f"  signature: {'verified against RAZORPAY_WEBHOOK_SECRET' if secret else 'NOT verified (RAZORPAY_WEBHOOK_SECRET unset)'}")
    print(f"  process  : {'classify -> decide -> schedule' + (' -> execute now' if args.execute_now else '') if args.process else 'ingest only (run `process --all` later)'}")
    print(f"  listening: http://{host}:{port}{ingest.WEBHOOK_PATH}  (GET {ingest.HEALTH_PATH} for a health check)")
    print("  note     : Razorpay needs a public URL; expose this port with e.g. `ngrok http "
          f"{port}` and register <public-url>{ingest.WEBHOOK_PATH} under Dashboard -> Settings -> Webhooks")
    if not secret:
        print("warning: RAZORPAY_WEBHOOK_SECRET is unset; any POST is accepted unsigned. Set it before exposing "
              "this port.", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nwebhook receiver: stopped")
    finally:
        server.server_close()
    return EXIT_OK


RESOLVED_PREFIX = "resolved by a person"


def cmd_resolve(args, session) -> int:
    a = _resolve_attempt(session, args.attempt)
    if a is None:
        print(f"no attempt {args.attempt!r}", file=sys.stderr)
        return EXIT_OTHER
    if args.recovered is not None and args.recovered <= 0:
        print("error: --recovered takes a positive amount in paise (e.g. 249900 for Rs 2,499.00)", file=sys.stderr)
        return EXIT_OTHER
    job = _latest(a.jobs)
    outcomes = sorted(a.outcomes, key=lambda o: o.id)
    # the parked "human_queue: ..." outcome the pipeline writes at decision time is a placeholder,
    # not a decision; a recovered outcome or an earlier resolve is, and needs --force to override
    decided = [o for o in outcomes if o.recovered or (o.note or "").startswith(RESOLVED_PREFIX)]
    if decided and not args.force:
        last = decided[-1]
        what = f"recovered {format_rupees(last.amount_recovered_paise)}" if last.recovered else last.note
        print(f"attempt {a.id} ({a.razorpay_payment_id}) already has an outcome: {what} (outcome#{last.id}); "
              f"pass --force to record another", file=sys.stderr)
        return EXIT_OTHER
    if job is not None and job.status != JobStatus.HUMAN_QUEUE.value and not args.force:
        print(f"attempt {a.id} ({a.razorpay_payment_id}) is not human-queued (latest job#{job.id} is {job.status}); "
              f"pass --force to record a person's outcome anyway", file=sys.stderr)
        return EXIT_OTHER
    note = f" ({args.note})" if args.note else ""
    if args.recovered is not None:
        text = f"{RESOLVED_PREFIX}: recovered {format_rupees(args.recovered)} outside the agent{note}"
        o = executor.record_outcome(session, a, job, True, args.recovered, text)
        print(f"{a.razorpay_payment_id} recovered {format_rupees(o.amount_recovered_paise)} (outcome#{o.id}, attempt {a.id})")
    else:
        text = f"{RESOLVED_PREFIX}: closed, not recovered{note}"
        o = executor.record_outcome(session, a, job, False, 0, text)
        print(f"{a.razorpay_payment_id} closed, not recovered (outcome#{o.id}, attempt {a.id})")
    return EXIT_OK


# ---- entrypoint -------------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m app.main", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="command")
    sub.add_parser("seed", help="insert the 22 seed events (idempotent)")
    pr = sub.add_parser("process", help="classify -> decide -> schedule (-> execute) failed payments")
    g = pr.add_mutually_exclusive_group()
    g.add_argument("--all", action="store_true", help="every attempt without a recovery job yet (default)")
    g.add_argument("--attempt", metavar="ID", help="one attempt, by id or pay_... (re-running it demonstrates skipped_duplicate)")
    pr.add_argument("--execute-now", action="store_true", help="run the job immediately instead of waiting for its delay")
    rd = sub.add_parser("run-due", help="execute pending jobs whose scheduled time has passed")
    rd.add_argument("--now", metavar="ISO", help="pretend it is this UTC time, e.g. 2026-09-08T12:00:00")
    rd.add_argument("--loop", action="store_true", help="keep sweeping until Ctrl-C (a fresh session per sweep)")
    rd.add_argument("--interval", type=int, default=60, metavar="SECONDS", help="seconds between sweeps with --loop (default 60)")
    sub.add_parser("poll", help="fetch each open payment link and record paid / expired outcomes")
    sh = sub.add_parser("show", help="one row per attempt")
    sh.add_argument("--attempt", metavar="ID")
    sh.add_argument("--by-value", action="store_true", help="highest expected recovery first, with the estimate's source")
    qu = sub.add_parser("queue", help="the human queue, highest expected recovery first (amount x P(recover), source named)")
    qu.add_argument("--limit", type=int, metavar="N", help="show only the top N")
    po = sub.add_parser("policy", help="the policy table, or one merchant's effective table after merchants/<id>.json")
    po.add_argument("--merchant", metavar="ID", help="merchant_id whose overrides to apply")
    au = sub.add_parser("audit", help="the audit trail for one attempt")
    au.add_argument("--attempt", metavar="ID", required=True)
    au.add_argument("--no-data", action="store_true", help="messages only, without the data payloads")
    mp = sub.add_parser("mark-paid", help="record the outcome for a link you paid in test mode")
    mp.add_argument("--job", type=int, required=True)
    sub.add_parser("demo", help="seed + process --all --execute-now + poll + show, with an honesty header")
    ing = sub.add_parser("ingest", help="one Razorpay webhook payload from a file (payment.failed, payment_link.paid/expired, payment.captured, order.paid)")
    ing.add_argument("file", metavar="FILE.json")
    ing.add_argument("--process", action="store_true", help="classify -> decide -> schedule the attempt right away")
    ing.add_argument("--execute-now", action="store_true", help="with --process: run the job immediately")
    ing.add_argument("--signature", metavar="HEX", help="the X-Razorpay-Signature to verify against RAZORPAY_WEBHOOK_SECRET")
    ic = sub.add_parser("import-csv", help="the dashboard payments export: failed rows in, batch summary out")
    ic.add_argument("file", metavar="FILE.csv")
    ic.add_argument("--process", action="store_true", help="process every imported attempt and print the batch totals")
    ic.add_argument("--execute-now", action="store_true", help="with --process: run each job immediately")
    sw = sub.add_parser("serve-webhook", help="HTTP receiver for Razorpay webhooks (POST /razorpay/webhook, GET /health)")
    sw.add_argument("--host", default="0.0.0.0")
    sw.add_argument("--port", type=int, default=8080)
    sw.add_argument("--process", action="store_true", help="process each payment.failed as it arrives")
    sw.add_argument("--execute-now", action="store_true", help="with --process: run the job immediately")
    rs = sub.add_parser("resolve", help="a person's decision on a human-queued attempt")
    rs.add_argument("--attempt", metavar="ID", required=True, help="attempt id or pay_...")
    rg = rs.add_mutually_exclusive_group(required=True)
    rg.add_argument("--recovered", type=int, metavar="AMOUNT_PAISE", help="the payment was recovered for this amount")
    rg.add_argument("--closed", action="store_true", help="closed without recovery")
    rs.add_argument("--note", metavar="TEXT", help="why, for the audit trail")
    rs.add_argument("--force", action="store_true", help="record even if a decided outcome already exists")
    ins = sub.add_parser("insights", help="recovery rates by cause and delay with intervals, LLM usage, money by merchant, "
                                          "and policy proposals (evidence for a person; never applied)")
    ins.add_argument("--write", metavar="PATH", help="also write the report as Markdown")
    ins.add_argument("--min-samples", type=int, default=30, metavar="N", help="per-bucket sample size a proposal needs (default 30)")
    ins.add_argument("--merchant", metavar="ID", help="restrict to one merchant_id")
    prr = sub.add_parser("propose-rules", help="recurring unmapped descriptions as rule candidates for app/classify.py (a person adds them)")
    prr.add_argument("--write", metavar="PATH", help="also write the candidates as Markdown")
    prr.add_argument("--min-occurrences", type=int, default=3, metavar="N", help="decisions a group needs to become a candidate (default 3)")
    ops.add_subparsers(sub)  # init, doctor, serve, digest, alert-test (app/ops.py)
    ap = sub.add_parser("apply-proposal", help="apply an insights proposal as a merchant override, with evidence and a rollback entry")
    ap.add_argument("--proposal", required=True, metavar="ID", help="proposal id from `insights` (e.g. INSUFFICIENT_FUNDS-48h-to-24h)")
    ap.add_argument("--merchant", metavar="ID", help="merchant_id, or omit for all merchants (_default.json)")
    ap.add_argument("--actor", required=True, metavar="NAME", help="who is applying it (audited)")
    rb = sub.add_parser("rollback-override", help="restore the previous override entry for a class")
    rb.add_argument("--merchant", metavar="ID", help="merchant_id, or omit for _default.json")
    rb.add_argument("--class", dest="class_name", required=True, metavar="CLASS")
    rb.add_argument("--actor", required=True, metavar="NAME")
    sub.add_parser("plan", help="shadow-mode summary: what the agent would have done, by action, with expected recovery")
    return p


def cmd_apply_proposal(args, session) -> int:
    from . import merchants
    argv = ["apply", "--proposal", args.proposal, "--actor", args.actor]
    argv += ["--merchant", args.merchant] if args.merchant else ["--all"]
    return merchants.main(argv)


def cmd_rollback_override(args, session) -> int:
    from . import merchants
    argv = ["rollback", "--class", args.class_name, "--actor", args.actor]
    argv += ["--merchant", args.merchant] if args.merchant else ["--all"]
    return merchants.main(argv)


def cmd_plan(args, session) -> int:
    s = pipeline.shadow_summary(session)
    if not s.get("jobs") and not s.get("by_action"):
        print("no shadow jobs yet: run with PRA_MODE=shadow (pra init --mode shadow), ingest or import a batch, then come back")
        return EXIT_OK
    print(f"shadow plan: {s.get('jobs', 0)} job(s) that would have gone out; {s.get('reminders', 0)} reminder(s)")
    for action, row in (s.get("by_action") or {}).items():
        print(f"  {action:<20} {row.get('jobs', 0):>4}  amount {format_rupees(row.get('amount_paise', 0)):>14}  "
              f"expected {format_rupees(row.get('expected_paise', 0)):>14}")
    if "expected_paise" in s:
        print(f"  expected recovery (simulation prior until n >= 30): {format_rupees(s['expected_paise'])}")
    return EXIT_OK


def cmd_insights(args, session) -> int:
    from . import insights
    argv = ["--min-samples", str(args.min_samples)]
    if args.write:
        argv += ["--write", args.write]
    if args.merchant:
        argv += ["--merchant", args.merchant]
    return insights.main(argv)


def cmd_propose_rules(args, session) -> int:
    from . import rule_candidates
    argv = ["--min-occurrences", str(args.min_occurrences)]
    if args.write:
        argv += ["--write", args.write]
    return rule_candidates.main(argv)


COMMANDS = {"seed": cmd_seed, "process": cmd_process, "run-due": cmd_run_due, "poll": cmd_poll, "show": cmd_show,
            "audit": cmd_audit, "mark-paid": cmd_mark_paid, "demo": cmd_demo, "ingest": cmd_ingest,
            "import-csv": cmd_import_csv, "serve-webhook": cmd_serve_webhook, "resolve": cmd_resolve,
            "insights": cmd_insights, "propose-rules": cmd_propose_rules, "apply-proposal": cmd_apply_proposal,
            "rollback-override": cmd_rollback_override, "plan": cmd_plan, "queue": cmd_queue, "policy": cmd_policy,
            "digest": ops.cmd_digest}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return EXIT_OTHER
    if args.command in ("init", "doctor", "serve", "alert-test"):  # no session here: init runs before .env exists,
        return ops.dispatch(args)                                    # doctor reports a dead database instead of dying on it
    try:
        merchants.load()  # every merchant file is validated before anything runs; a bad one stops the CLI
    except merchants.MerchantConfigError as exc:
        print(f"error: merchant override file rejected: {exc}\nfix the file (schema: docs/merchants.md) or move it out of "
              f"{merchants.merchants_dir()}; the agent does not fall back to the defaults silently", file=sys.stderr)
        return EXIT_OTHER
    try:
        if faults.is_active("db_unavailable"):  # PRA_FAULTS=db_unavailable: the CLI must refuse, not pretend
            db.configure(UNOPENABLE_DB_URL)
        db.init_db()
        session = db.session()
        try:
            return COMMANDS[args.command](args, session)
        finally:
            session.close()
    except pipeline.PipelineDBError as exc:
        if getattr(exc, "link_may_exist", False):
            tail = ("a recovery link may already exist and its job is recorded; a redelivery is "
                    "skipped_duplicate and sends nothing new, so redeliver once the database is back")
        else:
            tail = "the event was NOT acknowledged and no link was created; redeliver it once the database is back"
        print(f"error: {exc}\n{tail}", file=sys.stderr)
        return EXIT_DB
    except DBAPIError as exc:
        print(f"error: database unavailable ({pipeline._describe_db_error(exc)})\n"
              "nothing was acknowledged; retry once the database is back", file=sys.stderr)
        return EXIT_DB
    except RuntimeError as exc:  # e.g. config.razorpay_live() refusing a non-test key
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_OTHER


if __name__ == "__main__":
    sys.exit(main())
