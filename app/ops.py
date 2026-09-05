"""Running the agent like a product (docs/ops.md): `pra init`, `pra doctor`, `pra serve`,
`pra digest`, `pra alert-test`.

  init      guided setup: keys (test-mode only, verified with one read-only call), LLM key, webhook
            secret, .env written from .env.example, the Razorpay dashboard steps, then doctor
  doctor    PASS/WARN/FAIL lines; exit 1 on any FAIL. Includes a synthetic payment.failed run
            through ingest_event in a throwaway in-memory SQLite to prove the pipeline runs.
  serve     ONE process: webhook receiver + scheduler loop + operator view, each a daemon thread,
            one lock shared by the receiver and the scheduler, human-queue alerts every sweep.
  digest    a plain-text/Markdown summary of a period; --post sends {"text": ...} to ALERT_WEBHOOK_URL.

Nothing here edits the pipeline: alerts on new human-queue entries are a poll inside `serve`
(jobs whose status is human_queue created since the last sweep, dedup by job id).
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import secrets
import socket
import sys
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session

from . import config, db
from .clock import utcnow
from .models import Outcome, PaymentAttempt, RecoveryDecision, RecoveryJob, with_reminders
from .nudge_templates import format_rupees
from .taxonomy import Action, JobStatus

EXIT_OK, EXIT_FAIL = 0, 1
IST = timezone(timedelta(hours=5, minutes=30), "IST")
STANDALONE_COMMANDS = ("init", "doctor", "serve", "digest", "alert-test")
WEBHOOK_EVENTS = ("payment.failed", "payment.captured", "order.paid", "payment_link.paid", "payment_link.expired",
                  "subscription.pending", "subscription.halted")
DEFAULT_PORT, DEFAULT_WEB_PORT, DEFAULT_INTERVAL = 8080, 8000, 30
_ENV_KEYS = ("RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET", "ANTHROPIC_API_KEY", "RAZORPAY_WEBHOOK_SECRET", "PRA_MODE",
             "ALERT_WEBHOOK_URL", "OPERATOR_TOKEN", "DATABASE_URL")
MODE_EXPLANATION = """\
Modes (PRA_MODE in .env):
  live    the executor makes the outbound Razorpay calls. Test-mode keys only, ever: a rzp_live_
          key is refused at startup. With no keys at all it runs against the in-memory fixture client.
  shadow  the whole pipeline runs and records what it WOULD do (classification, decision, schedule,
          nudge text), but the executor makes NO outbound call and marks each job `shadow`.
          Start here; flip to live once `pra digest` looks right."""


def local_time(dt: datetime | None) -> str:
    """A naive-UTC datetime rendered in DIGEST_TIMEZONE (Asia/Kolkata = fixed +05:30; else UTC)."""
    if dt is None:
        return "-"
    aware = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    if config.DIGEST_TIMEZONE == "Asia/Kolkata":
        return aware.astimezone(IST).strftime("%Y-%m-%d %H:%M IST")
    return aware.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%MZ")


def mode_line() -> str:
    mode = getattr(config, "PRA_MODE", "live")
    if mode == "shadow":
        return "shadow: the executor makes NO outbound call; jobs are recorded as `shadow`"
    return "live: the executor makes the (test-mode) Razorpay calls"


# ---- keys ----------------------------------------------------------------------------------------

def check_key_id(key_id: str) -> tuple[str, str]:
    """('fixture'|'test'|'refused', explanation). Live keys are refused before anything else runs."""
    key_id = (key_id or "").strip()
    if not key_id:
        return "fixture", "no RAZORPAY_KEY_ID: fixture mode (in-memory Payment Links, nothing leaves this machine)"
    if key_id.startswith("rzp_test_"):
        return "test", f"test-mode key {key_id[:12]}..."
    return "refused", f"{key_id[:8]}... is not a test-mode key (rzp_test_...); this agent never touches live money"


def verify_razorpay_keys(key_id: str, key_secret: str) -> tuple[bool, str]:
    """ONE read-only call, GET /payments?count=1, through the live client's own request helper."""
    from .razorpay_client import LiveRazorpayClient, RazorpayError
    try:
        client = LiveRazorpayClient(key_id, key_secret)
        body = client._request("GET", "/payments?count=1")
    except RazorpayError as exc:
        if exc.status == 401:
            return False, "Razorpay answered 401: the key id / secret pair is wrong (Dashboard -> Settings -> API Keys)"
        if exc.status == 0:
            return False, f"could not reach api.razorpay.com: {exc.description}"
        return False, f"Razorpay answered {exc.status} {exc.code}: {exc.description}"
    except RuntimeError as exc:
        return False, str(exc)
    n = body.get("count", len(body.get("items", []))) if isinstance(body, dict) else "?"
    return True, f"GET /payments?count=1 answered 200 ({n} payment(s) visible in test mode)"


