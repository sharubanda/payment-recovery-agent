# Operator runbook: running the agent as a product

One install, one guided setup, one process, and it tells you what it did. Everything here is
`app/ops.py`; the `pra` command is `app.main:main` (so `python -m app.main <cmd>` is the same thing).

## 1. Install

```bash
git clone <repo> && cd payment-recovery-agent
python3 -m venv .venv && . .venv/bin/activate
make install            # = pip install -e .   -> the `pra` command
pra --help
```

`pyproject.toml` pins the same versions as `requirements.txt`. The few-shot fixture `app/llm.py`
reads at import (`tests/fixtures/failure_cases.json`) also ships as `app/data/failure_cases.json`, so
a non-editable install (`pip install .`, the Docker image) still renders examples into the prompt.
`merchants/_example.json` ships as `app/data/merchants_example.json`.

## 2. Guided setup: `pra init`

```bash
pra init                                # interactive
pra init --yes                          # no prompts: fixture mode, generated webhook secret, doctor
pra init --yes --mode shadow --key-id rzp_test_XXXX --key-secret YYYY \
         --anthropic-key sk-ant-... --alert-webhook-url https://hooks.slack.com/services/...
```

What it does, in order:

1. explains the two modes (below);
2. reads `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET` (flag, prompt or the existing `.env`). Anything
   that is not `rzp_test_...` is refused and nothing is written. A test-mode pair is verified with
   ONE read-only call, `GET /payments?count=1`, through the live client's own request helper; a 401
   or a network error is explained and nothing is written (`--skip-verify` writes anyway). No keys =
   fixture mode (in-memory Payment Links, nothing leaves the machine);
3. reads `ANTHROPIC_API_KEY` (optional; format only, never called);
4. reads `RAZORPAY_WEBHOOK_SECRET` or generates one (`secrets.token_hex(24)`);
5. writes `.env` from `.env.example`, keeping its comments and order. It never overwrites an existing
   `.env` without `--force`; `--env PATH` writes elsewhere;
6. prints the exact Razorpay dashboard steps (Settings -> Webhooks -> Add New Webhook: URL
   `<public-url>/razorpay/webhook`, the secret, the events `payment.failed`, `payment.captured`,
   `order.paid`, `payment_link.paid`, `payment_link.expired`, `subscription.pending`,
   `subscription.halted`) and the `ngrok http 8080` line;
7. runs `pra doctor` against the values it just wrote (`--no-doctor` skips it).

Flags: `--yes/-y`, `--force`, `--env PATH`, `--mode live|shadow`, `--key-id`, `--key-secret`,
`--anthropic-key`, `--webhook-secret`, `--alert-webhook-url`, `--operator-token`, `--database-url`,
`--skip-verify`, `--no-doctor`, `--port` (only for the printed ngrok line).

## 3. Modes: `PRA_MODE=live|shadow`

- `live` (default): the executor makes the outbound Razorpay calls. Test-mode keys only, as always; a
  `rzp_live_` key is refused at startup by `config.razorpay_live()`, by `LiveRazorpayClient`, by `pra init`
  and by `pra doctor`.
- `shadow`: the whole pipeline runs and records what it WOULD do (classification, decision, schedule,
  nudge text), but the executor makes NO outbound call and marks each job `shadow`. Start a new merchant
  here, read `pra digest` for a few days, then flip to `live`. The guard lives in `app/executor.py`
  (`getattr(config, "PRA_MODE", "live")`); `app/ops.py` only declares, documents and prints it.

The mode is printed by every header (`demo`, `serve-webhook`, `pra serve`) and by `pra digest`.

## 4. Health: `pra doctor`

```bash
pra doctor [--host 0.0.0.0] [--port 8080] [--web-port 8000]
```

One `PASS`/`WARN`/`FAIL` line per check, then a summary; exit 1 on any FAIL, so it works as a
container health check or a CI gate:

| check | FAIL when | WARN when |
|---|---|---|
| python | < 3.11 | |
| .env | | missing (defaults apply) |
| mode | not live/shadow | |
| razorpay keys | a non-`rzp_test_` key, or a key id without a secret | |
| llm key | malformed | absent (templates + human queue) |
| webhook secret | | unset (unsigned POSTs accepted) |
| alerts | | `ALERT_WEBHOOK_URL` unset |
| merchants | a malformed `merchants/*.json` | |
| database | unreachable, or tables missing after `create_all` | |
| webhook port / web port | | in use |
| pipeline | a synthetic `payment.failed` through `ingest_event` raises | |

The pipeline check runs in a throwaway in-memory SQLite against the fixture client and is rolled
back: the configured database and the network are never touched.

## 5. One process: `pra serve`

```bash
pra serve [--host 0.0.0.0] [--port 8080] [--web-port 8000] [--interval 30] [--no-web]
make run
```

Three daemon threads, one lock, clean Ctrl-C:

- **webhook receiver** on `--port`: the handler from `app.ingest.make_handler` (the same one
  `serve-webhook` uses) with `process=True, execute_now=True`: `POST /razorpay/webhook`, `GET /health`.
  Signatures are verified when `RAZORPAY_WEBHOOK_SECRET` is set; otherwise a warning is printed;
- **scheduler**: `scheduler.run_due` every `--interval` seconds, a fresh session per sweep, under the
  receiver's lock (SQLite and the pipeline are single-writer). After each sweep it polls for
  `human_queue` jobs created since the last sweep and alerts once per job id;
- **operator view** on `--web-port`: `app.web.make_server` (skipped with `--no-web`, or with a warning if
  `app.web` is not importable). Set `OPERATOR_TOKEN` before binding it to anything but loopback.

The startup banner lists both URLs, the mode, which Razorpay client and LLM are real, the database,
the merchant overrides, where alerts go, and the ngrok line. `serve-webhook`, `run-due --loop` and
`python -m app.web` keep working as before.

## 6. Digest and alerts

```bash
pra digest [--since 24h|7d|30m] [--write digest.md] [--post]
make digest
pra alert-test
```

A plain-text/Markdown summary of the period, in IST (`DIGEST_TIMEZONE=Asia/Kolkata` is a fixed +05:30
offset; anything else renders UTC): events in and the amount at risk, links created (and shadow jobs),
reminders sent, recovered count and amount, expired unpaid, cancelled (paid elsewhere), human-queue
additions with amounts, LLM calls and fallbacks, the top 5 queue entries by expected recovery
(`app.priority`), and the "needs a person" list with the `pra resolve` line.

`--post` sends it as `{"text": ...}` (Slack-compatible incoming webhook) to `ALERT_WEBHOOK_URL`;
`pra alert-test` posts one test line. Exit 1 when the post fails or the URL is unset. Inside `pra serve`
every new human-queue entry is posted the same way (`ops.alert_human_queue(session, attempt, job)`);
without a URL the alert is printed only. A daily digest is one cron line:

```
0 9 * * *  cd /srv/pra && .venv/bin/pra digest --since 24h --post
```

## 7. Docker

```bash
docker build -t pra .
docker run --env-file .env -p 8080:8080 -p 8000:8000 -v pra-data:/srv/pra/data pra
docker run --env-file .env pra pra doctor
```

`python:3.11-slim`, non-root user `pra`, `pip install .`, `CMD pra serve --host 0.0.0.0`. SQLite lives
on the `/srv/pra/data` volume (`DATABASE_URL` defaults there); point `DATABASE_URL` at Postgres for
more than one process. The image was written, not built, in the authoring environment (no Docker).

## 8. CI

`.github/workflows/ci.yml` has two jobs. `test` is unchanged (the README's clean-clone order, SQLite).
`postgres` starts a `postgres:16` service, `pip install -e . psycopg2-binary`, sets
`DATABASE_URL=postgresql+psycopg2://recovery:recovery@localhost:5432/recovery`, runs the test suite,
`python -m app.main demo` and `pra doctor`. It could not be exercised in the authoring environment (no Postgres there); its first run on the
public repository passed: suite, demo and doctor all green against `postgres:16`.

## 9. Make targets

`make install` (pip install -e .), `make init`, `make doctor`, `make run` (= `pra serve`),
`make digest`; the older `setup`, `demo`, `chaos`, `simulate`, `eval`, `insights`, `test` are unchanged.
