# Per-merchant overrides, the value-ordered queue, and /metrics

Three additions for the people who run the agent rather than build it: an ops team configures
the policy table per merchant with a JSON file, works the human queue in expected-value order, and
an SRE scrapes the agent with Prometheus. Nothing here changes what the agent does for a merchant
without a file; the default path through `policy.decide` is byte-identical (pinned by
`tests/test_merchants.py::test_no_overrides_is_byte_identical_to_the_table`).

Files: `app/merchants.py`, `app/priority.py`, additive changes in `app/policy.py`, `app/main.py`,
`app/web.py`; `merchants/_example.json`; `tests/test_merchants.py`, `tests/test_priority.py`.

## 1. Per-merchant policy overrides

```
merchants/<merchant_id>.json    overrides for one merchant (PaymentAttempt.merchant_id, e.g. merchant_acme.json)
merchants/_default.json         optional; every merchant inherits it, its own file wins field by field
merchants/_example.json         documentation only; any other file starting with "_" is skipped
```

The directory is `MERCHANTS_DIR` (relative to the repo root; default `merchants`; see
`.env.example`). `app/config.py` does not declare it yet, so `app/merchants.py` reads
`config.MERCHANTS_DIR` if present, then the environment, then the default. A missing directory
means "table defaults for every merchant" and the header says so.

### Schema

Every field is optional. Unknown keys are rejected, so a typo cannot silently do nothing.

```json
{
  "classes": {
    "INSUFFICIENT_FUNDS": {"delay_seconds": 86400, "max_attempts": 3, "nudge": false},
    "ISSUER_DOWN": {"action": "human_queue"}
  },
  "nudge_language": "hi",
  "max_contacts_per_week": 2,
  "notify_customer": false,
  "disabled_actions": ["token_retry"],
  "human_queue_all": false
}
```

| field | type / range | effect |
|---|---|---|
| `classes.<CLASS>.delay_seconds` | int >= 0 | base delay before the first attempt; the class's backoff multiplier still applies to later ones |
| `classes.<CLASS>.max_attempts` | int 0..5 | the stop rule; 0 closes the class `no_action` after the decision |
| `classes.<CLASS>.nudge` | bool | draft a customer message with a link action (a token retry is always silent) |
| `classes.<CLASS>.action` | only `"human_queue"` | route the class to a person for this merchant |
| `nudge_language` | `en` / `hi` / `hinglish` | language of the drafted nudge when the attempt carries none |
| `max_contacts_per_week` | int 0..50 | link/nudge sends per customer phone or email in a rolling 7 days (replaces `MAX_CONTACTS_PER_CUSTOMER_PER_WEEK` for this merchant) |
| `notify_customer` | bool | Razorpay `notify.sms/email` on the link; `false` always wins, `true` needs the global `RAZORPAY_NOTIFY_CUSTOMER=1` too |
| `disabled_actions` | list of `token_retry`, `recovery_link`, `nudge_change_method` | a disabled `token_retry` falls back to the class's link action; a disabled link action parks the class for a person (a class whose token path survives keeps it) |
| `human_queue_all` | bool | kill switch: every decision for this merchant goes to a person |

### What a file cannot do (enforced in `app/policy.py`, not only in the schema)

- `RISK_BLOCKED` and `UNKNOWN` stay `human_queue`. The only override accepted for them is
  `"action": "human_queue"` (a no-op); `policy.effective_entry` returns their table row untouched
  even for a hand-built object that bypassed validation.
- `action` may only move to `human_queue`. A merchant can make the agent more conservative, never
  less: no file turns a link into a token charge or a human-queued class into a retry. A forged
  `action` that is not `human_queue` is ignored by `policy.effective_entry`.
- `delay_seconds >= 0`, `max_attempts` in 0..5, `max_contacts_per_week` in 0..50.
- The gates in `decide` still run first: the LLM confidence gate, the reachability gate and the
  stop rule are not overridable.

### Loading, caching, failing

Files are read once per process (`merchants.load()`, called by `app.main` before any command and
by `app.web.make_server`) and cached; `merchants.reload()` re-reads them. An edit takes effect at
the next process start, never mid-run. A malformed file raises `MerchantConfigError` naming the
file and the field:

```
error: merchant override file rejected: merchants/merchant_bad.json: field classes.HARD_DECLINE.action:
action may only be 'human_queue' (or omitted to keep the table's action); a merchant file can make
the agent more conservative, never less; got 'token_retry'
```

and the CLI exits 1 before touching the database. It never falls back to the defaults silently:
an override that quietly did not apply is the worst outcome in a money path. The `demo` /
`serve-webhook` header carries a `merchants:` line saying how many files loaded from where and
which underscore files were skipped.

`merchants/_example.json` ships as documentation: it starts with an underscore so it is skipped,
and it carries a `_comment` key a real file may not. Copy it to `merchants/<merchant_id>.json`,
delete `_comment`, keep only the fields you mean to change.

### Seeing the effective table

```
python -m app.main policy                      # app/policy.py as shipped
python -m app.main policy --merchant merchant_acme
```