def check_anthropic_key(key: str) -> tuple[str, str]:
    key = (key or "").strip()
    if not key:
        return "absent", "no ANTHROPIC_API_KEY: unmapped failures -> human queue, nudges -> templates (nothing blocks)"
    if key.startswith("sk-ant-") and len(key) > 20:
        return "present", f"ANTHROPIC_API_KEY {key[:10]}... (format only; not called)"
    return "malformed", "ANTHROPIC_API_KEY does not look like an Anthropic key (sk-ant-...); check it"


# ---- .env ----------------------------------------------------------------------------------------

def env_example_path() -> Path:
    return config.BASE_DIR / ".env.example"


def render_env(values: dict[str, str], template: str | None = None) -> str:
    """The .env text: .env.example's comments and order, each KEY= line filled from `values`;
    keys the template lacks are appended."""
    if template is None:
        try:
            template = env_example_path().read_text(encoding="utf-8")
        except OSError:
            template = ""
    seen: set[str] = set()
    out: list[str] = []
    for line in template.splitlines():
        m = re.match(r"^([A-Z][A-Z0-9_]*)=(.*)$", line)
        if m and m.group(1) in values:
            out.append(f"{m.group(1)}={values[m.group(1)]}")
            seen.add(m.group(1))
        else:
            out.append(line)
    missing = [k for k in values if k not in seen]
    if missing:
        out.append("")
        out.append("# ---- written by pra init ----")
        out.extend(f"{k}={values[k]}" for k in missing)
    return "\n".join(out).rstrip("\n") + "\n"


def write_env(path: Path, values: dict[str, str], *, force: bool = False) -> None:
    """Refuses to overwrite an existing file unless force."""
    if path.exists() and not force:
        raise FileExistsError(f"{path} exists; re-run with --force to overwrite it")
    path.write_text(render_env(values), encoding="utf-8")


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^([A-Z][A-Z0-9_]*)=(.*)$", line.strip())
            if m:
                values[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    except OSError:
        pass
    return values


def dashboard_steps(port: int = DEFAULT_PORT, public_url: str = "<public-url>") -> str:
    from .ingest import WEBHOOK_PATH
    events = "\n".join(f"      [x] {e}" for e in WEBHOOK_EVENTS)
    return f"""\
Razorpay dashboard (test mode) -> Settings -> Webhooks -> Add New Webhook
  1. Webhook URL : {public_url}{WEBHOOK_PATH}
  2. Secret      : the RAZORPAY_WEBHOOK_SECRET from .env (the receiver refuses unsigned POSTs with 401)
  3. Alert email : yours
  4. Active events:
{events}
  5. Save. Razorpay sends a test ping; `pra serve` logs it as one line.
A public URL for a laptop:   ngrok http {port}      (then use the https://....ngrok-free.app host above)"""


# ---- init ------------------------------------------------------------------------------------------

def _ask(prompt: str, default: str = "", *, secret: bool = False, yes: bool = False) -> str:
    if yes or not sys.stdin.isatty():
        return default
    shown = f"{prompt} [{default}]: " if default else f"{prompt}: "
    try:
        value = getpass.getpass(shown) if secret else input(shown)
    except EOFError:
        return default
    return value.strip() or default


def cmd_init(args, out: Callable[[str], None] = print) -> int:
    env_path = Path(args.env) if args.env else config.BASE_DIR / ".env"
    yes = bool(args.yes)
    out("payment-recovery-agent: guided setup")
    out(MODE_EXPLANATION)
    if env_path.exists() and not args.force:
        out(f"error: {env_path} exists; re-run with --force to overwrite it (your current values are kept until then)")
        return EXIT_FAIL
    existing = read_env_file(env_path) if env_path.exists() else {}
    mode = (args.mode or existing.get("PRA_MODE") or "live").lower()
    mode = _ask("Mode (live / shadow)", mode, yes=yes).lower()
    if mode not in ("live", "shadow"):
        out(f"error: mode must be live or shadow, not {mode!r}")
        return EXIT_FAIL

    key_id = args.key_id if args.key_id is not None else _ask("RAZORPAY_KEY_ID (rzp_test_..., blank = fixture mode)",
                                                              existing.get("RAZORPAY_KEY_ID", ""), yes=yes)
    kind, note = check_key_id(key_id)
    if kind == "refused":
        out(f"error: {note}")
        return EXIT_FAIL
    key_secret = ""
    if kind == "test":
        key_secret = args.key_secret if args.key_secret is not None else _ask(
            "RAZORPAY_KEY_SECRET", existing.get("RAZORPAY_KEY_SECRET", ""), secret=True, yes=yes)
        if not key_secret:
            out("error: RAZORPAY_KEY_SECRET is required with a key id (or leave both blank for fixture mode)")
            return EXIT_FAIL
        if args.skip_verify:
            out(f"razorpay : {note}, NOT verified (--skip-verify)")
        else:
            ok, msg = verify_razorpay_keys(key_id, key_secret)
            if not ok:
                out(f"error: Razorpay keys rejected: {msg}")
                out("        nothing was written; fix the keys and run `pra init` again (or --skip-verify to write anyway)")
                return EXIT_FAIL
            out(f"razorpay : {note}, verified: {msg}")
    else:
        out(f"razorpay : {note}")

    llm_key = args.anthropic_key if args.anthropic_key is not None else _ask(
        "ANTHROPIC_API_KEY (optional, sk-ant-...)", existing.get("ANTHROPIC_API_KEY", ""), secret=True, yes=yes)
    lkind, lnote = check_anthropic_key(llm_key)
    if lkind == "malformed":
        out(f"error: {lnote}")
        return EXIT_FAIL
    out(f"llm      : {lnote}")

    secret = args.webhook_secret if args.webhook_secret is not None else existing.get("RAZORPAY_WEBHOOK_SECRET", "")
    if not secret:
        answer = _ask("RAZORPAY_WEBHOOK_SECRET (blank = generate one now)", "", secret=True, yes=yes)
        secret = answer or secrets.token_hex(24)
        generated = not answer
    else:
        generated = False
    out(f"webhook  : secret {'generated' if generated else 'set'} ({len(secret)} chars); type the same one into the dashboard")

    values = {
        "RAZORPAY_KEY_ID": key_id if kind == "test" else "",
        "RAZORPAY_KEY_SECRET": key_secret,
        "ANTHROPIC_API_KEY": llm_key,
        "RAZORPAY_WEBHOOK_SECRET": secret,
        "PRA_MODE": mode,
        "ALERT_WEBHOOK_URL": args.alert_webhook_url if args.alert_webhook_url is not None else existing.get("ALERT_WEBHOOK_URL", ""),
        "OPERATOR_TOKEN": args.operator_token if args.operator_token is not None else existing.get("OPERATOR_TOKEN", ""),
        "DATABASE_URL": args.database_url or existing.get("DATABASE_URL") or "sqlite:///./recovery.db",
    }
    try:
        write_env(env_path, values, force=bool(args.force))
    except (FileExistsError, OSError) as exc:
        out(f"error: {exc}")
        return EXIT_FAIL
    out(f"wrote    : {env_path} (mode {mode})")
    out("")
    out(dashboard_steps(args.port))
    out("")
    if args.no_doctor:
        return EXIT_OK
    out("running doctor against the new .env:")
    checks = run_doctor(env=values)
    for line in format_checks(checks):
        out(line)
    return EXIT_FAIL if any(c.level == "FAIL" for c in checks) else EXIT_OK


# ---- doctor ----------------------------------------------------------------------------------------

@dataclass
class Check:
    level: str  # PASS | WARN | FAIL
    name: str
    detail: str


def _port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host if host not in ("", "0.0.0.0") else "127.0.0.1", port))
            return True
        except OSError:
            return False


