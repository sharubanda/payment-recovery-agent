"""Read-only operator view: python -m app.web [--port 8000] [--host 127.0.0.1]

A handful of plain HTML pages over the same SQLite database the CLI writes, for the demo
video. It mirrors `python -m app.main show` (the attempts table), `audit --attempt ID` (the
trail) and the `demo` honesty header, so a viewer knows what is real: fixture vs live test
mode, LLM present or not, which database, which faults.

What it is NOT: it has no login. Every GET is read-only and anonymous. Operator actions
(POST /action/resolve|cancel|resend|retry|apply-proposal, app/actions.py) exist only when
OPERATOR_TOKEN is set: the form carries the token the operator typed, compared with
hmac.compare_digest, plus an actor name that goes into every audit row; with the token blank
every POST is 403 and the pages say how to enable actions (docs/web.md). It serves on the
loopback interface unless --host is given explicitly. GET /metrics (Prometheus text format, computed from the database per scrape)
is as unauthenticated as the rest: scrape it from the same host or a network you control. Anyone who can reach the port can read every row, including customer
contact details, so do not expose it beyond the machine you are demoing on.

Stdlib only: http.server + hand-written HTML with html.escape, one inline stylesheet, no
JavaScript (an action asks for confirmation on a second page, not with a script), no external assets.
"""
import argparse
import hmac
import html
import json
import os
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from sqlalchemy import select

from . import actions, config, db, faults, insights, llm, merchants, priority, razorpay_client, rule_candidates
from .models import AuditEvent, Outcome, PaymentAttempt, RecoveryDecision, RecoveryJob
from .nudge_templates import format_rupees
from .policy import human_delay
from .taxonomy import FailureClass, JobStatus

METRICS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
BUILD_VERSION = "0.1"

DOCS_DIR = config.BASE_DIR / "docs"
REPORTS = {
    "failure": (DOCS_DIR / "failure_report.md", "make chaos  (scripts/chaos.py --all --write docs/failure_report.md)"),
    "simulation": (DOCS_DIR / "simulation_report.md", "make simulate  (scripts/simulate.py --write docs/simulation_report.md)"),
}
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
REFRESH_SECONDS = 5

STYLE = """
body{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:13px;color:#1a1a1a;
background:#fafafa;margin:0;padding:16px 24px;max-width:1240px}
h1{font-size:18px;margin:0 0 8px}h2{font-size:14px;margin:20px 0 6px;border-bottom:1px solid #ccc}
nav a{margin-right:14px}a{color:#0b5cad}
pre{background:#fff;border:1px solid #ddd;padding:8px;white-space:pre-wrap;word-break:break-word}
pre.header{background:#f2f2f2;margin:0 0 12px}
table{border-collapse:collapse;background:#fff;width:100%}
th,td{border:1px solid #ddd;padding:3px 6px;text-align:left;vertical-align:top}
th{background:#eee}td.num{text-align:right}tr:nth-child(even){background:#f7f7f7}
.totals td{font-weight:bold}.warn{color:#8a4b00}.muted{color:#666}
details summary{cursor:pointer;color:#0b5cad}details pre{margin:4px 0 0}
form.filter{margin:8px 0}form.filter select{font-family:inherit;font-size:inherit}
footer{margin-top:24px;color:#666;border-top:1px solid #ccc;padding-top:8px}
form.action{display:inline-block;margin:2px 8px 2px 0;padding:4px 6px;border:1px solid #cfd8e3;background:#f4f8fc}
form.action input,form.action select,form.action button{font-family:inherit;font-size:inherit}
form.action input[type=text],form.action input[type=password]{width:9em}form.action input.note{width:16em}
p.flash{background:#e8f5e9;border:1px solid #9ccc9f;padding:6px 10px}p.flash.err{background:#fdecea;border-color:#e6a5a0}
.actions{margin:8px 0;padding:6px 10px;border:1px dashed #9ab;background:#fbfdff}
"""

FOOTER = ("payment-recovery-agent operator view: read-only, no authentication, loopback by default. "
          "It shows every row in the database, so do not expose the port beyond the demo machine.")
FOOTER_ACTIONS = (" Operator actions are ON (OPERATOR_TOKEN set): every POST needs the token and an actor name, "
                  "and writes an audit row under stage \"operator\".")
MAX_BODY = 64 * 1024
ACTION_PATHS = ("/action/resolve", "/action/cancel", "/action/resend", "/action/retry", "/action/apply-proposal")
HOW_TO_ENABLE = ("actions are disabled: set OPERATOR_TOKEN in .env (any long random string) and restart "
                 "python -m app.web; the forms then appear on the attempt, human-queue and insights pages")


def operator_token() -> str:
    """config.OPERATOR_TOKEN (declared in app/config.py) or the env; blank = no actions at all."""
    return (getattr(config, "OPERATOR_TOKEN", "") or os.getenv("OPERATOR_TOKEN", "")).strip()


def actions_enabled() -> bool:
    return bool(operator_token())


def token_matches(supplied: str | None) -> bool:
    expected = operator_token()
    return bool(expected) and hmac.compare_digest((supplied or "").encode("utf-8"), expected.encode("utf-8"))


# ---- helpers -----------------------------------------------------------------------------------

def esc(value) -> str:
    return html.escape("-" if value is None else str(value), quote=True)


def ts(dt: datetime | None) -> str:
    """As `show` prints scheduled times."""
    return dt.strftime("%Y-%m-%d %H:%MZ") if dt else "-"


def ts_audit(dt: datetime | None) -> str:
    """As `audit --attempt` prints them."""
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else "-"


def latest(items):
    return max(items, key=lambda x: x.id) if items else None