or `/policy?merchant=merchant_acme` in the operator view. The `override` column names the fields
the file changed; `policy.policy_table(overrides=...)` and `merchants.effective_table(merchant_id)`
return the same rows for docs and insights.

### Wiring the pipeline and executor (applied)

`policy.decide` gained `overrides: MerchantPolicy | None = None`; `merchants.for_attempt(attempt)`
returns the merged policy for an attempt's `merchant_id` (or `None`). It is wired in: both
`policy_mod.decide(...)` calls in `app/pipeline.py` (`_process` and `schedule_followup`) pass
`overrides=merchants.for_attempt(attempt)`; `attach_nudge` drafts in the customer's own
`customer_language` first and the merchant's `nudge_language` second; `app/scheduling.py:contact_cap`
takes the merchant's `max_contacts_per_week` when set; `app/executor.py:build_payment_link_payload`
ANDs the merchant's `notify_customer` into the link's `notify` flags (a file can only switch
notifications off). `tests/test_money_path.py::test_merchant_override_changes_the_delay_and_nudge_language`
drives a real webhook payload through the wired path.

Until that lands, files are loaded, validated and shown (`policy --merchant`, `/policy`,
the header) but do not change decisions.

## 2. Expected-value prioritisation

```
expected_recovery(attempt, failure_class, action) = amount_paise x P(recover | class, action)
```

`P` comes from one of two sources, and every estimate names which:

- **simulation prior**: `scripts/simulate.py`'s `RECOVERY_MODEL`, the hand-specified table the
  simulation report carries its caveat for. It is an assumption, not a measurement; good enough to
  order a queue, never a forecast. `app/priority.py` imports it (a copy pinned equal by test stands
  in if `scripts/` is missing). The lookup is `(class, action kind, delay bucket)` at the table's
  delay (or the decision's, for an override); an unmodelled delay takes the class/action's most
  pessimistic cell; an unmodelled pair is worth 0.
- **empirical (n=..)**: `app/insights.py`'s recovered-among-sent-links rate for the class, used
  instead of the prior once the class has `n >= 30` sent links (the same bar insights sets for a
  proposal). Below that the interval is too wide to beat a stated assumption.

A parked attempt (`human_queue`) or a closed one (`no_action`) has P = 0 for the action itself,
which would make every queue row worth nothing. The queue is ordered by what a person could
recover by taking the table's action by hand, so the estimate uses the class's own table action
with the attempt's token state and is labelled `as if recovery_link@48h` (and so on). RISK_BLOCKED
rows are worth 0 by the model and sort last; UNKNOWN rows carry the model's 0.05 blind-link cell.

Where it shows:

```
python -m app.main queue [--limit N]      # the human queue, highest expected recovery first
python -m app.main show --by-value        # every attempt, highest expected recovery first, + source column
python -m app.main process --all          # batch line + "expected recovery (estimate, ...)" + top 5
python -m app.main import-csv F --process # same two lines after the batch totals
```

and `/human-queue` in the operator view (columns `expected`, `p`, `source`). Each estimate prints
as `Rs 1,049.58 = Rs 2,499.00 x 0.42 [simulation prior, recovery_link@48h]`. The batch total is
labelled an estimate from a simulation prior; it is not money in the bank.

## 3. `GET /metrics`

Prometheus text exposition format 0.0.4 (`Content-Type: text/plain; version=0.0.4; charset=utf-8`),
written by hand, computed from the database on every scrape. The agent keeps no in-process
counters, so a restart loses nothing and two replicas agree; the `*_total` series are counters in
the Prometheus sense only as long as rows are never deleted.

| metric | labels | meaning |
|---|---|---|
| `pra_build_info` | `version="0.1"` | always 1 |
| `pra_attempts_total` | `class=<FailureClass>` | attempts by their latest decision's class (every class always present) |
| `pra_jobs` | `status=<JobStatus>` | attempts by the status of their latest job (every status always present) |
| `pra_links_created_total` | | jobs with a Razorpay link id |
| `pra_recovered_total` | | outcome rows with `recovered=true` |
| `pra_recovered_amount_paise` | | sum of those amounts |
| `pra_human_queue_size` | | attempts whose latest job is `human_queue` |
| `pra_human_queue_expected_paise` | | their expected recovery (estimate, see above) |
| `pra_llm_decisions_total` | `by=rules|llm|fallback` | decisions by classification source |
| `pra_llm_fallbacks_total` | `fallback=<fallback_taken>` | decisions where an LLM path fell back (`fallback="none"` 0 when there are none) |
| `pra_open_links` | | sent links with no outcome yet |
| `pra_merchant_overrides` | | merchant override files loaded |

It is unauthenticated like the rest of the operator view (`python -m app.web`, loopback by
default): scrape it from the same host or a network you control. A scrape that hits a broken row
answers 500 with `# error: ...` in the same content type rather than taking the view down.
`tests/test_web.py::test_metrics_are_prometheus_text_format_computed_from_the_database` parses
every sample line as `name{labels} value` and checks the numbers against the index page.