def synthetic_payment_failed(now: datetime | None = None) -> dict:
    """A payment.failed envelope no real merchant ever sent: a doctor-only id, the fixture's shapes."""
    now = now or utcnow()
    return {"entity": "event", "account_id": "acc_doctor", "event": "payment.failed", "contains": ["payment"],
            "created_at": int(now.timestamp()),
            "payload": {"payment": {"entity": {
                "id": "pay_DOCTOR000000001", "entity": "payment", "amount": 49900, "currency": "INR", "status": "failed",
                "order_id": "order_DOCTOR0000001", "method": "upi", "captured": False, "vpa": "doctor@upi",
                "email": "doctor@example.com", "contact": "+919999900001", "notes": {},
                "error_code": "BAD_REQUEST_ERROR", "error_description": "Payment failed due to insufficient funds in the bank account",
                "error_source": "customer", "error_step": "payment_authorization", "error_reason": "payment_failed",
                "created_at": int(now.timestamp())}}}}


def pipeline_smoke() -> tuple[bool, str]:
    """A synthetic payment.failed through ingest_event in a throwaway in-memory SQLite, rolled back
    (and discarded) afterwards. Proves the pipeline imports, classifies, decides and executes
    against the fixture client without touching the configured database or the network."""
    from . import models  # noqa: F401
    from .ingest import ingest_event
    from .razorpay_client import FixtureRazorpayClient
    engine = create_engine("sqlite://")
    db.Base.metadata.create_all(engine)
    session = Session(bind=engine, expire_on_commit=False)
    try:
        out = ingest_event(session, synthetic_payment_failed(), process=True, execute_now=True,
                           rz_client=FixtureRazorpayClient(), llm_client=None, sleep=lambda s: None)
        session.rollback()
        cls = out.get("failure_class") or "?"
        return True, (f"synthetic payment.failed -> {cls} -> {out.get('action') or '?'} -> "
                      f"{out.get('job_status') or 'ingested'} (in-memory SQLite, rolled back)")
    except Exception as exc:  # any error here IS the finding
        return False, f"synthetic payment.failed raised {type(exc).__name__}: {exc}"
    finally:
        session.close()
        engine.dispose()