def outcome_text(o: Outcome | None, job: RecoveryJob | None) -> str:
    if o is not None:
        return f"recovered {format_rupees(o.amount_recovered_paise)}" if o.recovered else (o.note or "not recovered").split(":")[0]
    if job is not None and job.status == JobStatus.SENT.value:
        return "open"
    return "-"


def header_lines() -> list[str]:
    """The demo honesty header. Reuses app.main's if present so the two never drift; otherwise a
    local rendering of the same facts (client kind, LLM key, database URL, active faults)."""
    try:
        from .main import _clients, _describe
        rz, lm = _clients()
        return _describe(rz, lm)
    except Exception:  # main.py is someone else's file; degrade to our own wording rather than 500
        pass
    rz, lm = razorpay_client.get_client(), llm.get_client()
    if isinstance(rz, razorpay_client.FixtureRazorpayClient) or (
            isinstance(rz, razorpay_client.FaultingRazorpayClient)
            and isinstance(getattr(rz, "inner", None), razorpay_client.FixtureRazorpayClient)):
        rz_line = "fixture client (no RAZORPAY_KEY_ID): in-memory Payment Links, nothing leaves this machine"
    else:
        rz_line = f"live TEST MODE ({config.RAZORPAY_KEY_ID[:12]}...): real Payment Links at {razorpay_client.API_BASE_URL}"
    lm_line = ("none (ANTHROPIC_API_KEY unset): unmapped failures -> human queue, nudges -> templates" if lm is None
               else f"anthropic {getattr(lm, 'model', config.LLM_MODEL)}")
    active = ", ".join(sorted(faults.active())) or "none"
    return [f"razorpay : {rz_line}", f"llm      : {lm_line}", f"database : {db.url()}", f"faults   : {active}",
            f"merchants: {merchants.header_line()}"]


def flash_html(query: dict | None) -> str:
    """The ?msg= / ?err= a redirect after an action carries, escaped; nothing when absent."""
    if not query:
        return ""
    msg = (query.get("msg") or [""])[0]
    err = (query.get("err") or [""])[0]
    if msg:
        return f'<p class="flash">{esc(msg[:600])}</p>'
    if err:
        return f'<p class="flash err">{esc(err[:600])}</p>'
    return ""


def page(title: str, body: str, *, refresh: bool = False, flash: str = "") -> str:
    meta = f'<meta http-equiv="refresh" content="{REFRESH_SECONDS}">' if refresh else ""
    nav = ('<nav><a href="/">attempts</a><a href="/human-queue">human queue</a>'
           '<a href="/reports/failure">failure report</a><a href="/reports/simulation">simulation report</a>'
           '<a href="/insights">insights</a><a href="/rule-candidates">rule candidates</a>'
           '<a href="/policy">policy</a><a href="/metrics">metrics</a><a href="/health">health</a></nav>')
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">{meta}'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{esc(title)} - payment-recovery-agent</title><style>{STYLE}</style></head><body>"
            f"<h1>{esc(title)}</h1>{nav}{flash}{body}<footer>{esc(FOOTER)}"
            f"{esc(FOOTER_ACTIONS) if actions_enabled() else ''}</footer></body></html>")


# ---- action forms (rendered only when OPERATOR_TOKEN is set) ------------------------------------

def _hidden(fields: dict) -> str:
    return "".join(f'<input type="hidden" name="{esc(k)}" value="{esc(v)}">' for k, v in fields.items() if v is not None)


def _credentials() -> str:
    return ('actor <input type="text" name="actor" maxlength="40" required placeholder="your name"> '
            'token <input type="password" name="token" required>')


def action_form(path: str, label: str, hidden: dict, extra: str = "") -> str:
    """One inline form. It POSTs to /action/<x> WITHOUT confirm=1, so the server answers with a
    confirmation page (no JavaScript); the second POST carries confirm=1."""
    return (f'<form class="action" method="post" action="{esc(path)}">{_hidden(hidden)}{extra} {_credentials()} '
            f'<button type="submit">{esc(label)}</button></form>')


def resolve_forms(attempt_id: int, back: str) -> str:
    return (action_form("/action/resolve", "resolve: recovered", {"attempt": attempt_id, "back": back, "mode": "recovered"},
                        'paise <input type="text" name="recovered_paise" size="8" required> note <input class="note" type="text" name="note" maxlength="200">')
            + action_form("/action/resolve", "resolve: closed", {"attempt": attempt_id, "back": back, "mode": "closed"},
                          'note <input class="note" type="text" name="note" maxlength="200">'))


def job_forms(job: RecoveryJob, back: str) -> str:
    out = []
    if job.status == JobStatus.SENT.value and job.razorpay_link_id:
        out.append(action_form("/action/cancel", f"cancel link (job#{job.id})", {"job": job.id, "back": back},
                               'reason <input class="note" type="text" name="reason" maxlength="200">'))
        out.append(action_form("/action/resend", f"resend notification (job#{job.id})", {"job": job.id, "back": back},
                               'by <select name="medium"><option value="sms">sms</option><option value="email">email</option></select>'))
    if job.status == JobStatus.PENDING.value:
        out.append(action_form("/action/retry", f"run job#{job.id} now", {"job": job.id, "back": back}))
    return "".join(out)


def actions_note() -> str:
    if actions_enabled():
        return ""
    return f'<p class="muted">{esc(HOW_TO_ENABLE)}.</p>'


