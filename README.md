# payment-recovery-agent

A CLI agent that takes failed Razorpay payment events, works out *why* each one failed, and takes the one recovery action that fits that cause: a fresh Payment Link with a customer message, a (stubbed) token retry, or nothing, with a person deciding. Built in a four-hour timebox for Razorpay's AI Buildathon, Track 03 (AI Revenue Recovery), and extended since with the behaviours documented under `docs/`.

Four failures that look identical to a "retry everything at T+1h" cron job need four different responses. Insufficient funds: a retry in ten minutes is worthless, the balance replenishes on the salary cycle, so ask again in 48 hours. Issuer outage: a retry in fifteen minutes is exactly right. Stolen card: a retry is fraud-adjacent and must never happen. Abandoned at the OTP page: do not retry at all, send the customer a link back to checkout while the intent is still warm. Blanket retry burns issuer trust, risks double charges, and recovers the wrong subset. This agent classifies first with deterministic rules, decides from a policy table, and only then acts, under an idempotency key that is UNIQUE in the database and checked before any outbound call, so a redelivered event cannot schedule or send a second recovery. It also stops the moment the order is paid another way, times customer contact around salary cycles and quiet hours, and caps how often one customer is contacted.

## What recovery actually is

You cannot re-charge a failed card. Without a saved token or an active mandate there is no instrument to charge; Razorpay (like every gateway) needs the customer back in the loop. So "recovery" is one of exactly three real actions, and the agent's `Action` enum (`app/taxonomy.py`) has nothing else in it apart from `reminder`, which re-notifies a link that already exists and never creates one:

1. **Token retry** (`token_retry`): charge the saved token or mandate again, silently. Only when `has_token` is true, and only for the two transient classes (`ISSUER_DOWN`, `NETWORK_TIMEOUT`). **Stubbed in this build.** There are no real tokens in test mode, and a recurring charge needs the Orders and Payments (recurring) APIs, which were not verifiable offline. The executor records the job as `stubbed` with the audit line `STUB: would charge saved token via Orders+Payments API (recurring); no real tokens in test mode` and makes zero outbound calls (`app/executor.py`, `execute_job`).
2. **Recovery link** (`recovery_link`, and `nudge_change_method` for a dead card): create a fresh Razorpay Payment Link and draft the customer message. This is done for real in test mode when keys are present, and against an in-memory fixture with the same request and response shapes when they are not. The drafted message is stored on the job and never sent: no SMS or email provider is in scope. What *can* reach the customer is Razorpay's own link notification (`notify.sms` / `notify.email` at creation, `POST /payment_links/{id}/notify_by/{sms|email}` for reminders), only when `RAZORPAY_NOTIFY_CUSTOMER=1`, and the `deliver` audit rows say which of the two happened (`docs/delivery.md`).
3. **No action** (`human_queue`, `no_action`): risk blocks, unknown causes, low-confidence model answers, exhausted retry budgets, an order that was paid another way, or a customer contacted too often this week. The event is parked for a person or closed, and an `outcomes` row records it.

## Quickstart

Python 3.11. Dependencies are stdlib plus `sqlalchemy`, `anthropic`, `python-dotenv`, `pydantic` and `pytest`, pinned in `requirements.txt`. No web framework, no Razorpay SDK (see below for why).

```sh
git clone <repo-url> payment-recovery-agent
cd payment-recovery-agent
make setup        # python3 -m venv .venv, pip install -r requirements.txt, cp .env.example .env
make demo         # seed 22 events -> classify -> decide -> execute -> poll -> show
make chaos        # inject all nine faults, regenerate docs/failure_report.md
make simulate     # 500 synthetic events, baseline vs agent, regenerate docs/simulation_report.md
make insights     # recovery rates by cause and delay with intervals, LLM usage, money by merchant, proposals
make test         # pytest (624 tests at the time of writing)
make eval         # score the rule classifier on 118 labelled cases, regenerate docs/classifier_eval.md
make serve        # read-only operator view on http://127.0.0.1:8000
make install      # pip install -e . -> the `pra` command; then: pra init, pra doctor, pra serve (see "Running it as a product")
```

`make demo` runs end to end from a clean clone with an empty `.env`. Real inputs (webhook payloads, the dashboard CSV export, a live receiver) are covered under "Getting events in" below. Its first lines say what is real in the run you are watching:

```
payment-recovery-agent demo
  razorpay : fixture client (no RAZORPAY_KEY_ID): in-memory Payment Links, nothing leaves this machine
  llm      : none (ANTHROPIC_API_KEY unset): unmapped failures -> human queue, nudges -> templates
  database : sqlite:///./recovery.db
  faults   : none
  merchants: 0 merchant override file(s) from merchants/; skipped _example.json (leading underscore: documentation only)
  note     : --execute-now runs each job immediately instead of at its scheduled time; the audit trail says so
```

**Without keys** (the default, and what CI runs): Razorpay is `FixtureRazorpayClient` (`app/razorpay_client.py`), an in-memory Payment Links server with the documented field names, `plink_`-shaped ids and `https://rzp.io/i/` short URLs, deterministic from the request. The LLM client is `None`: every event no rule matches goes to the human queue with `fallback_taken = llm_unavailable->human_queue`, and every customer message comes from the per-class template in `app/nudge_templates.py`. The demo's "recovered" rows are the fixture simulating a customer paying every other link, and it prints exactly that (`[fixture] simulating customer payment on 8 of 15 open links ...`). Nothing leaves the machine.