def run_doctor(*, env: dict[str, str] | None = None, db_url: str | None = None, env_path: Path | None = None,
               host: str = "0.0.0.0", port: int = DEFAULT_PORT, web_port: int = DEFAULT_WEB_PORT) -> list[Check]:
    """Every check, never raising. `env` overrides config (init passes the values it just wrote)."""
    env = env or {}

    def get(name: str) -> str:
        return env[name] if name in env else str(getattr(config, name, "") or "")

    checks: list[Check] = []
    v = sys.version_info
    checks.append(Check("PASS" if v >= (3, 11) else "FAIL", "python", f"{v.major}.{v.minor}.{v.micro} (needs >= 3.11)"))

    env_path = env_path or config.BASE_DIR / ".env"
    checks.append(Check("PASS" if env_path.exists() else "WARN", ".env",
                        str(env_path) if env_path.exists() else f"{env_path} missing: run `pra init` (defaults apply: fixture, SQLite)"))

    mode = (get("PRA_MODE") or "live").lower()
    checks.append(Check("PASS" if mode in ("live", "shadow") else "FAIL", "mode",
                        mode_line() if mode == getattr(config, "PRA_MODE", "live") else f"{mode} (from the .env just written)"))

    kind, note = check_key_id(get("RAZORPAY_KEY_ID"))
    if kind == "test" and not get("RAZORPAY_KEY_SECRET"):
        checks.append(Check("FAIL", "razorpay keys", f"{note} but RAZORPAY_KEY_SECRET is empty"))
    else:
        checks.append(Check("FAIL" if kind == "refused" else "PASS", "razorpay keys", note))

    lkind, lnote = check_anthropic_key(get("ANTHROPIC_API_KEY"))
    checks.append(Check({"present": "PASS", "absent": "WARN", "malformed": "FAIL"}[lkind], "llm key", lnote))

    secret = get("RAZORPAY_WEBHOOK_SECRET")
    checks.append(Check("PASS" if secret else "WARN", "webhook secret",
                        "set: X-Razorpay-Signature is verified" if secret else "unset: unsigned POSTs are accepted; set it before exposing the port"))

    url = get("ALERT_WEBHOOK_URL")
    checks.append(Check("PASS" if url else "WARN", "alerts", "ALERT_WEBHOOK_URL set" if url
                        else "ALERT_WEBHOOK_URL unset: digests and human-queue alerts print to stdout only"))

    try:
        from . import merchants
        merchants.load()
        checks.append(Check("PASS", "merchants", merchants.header_line()))
    except Exception as exc:
        checks.append(Check("FAIL", "merchants", f"{type(exc).__name__}: {exc}"))

    url = db_url or (env.get("DATABASE_URL") if env.get("DATABASE_URL") else None) or db.url()
    try:
        from . import models  # noqa: F401
        engine = create_engine(url, connect_args={"check_same_thread": False} if url.startswith("sqlite") else {})
        try:
            db.Base.metadata.create_all(engine)
            tables = set(inspect(engine).get_table_names())
            missing = sorted(set(db.Base.metadata.tables) - tables)
            checks.append(Check("FAIL" if missing else "PASS", "database",
                                f"{url}: missing tables {missing}" if missing else f"{url}: reachable, {len(tables)} tables"))
        finally:
            engine.dispose()
    except Exception as exc:
        checks.append(Check("FAIL", "database", f"{url}: {type(exc).__name__}: {str(exc).splitlines()[0]}"))

    for label, p in (("webhook port", port), ("web port", web_port)):
        checks.append(Check("PASS" if _port_free(host, p) else "WARN", label,
                            f"{p} free" if _port_free(host, p) else f"{p} in use: pass --port/--web-port to `pra serve`"))

    ok, detail = pipeline_smoke()
    checks.append(Check("PASS" if ok else "FAIL", "pipeline", detail))
    return checks


def format_checks(checks: list[Check]) -> list[str]:
    lines = [f"{c.level:<4} {c.name:<15} {c.detail}" for c in checks]
    fails = sum(1 for c in checks if c.level == "FAIL")
    warns = sum(1 for c in checks if c.level == "WARN")
    lines.append(f"doctor: {len(checks)} checks, {fails} FAIL, {warns} WARN -> {'not ready' if fails else 'ready'}")
    return lines


def cmd_doctor(args, out: Callable[[str], None] = print) -> int:
    checks = run_doctor(host=args.host, port=args.port, web_port=args.web_port)
    for line in format_checks(checks):
        out(line)
    return EXIT_FAIL if any(c.level == "FAIL" for c in checks) else EXIT_OK