def table(headers: list[str], rows: list[list[str]], *, right: set[int] = frozenset(), cls: str = "") -> str:
    """Cells are already-escaped HTML fragments; headers are escaped here."""
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f'<td{" class=num" if i in right else ""}>{c}</td>' for i, c in enumerate(r)) + "</tr>"
                   for r in rows)
    if not rows:
        body = f'<tr><td colspan="{len(headers)}" class="muted">(no rows)</td></tr>'
    return f'<table class="{cls}"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'


def link_cell(job: RecoveryJob | None) -> str:
    if job is None:
        return "-"
    if job.razorpay_link_url:
        return f'<a href="{esc(job.razorpay_link_url)}" rel="noopener">{esc(job.razorpay_link_url)}</a>'
    return "(exists)" if job.status == JobStatus.SENT.value else "-"


def latest_jobs(session) -> dict[int, RecoveryJob]:
    """One status per attempt: its latest job (as `demo` totals count them)."""
    out: dict[int, RecoveryJob] = {}
    for j in session.execute(select(RecoveryJob)).scalars().all():
        if j.attempt_id not in out or j.id > out[j.attempt_id].id:
            out[j.attempt_id] = j
    return out


# ---- pages ---------------------------------------------------------------------------------------

def render_index(session, query: dict[str, list[str]]) -> str:
    want_class = (query.get("class") or [""])[0]
    want_status = (query.get("status") or [""])[0]
    attempts = session.execute(select(PaymentAttempt).order_by(PaymentAttempt.id)).scalars().all()

    rows = []
    for a in attempts:
        d, j, o = latest(a.decisions), latest(a.jobs), latest(a.outcomes)
        if want_class and (d.failure_class if d else "") != want_class:
            continue
        if want_status and (j.status if j else "") != want_status:
            continue
        rows.append([
            f'<a href="/attempt/{a.id}">{a.id}</a>', esc(a.razorpay_payment_id), esc(a.method), esc(format_rupees(a.amount_paise)),
            esc(d.failure_class if d else "-"), esc(d.classified_by if d else "-"), esc(d.action if d else "-"),
            esc(human_delay(d.delay_seconds) if d else "-"),
            esc(f"#{j.id}/{j.retry_seq}" if j else "-"), esc(j.status if j else "-"),
            link_cell(j), esc((j.nudge_source or "-") if j else "-"), esc(outcome_text(o, j)),
        ])
    headers = ["id", "payment_id", "method", "amount", "class", "by", "action", "delay", "job", "status", "link",
               "nudge", "outcome"]

    # totals, as `demo` prints them
    per_attempt = latest_jobs(session)
    counts: dict[str, int] = {}
    for j in per_attempt.values():
        counts[j.status] = counts.get(j.status, 0) + 1
    all_jobs = session.execute(select(RecoveryJob)).scalars().all()
    links = sum(1 for j in all_jobs if j.razorpay_link_id)
    recovered = session.execute(select(Outcome).where(Outcome.recovered.is_(True))).scalars().all()
    total_paise = sum(o.amount_recovered_paise for o in recovered)
    always = [JobStatus.SENT, JobStatus.HUMAN_QUEUE, JobStatus.STUBBED, JobStatus.FAILED]
    shown = [s.value for s in always] + [s.value for s in JobStatus if s not in always and counts.get(s.value)]
    tot_headers = ["attempts"] + shown + ["links created", "recovered", "amount recovered"]
    tot_row = [esc(len(attempts))] + [esc(counts.get(s, 0)) for s in shown] + [
        esc(links), esc(len(recovered)), esc(format_rupees(total_paise))]

    opts = lambda enum, chosen: "".join(  # noqa: E731
        f'<option value="{esc(e.value)}"{" selected" if e.value == chosen else ""}>{esc(e.value)}</option>' for e in enum)
    filt = (f'<form class="filter" method="get" action="/">class <select name="class"><option value="">(any)</option>'
            f'{opts(FailureClass, want_class)}</select> status <select name="status"><option value="">(any)</option>'
            f'{opts(JobStatus, want_status)}</select> <button type="submit">filter</button> <a href="/">clear</a>'
            f' <span class="muted">(GET query only; nothing is written)</span></form>')
    body = (f'<pre class="header">{esc(chr(10).join(header_lines()))}</pre>'
            f"<h2>totals</h2>{table(tot_headers, [tot_row], cls='totals')}"
            f"<h2>attempts ({len(rows)} of {len(attempts)} shown)</h2>{filt}{table(headers, rows, right={3})}"
            f'<p class="muted">Same columns as <code>python -m app.main show</code>. Auto-refreshes every {REFRESH_SECONDS}s.</p>')
    return page("attempts", body, refresh=True)