**With `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET`** (`rzp_test_` keys only): `LiveRazorpayClient` creates real test-mode Payment Links at `https://api.razorpay.com/v1/payment_links`, `poll` fetches their real status, and `mark-paid --job ID` records a link you paid yourself. A key that does not start with `rzp_test_` is refused at startup (`config.razorpay_live` raises; exit code 1). This agent never touches live money. Razorpay-side notification of the link (`notify.sms` / `notify.email`, `reminder_enable`, and the agent's own `notify_by` reminders) is off unless `RAZORPAY_NOTIFY_CUSTOMER=1`: the seed events carry realistic-looking contacts and a live test run would message them. The customer's name, contact and email still travel with the link so the hosted page is prefilled. With real keys the executor also asks `GET /orders/{id}/payments` before creating a link (`CHECK_ORDER_BEFORE_SEND=auto`), so an order paid without a webhook arriving is not linked twice.

**With `ANTHROPIC_API_KEY`**: the two LLM call sites go live. Unmapped failure descriptions are classified by the model into the closed `FailureClass` enum with a confidence that must clear 0.7, and customer nudges are drafted by the model instead of the template (model from `LLM_MODEL`, default `claude-opus-5`; timeout `LLM_TIMEOUT_SECONDS`, default 8). Every failure mode of both calls degrades to the same paths the keyless run takes.

Disclosure: both live paths are unit-tested against fakes (a fake `urlopen` in `tests/test_razorpay_client.py`, a fake Anthropic client in `tests/test_llm.py`) and were not run against the real APIs during the build. `docker-compose.yml` provides an optional Postgres; SQLite is the default and needs nothing.

## Pipeline

```
failed payment event (payment.failed, subscription.pending/halted, CSV export; here: scripts/seed.py or chaos.py)
      |
      v
[1] classify        app/classify.py    13 ordered rules on (code, source, step, reason) + description keywords. NO LLM.
      |                                  first match wins; nothing matched -> UNKNOWN
      | UNKNOWN only
      v
[2] llm_classify    app/llm.py         free-text description -> JSON {failure_class, confidence, rationale}
      |                                  pydantic against the closed enum; one repair call; any failure -> UNKNOWN (fallback)
      v
[3] policy          app/policy.py      table: class -> action, delay, backoff, max_attempts, nudge; merchants/<id>.json
      |             app/cadence.py     may only make it more conservative; subscription override (pending / halted)
      |                                  confidence < 0.7 from a non-rules source -> human_queue
      v
    persist         RecoveryDecision committed (class, action, who classified, model, latency, fallback)
      |
      v
[4] schedule        app/executor.py    INSERT recovery_jobs under sha256(payment_id:retry_seq), UNIQUE, committed
      |             app/scheduling.py  IntegrityError -> skipped_duplicate; order already paid -> no_action;
      |                                  salary window / quiet hours move the send; weekly contact cap -> human_queue
      v
    execute         app/executor.py    re-read the row; refuse anything already executed; order paid -> no_action;
      |                                  then the outbound call: Payment Links API with reference_id = key[:40];
      |                                  429/5xx/network: 3 attempts, backoff
      v
    reconcile       app/executor.py    a failed call that may have landed (5xx, transport, client crash): look the
      |                                  reference_id up at Razorpay BEFORE any follow-up; found -> sent, absent -> next
      |                                  attempt, cannot tell -> a person. A final 4xx or a local guard: a person, no retry
      v
    nudge           app/llm.py         draft the customer message AFTER the link exists (real short_url in the body)
      |                                  stored on the job, never sent by the agent
      v
    deliver         app/cadence.py     what Razorpay sent (notify.sms / notify.email receipts) or why nothing went
      |
      v
    reminders       app/cadence.py     per-class reminder jobs on the same link (notify_by), voided when the link closes
      |
      v
[5] outcome log     outcomes + audit_log    recovered / not recovered / human_queue / no_action / cancelled
```

The scheduler (`app/scheduler.py`, `run-due`) runs pending jobs and reminders when their time comes and walks the backoff chain for failed outbound calls that provably did not land. `poll` asks Razorpay what became of each open link; a `payment.captured` / `order.paid` webhook stops everything for that order and cancels a live link; a `payment_link.expired` webhook (or `poll` seeing `expired`) mints the class's next attempt. Details: `docs/money_path.md`, `docs/delivery.md`.

> The LLM is used in exactly two places: classifying failure descriptions that don't map to a known error code, and drafting the customer nudge. Everything else — the classification of known codes, the policy table, the retry scheduling, the timing rules, the reminder cadence — is deterministic. Razorpay's error codes are a finite enumerated set. An LLM over a lookup table would be slower, more expensive, non-deterministic, and less accurate. In a system that moves money, deterministic beats clever.

Concretely: `classify.classify` is pure and never raises; `policy.decide` is pure and never raises; for a rules-classified event the same event produces the same decision every time, and a duplicate delivery writes an identical second decision row to prove it (see the `duplicate_event` fault); an event that reaches the model is re-asked on each delivery and its decision row can differ. The model sees the error object, method and amount for classification and the customer's first name for the nudge; contact details never leave the process. The system prompt at each site is one byte-stable block marked for prompt caching (the classification block carries two labelled examples per class from `tests/fixtures/failure_cases.json`, chosen by sorted id, never at random); every call's tokens, cache reads, latency and stop reason go to an in-memory `usage_log` and a log line, and `llm_latency_ms` is stored per decision. No latency, token or cost figure is quoted here because none was measured against the real API. The nudge can be drafted in English, Hindi (Devanagari) or Hinglish: the language comes from the webhook's `notes.language`, the CSV's `language` column, the merchant file's `nudge_language`, then `NUDGE_LANGUAGE_DEFAULT`; an SMS is capped at 300 characters when every character is Latin-1 and 200 otherwise (UCS-2 segments), and a model answer that changes the amount, drops the link, promises a refund, uses a risk word or an emoji, or answers in the wrong script falls back to the template in the same language. `docs/llm.md` states the request shape, every rule and every fallback name.

## Getting events in: webhooks and the dashboard export

`scripts/seed.py` exists so the demo runs with nothing configured. Real inputs come through `app/ingest.py`, and every one of them lands on the same `PaymentAttempt` row and the same `pipeline.process_attempt`, so the guarantees below apply unchanged:

| entry point | command |
|---|---|
| a `payment.failed` webhook payload (or the bare `GET /payments/{id}` entity) from a file | `python -m app.main ingest FILE.json [--process] [--execute-now] [--signature HEX]` |
| `subscription.pending` / `subscription.halted` (a failed mandate charge) | the same `ingest` command; the attempt carries `subscription_id`, `subscription_status`, `subscription_url` |
| the dashboard's failed-payments CSV export (Transactions, Payments, Download) | `python -m app.main import-csv FILE.csv [--process] [--execute-now]` |
| a live receiver for Razorpay webhooks | `python -m app.main serve-webhook [--port 8080] [--process] [--execute-now]`: `POST /razorpay/webhook`, `X-Razorpay-Signature` verified when `RAZORPAY_WEBHOOK_SECRET` is set (401 on mismatch), a refused database write answers 500 so Razorpay redelivers |
| `payment_link.paid`, closing the loop without `poll` | `python -m app.main ingest paid.json` (matched by link id or `reference_id`, idempotent on redelivery) |
| `payment_link.expired`, minting the next attempt | the same command; the follow-up is counted from the expiry time |
| `payment.captured` / `order.paid`, stopping everything for that order | the same command; pending jobs voided, a live link cancelled at Razorpay |
| a person's decision on a human-queued attempt | `python -m app.main resolve --attempt ID (--recovered PAISE \| --closed) [--note TEXT]` |
| the scheduler as a daemon | `python -m app.main run-due --loop [--interval SECONDS]` |

`import-csv --process` ends with a batch line: amount at risk, how many got a link, how many were human-queued or stubbed, and an expected-recovery estimate labelled as such. Example payloads for every event above are in `docs/examples/`; the field mapping, the signature check, redelivery behaviour and a worked example pasted from a real run are in `docs/ingest.md`. Disclosure: the receiver was driven by tests and local requests only and has not received a live Razorpay delivery from this build; the payload fields it reads and the CSV header names are stated as assumptions in that document, and the shapes of the `payment.captured`, `order.paid`, `payment_link.expired` and `subscription.*` envelopes are assumptions listed in `docs/money_path.md` and `docs/delivery.md`.

## Failure taxonomy and policy table

Classification rules, in evaluation order (generated from `app/classify.py`, `rules_table()`). Keywords are word-boundary regexes on the lowercased description, so `otp` does not match `footprint`.

| # | rule | class | matches |
|---|---|---|---|
| 1 | `risk_flag` | RISK_BLOCKED | reason mentions risk/fraud (e.g. payment_risk_check_failed) or the description carries a risk, fraud, suspicious, velocity, blacklist or chargeback flag |
| 2 | `hard_decline_structured` | HARD_DECLINE | reason is international_transaction_not_allowed |
| 3 | `hard_decline_reason` | HARD_DECLINE | reason is card_declined and the description says blocked, expired, invalid, not enabled, restricted, lost, stolen, international or not permitted (and not 'not blocked') |
| 4 | `hard_decline_description` | HARD_DECLINE | outside the authentication step, the description names a blocked, expired, invalid, restricted, lost or stolen card, a card not enabled for the transaction, international cards not supported, a transaction not permitted to the cardholder, or a cancelled/expired/revoked mandate |
| 5 | `insufficient_funds` | INSUFFICIENT_FUNDS | reason mentions insufficient funds or the description says insufficient, not enough funds, low balance, balance too low, credit limit, or the Hinglish paryapt/aparyapt |
| 6 | `limit_exceeded_structured` | LIMIT_EXCEEDED | reason is transaction_limit_exceeded |
| 7 | `limit_exceeded_description` | LIMIT_EXCEEDED | the description names a transaction, daily, per-transaction, spending, withdrawal, transfer, card or UPI limit, or says a limit was exceeded/reached/exhausted or the amount exceeds a limit; never a rate limit, time limit or credit limit |
| 8 | `auth_abandoned_structured` | AUTH_ABANDONED | reason is payment_cancelled, or the payment timed out at the authentication step |
| 9 | `auth_abandoned_description` | AUTH_ABANDONED | at the authentication step the description says cancelled, OTP, 3DS, not completed, timed out or similar; at any step it says the customer or payer cancelled, declined or did not approve, or the collect request expired |
| 10 | `issuer_down_structured` | ISSUER_DOWN | reason is bank_technical_error or bank_failure |
| 11 | `issuer_down_description` | ISSUER_DOWN | source is bank (or the description names the bank/issuer) and it says down, unavailable, technical, outage, maintenance, not responding, inoperative, unable to process, or the Hinglish kaam nahi kar raha |
| 12 | `network_timeout_structured` | NETWORK_TIMEOUT | source is network, or reason is payment_timed_out outside authentication, or a GATEWAY_ERROR with reason gateway_technical_error, or a SERVER_ERROR with reason server_error |
| 13 | `network_timeout_description` | NETWORK_TIMEOUT | the description says timeout, timed out, no response, network, connection error or a temporary issue |
| - | (no rule) | UNKNOWN | anything else, by design: a generic "declined by the bank" is UNKNOWN, not a guess between insufficient funds and a dead card |

Risk beats everything; a lost or stolen card is a hard decline unless the same text also carries a risk word. Razorpay's exact `reason` vocabulary is partially uncertain, which is why every class has both a structured rule and a description rule, and why unmatched text falls through rather than being forced into a class.

Policy table (generated from `app/policy.py`, `policy_table()`; `python -m app.main policy [--merchant ID]` prints it, with a merchant's overrides applied):

| class | action (no token) | action (with token) | delay | backoff | max attempts | nudge | rationale |
|---|---|---|---|---|---|---|---|
| INSUFFICIENT_FUNDS | recovery_link | recovery_link | 48h | x1 | 2 | yes | Balance replenishes on salary cycles; a 10-minute retry burns an issuer attempt for nothing, so send a link and ask again in 48 hours. |
| ISSUER_DOWN | recovery_link | token_retry | 15m | x3 | 3 | yes (link only) | The issuer outage is transient but without a saved token the customer must re-initiate, so send a fresh link after 15 minutes and back off threefold if it stays down. |
| AUTH_ABANDONED | recovery_link | recovery_link | 10m | x1 | 2 | yes | Intent existed and friction at OTP or 3DS killed it; nothing can be retried without the customer, so send them back to checkout after 10 minutes. |
| HARD_DECLINE | nudge_change_method | nudge_change_method | now | x1 | 1 | yes | A blocked, expired or invalid card can never authorise, so the only recovery is asking the customer for a different payment method right away. |
| RISK_BLOCKED | human_queue | human_queue | now | - | 0 | no | A risk or fraud flag is never auto-retried; a person reviews it because a retry is fraud-adjacent and can get the merchant flagged. |
| NETWORK_TIMEOUT | recovery_link | token_retry | 5m | x2 | 2 | yes (link only) | A gateway timeout is likely transient but without a saved token the customer must re-initiate, so send a fresh link after 5 minutes and double the wait if it recurs. |
| LIMIT_EXCEEDED | recovery_link | recovery_link | 24h | x1 | 2 | yes | A daily or per-transaction limit clears when its window resets, so an immediate retry (token or not) burns an issuer attempt against the same cap; send a link and ask again after 24 hours. |
| UNKNOWN | human_queue | human_queue | now | - | 0 | no | No rule matched and the LLM did not resolve the cause with confidence, so a person decides rather than the agent guessing in a money path. |

Stopping rules and gates, in the order `policy.decide` and `executor.schedule_job` apply them:

- **Confidence gate.** A classification whose source is not `rules` (an LLM answer, or a fallback after an LLM failure) must have `confidence >= 0.7` (`config.LLM_MIN_CONFIDENCE`) or the decision is `human_queue` with the rationale `LLM confidence 0.55 is below the 0.7 threshold; a person decides ...`. A fallback classification carries confidence 0.0, so every LLM failure mode lands here regardless of class.
- **Terminal classes are not attempts.** RISK_BLOCKED and UNKNOWN have `max_attempts = 0` and ignore the retry sequence: a human queue is not something you retry. No merchant file can change that.
- **Max attempts.** `retry_seq > max_attempts` returns `no_action` with a rationale starting `stop rule: max attempts reached`. The delay before attempt *n* is `base * backoff ** (n - 1)`: ISSUER_DOWN waits 15m, 45m, 2h15m and then stops; NETWORK_TIMEOUT waits 5m, 10m and then stops.
- **Token retries are silent.** `nudge` is forced off for `token_retry`: the customer has nothing to do. INSUFFICIENT_FUNDS never token-retries even with a token; the same empty account in 48 hours is a link plus a message, not a silent charge.
- **A link needs a recipient.** A link action for an event with neither `customer_contact` nor `customer_email` is `human_queue` (`reachable=False` in `policy.decide`): a link nobody receives is not a recovery. A token retry needs no channel.
- **A subscription in Razorpay's own retry window is left alone.** `subscription.pending` (Razorpay retrying at T+1/2/3) is parked `human_queue` with the reason, unless the class is HARD_DECLINE or RISK_BLOCKED; `subscription.halted` uses the subscription's re-authorisation URL as the link and creates no Payment Link (`app/cadence.py`, `subscription_override`).
- **An order that is already paid stops everything.** `schedule_job` and `execute_job` both read `order_paid_at` before anything goes out; a paid order is `no_action` with `last_error = order already paid by <payment_id>` and an outcome, zero calls, token retries included.
- **Timing, then the cap.** A link job's send time moves out of the 24th-31st into day 2 of the next month at 10:00 IST for INSUFFICIENT_FUNDS (a salary-cycle heuristic, `SALARY_WINDOW_DAY`) and out of 21:00-09:00 IST for every class; the move is on the row as `schedule_note` and in the trail. A customer whose phone or email already received `MAX_CONTACTS_PER_CUSTOMER_PER_WEEK` (3) link or reminder sends in the last 7 days gets `human_queue` instead of a fourth (`app/scheduling.py`).
- **A sent link is never re-issued while it is open; an expired one earns the next attempt.** A live link is re-nudged by the reminder cadence on the same link, never replaced. A follow-up under a new key is minted in exactly two cases: the outbound call failed after its bounded HTTP retries *and* a `reference_id` lookup has shown nothing reached Razorpay (`app/executor.py`, `reconcile_failed_job`; `app/pipeline.py`, `finish_job`), or the link expired unpaid (`payment_link.expired` or `poll`), in which case the class's next delay counts from the expiry time until `max_attempts` closes the payment. If the lookup finds the link, the job is marked `sent` with it and gets its nudge; if it cannot answer, the payment is parked for a person. A final 4xx or a local guard (bad amount) is parked too: the same request would fail the same way.

### Classifier evaluation

`make eval` scores the rules over `tests/fixtures/failure_cases.json`: 118 hand-labelled cases (103 with a class, 15 UNKNOWN by design) covering Razorpay's standard checkout messages as remembered, ISO 8583 response codes, UPI, netbanking, wallet and Hinglish wording. It writes `docs/classifier_eval.md`. Two numbers matter: *wrong* (a class that is neither the expected one nor UNKNOWN), which is 0, and coverage of the cases that have a class, which is 100%; `tests/test_eval_classify.py` pins both. The rules were tuned against this set, so that score is a regression floor, not an estimate of production accuracy: the set is not production traffic, and Razorpay's `error_reason` vocabulary is partly assumed. Before tuning, the original rules scored 3 wrong and 26 missed on the same set; all three wrongs were guesses ("balance confirmation could not be obtained" read as insufficient funds, "mandate cancelled" as abandonment, "the card is not blocked" as a hard decline), which is exactly the failure the fall-through design exists to prevent. ISO 8583 `96 system malfunction` is left UNKNOWN on purpose: `scripts/chaos.py` uses it as the archetype of an unmapped message. The same file feeds the few-shot examples in the cached classification prompt, so a case added here changes both the regression floor and the model's examples.

### Offers: partial payment, repeat-failer memory, UPI preference

Three deterministic post-policy overrides in `app/offers.py` (`docs/offers.md`): a large insufficient-funds or limit-exceeded failure whose first link expired unpaid gets a second link that accepts a partial payment (`accept_partial`, minimum half the amount, at least Rs 100), recorded as recovered for the amount paid; a customer with three balance failures in sixty days at the same merchant gets the partial offer on the first link, three hard declines park for a person, and any risk block in their history parks everything; a dead card's change-method link prefers UPI. The Payment Link fields these rely on are assumptions with an audited fallback to a plain link.

### Recording the model path

The repository ships with no recorded model call. `LLM_CASSETTE_MODE=record` with a key records every call to a cassette; `replay` serves it back with the network client replaced by one that raises; `python scripts/eval_llm.py` then scores the model on the unmapped cases offline. Until someone records one, that script says so and writes no percentage (`docs/llm.md`, section 11).

## Idempotency, or why a redelivery cannot send twice

Double-charging is the catastrophic failure of any retry system, so the guard is layered, and the layers are in `app/executor.py` and `app/models.py`.

**The key.** `idempotency_key(razorpay_payment_id, retry_seq) = sha256(f"{razorpay_payment_id}:{retry_seq}").hexdigest()`. An event delivery is always "the first recovery for this payment" (`retry_seq = 1`), however many times it is delivered; only the scheduler and the expiry path pass a higher sequence. A reminder has its own key, `sha256(f"{payment_id}:{retry_seq}:reminder:{n}")`, under the same constraint.

**The constraint.** `recovery_jobs.idempotency_key` carries `UniqueConstraint("idempotency_key", name="uq_recovery_jobs_idempotency_key")`. The database, not application logic, is the arbiter.

**The check happens before every outbound call.** `schedule_job` INSERTs the job row under its key and commits *before* anything is sent. If the INSERT fails with `IntegrityError`, it rolls back, writes an audit row `skipped_duplicate: idempotency key already present` (with the existing job's id and status), commits, and returns the existing job with `created = False`. The pipeline then returns a summary with `duplicate: True` and executes nothing; the existing job is not touched. In `process_attempt` only a freshly created `pending` job that is due (or run with `--execute-now`) reaches `execute_job`, and `execute_job` re-reads the row first and refuses any job whose status is `sent`, `stubbed`, `executing`, `cancelled`, `human_queue` or `no_action` with zero client calls (audit: `already executed: status sent; nothing sent`). (`skipped_duplicate` is reported for a redelivery but never stored on a job row: `schedule_job` returns the existing job untouched.)

**The key is echoed to Razorpay.** The Payment Link is created with `reference_id = key[:40]` (Razorpay caps `reference_id` at 40 characters; 160 bits of the hash is still collision-proof here), and `notes` carry `payment_id` and `retry_seq`. If the API rejects a create with HTTP 400 whose description mentions the reference and "already exists" (or exist/unique/duplicate), the executor looks the link up by that `reference_id` (`GET /payment_links?reference_id=`, `list_payment_links` on every client), records its id and short URL on the job as `sent (already existed)` so `poll` and `mark-paid` can close it, and creates nothing; if the lookup cannot find it, the job is parked `human_queue` with the reason in `last_error` rather than guessed at.

**Duplicate delivery, step by step.** The same `payment.failed` event arrives twice (webhooks are at-least-once). Delivery 1: classify, decide, INSERT job under `sha256('pay_96hhPI3tuHgqH7:1')`, create one link, draft one nudge. Delivery 2: classify and decide again (deterministic, so an identical decision row lands in the trail), INSERT fails on the UNIQUE constraint, `skipped_duplicate` audited, nothing sent. One job row, one link, one create call. The CLI shows it:

```
$ python -m app.main process --attempt pay_96hhPI3tuHgqH7 --execute-now
pay_96hhPI3tuHgqH7 card   Rs 1,999.00  AUTH_ABANDONED (rules)  -> recovery_link  in 10m  job#7  skipped_duplicate (idempotency key already present; existing job is sent; nothing sent)
```

**Ordering under database failure.** The commit order is: decision row, then job row with its key, then `attempts_made` incremented and committed before *each* HTTP call, then status `sent` with the link id. Every write is wrapped in `db_guarded` (`app/pipeline.py`): a database error before the job row exists raises `PipelineDBError`, the CLI exits 2 with `the event was NOT acknowledged ... redeliver it`, and zero outbound calls were made. The source redelivers, the redelivery is processed once. If the process dies *after* the HTTP call went out but before `sent` was written, the committed `pending` row (with `attempts_made = 1` as evidence) is left alone by a redelivery (`skipped_duplicate`) and re-executed by `run-due` once it is due, where it meets Razorpay's duplicate-`reference_id` rejection: the existing link is looked up and recorded, no second link. If the HTTP *answer* is lost instead (a transport failure or 5xx after the request landed), the job fails after its bounded retries and is reconciled by the same lookup before any follow-up: found means `sent`, absent means the next attempt, unknown means a person.

**The same layering stops a recovery for an order paid another way.** The webhook (`payment.captured` / `order.paid`, or `payment_link.paid` for a sibling attempt) writes `order_paid_at` on every attempt sharing the `order_id`; `schedule_job` reads it before inserting, `execute_job` reads it again before the claim; a live link is cancelled at Razorpay and the job ends `cancelled`; a cancel that fails parks the job `human_queue` with the link named, never a silent live link; and with real keys `GET /orders/{id}/payments` is asked before every create as the last layer. The `order_paid_elsewhere` fault runs all four steps (`docs/money_path.md`, section 1).

**What is verified by tests and what is assumed.**

- Verified: `tests/test_idempotency.py` (key stability and uniqueness per payment and sequence; second schedule for the same event is `skipped_duplicate`; a raw duplicate INSERT is rejected by the constraint itself; executing an already-sent job makes zero client calls; other terminal statuses are refused; executing the same job twice creates one link; an API "reference_id already exists" answer maps to `sent` with the existing link recovered by lookup, or to `human_queue` when it cannot be, never to a second link; the duplicate-event flow end to end). Also `tests/test_pipeline.py::test_duplicate_delivery_is_skipped_and_makes_one_link`, `::test_db_unavailable_raises_and_creates_zero_links`, `::test_transport_failure_that_landed_is_reconciled_to_one_link`, `::test_ambiguous_failure_with_no_lookup_answer_parks_for_a_person` and `::test_same_request_failures_do_not_walk_the_backoff_chain`; `tests/test_money_path.py` for the paid-elsewhere stop, the expiry follow-up, the timing rules and the cap through `schedule_job`; `tests/test_cadence.py::test_reminder_scheduling_is_idempotent_on_redelivery`; and the `duplicate_event`, `db_unavailable` and `order_paid_elsewhere` faults in `docs/failure_report.md`.
- Assumed: that Razorpay rejects a second Payment Link with the same `reference_id` with a 400, and the wording of that rejection; that `GET /payment_links?reference_id=<ref>` lists the links under a reference as `{"payment_links": [...]}`; that `POST /payment_links/{id}/cancel` answers the entity with `status: "cancelled"`; and that `GET /orders/{id}/payments` answers `{"items": [...]}` with a `status` per payment. The fixture implements all four as stated in its docstring; the tests of those behaviours test the fixture, not Razorpay. If the real API accepted duplicate `reference_id`s, the worst case in the crash-after-send path would be a redundant link, never a second charge: a Payment Link only collects money when a customer chooses to pay it, and the DB constraint remains the primary guard for the redelivery case. If the lookup answered differently, the reconcile step reports `unknown` and parks the payment for a person; if the cancel answered differently, the job is parked with the link named.
- Partly covered: two processes executing the same `pending` job at the same instant. `execute_job` claims the row with an atomic compare-and-set (`UPDATE ... WHERE status = 'pending'` -> `executing`) before any outbound call, so the second worker sees `executing` and refuses (`tests/test_idempotency.py::test_atomic_claim_refuses_a_second_worker_on_one_pending_job`); a job left `executing` by a crash is reclaimed and reconciled by `run-due`. In a true multi-worker deployment this still wants a lease with a timeout (part of the queue migration in D6); the `reference_id` guard remains the backstop.

## Failure recovery

Every fault in `app/faults.py` is injected at the client boundary (LLM client, Razorpay client, database engine, event source), never inside business logic, so the fallback exercised is the one production would take. `make chaos` (or `python scripts/chaos.py --fault <name>`) runs a real event through the real pipeline on a throwaway SQLite file, prints inject, path (from the audit rows actually written), fallback and final state, asserts the invariant, and exits non-zero on any failure. `docs/failure_report.md` is its output, reproducible byte for byte.

| fault | path taken | fallback | final state |
|---|---|---|---|
| `llm_timeout` | ingest -> classify (UNKNOWN) -> llm (1 call, TimeoutError) -> policy -> schedule -> outcome | `llm_timeout->human_queue`; for the nudge on a rules-classified link, `llm_timeout->template` | human_queue, 0 links; the link event is still sent, message from template |
| `llm_bad_json` | same, with 2 model calls (attempt + one repair, both truncated JSON) | `llm_bad_json->human_queue`; nudge `llm_bad_json->template` | human_queue, 0 links; nudge from template |
| `llm_hallucinated_class` | same, 2 calls; `CUSTOMER_MOVED_ABROAD` at confidence 0.97 fails the closed enum | `llm_hallucinated_class->human_queue` (rejected, never coerced to the nearest class) | human_queue, 0 links |
| `razorpay_429` | ingest -> classify -> policy -> schedule -> execute x3 -> nudge | backoff 0.5s then 1.0s on two 429s, success on attempt 3 of 3 | sent, `attempts_made = 3`, exactly 1 link, audit rows name `fault:razorpay_429` |
| `razorpay_5xx` | ... -> execute x4 -> reconcile -> policy -> schedule -> execute x3 -> reconcile -> policy -> schedule -> execute x3 -> reconcile -> policy -> schedule -> outcome | 3 HTTP attempts per job, job `failed` with the 502 kept (`server_error`); each failure reconciled by `reference_id` (no link found) before the next key is minted; follow-ups at 15m x3 (seq 2 at +45m, seq 3 at +3h) until `max_attempts`, then `no_action` | seq 1-3 failed, seq 4 no_action by the stop rule; 9 HTTP attempts; 0 links; outcome not recovered |
| `duplicate_event` | delivery 1 full path; delivery 2: classify -> policy -> schedule (`skipped_duplicate`) | INSERT under the UNIQUE key fails; existing job untouched | 1 job row, 1 link, 2 identical decision rows |
| `unknown_error_code` | ingest -> classify (UNKNOWN) -> llm (no client) -> policy -> schedule -> outcome | `llm_unavailable->human_queue`; `llm_used = False`, nothing billed | human_queue, 0 Razorpay calls, 0 model calls |
| `db_unavailable` | classify and policy computed in memory; decision commit REFUSED -> `PipelineDBError`; then two probes: a lost `sent` write (pending job re-run) and a lost HTTP answer (execute x4 -> reconcile -> nudge) | event not acknowledged, exit 2; redelivery after recovery processed once; both lost-state probes resolve by `reference_id` lookup to the one existing link, never a second | 0 rows written, 0 calls during the outage; 1 link after redelivery; 1 link and 1 job per payment in both probes |
| `order_paid_elsewhere` | four steps: `payment.captured` between decision and execution; the pre-send order check (`CHECK_ORDER_BEFORE_SEND=1`); a capture after the link was sent; a capture whose cancel call answers 502 | pending job voided `no_action` + outcome, `run-due` executes nothing, a stale `payment.failed` for the order is refused at schedule; one `GET /orders/{id}/payments`, no create; one cancel call, job `cancelled`; cancel refused -> `human_queue` naming the live link | 0 links before; 1 cancel after; a redelivered capture makes no second cancel and no second outcome |

The report also states what it does not cover: the live HTTP paths themselves (faults are injected one layer below them), two processes racing on one job, and the Razorpay assumptions the probes rest on (duplicate `reference_id` rejection, the lookup by `reference_id`, the cancel and order-payments shapes).

`PRA_FAULTS=<name>` injects a fault into any CLI command, not only the harness: `PRA_FAULTS=db_unavailable python -m app.main process --all` exits 2 having acknowledged nothing; `PRA_FAULTS=duplicate_event` delivers every processed event a second time (the second pass prints `skipped_duplicate` for each); `PRA_FAULTS=unknown_error_code` forces the LLM off; `PRA_FAULTS=order_paid_elsewhere` pays every order that just got a link so every link is cancelled again; the `llm_*` and `razorpay_*` faults wrap their clients. The `demo` header names the active faults and what each does.

## Baseline vs agent

> Measured on 500 synthetic failure events with a hand-specified recovery model. This is a simulation, not production data, and the absolute numbers depend on assumptions stated in `scripts/simulate.py`. The comparison is meaningful; the absolute recovery rate is not.

`make simulate` runs two policies over the same 500 deliveries (480 unique payments, 20 duplicate deliveries, 71 with a saved token; seed 42) with the same coin flips, so the comparison is paired. Baseline: retry everything once at T+1h (token charge if a token exists, else a link), no stop rules, no human queue, no idempotency. Agent: `app.policy.decide` over the ground-truth class, with the executor's idempotency key. Full table, model, definitions and "where the simulation is wrong" are in `docs/simulation_report.md`.

| metric | baseline | agent | delta |
|---|---:|---:|---:|
| recovered (of 480 payments) | 75 | 157 | +82 |
| recovery rate | 15.6% | 32.7% | +17.1 pp |
| amount recovered | Rs 4,18,442.00 | Rs 9,62,003.00 | +Rs 5,43,561.00 |
| attempts made | 500 | 439 | -61 |
| attempts per recovery | 6.67 | 2.80 | -3.87 |
| wasted attempts | 146 | 0 | -146 |
| risk-block violations | 25 | 0 | -25 |
| duplicate charges | 0 | 0 | 0 |
| duplicate links | 17 | 0 | -17 |
| duplicates rejected by the idempotency key | 0 | 20 | +20 |
| human-queued | 0 | 48 | +48 |

Sensitivity over seeds 1 to 5: the recovered-count delta stays between +76 and +92, never negative. With seed 42 the baseline's 20 duplicate deliveries happened to produce 17 duplicate links and no double charge; other seeds do produce baseline double charges on token retries, the agent produces none in any seed. The recovery probabilities are hand-picked; the *ordering* (48h beats 1h for insufficient funds, minutes beat an hour for abandonment and outages) is the claim, the magnitudes are not. Classification is assumed perfect in the simulation, and the 48 human-queued payments recover nothing inside it. The simulation never calls `schedule_job`, so the salary window, quiet hours, contact cap, reminders and the paid-elsewhere stop are not modelled and its numbers are unchanged by them.

## Insights: learning that a person reads

`python -m app.main insights [--write PATH] [--min-samples N] [--merchant ID]` (`make insights`, `GET /insights`) reads the audit tables back: per class, the recovery rate among sent links with a 95% Wilson interval and the average time to recovery; the same rate per scheduled-delay bucket with the policy's bucket marked; LLM usage (decisions consulted, every `fallback_taken` by count, median and p95 latency); money at risk, recovered, open and parked per merchant; and one verdict per class. A change of delay is *proposed* only when the policy bucket and a competing bucket both have `n >= 30` and their intervals do not overlap; everything else says `insufficient evidence (n=..)`, which is what the 22-event demo database says for every class (`tests/test_insights.py::test_demo_database_is_honest_about_small_n`). Nothing is applied: `app/policy.py` is a code-reviewed file and the report is evidence for the reviewer, not a bandit. `python -m app.main propose-rules [--write PATH] [--min-occurrences N]` (`GET /rule-candidates`) groups model-classified and fallback decisions by a normalised description signature and prints a ready-to-paste `Rule(...)` stub for each group that recurs with an agreed class at mean confidence >= 0.85, or "needs a human label" when only fallbacks recur; `app/classify.py` is never modified (a test checks its hash). `docs/insights.md`.

## Per-merchant overrides, the value-ordered queue, and /metrics

A file `merchants/<merchant_id>.json` (plus an optional `_default.json` every merchant inherits) can change a class's `delay_seconds`, `max_attempts` (0..5) and `nudge`, route a class to `human_queue`, set `nudge_language`, lower `max_contacts_per_week`, turn `notify_customer` off, disable `token_retry` or a link action, or set `human_queue_all` as a kill switch. A file can only make the agent more conservative: `action` may only move to `human_queue`, RISK_BLOCKED and UNKNOWN cannot be unparked, `notify_customer: true` still needs the global flag, and the confidence, reachability and stop-rule gates run first (`app/policy.py`, `effective_entry`). Unknown keys and out-of-range values are rejected at startup with the file and field named, exit 1, never a silent fallback to the defaults. The overrides reach `policy.decide`, the nudge language, the contact cap and the link's `notify` flags (`app/pipeline.py`, `app/scheduling.py`, `app/executor.py`; `tests/test_money_path.py::test_merchant_override_changes_the_delay_and_nudge_language`); with no files the decision path is byte-identical to the table (`tests/test_merchants.py`). `python -m app.main policy --merchant ID` and `GET /policy?merchant=ID` show the effective table with an `override` column.

`queue [--limit N]`, `show --by-value`, the `process --all` and `import-csv --process` batch lines and `/human-queue` order attempts by expected recovery, `amount x P(recover | class, action)`. `P` is the hand-specified simulation prior from `scripts/simulate.py` until a class has `n >= 30` sent links in this database, after which the measured rate is used; every estimate names its source (`[simulation prior, recovery_link@48h]`) and the batch total is labelled an estimate, not money in the bank. `GET /metrics` is Prometheus text exposition computed from the database on every scrape (attempts by class, jobs by status, links created, recovered count and paise, human-queue size and expected paise, decisions by classification source, fallbacks by name, open links, merchant files loaded). `docs/merchants.md`.

## Audit trail

Five tables in `app/models.py`, and together they are the trail:

- `payment_attempts`: the event as Razorpay reports it (`error_code`, `error_source`, `error_step`, `error_reason`, `error_description`, `has_token`, method, amount, customer fields, `customer_language`), `order_paid_at` and `paid_by_payment_id` once the order is paid another way, and `subscription_id`, `subscription_status`, `subscription_url` for a mandate charge.
- `recovery_decisions`: `failure_class`, `action`, `delay_seconds`, `max_attempts`, `reason` (classification reason plus policy rationale), `classified_by` (rules / llm / fallback), `llm_used`, `llm_model`, `llm_latency_ms`, `confidence`, `fallback_taken`.
- `recovery_jobs`: `retry_seq`, `scheduled_at`, `schedule_note` (why the send time is not `now + delay`), `idempotency_key` (UNIQUE), `status`, `razorpay_link_id`, `razorpay_link_url`, `link_source` (`payment_link` or `subscription_url`), `parent_job_id` (a reminder's link job), `attempts_made`, `last_error` (for a failed link job `<kind>: <detail>`, kind one of `local_guard`, `final_4xx`, `rate_limited`, `server_error`, `transport`, `client_error`; the pipeline routes on it; `order already paid by <id>` and `contact cap: ...` for parked jobs), the drafted nudge (`nudge_channel`, `nudge_subject`, `nudge_body`, `nudge_source`), `executed_at`. Reminder rows are hidden from plain job queries unless a statement asks for them (`models.with_reminders`), so `show`, the operator view and insights read the link, not its reminder.
- `outcomes`: `recovered`, `recovered_at`, `amount_recovered_paise`, `note` (`payment_link.paid via poll (...)`, `payment link expired (...)`, `payment link cancelled (...)`, `order paid elsewhere: no recovery needed`, `human_queue: ...`, `no_action: ...`, `token_retry stubbed: ...`, `resolved by a person: ...`). Every job that cannot recover anything gets one, so nothing stays open forever; only a sent link waits for `poll` or a webhook.
- `audit_log`: append-only, at least one row per stage per attempt with a JSON payload. Stages: `ingest`, `classify`, `llm`, `policy`, `schedule`, `execute`, `reconcile`, `nudge`, `deliver`, `cancel`, `poll`, `outcome`.

```
$ python -m app.main audit --attempt 1 --no-data
audit trail for attempt 1 (pay_LrfTLGu5EgYUPo, card, Rs 2,499.00): 13 rows
2026-09-05 15:21:08  ingest    failed payment event received
2026-09-05 15:21:08  classify  INSUFFICIENT_FUNDS (rules): rule insufficient_funds: reason mentions insufficient funds or the description says insufficient, not enough funds, low balance, balance too low, credit limit, or the Hinglish paryapt/aparyapt
2026-09-05 15:21:08  policy    recovery_link in 48h (max 2 attempts, backoff x1, nudge=yes): Balance replenishes on salary cycles; a 10-minute retry burns an issuer attempt for nothing, so send a link and ask again in 48 hours.
2026-09-05 15:21:08  schedule  scheduled recovery_link (seq 1) for 2026-09-07T15:21:08.436414
2026-09-05 15:21:08  execute   execute_now: running ahead of schedule (was due 2026-09-07T15:21:08.436414)
2026-09-05 15:21:08  execute   sent: payment link plink_TBJfbhCOZ3wnwH created (https://rzp.io/i/wZMGIy1X)
2026-09-05 15:21:08  nudge     nudge drafted via template for sms; NOT sent: no SMS/email provider in scope (fallback: llm_unavailable->template)
2026-09-05 15:21:08  deliver   not delivered: RAZORPAY_NOTIFY_CUSTOMER is off; message drafted and stored only
2026-09-05 15:21:08  schedule  reminder 1 of link plink_TBJfbhCOZ3wnwH scheduled for 2026-09-06T15:21:08.436414 (+24h after send)
2026-09-05 15:21:08  schedule  reminder 2 of link plink_TBJfbhCOZ3wnwH scheduled for 2026-09-08T03:30:00 (+60h after send; quiet hours: 8 Sep 09:00 IST)
2026-09-05 15:21:08  outcome   recovered: payment_link.paid via poll (plink_TBJfbhCOZ3wnwH)
2026-09-05 15:21:08  schedule  no_action: reminder job#2 (reminder 1: +24h after send) voided before execution, link plink_TBJfbhCOZ3wnwH paid; nothing will be sent
2026-09-05 15:21:08  schedule  no_action: reminder job#3 (reminder 2: +60h after send; quiet hours: 8 Sep 09:00 IST) voided before execution, link plink_TBJfbhCOZ3wnwH paid; nothing will be sent
```

Without `--no-data` each row is followed by its JSON payload (idempotency key, HTTP status and backoff on a retry, the client that answered, the nudge body, the delivery receipt). The `execute_now` row is the demo being honest: it runs a 48-hour job immediately because a demo cannot wait 48 hours, and the trail says so; the two reminder rows show the cadence being scheduled and then voided the moment the link was paid. Commands: `seed`, `process [--all | --attempt ID] [--execute-now]`, `run-due [--now ISO] [--loop [--interval SECONDS]]` (replay the scheduler at a pretend time, or run it as a daemon), `poll`, `show [--attempt ID] [--by-value]`, `audit --attempt ID [--no-data]`, `mark-paid --job ID`, `demo`, `ingest`, `import-csv`, `serve-webhook`, `resolve`, `insights`, `propose-rules`, `queue [--limit N]`, `policy [--merchant ID]`. Exit codes: 0 ok; 2 the database refused a write (the event was not acknowledged, redeliver it); 1 anything else, including a rejected merchant file.

## Running it as a product

`pip install -e .` (or `make install`) gives a `pra` command; `docs/ops.md` is the runbook.

| command | what it does |
|---|---|
| `pra init [--yes] [--mode live\|shadow]` | guided setup: refuses non-test keys, verifies a test pair with one read-only call, generates the webhook secret, writes `.env`, prints the dashboard webhook steps, runs doctor |
| `pra doctor` | twelve PASS/WARN/FAIL checks, including a synthetic `payment.failed` through the real pipeline on an in-memory database |
| `pra serve` | the webhook receiver, the scheduler loop and the operator view in one process, with human-queue alerts to `ALERT_WEBHOOK_URL` |
| `PRA_MODE=shadow` + `pra plan` | shadow mode: every stage runs up to the atomic claim, nothing leaves the process, jobs end `shadow` with the would-be payload in the trail; `plan` totals what would have gone out and the expected recovery. This is how a merchant sees the agent's plan for a batch before switching it on |
| `pra digest [--since 24h] [--post]` | a period summary (events, links, reminders, recovered, expired, cancelled, queue, model usage), optionally posted to a Slack-compatible webhook |

A `Dockerfile` and a Postgres job in CI exist and are written correctly to the best of this build's knowledge, but neither was executed here (no Docker, no Postgres in the build environment); the README says so rather than claiming them.

## Operator view

`make serve` (or `python -m app.web --port 8000`) serves a read-only view over the same SQLite database the CLI writes: the demo's honesty header, totals, the `show` table with clickable payment links, a per-attempt page mirroring `audit --attempt`, the human queue in expected-value order (`/human-queue`), the insights and rule-candidate reports (`/insights`, `/rule-candidates`), the policy table with a merchant's overrides (`/policy?merchant=ID`), the two generated reports (`/reports/failure`, `/reports/simulation`), `/metrics` and `/health`. Plain HTML, no JavaScript, no external assets, nothing is written (POST is refused). It has no authentication and shows customer contact details, so it binds to `127.0.0.1` unless `--host` is passed explicitly. Resolution of queued attempts stays on the command line.

**Actions from the browser.** With `OPERATOR_TOKEN` set, the attempt page, the human queue and the insights page gain forms for resolve, cancel link, resend notification, retry now and apply proposal. The token is typed by the operator (never embedded in a page), every POST goes through a confirmation page rather than JavaScript so the content-security policy stays `default-src 'none'`, and every action lands in the audit trail under stage `operator` with the actor. Without a token every POST is refused with 403 and the pages stay read-only. `pra apply-proposal --proposal ID --actor NAME` and `pra rollback-override` do the same from the command line; a proposal that would make the agent less conservative is accepted only with n >= 30 on both delay buckets and the evidence is stored in the merchant file's `_history`. `docs/web.md`.

## How this compares

`docs/landscape.md` surveys twelve products (Stripe Smart Retries and Adaptive Acceptance, Chargebee, Recurly, Churn Buster, Gravy, Butter, Paddle Retain, Baremetrics Recover, Razorpay's Agent Studio recovery agent and its platform features, Juspay Hyperswitch, Cashfree, PayU) from their public pages, with every claim that rests on a search snippet marked *reported*, and lists what they have that this agent does not. Since that survey, three of its gaps have been closed here in the deterministic way it proposed: delivery with receipts through Razorpay's own `notify` and `notify_by` endpoints, a per-class reminder cadence on the same link, subscription webhooks in, and per-merchant configuration with a recovered-revenue report. Still open, and stated as such: the real token or mandate re-charge behind the stub, subscription pause or cancel at the end of dunning, in-session re-routing (which happens inside the gateway before a `payment.failed` exists), cross-merchant network signals (only the gateway has them), and ML-timed retries, which this agent deliberately replaces with a table of timing rules and a report a person reads. What no vendor in that survey exposes and this agent does: the policy table as a readable file, named classification rules with a pinned regression floor, and a chaos harness a merchant can run.

## Scope, scale and what is next

**Cut, deliberately.** Rail-switch logic (offering UPI when a card is dead: the change-method link asks the customer, the agent does not choose a rail). Real token retries (stubbed, see above). A delivery provider of our own for the drafted wording: what the customer receives is Razorpay's own link message, and only when notifications are on. A real queue (the scheduler is a poll over `recovery_jobs`). Migrations (`db.init_db` is `create_all`; there is no Alembic). Live validation of every Razorpay assumption (`serve-webhook` exists and is tested with local requests; the cancel, order-payments, `notify_by`, subscription and expired-link shapes are implemented as stated in the fixture and the docs, not observed). Postgres is optional via `docker-compose.yml` and `psycopg2-binary` is deliberately not pinned. The Razorpay SDK was left out on purpose: it collapses every non-2xx into an exception carrying only a message, and the backoff policy needs the HTTP status (429 and 5xx retry, other 4xx are final), so `app/razorpay_client.py` is a thin `urllib` client.

**What breaks at 10,000 merchants.** The scheduler. One process scans one table for due rows (links and reminders) and executes them in order; no lease, no worker pool, no partitioning. The migration is a queue (SQS, Kafka, or a Postgres `SKIP LOCKED` worker pool) fed by the same `schedule_job` rows, and it is safe to make because the job row and its `reference_id` exist before any call: two workers on one job are refused by the status re-read or, if they race past it, by Razorpay's duplicate-`reference_id` rejection (assumed, not verified; a lease on `execute_job` is part of the migration). SQLite moves to Postgres via `DATABASE_URL`. `poll` becomes the `payment_link.paid` / `payment_link.expired` / `payment.captured` webhooks alone (`ingest` already handles them). `/metrics` recomputes every series from the database per scrape, and the merchant registry is loaded once per process from a directory: both are fine for tens of merchants and become a table with a cache and a materialised counter set at thousands. A redelivered UNKNOWN event currently calls the LLM again before the key rejects it: not a money action, but a second billable, non-deterministic call per duplicate.

**Next.** Pre-emptive subscription recovery (a card or mandate expiring before the next charge) was scoped in the last round and cut for time, cleanly; it is the first thing to add. `serve-webhook` behind a public URL receiving real test-mode deliveries, and every payload shape above pinned from what arrives; real token retries through the recurring-payments API behind the same `execute_job` guard, with a mandate check before any debit; `subscription.activated` in and pause/cancel out at the stop rule; token counts persisted next to `llm_latency_ms` so the cache hit rate and cost are one query away; calibration of the LLM's confidence against human-queue verdicts before trusting the 0.7 gate in production; and, once `outcomes` has thirty sent links per class, the first `insights` proposal a reviewer can act on.

## Honest-claims checklist

- [x] No recovery-rate percentage presented as real; simulation labelled as simulation. The only rates in this README are in the "Baseline vs agent" section, under the caveat, from `docs/simulation_report.md`; the `insights` rates carry their sample size and interval, and on the demo database every one says insufficient evidence.
- [x] The expected-value numbers in `queue`, `show --by-value` and the batch lines are a simulation prior until a class has `n >= 30` sent links, and every estimate names its source.
- [x] No latency or cost figure you didn't measure. The README quotes none. ARCHITECTURE's panel-prep answer gives list price and a bound derived from the configuration, both labelled as not measured, and one measured figure (rules at about 26 microseconds per event on the build machine, labelled a one-off); `llm_latency_ms` is stored per decision and token counts are logged per call so the real numbers can be.
- [x] No claim of production deployment or users. There are none.
- [x] Token-retry path clearly marked as stubbed, with the reason (no real tokens in test mode; recurring-charge API not verifiable offline).
- [x] Any recorded-fixture fallback disclosed: `FixtureRazorpayClient` (in-memory Payment Links, cancel, order payments and `notify_by`, with its assumptions listed in its docstring), the fixture re-creating links from job rows in a new process (`[fixture] restored N in-memory links ...`), the fixture simulating customer payments in the demo, and template nudges without a key. Neither live path was run against the real API during the build.
- [x] The Razorpay API shapes this build adds are assumptions, listed per document: cancel, order payments, `payment.captured`, `order.paid` and `payment_link.expired` in `docs/money_path.md` section 5; `notify_by`, creation-time notification receipts and the `subscription.*` envelopes in `docs/delivery.md`; the failed-payment fields, `token_id`, `account_id` and the CSV headers in `docs/ingest.md`. Each costs at most a parked job or a failed reminder if wrong, never a second link or a charge.
- [x] The classifier's 100% on its eval set is labelled as a regression floor on the tuning set, not accuracy on production traffic; the receiver and the CSV mapping are labelled as untested against live Razorpay deliveries.
- [x] The "delivered" rows mean Razorpay accepted a request to send its own link message; the agent's drafted wording is never what the customer reads, and with notifications off (the default) nothing is delivered and the trail says so.
- [x] Shadow mode, partial-payment offers and the UPI checkout preference rely on Payment Link fields and behaviours that are assumptions (`docs/offers.md`, section 6), each with an audited fallback to a plain link.
- [x] The Dockerfile and the Postgres CI job were written, not executed, in this build environment.
- [x] No model call was recorded: `scripts/eval_llm.py` reports "not recorded" and quotes no model accuracy until a cassette exists.
- [x] Git history audited for secrets. The working tree was scanned for `rzp_test_`/`rzp_live_`/`sk-ant-`-shaped strings and non-empty key assignments; the only hits are the placeholder `sk-ant-` dummies that `tests/test_llm.py` and `scripts/chaos.py` monkeypatch into config (not keys) and the grep command below in this README; no real key exists; `.env` is gitignored and only `.env.example` is tracked. Re-run on the final commit before the repo goes public: `git log -p -- . | grep -nE 'rzp_(test|live)_[A-Za-z0-9]{6,}|sk-ant-'`.

## Repo layout

```
payment-recovery-agent/
├── README.md
├── ARCHITECTURE.md              eleven decisions, each with the alternatives rejected
├── docs/
│   ├── failure_report.md        output of `make chaos` (nine faults, all PASS)
│   ├── simulation_report.md     output of `make simulate` (baseline vs agent, seed 42)
│   ├── classifier_eval.md       output of `make eval` (118 labelled cases)
│   ├── ingest.md                webhook fields, signature check, CSV mapping, worked example
│   ├── money_path.md            stop on paid-elsewhere, expiry follow-ups, salary window / quiet hours / contact cap
│   ├── delivery.md              Razorpay-native delivery and receipts, reminder cadence, subscription webhooks
│   ├── insights.md              recovery rates with intervals, proposals that never auto-apply, rule candidates
│   ├── merchants.md             per-merchant overrides, expected-value queue, /metrics
│   ├── llm.md                   the two call sites: cached prompt, few-shot, usage, languages, every fallback
│   ├── landscape.md             twelve recovery products and the gap list
│   ├── ops.md                   the runbook: install, init, doctor, serve, shadow mode, digest, Docker, Postgres CI
│   ├── web.md                   operator actions, the token model, the proposal-apply evidence rule
│   ├── offers.md                partial payment, repeat-failer memory, UPI preference, shadow mode
│   └── examples/                payment.failed (card, UPI), payment_link.paid/expired, payment.captured,
│                                subscription.pending/halted payloads, a dashboard-style CSV
├── merchants/
│   └── _example.json            documentation only (skipped); copy to <merchant_id>.json
├── .env.example                 the only env file tracked; keys blank by default; every knob commented
├── docker-compose.yml           optional Postgres (SQLite is the default)
├── Makefile                     setup / demo / chaos / simulate / eval / insights / serve / test / clean
├── requirements.txt             pinned
├── pyproject.toml               the `pra` console script; pip install -e .
├── Dockerfile                   python:3.11-slim, non-root, pra serve (written, not built here)
├── pytest.ini
├── .github/workflows/ci.yml     pytest, demo, chaos, simulate, all without keys
├── app/
│   ├── taxonomy.py              FailureClass, Action (incl. reminder), JobStatus (incl. cancelled), Classification, PolicyDecision, Nudge
│   ├── models.py                the five tables, audit(), the reminder-hiding loader criterion and with_reminders()
│   ├── db.py                    engine, session, init_db (create_all, no migrations)
│   ├── config.py                env; refuses non-test Razorpay keys; LLM_MIN_CONFIDENCE = 0.7; notify, order-check, timing, reminder knobs
│   ├── clock.py                 utcnow(), naive UTC everywhere
│   ├── faults.py                the nine fault names
│   ├── classify.py              13 ordered deterministic rules, UNKNOWN by fall-through
│   ├── policy.py                the table, decide(), delay_for(), stop rules, effective_entry() for merchant overrides
│   ├── merchants.py             merchants/<id>.json loader, validation, effective_table(), header line
│   ├── llm.py                   classify_unmapped, draft_nudge; cached system prompts, few-shot, usage_log; clients
│   ├── nudge_templates.py       per-class templates in en / hi / hinglish, SMS budgets, format_rupees
│   ├── razorpay_client.py       LiveRazorpayClient (urllib), FixtureRazorpayClient, FaultingRazorpayClient
│   ├── executor.py              idempotency key, schedule_job, execute_job, reconcile_failed_job, order-paid guards, record_outcome
│   ├── scheduling.py            salary window, quiet hours (IST), contact cap
│   ├── cadence.py               deliver receipts, reminder jobs and their execution, subscription override
│   ├── priority.py              expected recovery: simulation prior, empirical rate at n >= 30
│   ├── pipeline.py              process_attempt: the stages in order; finish_job; expiry follow-ups; poll_outcomes
│   ├── scheduler.py             due_jobs, run_due (the poll loop that a queue replaces; reminders included)
│   ├── insights.py              per-class and per-delay rates with Wilson intervals, LLM usage, money, proposals
│   ├── rule_candidates.py       recurring unmapped descriptions -> Rule(...) stubs for a person
│   ├── ingest.py                payment.failed / captured, order.paid, payment_link.paid / expired, subscription.*, signature, CSV
│   ├── web.py                   read-only operator view (stdlib http.server), /insights, /rule-candidates, /policy, /metrics
│   ├── main.py                  argparse CLI (the commands listed under "Audit trail", plus init/doctor/serve/digest/plan/apply-proposal)
│   ├── ops.py                   pra init, doctor, serve, digest, alerts
│   ├── actions.py               operator actions (resolve, cancel, resend, retry, apply proposal), audited
│   ├── offers.py                partial-payment offer, repeat-failer memory, UPI preference; shadow helpers
│   ├── llm_cassette.py          record / replay of model calls
│   └── data/                    packaged copies of the labelled cases and the merchants example
├── scripts/
│   ├── seed.py                  22 events across all eight classes, with ground truth
│   ├── chaos.py                 the nine faults, --fault / --all / --write
│   ├── simulate.py              500 synthetic events, baseline vs agent; RECOVERY_MODEL (the prioritisation prior)
│   ├── eval_classify.py         score the rules over tests/fixtures/failure_cases.json
│   └── eval_llm.py              score the model path from a recorded cassette (ships as "not recorded")
└── tests/
    ├── conftest.py              throwaway SQLite per process, all external services off
    ├── test_idempotency.py      if only one test file existed, it would be this one
    ├── test_classify.py  test_policy.py  test_executor.py  test_razorpay_client.py
    ├── test_llm.py  test_nudge_templates.py  test_pipeline.py  test_scheduler.py  test_seed.py
    ├── test_chaos.py  test_simulate.py  test_eval_classify.py
    ├── test_ingest.py  test_web.py  test_money_path.py  test_scheduling.py  test_cadence.py
    ├── test_insights.py  test_rule_candidates.py  test_merchants.py  test_priority.py
    ├── test_ops.py  test_actions.py  test_offers.py  test_llm_cassette.py
    ├── fixtures/failure_cases.json  118 labelled failure cases (also the few-shot source)
    └── fixtures/llm_cassettes/synthetic.jsonl  a scripted cassette for tests, labelled synthetic
```

MIT licence.