# ---- alerts ----------------------------------------------------------------------------------------

def post_alert(text: str, url: str | None = None, timeout: float = 10.0) -> tuple[bool, str]:
    """POST {"text": ...} (Slack-compatible) to ALERT_WEBHOOK_URL. Never raises."""
    url = url if url is not None else config.ALERT_WEBHOOK_URL
    if not url:
        return False, "ALERT_WEBHOOK_URL unset; not posted"
    data = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "payment-recovery-agent/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True, f"posted ({getattr(resp, 'status', 200)})"
    except urllib.error.HTTPError as exc:
        return False, f"webhook answered {exc.code}"
    except (OSError, ValueError) as exc:
        return False, f"could not post: {type(exc).__name__}: {exc}"


def human_queue_alert_text(attempt: PaymentAttempt, job: RecoveryJob) -> str:
    decision = max(attempt.decisions, key=lambda d: d.id) if attempt.decisions else None
    cls = decision.failure_class if decision else "UNKNOWN"
    why = job.last_error or (decision.reason if decision else "-")
    return (f"needs a person: {attempt.razorpay_payment_id} {format_rupees(attempt.amount_paise)} {attempt.method} "
            f"[{cls}] merchant {attempt.merchant_id}: {why}\n"
            f"resolve with: pra resolve --attempt {attempt.razorpay_payment_id} --recovered PAISE | --closed")


def alert_human_queue(session: Session, attempt: PaymentAttempt, job: RecoveryJob,
                      out: Callable[[str], None] = print) -> bool:
    """One alert for one human-queue entry: printed, and posted when ALERT_WEBHOOK_URL is set.
    Returns whether it was posted."""
    text = human_queue_alert_text(attempt, job)
    out(f"alert: {text.splitlines()[0]}")
    posted, note = post_alert(text)
    if not posted and config.ALERT_WEBHOOK_URL:
        out(f"alert: {note}")
    return posted


class HumanQueueWatcher:
    """Polls for human_queue jobs created since the last look; dedups by job id."""

    def __init__(self, out: Callable[[str], None] = print, since: datetime | None = None):
        self.seen: set[int] = set()
        self.since = since or utcnow()
        self.out = out

    def poll(self, session: Session) -> list[RecoveryJob]:
        stmt = (select(RecoveryJob).where(RecoveryJob.status == JobStatus.HUMAN_QUEUE.value,
                                          RecoveryJob.created_at >= self.since).order_by(RecoveryJob.id))
        new = [j for j in session.execute(stmt).scalars().all() if j.id not in self.seen]
        for job in new:
            self.seen.add(job.id)
            attempt = job.attempt or session.get(PaymentAttempt, job.attempt_id)
            alert_human_queue(session, attempt, job, out=self.out)
        return new


def cmd_alert_test(args, out: Callable[[str], None] = print) -> int:
    text = f"payment-recovery-agent alert test at {local_time(utcnow())} ({mode_line()})"
    posted, note = post_alert(text)
    out(f"alert-test: {note}")
    return EXIT_OK if posted else EXIT_FAIL


# ---- digest ----------------------------------------------------------------------------------------

_SINCE = re.compile(r"^(\d+)\s*([mhd])$")


def parse_since(text: str) -> timedelta:
    m = _SINCE.match((text or "").strip().lower())
    if not m:
        raise ValueError(f"--since must look like 30m, 24h or 7d, not {text!r}")
    n, unit = int(m.group(1)), m.group(2)
    return timedelta(**{{"m": "minutes", "h": "hours", "d": "days"}[unit]: n})


@dataclass
class Digest:
    since: datetime
    until: datetime
    events_in: int = 0
    events_amount: int = 0
    links_created: int = 0
    reminders_sent: int = 0
    shadow_jobs: int = 0
    recovered: int = 0
    recovered_amount: int = 0
    expired: int = 0
    cancelled: int = 0
    human_added: list[tuple[str, int, str]] = field(default_factory=list)
    llm_calls: int = 0
    llm_fallbacks: int = 0
    top_queue: list[tuple[str, int, int, str]] = field(default_factory=list)  # payment_id, amount, expected, class
    needs_person: list[tuple[str, int, str, str]] = field(default_factory=list)  # payment_id, amount, class, reason