def render_attempt(session, attempt_id: int, query: dict | None = None) -> str | None:
    a = session.get(PaymentAttempt, attempt_id)
    if a is None:
        return None
    back = f"/attempt/{a.id}"
    ops = actions_note()
    if actions_enabled():
        latest_job = latest(a.jobs)
        parts = []
        if latest_job is not None and latest_job.status == JobStatus.HUMAN_QUEUE.value:
            parts.append(resolve_forms(a.id, back))
        parts.extend(job_forms(j, back) for j in sorted(a.jobs, key=lambda x: x.id))
        inner = "".join(parts) or '<span class="muted">nothing to do: no human-queued, pending or sent job on this attempt</span>'
        ops = f'<h2>operator actions</h2><div class="actions">{inner}</div>'
    err = table(["code", "source", "step", "reason", "description"],
                [[esc(a.error_code), esc(a.error_source), esc(a.error_step), esc(a.error_reason), esc(a.error_description)]])
    evt = table(["payment_id", "order_id", "merchant", "method", "amount", "has_token", "customer", "contact", "email",
                 "failed_at"],
                [[esc(a.razorpay_payment_id), esc(a.order_id), esc(a.merchant_id), esc(a.method), esc(format_rupees(a.amount_paise)),
                  esc(a.has_token), esc(a.customer_name), esc(a.customer_contact), esc(a.customer_email), esc(ts(a.failed_at))]])
    decisions = table(
        ["id", "class", "action", "delay", "max attempts", "classified_by", "model", "latency ms", "confidence",
         "fallback_taken", "reason", "at"],
        [[esc(d.id), esc(d.failure_class), esc(d.action), esc(human_delay(d.delay_seconds)), esc(d.max_attempts),
          esc(d.classified_by), esc(d.llm_model), esc(d.llm_latency_ms), esc(d.confidence), esc(d.fallback_taken),
          esc(d.reason), esc(ts(d.created_at))] for d in sorted(a.decisions, key=lambda x: x.id)])
    jobs = table(
        ["id", "retry_seq", "action", "status", "idempotency key", "reference_id", "link id", "link", "attempts_made",
         "last_error", "nudge channel", "nudge subject", "nudge body", "nudge source", "scheduled", "executed"],
        [[esc(j.id), esc(j.retry_seq), esc(j.action), esc(j.status), esc(j.idempotency_key), esc(j.idempotency_key),
          esc(j.razorpay_link_id), link_cell(j), esc(j.attempts_made), esc(j.last_error), esc(j.nudge_channel),
          esc(j.nudge_subject), esc(j.nudge_body), esc(j.nudge_source), esc(ts(j.scheduled_at)), esc(ts(j.executed_at))]
         for j in sorted(a.jobs, key=lambda x: x.id)])
    outcomes = table(["id", "job", "recovered", "amount", "note", "recovered_at", "at"],
                     [[esc(o.id), esc(o.job_id), esc(o.recovered), esc(format_rupees(o.amount_recovered_paise)), esc(o.note),
                       esc(ts(o.recovered_at)), esc(ts(o.created_at))] for o in sorted(a.outcomes, key=lambda x: x.id)])

    trail = session.execute(select(AuditEvent).where(AuditEvent.attempt_id == a.id).order_by(AuditEvent.id)).scalars().all()
    audit_rows = []
    for r in trail:
        payload = ""
        if r.data_json:
            try:
                data = json.dumps(json.loads(r.data_json), sort_keys=True, indent=2)
            except ValueError:
                data = r.data_json
            payload = f"<details><summary>data</summary><pre>{esc(data)}</pre></details>"
        audit_rows.append([esc(ts_audit(r.created_at)), esc(r.stage), esc(r.message), payload])
    audit = table(["time", "stage", "message", "data"], audit_rows)

    body = (f"<p><a href=\"/\">&larr; all attempts</a></p>{ops}<h2>event</h2>{evt}<h2>error object (verbatim from Razorpay)</h2>{err}"
            f"<h2>decision</h2>{decisions}<h2>jobs</h2>{jobs}<h2>outcomes</h2>{outcomes}"
            f"<h2>audit trail for attempt {a.id} ({esc(a.razorpay_payment_id)}, {esc(a.method)}, "
            f"{esc(format_rupees(a.amount_paise))}): {len(trail)} rows</h2>{audit}"
            f'<p class="muted">Same rows as <code>python -m app.main audit --attempt {a.id}</code>.</p>')
    return page(f"attempt {a.id} {a.razorpay_payment_id}", body, flash=flash_html(query))


def render_human_queue(session, query: dict | None = None) -> str:
    items = priority.human_queue(session)
    total = sum(it.estimate.expected_paise for it in items)
    rows = []
    for it in items:
        a, j, e = it.attempt, it.job, it.estimate
        rows.append([f'<a href="/attempt/{a.id}">{a.id}</a>', esc(a.razorpay_payment_id), esc(a.method),
                     esc(format_rupees(a.amount_paise)), esc(format_rupees(e.expected_paise)), esc(f"{e.probability:.2f}"),
                     esc(e.source + (" as if " if e.as_if else " ") + e.basis), esc(it.failure_class), esc(it.classified_by),
                     esc(it.fallback_taken), esc(it.reason), esc(f"#{j.id}/{j.retry_seq}"), esc(ts(it.queued_at))]
                    + ([resolve_forms(a.id, "/human-queue")] if actions_enabled() else []))
    headers = ["id", "payment_id", "method", "amount", "expected", "p", "source", "class", "by", "fallback", "reason",
               "job", "queued at"] + (["resolve"] if actions_enabled() else [])
    intro = (('<p class="warn">Resolving here writes an outcome and an audit row naming you; the CLI '
              '<code>python -m app.main resolve --attempt ID</code> does the same.</p>') if actions_enabled() else
             ('<p class="warn">Read-only. Resolution happens on the command line: '
              "<code>python -m app.main resolve --attempt ID</code>. Nothing on this page changes state.</p>"
              + actions_note()))
    body = (intro
            + f"<h2>{len(rows)} attempts parked for a person, highest expected recovery first "
            f"({esc(format_rupees(total))} expected in total)</h2>{table(headers, rows, right={3, 4, 5})}"
            f'<p class="muted">An attempt is listed when its latest job is <code>human_queue</code>. '
            f"{esc(priority.PRIOR_NOTE)}. Same rows as <code>python -m app.main queue</code>. "
            + (f"Auto-refreshes every {REFRESH_SECONDS}s.</p>" if not actions_enabled() else
               "No auto-refresh while actions are on (it would clear a form you are filling in).</p>"))
    return page("human queue", body, refresh=not actions_enabled(), flash=flash_html(query))