def compute_digest(session: Session, since: timedelta, now: datetime | None = None) -> Digest:
    now = now or utcnow()
    cutoff = now - since
    d = Digest(since=cutoff, until=now)
    attempts = session.execute(select(PaymentAttempt).where(PaymentAttempt.created_at >= cutoff)).scalars().all()
    d.events_in, d.events_amount = len(attempts), sum(a.amount_paise for a in attempts)
    jobs = session.execute(with_reminders(select(RecoveryJob).where(RecoveryJob.created_at >= cutoff)
                                          .order_by(RecoveryJob.id))).scalars().all()
    executed = session.execute(with_reminders(select(RecoveryJob).where(RecoveryJob.executed_at >= cutoff))).scalars().all()
    for j in {j.id: j for j in jobs + executed}.values():
        recent = j.executed_at is not None and j.executed_at >= cutoff
        if j.action == Action.REMINDER.value:
            d.reminders_sent += int(recent and j.status == JobStatus.SENT.value)
            continue
        if recent and j.status == JobStatus.SENT.value and j.razorpay_link_id:
            d.links_created += 1
        if j.status == "shadow" and j.created_at >= cutoff:
            d.shadow_jobs += 1
        if j.status == JobStatus.CANCELLED.value and (recent or j.created_at >= cutoff):
            d.cancelled += 1
        if j.status == JobStatus.HUMAN_QUEUE.value and j.created_at >= cutoff:
            a = j.attempt or session.get(PaymentAttempt, j.attempt_id)
            d.human_added.append((a.razorpay_payment_id, a.amount_paise, j.last_error or "-"))
    for o in session.execute(select(Outcome).where(Outcome.created_at >= cutoff)).scalars().all():
        if o.recovered:
            d.recovered += 1
            d.recovered_amount += o.amount_recovered_paise
        elif o.note and "expired" in o.note:
            d.expired += 1
    for dec in session.execute(select(RecoveryDecision).where(RecoveryDecision.created_at >= cutoff)).scalars().all():
        d.llm_calls += int(bool(dec.llm_used))
        d.llm_fallbacks += int(bool(dec.fallback_taken))
    try:
        from . import priority
        items = priority.human_queue(session)
        d.top_queue = [(it.attempt.razorpay_payment_id, it.attempt.amount_paise, it.estimate.expected_paise, it.failure_class)
                       for it in items[:5]]
        d.needs_person = [(it.attempt.razorpay_payment_id, it.attempt.amount_paise, it.failure_class, it.reason) for it in items]
    except Exception:  # priority unavailable: the plain queue, oldest first
        stmt = select(RecoveryJob).where(RecoveryJob.status == JobStatus.HUMAN_QUEUE.value).order_by(RecoveryJob.id)
        for j in session.execute(stmt).scalars().all():
            a = j.attempt or session.get(PaymentAttempt, j.attempt_id)
            d.needs_person.append((a.razorpay_payment_id, a.amount_paise, "-", j.last_error or "-"))
    return d


def render_digest(d: Digest, since_text: str) -> str:
    lines = [f"# payment-recovery-agent digest: last {since_text}",
             f"{local_time(d.since)} -> {local_time(d.until)} | mode {getattr(config, 'PRA_MODE', 'live')} | database {db.url()}",
             "",
             "## Flow",
             f"- events in: {d.events_in} ({format_rupees(d.events_amount)} at risk)",
             f"- links created: {d.links_created}" + (f" | shadow jobs (nothing sent): {d.shadow_jobs}" if d.shadow_jobs else ""),
             f"- reminders sent: {d.reminders_sent}",
             f"- recovered: {d.recovered} ({format_rupees(d.recovered_amount)})",
             f"- expired unpaid: {d.expired}",
             f"- cancelled (paid elsewhere): {d.cancelled}",
             f"- human queue additions: {len(d.human_added)} ({format_rupees(sum(a for _, a, _ in d.human_added))})",
             f"- LLM calls: {d.llm_calls} | fallbacks: {d.llm_fallbacks}",
             "",
             "## Top 5 by expected recovery in the queue"]
    lines += [f"- {pid} {format_rupees(amt)} [{cls}] expected {format_rupees(exp)}" for pid, amt, exp, cls in d.top_queue] or ["- (queue empty)"]
    lines += ["", "## Needs a person"]
    lines += [f"- {pid} {format_rupees(amt)} [{cls}]: {reason}" for pid, amt, cls, reason in d.needs_person] or ["- nobody: the queue is empty"]
    if d.needs_person:
        lines.append("resolve with: pra resolve --attempt <pay_...> --recovered PAISE | --closed")
    return "\n".join(lines) + "\n"


def cmd_digest(args, session: Session, out: Callable[[str], None] = print) -> int:
    try:
        since = parse_since(args.since)
    except ValueError as exc:
        out(f"error: {exc}")
        return EXIT_FAIL
    text = render_digest(compute_digest(session, since), args.since)
    out(text.rstrip("\n"))
    if args.write:
        Path(args.write).write_text(text, encoding="utf-8")
        out(f"digest: written to {args.write}")
    if args.post:
        posted, note = post_alert(text)
        out(f"digest: {note}")
        if not posted:
            return EXIT_FAIL
    return EXIT_OK


# ---- serve -----------------------------------------------------------------------------------------

@dataclass
class Serve:
    """The three threads and their servers, built but not started (tests start and stop them)."""
    webhook: ThreadingHTTPServer
    web: ThreadingHTTPServer | None
    interval: int
    rz: object
    lm: object
    lock: threading.Lock
    out: Callable[[str], None] = print
    watcher: HumanQueueWatcher = field(default_factory=HumanQueueWatcher)
    stop: threading.Event = field(default_factory=threading.Event)
    threads: list[threading.Thread] = field(default_factory=list)
    sweeps: int = 0

    @property
    def webhook_url(self) -> str:
        from .ingest import WEBHOOK_PATH
        h, p = self.webhook.server_address[:2]
        return f"http://{h}:{p}{WEBHOOK_PATH}"

    @property
    def web_url(self) -> str | None:
        if self.web is None:
            return None
        h, p = self.web.server_address[:2]
        return f"http://{h}:{p}/"

    def sweep(self) -> None:
        """One scheduler pass then the human-queue poll, under the receiver's lock, a fresh session."""
        from . import scheduler
        from .main import _sweep_line
        with self.lock:
            session = db.session()
            try:
                now = utcnow()
                jobs = scheduler.run_due(session, now=now, rz_client=self.rz, llm_client=self.lm)
                if jobs:
                    self.out(_sweep_line(jobs, now, session))
                self.watcher.poll(session)
                self.sweeps += 1
            except Exception as exc:  # the loop must survive a bad sweep; the next one retries
                self.out(f"sweep: {type(exc).__name__}: {exc}")
            finally:
                session.close()

    def _loop(self) -> None:
        while not self.stop.is_set():
            self.sweep()
            self.stop.wait(self.interval)

    def start(self) -> None:
        self.threads = [threading.Thread(target=self.webhook.serve_forever, name="webhook", daemon=True),
                        threading.Thread(target=self._loop, name="scheduler", daemon=True)]
        if self.web is not None:
            self.threads.append(threading.Thread(target=self.web.serve_forever, name="web", daemon=True))
        for t in self.threads:
            t.start()

    def shutdown(self) -> None:
        self.stop.set()
        self.webhook.shutdown()
        self.webhook.server_close()
        if self.web is not None:
            self.web.shutdown()
            self.web.server_close()
        for t in self.threads:
            t.join(timeout=5)

    def banner(self) -> list[str]:
        from .main import _describe
        lines = ["payment-recovery-agent: one process (Ctrl-C to stop)"]
        lines += [f"  {line}" for line in _describe(self.rz, self.lm)]
        lines.append(f"  webhook  : POST {self.webhook_url}  (GET /health)  signature "
                     f"{'verified' if config.RAZORPAY_WEBHOOK_SECRET else 'NOT verified: RAZORPAY_WEBHOOK_SECRET unset'}")
        lines.append(f"  scheduler: run_due every {self.interval}s, fresh session per sweep, same lock as the receiver")
        lines.append(f"  operator : {self.web_url or 'off (--no-web, or app.web not importable)'}"
                     + ("  (bearer OPERATOR_TOKEN)" if self.web_url and config.OPERATOR_TOKEN else ""))
        lines.append(f"  alerts   : {'POST ' + config.ALERT_WEBHOOK_URL[:40] + '...' if config.ALERT_WEBHOOK_URL else 'stdout only (ALERT_WEBHOOK_URL unset)'}"
                     " on every new human-queue entry")
        lines.append(f"  ngrok    : ngrok http {self.webhook.server_address[1]}  then register <public-url>/razorpay/webhook")
        return lines


def build_serve(*, host: str = "0.0.0.0", port: int = DEFAULT_PORT, web_port: int | None = DEFAULT_WEB_PORT,
                interval: int = DEFAULT_INTERVAL, no_web: bool = False, rz_client=None, llm_client=None,
                out: Callable[[str], None] = print) -> Serve:
    """The receiver from app.ingest's handler factory, the operator view from app.web.make_server."""
    from . import ingest, llm, razorpay_client
    rz = rz_client if rz_client is not None else razorpay_client.get_client()
    lm = llm_client if llm_client is not None else llm.get_client()
    lock = threading.Lock()
    handler = ingest.make_handler(secret=config.RAZORPAY_WEBHOOK_SECRET or None, process=True, execute_now=True,
                                  lock=lock, log=out, rz_client=rz, llm_client=lm)
    webhook = ThreadingHTTPServer((host, port), handler)
    webhook.daemon_threads = True
    web_server = None
    if not no_web:
        try:
            from . import web
            web_server = web.make_server(host if host in ("0.0.0.0", "127.0.0.1", "localhost", "::1") else host, web_port or 0)
        except ImportError as exc:
            out(f"warning: operator view skipped (app.web not importable: {exc})")
        except OSError as exc:
            webhook.server_close()
            raise OSError(f"operator view cannot listen on {host}:{web_port}: {exc}") from None
    return Serve(webhook=webhook, web=web_server, interval=max(1, int(interval)), rz=rz, lm=lm, lock=lock, out=out,
                 watcher=HumanQueueWatcher(out=out))