def render_policy(query: dict[str, list[str]]) -> str:
    """The policy table, or one merchant's effective table after merchants/<id>.json (docs/merchants.md)."""
    merchant = (query.get("merchant") or [""])[0] or None
    rows = merchants.effective_table(merchant)
    ov = merchants.get(merchant)
    cells = [[esc(r["failure_class"]), esc(r["action"]), esc(r["action_with_token"]), esc(r["delay"]),
              esc(f"x{r['backoff_multiplier']:g}"), esc(r["max_attempts"]), "yes" if r["nudge"] else "no",
              f'<span class="warn">{esc(r["override"])}</span>' if r["override"] else "-", esc(r["rationale"])]
             for r in rows]
    filt = (f'<form class="filter" method="get" action="/policy">merchant <input name="merchant" size="16" '
            f'value="{esc(merchant or "")}"> <button type="submit">show</button> <a href="/policy">clear</a> '
            f'<span class="muted">(GET query only; nothing is written)</span></form>')
    if merchant:
        head = f"<p>merchant <b>{esc(merchant)}</b>: {esc(ov.describe() if ov else '(no override file; table defaults)')}</p>"
    else:
        head = '<p class="muted">app/policy.py as shipped; per-merchant overrides live in merchants/&lt;merchant_id&gt;.json.</p>'
    body = (f'<pre class="header">{esc(chr(10).join(header_lines()))}</pre>{filt}{head}'
            + table(["class", "action", "with token", "delay", "backoff", "max attempts", "nudge", "override", "rationale"],
                    cells, right={5})
            + f'<p class="muted">Same table as <code>python -m app.main policy'
              f'{" --merchant " + esc(merchant) if merchant else ""}</code>.</p>')
    return page(f"policy{' ' + merchant if merchant else ''}", body)


# ---- metrics -----------------------------------------------------------------------------------

def _metric_line(name: str, value, labels: dict | None = None) -> str:
    def q(v):
        return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    lab = "{" + ",".join(f'{k}="{q(v)}"' for k, v in labels.items()) + "}" if labels else ""
    return f"{name}{lab} {value}"


def render_metrics(session) -> str:
    """Prometheus text exposition (format 0.0.4), computed from the database on every scrape: the
    agent keeps no in-process counters, so a restart loses nothing and two replicas agree. The
    *_total series are counters in the Prometheus sense only as long as rows are never deleted."""
    attempts = session.execute(select(PaymentAttempt)).scalars().all()
    decisions = session.execute(select(RecoveryDecision).order_by(RecoveryDecision.id)).scalars().all()
    latest_decision: dict[int, RecoveryDecision] = {}
    for d in decisions:
        latest_decision[d.attempt_id] = d
    jobs = session.execute(select(RecoveryJob)).scalars().all()
    per_attempt = latest_jobs(session)
    outcomes = session.execute(select(Outcome).where(Outcome.recovered.is_(True))).scalars().all()

    by_class = {c.value: 0 for c in FailureClass}
    for a in attempts:
        d = latest_decision.get(a.id)
        cls = d.failure_class if d else FailureClass.UNKNOWN.value
        by_class[cls] = by_class.get(cls, 0) + 1
    by_status = {s.value: 0 for s in JobStatus}
    for j in per_attempt.values():
        by_status[j.status] = by_status.get(j.status, 0) + 1
    by_source = {"rules": 0, "llm": 0, "fallback": 0}
    fallbacks: dict[str, int] = {}
    for d in decisions:
        by_source[d.classified_by or "rules"] = by_source.get(d.classified_by or "rules", 0) + 1
        if d.fallback_taken:
            fallbacks[d.fallback_taken] = fallbacks.get(d.fallback_taken, 0) + 1
    queue = priority.human_queue(session)
    open_links = sum(1 for j in jobs if j.status == JobStatus.SENT.value and j.razorpay_link_id
                     and not any(o.job_id == j.id for o in outcomes))

    out = ["# HELP pra_build_info Build information for payment-recovery-agent.", "# TYPE pra_build_info gauge",
           _metric_line("pra_build_info", 1, {"version": BUILD_VERSION}),
           "# HELP pra_attempts_total Failed payment attempts ingested, by latest failure class.",
           "# TYPE pra_attempts_total counter"]
    out += [_metric_line("pra_attempts_total", n, {"class": c}) for c, n in sorted(by_class.items())]
    out += ["# HELP pra_jobs Attempts by the status of their latest recovery job.", "# TYPE pra_jobs gauge"]
    out += [_metric_line("pra_jobs", n, {"status": st}) for st, n in sorted(by_status.items())]
    out += ["# HELP pra_links_created_total Razorpay Payment Links created.", "# TYPE pra_links_created_total counter",
            _metric_line("pra_links_created_total", sum(1 for j in jobs if j.razorpay_link_id)),
            "# HELP pra_recovered_total Payments recovered (outcome rows with recovered=true).",
            "# TYPE pra_recovered_total counter", _metric_line("pra_recovered_total", len(outcomes)),
            "# HELP pra_recovered_amount_paise Amount recovered, in paise.", "# TYPE pra_recovered_amount_paise counter",
            _metric_line("pra_recovered_amount_paise", sum(o.amount_recovered_paise for o in outcomes)),
            "# HELP pra_human_queue_size Attempts whose latest job is parked for a person.",
            "# TYPE pra_human_queue_size gauge", _metric_line("pra_human_queue_size", len(queue)),
            "# HELP pra_human_queue_expected_paise Expected recovery of the human queue, in paise (estimate: simulation prior or measured rate).",
            "# TYPE pra_human_queue_expected_paise gauge",
            _metric_line("pra_human_queue_expected_paise", sum(it.estimate.expected_paise for it in queue)),
            "# HELP pra_llm_decisions_total Recovery decisions by how the failure was classified.",
            "# TYPE pra_llm_decisions_total counter"]
    out += [_metric_line("pra_llm_decisions_total", n, {"by": k}) for k, n in sorted(by_source.items())]
    out += ["# HELP pra_llm_fallbacks_total Decisions where an LLM path fell back, by fallback taken.",
            "# TYPE pra_llm_fallbacks_total counter"]
    out += [_metric_line("pra_llm_fallbacks_total", n, {"fallback": k}) for k, n in sorted(fallbacks.items())] or [
        _metric_line("pra_llm_fallbacks_total", 0, {"fallback": "none"})]
    out += ["# HELP pra_open_links Sent payment links with no outcome yet.", "# TYPE pra_open_links gauge",
            _metric_line("pra_open_links", open_links),
            "# HELP pra_merchant_overrides Merchant override files loaded (merchants/).", "# TYPE pra_merchant_overrides gauge",
            _metric_line("pra_merchant_overrides", merchants.loaded_count())]
    return "\n".join(out) + "\n"