def cmd_serve(args, out: Callable[[str], None] = print) -> int:
    from .merchants import MerchantConfigError
    try:
        db.init_db()
        srv = build_serve(host=args.host, port=args.port, web_port=args.web_port, interval=args.interval,
                          no_web=args.no_web, out=out)
    except OSError as exc:
        out(f"error: cannot listen on {args.host}:{args.port}: {exc}")
        return EXIT_FAIL
    except MerchantConfigError as exc:
        out(f"error: merchant override file rejected: {exc} (schema: docs/merchants.md)")
        return EXIT_FAIL
    for line in srv.banner():
        out(line)
    if not config.RAZORPAY_WEBHOOK_SECRET:
        print("warning: RAZORPAY_WEBHOOK_SECRET is unset; any POST is accepted unsigned. Set it before exposing this port.",
              file=sys.stderr)
    if srv.web is not None and args.host not in ("127.0.0.1", "localhost", "::1") and not config.OPERATOR_TOKEN:
        print(f"warning: the operator view is bound to {args.host} without OPERATOR_TOKEN: it shows customer contact "
              "details to anyone who can reach it", file=sys.stderr)
    srv.start()
    try:
        while True:
            srv.stop.wait(3600)
    except KeyboardInterrupt:
        out("\nserve: stopping")
    finally:
        srv.shutdown()
    out(f"serve: stopped after {srv.sweeps} sweep(s)")
    return EXIT_OK


# ---- parser and dispatch ----------------------------------------------------------------------------

def add_subparsers(sub) -> None:
    ini = sub.add_parser("init", help="guided setup: keys, webhook secret, .env, the dashboard steps, then doctor")
    ini.add_argument("--yes", "-y", action="store_true", help="no prompts: take flags and defaults (fixture mode, generated secret)")
    ini.add_argument("--force", action="store_true", help="overwrite an existing .env")
    ini.add_argument("--env", metavar="PATH", help="where to write (default <repo>/.env)")
    ini.add_argument("--mode", choices=("live", "shadow"))
    ini.add_argument("--key-id", metavar="rzp_test_...")
    ini.add_argument("--key-secret")
    ini.add_argument("--anthropic-key", metavar="sk-ant-...")
    ini.add_argument("--webhook-secret", help="the dashboard webhook secret (default: generated)")
    ini.add_argument("--alert-webhook-url")
    ini.add_argument("--operator-token")
    ini.add_argument("--database-url")
    ini.add_argument("--skip-verify", action="store_true", help="do not call GET /payments?count=1 to verify the keys")
    ini.add_argument("--no-doctor", action="store_true")
    ini.add_argument("--port", type=int, default=DEFAULT_PORT, help="webhook port to print in the ngrok line")
    doc = sub.add_parser("doctor", help="PASS/WARN/FAIL checks; exit 1 on any FAIL")
    doc.add_argument("--host", default="0.0.0.0")
    doc.add_argument("--port", type=int, default=DEFAULT_PORT)
    doc.add_argument("--web-port", type=int, default=DEFAULT_WEB_PORT)
    sv = sub.add_parser("serve", help="one process: webhook receiver + scheduler loop + operator view")
    sv.add_argument("--host", default="0.0.0.0")
    sv.add_argument("--port", type=int, default=DEFAULT_PORT)
    sv.add_argument("--web-port", type=int, default=DEFAULT_WEB_PORT)
    sv.add_argument("--interval", type=int, default=DEFAULT_INTERVAL, metavar="SECONDS")
    sv.add_argument("--no-web", action="store_true", help="skip the operator view")
    dg = sub.add_parser("digest", help="a plain-text summary of a period: events, links, recoveries, queue, LLM usage")
    dg.add_argument("--since", default="24h", help="30m, 24h, 7d (default 24h)")
    dg.add_argument("--write", metavar="PATH", help="also write it as Markdown")
    dg.add_argument("--post", action="store_true", help='POST {"text": ...} to ALERT_WEBHOOK_URL')
    sub.add_parser("alert-test", help="POST one test message to ALERT_WEBHOOK_URL")


def dispatch(args) -> int:
    """init/doctor/serve/alert-test run without the CLI's database session (init runs before .env
    exists; doctor reports the database instead of dying on it). digest is dispatched by main.py."""
    if args.command == "init":
        return cmd_init(args)
    if args.command == "doctor":
        return cmd_doctor(args)
    if args.command == "serve":
        return cmd_serve(args)
    if args.command == "alert-test":
        return cmd_alert_test(args)
    raise argparse.ArgumentError(None, f"unknown ops command {args.command}")