def _esc_rows(rows: list[list[str]]) -> list[list[str]]:
    return [[esc(c) for c in r] for r in rows]


def render_insights(session, query: dict[str, list[str]]) -> str:
    """`python -m app.insights` as HTML: same sections, same numbers, tables instead of aligned text."""
    try:
        min_samples = max(1, int((query.get("min_samples") or [str(insights.MIN_SAMPLES)])[0]))
    except ValueError:
        min_samples = insights.MIN_SAMPLES
    merchant = (query.get("merchant") or [""])[0] or None
    report = insights.compute(session, min_samples=min_samples, merchant=merchant)
    scope = f" (merchant {merchant})" if merchant else ""
    head = (f'<pre class="header">{esc(chr(10).join(header_lines()))}</pre>'
            f'<p class="warn">{esc(insights.HEADER_NOTE)}</p>'
            f'<form class="filter" method="get" action="/insights">min-samples <input name="min_samples" size="4" '
            f'value="{esc(min_samples)}"> merchant <input name="merchant" size="16" value="{esc(merchant or "")}"> '
            f'<button type="submit">recompute</button> <a href="/insights">clear</a> '
            f'<span class="muted">(GET query only; nothing is written)</span></form>')
    if report.empty:
        body = head + '<p class="muted">no outcomes yet: no payment attempts in this database.</p>'
        return page(f"insights{scope}", body)
    h, rows = insights.class_rows(report)
    classes = table(h, _esc_rows(rows), right={1, 2, 3, 4, 5, 6, 7})
    h, rows = insights.bucket_rows(report)
    buckets = table(h, _esc_rows(rows)) if rows else '<p class="muted">(no sent links yet)</p>'
    h, rows = insights.llm_rows(report)
    llm_t = table(h, _esc_rows(rows), right={0, 1, 2, 3, 4, 5})
    h, rows = insights.fallback_rows(report)
    fallbacks = table(h, _esc_rows(rows), right={1}) if rows else '<p class="muted">fallbacks: none</p>'
    h, rows = insights.money_rows(report)
    money = table(h, _esc_rows(rows), right={1, 2, 3, 4, 5})
    h, rows = insights.proposal_rows(report)
    prop_rows = []
    by_class = {p.failure_class: p for p in report.proposals}
    for cls, text in rows:
        cell = esc(text)
        if text.startswith("PROPOSAL"):
            cell = f'<span class="warn">{cell}</span>'
            p = by_class.get(cls)
            if p is not None and p.id:
                cell += f' <code>id {esc(p.id)}</code>'
                if actions_enabled():
                    cell += action_form("/action/apply-proposal", f"apply {p.id}",
                                        {"proposal": p.id, "back": f"/insights?min_samples={min_samples}"
                                         + (f"&merchant={quote(merchant)}" if merchant else "")},
                                        f'merchant <input type="text" name="merchant" value="{esc(merchant or "all")}" size="14"> ')
        prop_rows.append([esc(cls), cell])
    proposals = table(h, prop_rows)
    gaps = [cs.failure_class for cs in report.classes if cs.rules_gap]
    gap_line = (f'<p class="warn">rules gap (human-queue share &gt; {insights.RULES_GAP_SHARE:.0%}): {esc(", ".join(gaps))} '
                f'&rarr; <a href="/rule-candidates">rule candidates</a></p>') if gaps else ""
    body = (head + f"<h2>per failure class ({report.attempts} attempts, {report.outcomes} outcome rows)</h2>{classes}"
            f'<p class="muted">{esc(insights.RATE_NOTE)}</p>'
            f"<h2>recovery rate by scheduled delay bucket (sent links only)</h2>{buckets}"
            f"<h2>LLM usage</h2>{llm_t}{fallbacks}<h2>money by merchant</h2>{money}"
            f"<h2>proposals (never applied)</h2>"
            f'<p class="muted">A change is proposed only when the policy bucket and a competing bucket both have '
            f"n &gt;= {min_samples} and their 95% intervals do not overlap. Proposals are printed, never applied.</p>"
            f"{proposals}{gap_line}"
            f'<p class="muted">Same numbers as <code>python -m app.insights --min-samples {min_samples}</code>. '
            f'A proposal with an id can be applied to a merchant file with its evidence: '
            f'<code>python -m app.merchants apply --proposal ID --merchant ID|--all --actor NAME</code>'
            f'{" (or the apply button)" if actions_enabled() else ""}; rollback with '
            f'<code>python -m app.merchants rollback</code>.</p>')
    return page(f"insights{scope}", body, flash=flash_html(query))


def render_rule_candidates(session, query: dict[str, list[str]]) -> str:
    """`python -m app.rule_candidates` as HTML. Descriptions only: never a name, contact or email."""
    try:
        min_occ = max(1, int((query.get("min_occurrences") or [str(rule_candidates.MIN_OCCURRENCES)])[0]))
    except ValueError:
        min_occ = rule_candidates.MIN_OCCURRENCES
    report = rule_candidates.scan(session, min_occurrences=min_occ)
    head = (f'<p class="warn">{esc(rule_candidates.HEADER_NOTE)}</p>'
            f'<form class="filter" method="get" action="/rule-candidates">min-occurrences '
            f'<input name="min_occurrences" size="4" value="{esc(min_occ)}"> <button type="submit">rescan</button> '
            f'<a href="/rule-candidates">clear</a> <span class="muted">(GET query only; nothing is written)</span></form>'
            f'<p class="muted">scanned {report.scanned} model-path decisions (classified_by in '
            f'{esc(", ".join(rule_candidates.MODEL_SOURCES))}) in {report.groups} signature group(s)</p>')
    if report.scanned == 0:
        return page("rule candidates", head + '<p class="muted">no model-path decisions yet: every classification so far '
                                              "came from the rules, or nothing has been processed.</p>")
    parts = [head, f"<h2>candidates ({len(report.candidates)})</h2>"]
    if not report.candidates:
        parts.append(f'<p class="muted">none: no signature recurs {min_occ}+ times with an agreed label.</p>')
    for i, c in enumerate(report.candidates, 1):
        conf = f" (mean confidence {c.mean_confidence:.2f})" if c.mean_confidence is not None else ""
        fields = table(list(c.fields.keys()), [[esc(v or "-") for v in c.fields.values()]])
        examples = "".join(f"<li>{esc(e)}</li>" for e in c.examples)
        parts.append(f"<h2>candidate {i}: <code>{esc(c.signature)}</code></h2>"
                     f"<p>count {c.count} (llm {c.llm_count}, fallback {c.fallback_count}); proposed class "
                     f"<b>{esc(c.proposed_class)}</b>{esc(conf)}; {esc(c.note)}</p>{fields}"
                     f"<p>examples (descriptions only):</p><ul>{examples}</ul>"
                     f"<p>rule stub for app/classify.py:</p><pre>{esc(c.rule_stub)}</pre>")
    if report.rejected:
        parts.append(f"<h2>recurring but rejected ({len(report.rejected)})</h2>"
                     + table(["count", "signature", "reason"],
                             [[esc(r.count), esc(r.signature), esc(r.reason)] for r in report.rejected], right={0}))
    parts.append(f'<p class="muted">Same content as <code>python -m app.rule_candidates --min-occurrences {min_occ}</code>.</p>')
    return page("rule candidates", "".join(parts))


def render_report(name: str) -> str | None:
    if name not in REPORTS:
        return None
    path, how = REPORTS[name]
    if path.is_file():
        text = path.read_text(encoding="utf-8", errors="replace")
        body = f'<p class="muted">{esc(path.relative_to(config.BASE_DIR))}, rendered as text (no markdown library).</p><pre>{esc(text)}</pre>'
    else:
        body = (f'<p class="warn">{esc(path.relative_to(config.BASE_DIR))} is not there yet. Generate it with:</p>'
                f"<pre>{esc(how)}</pre>")
    return page(f"{name} report", body)


# ---- server ------------------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "payment-recovery-agent-web/0"
    quiet = False

    def log_message(self, fmt, *args):  # noqa: D401
        if not self.quiet:
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, status: int, body: str, ctype: str = "text/html; charset=utf-8") -> None:
        raw = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)

    def do_HEAD(self):  # noqa: N802
        self.do_GET()

    def do_GET(self):  # noqa: N802
        parts = urlsplit(self.path)
        path, query = parts.path.rstrip("/") or "/", parse_qs(parts.query)
        if path == "/health":
            return self._send(200, "ok\n", "text/plain; charset=utf-8")
        if path == "/metrics":
            session = db.session()
            try:
                return self._send(200, render_metrics(session), METRICS_CONTENT_TYPE)
            except Exception as exc:  # a broken row must not take the scrape down; say what happened
                return self._send(500, f"# error: {type(exc).__name__}: {exc}\n", METRICS_CONTENT_TYPE)
            finally:
                session.close()
        try:
            body = self._route(path, query)
        except Exception as exc:  # a broken row must not take the whole view down; say what happened
            return self._send(500, page("error", f"<pre>{esc(type(exc).__name__ + ': ' + str(exc))}</pre>"))
        if body is None:
            return self._send(404, page("not found", f"<p>No such page: <code>{esc(path)}</code></p>"))
        self._send(200, body)

    def _route(self, path: str, query: dict) -> str | None:
        if path.startswith("/reports/"):
            return render_report(path[len("/reports/"):])
        session = db.session()
        try:
            if path == "/":
                return render_index(session, query)
            if path == "/human-queue":
                return render_human_queue(session, query)
            if path == "/insights":
                return render_insights(session, query)
            if path == "/rule-candidates":
                return render_rule_candidates(session, query)
            if path == "/policy":
                return render_policy(query)
            if path.startswith("/attempt/"):
                ref = path[len("/attempt/"):]
                if ref.startswith("pay_"):
                    a = session.execute(select(PaymentAttempt).where(PaymentAttempt.razorpay_payment_id == ref)).scalars().first()
                    return render_attempt(session, a.id, query) if a else None
                return render_attempt(session, int(ref), query) if ref.isdigit() else None
            return None
        finally:
            session.close()

    def do_POST(self):  # noqa: N802
        path = urlsplit(self.path).path.rstrip("/")
        if path not in ACTION_PATHS:
            return self._send(405, "read-only\n", "text/plain; charset=utf-8")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > MAX_BODY:
            return self._send(413, page("refused", "<p>form too large</p>"))
        raw = self.rfile.read(length) if length else b""
        form = {k: v[0] for k, v in parse_qs(raw.decode("utf-8", errors="replace"), keep_blank_values=True).items()}
        if not actions_enabled():
            return self._send(403, page("actions disabled", f'<p class="warn">{esc(HOW_TO_ENABLE)}.</p>'))
        if not token_matches(form.get("token")):
            return self._send(403, page("refused", '<p class="warn">refused: the operator token does not match '
                                                     "(OPERATOR_TOKEN). Nothing was changed.</p>"))
        actor = actions.check_actor(form.get("actor"))
        if actor is None:
            return self._send(400, page("refused", f'<p class="warn">refused: actor is required (1..{actions.ACTOR_MAX} '
                                                     "characters). Nothing was changed.</p>"))
        back = form.get("back") or "/"
        if not back.startswith("/") or back.startswith("//"):
            back = "/"
        if form.get("confirm") != "1":
            return self._send(200, confirm_page(path, form))
        try:
            result = run_action(path, form, actor)
        except Exception as exc:  # a bug must not leave the browser without an answer
            result = {"ok": False, "message": f"{type(exc).__name__}: {exc}"}
        sep = "&" if "?" in back else "?"
        target = f"{back}{sep}{'msg' if result.get('ok') else 'err'}={quote(str(result.get('message', ''))[:600])}"
        self.send_response(303)
        self.send_header("Location", target)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    do_PUT = do_DELETE = do_PATCH = do_POST


def confirm_page(path: str, form: dict) -> str:
    """Step two of every action: the same fields echoed as hidden inputs plus confirm=1. This is
    the confirm() dialog without JavaScript, so the CSP stays default-src 'none'."""
    shown = {k: v for k, v in form.items() if k not in ("token", "confirm", "back") and v != ""}
    rows = [[esc(k), esc(v)] for k, v in shown.items()]
    hidden = {k: v for k, v in form.items() if k != "confirm"}
    body = (f'<p class="warn">Confirm <b>{esc(path[len("/action/"):])}</b>? It writes to the database'
            f'{" and calls Razorpay" if path in ("/action/cancel", "/action/resend", "/action/retry") else ""}'
            f'{" and rewrites a merchant override file" if path == "/action/apply-proposal" else ""}, '
            f'and the audit trail will name <b>{esc(form.get("actor"))}</b>.</p>{table(["field", "value"], rows)}'
            f'<form method="post" action="{esc(path)}">{_hidden(hidden)}<input type="hidden" name="confirm" value="1">'
            f'<button type="submit">yes, do it</button></form> <p><a href="{esc(form.get("back") or "/")}">no, go back</a></p>')
    return page("confirm action", body)


def run_action(path: str, form: dict, actor: str) -> dict:
    """Dispatch one confirmed POST to app.actions on its own session; returns the action's dict."""
    session = db.session()
    try:
        if path == "/action/resolve":
            if form.get("mode") == "recovered":
                try:
                    paise = int((form.get("recovered_paise") or "").strip())
                except ValueError:
                    return {"ok": False, "message": "recovered amount must be a whole number of paise"}
                return actions.resolve(session, form.get("attempt"), recovered_paise=paise, note=form.get("note", ""), actor=actor)
            return actions.resolve(session, form.get("attempt"), closed=True, note=form.get("note", ""), actor=actor)
        if path == "/action/cancel":
            return actions.cancel_link(session, form.get("job"), actor, reason=form.get("reason", ""))
        if path == "/action/resend":
            return actions.resend_notification(session, form.get("job"), form.get("medium", ""), actor)
        if path == "/action/retry":
            return actions.retry_now(session, form.get("job"), actor)
        if path == "/action/apply-proposal":
            merchant = (form.get("merchant") or "").strip()
            return actions.apply_proposal(session, form.get("proposal", ""), actor,
                                          merchant_id=None if merchant in ("", merchants.ALL_MERCHANTS) else merchant)
        return {"ok": False, "message": f"no such action {path}"}
    finally:
        session.close()


def make_server(host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    """Bound but not serving; tests call serve_forever in a thread. init_db so an empty database renders.
    Merchant override files are validated here too: a malformed one raises merchants.MerchantConfigError."""
    merchants.load()
    db.init_db()
    ThreadingHTTPServer.allow_reuse_address = True
    return ThreadingHTTPServer((host, port), Handler)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.web", description=__doc__.split("\n\n")[0])
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--host", default=None,
                   help="interface to bind (default 127.0.0.1; anything else exposes an unauthenticated view)")
    args = p.parse_args(argv)
    host = args.host or "127.0.0.1"
    if host not in LOOPBACK_HOSTS:
        print(f"warning: binding to {host}: this view has NO authentication and shows customer contact details; "
              "only do this on a network you control", file=sys.stderr)
    try:
        server = make_server(host, args.port)
    except merchants.MerchantConfigError as exc:
        print(f"error: merchant override file rejected: {exc} (schema: docs/merchants.md)", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: cannot bind {host}:{args.port}: {exc}", file=sys.stderr)
        return 1
    bound_host, bound_port = server.server_address[:2]
    print(f"payment-recovery-agent operator view (read-only, no auth) on http://{bound_host}:{bound_port}/  "
          f"database {db.url()}  Ctrl-C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
