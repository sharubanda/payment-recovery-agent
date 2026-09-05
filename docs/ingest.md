# Ingesting real Razorpay inputs

Until now events came from `scripts/seed.py`. `app/ingest.py` adds three ways in from Razorpay itself and one way to close the loop, all landing on the same `PaymentAttempt` row and the same `pipeline.process_attempt`, so every guarantee in the README (rules first, policy table, idempotency key before any call, exit 2 on a refused write) applies unchanged.

| entry point | command | what it takes |
|---|---|---|
| a webhook payload from a file | `ingest FILE.json [--process] [--execute-now] [--signature HEX]` | one `payment.failed` or `payment_link.paid` envelope, or the bare `GET /payments/{id}` entity |
| the dashboard export | `import-csv FILE.csv [--process] [--execute-now]` | the payments CSV from Dashboard -> Transactions -> Payments -> Download; only `status = failed` rows |
| a live receiver | `serve-webhook [--port 8080] [--process] [--execute-now]` | `POST /razorpay/webhook` with `X-Razorpay-Signature`; `GET /health` |
| a person's decision | `resolve --attempt ID (--recovered PAISE \| --closed) [--note TEXT] [--force]` | closes a human-queued attempt |
| the scheduler as a daemon | `run-due --loop [--interval SECONDS]` | one sweep per interval, a fresh session each, until Ctrl-C |

Examples to try are in `docs/examples/`: `payment_failed_webhook.json` (card, insufficient funds, Rs 2,499.00, no `token_id`), `payment_failed_upi_webhook.json` (UPI collect expired), `payment_link_paid_webhook.json` (the paid event for the first one's link) and `failed_payments_export.csv` (12 rows: 9 importable, 2 non-failed, 1 with no amount).

## The webhook payload fields used

`payment.failed` arrives as `{"entity": "event", "account_id", "event", "contains": ["payment"], "payload": {"payment": {"entity": {...}}}, "created_at"}`. The bare entity (what `GET /payments/{id}` returns, or the same dict without the envelope) is accepted too. From the entity, `parse_payment_failed` reads:

| attempt column | from | notes |
|---|---|---|
| `razorpay_payment_id` | `id` | must start with `pay_` |
| `amount_paise` | `amount` | must be a positive integer (Razorpay sends paise); a float, a string of letters or 0 is refused |
| `currency` | `currency` | default `INR` |
| `method` | `method` | `card`, `upi`, `netbanking`, `wallet`, `emandate`; `unknown` when absent |
| `order_id` | `order_id` | empty string when absent |
| `error_code`, `error_description`, `error_source`, `error_step`, `error_reason` | the flat `error_*` fields on the entity | verbatim; this is what `app/classify.py` reads |
| `has_token` | `bool(token_id)` | a saved card or an active mandate exists; only then can the table choose `token_retry` |
| `merchant_id` | `account_id` on the envelope, else `notes.merchant_id`, else `merchant_default` | |
| `customer_name` | `notes.name`, else `notes.customer_name`, else `card.name` | else `None` |
| `customer_contact` | `contact` | |
| `customer_email` | `email` | `void@razorpay.com` (Razorpay's placeholder when no email was collected) becomes `None`, so the link is not "sent" to nobody |
| `failed_at` | the entity's `created_at` (unix seconds, UTC), else the envelope's, else now | |

Validation failures raise `IngestError` with the field and value in the message (`ingest` exits 1, the receiver answers 400). `status` must be `failed` when present; a captured payment is refused rather than "recovered". `notes` may be `{}` or `[]` (Razorpay sends an empty list for empty notes) and both are handled.

`payment_link.paid` arrives with `payload.payment_link.entity` (`id`, `reference_id`, `status`, `amount`, `amount_paid`) and `payload.payment.entity` (the payment that paid it). `parse_payment_link_paid` returns `{link_id, reference_id, amount_paid_paise, payment_id, paid_at}`.

## The signature check

Razorpay signs the raw request body with HMAC-SHA256 under the webhook secret you typed when creating the webhook, and sends the hex digest in `X-Razorpay-Signature`. `ingest.verify_signature(body, signature, secret)` recomputes it over the exact bytes received and compares with `hmac.compare_digest`. Put the secret in `.env` as `RAZORPAY_WEBHOOK_SECRET`.

- `serve-webhook`: when the secret is set, a missing or wrong signature is 401 and nothing is read from the body. When it is unset the receiver accepts unsigned POSTs and warns on stderr at startup: fine on localhost, not behind a public URL.
- `ingest FILE --signature HEX`: verifies the file's bytes against the given signature (exit 1 on mismatch, nothing ingested). A file has no headers, so with the secret set and no `--signature` the command warns on stderr and continues unverified.
- `X-Razorpay-Event-Id`, when present, is stored in the `ingest` audit row's JSON (`event_id`). Dedup itself keys on the payment id, which the `payment_attempts.razorpay_payment_id` UNIQUE constraint already guards.

## Redelivery

Webhooks are at-least-once. A `payment.failed` whose payment id already has a row is a redelivery: the stored row is not modified (even if the redelivered body differs), an `ingest` audit row says `redelivery: ... stored row untouched`, and with `--process` the event runs through classify -> policy -> schedule again, where the idempotency key `sha256(payment_id:1)` collides and the job is reported `skipped_duplicate`. One attempt, one job, one link, whatever the delivery count; this is the same path the README describes for `process --attempt` on an already-processed event.

A `payment_link.paid` that matches a job with an outcome already recorded (a redelivery, or a link `poll` already closed) writes an audit row and no second outcome. A paid event that matches no job here (a link this agent did not create) is acknowledged and audited with `matched: false`.

The receiver acknowledges with 200 only after the database has committed. `PipelineDBError` (the database refused a write) is answered 500 with `"acknowledged": false`, so Razorpay redelivers; zero Payment Links have gone out at that point because the job row is committed before any outbound call.

## The webhook receiver

```
python -m app.main serve-webhook --port 8080 --process --execute-now
```

`ThreadingHTTPServer` from the standard library; every request is processed under one lock (SQLite and the pipeline are single-writer) with a session of its own. One line per request goes to stdout. Answers:

| status | when |
|---|---|
| 200 `{"ok": true, ...}` | ingested; the body carries the same summary `process` prints (class, action, job status, link URL) or the paid-link result, or `{"ignored": "<event>"}` for events the agent does not act on |
| 400 | body is not a JSON object, or `IngestError` (bad amount, wrong id, not a failed payment) |
| 401 | `RAZORPAY_WEBHOOK_SECRET` is set and `X-Razorpay-Signature` is missing or wrong |
| 500 `{"ok": false, "acknowledged": false}` | the database refused a write; Razorpay redelivers |

Razorpay only delivers to a public HTTPS URL, so for a test-mode run on a laptop expose the port (`ngrok http 8080`) and register `https://<your-ngrok-host>/razorpay/webhook` under Dashboard -> Settings -> Webhooks with the events `payment.failed` and `payment_link.paid` and the same secret as in `.env`. Without `--process` the receiver only stores the attempt; run `process --all` later. `run-due --loop` in a second terminal executes the jobs whose delay has elapsed.

## The CSV mapping

`parse_payments_csv` matches headers after lowercasing and turning spaces and dashes into underscores, so `Payment Id`, `payment_id` and `Payment-ID` are the same column. First match wins:

| attempt column | accepted headers |
|---|---|
| `razorpay_payment_id` | `id`, `payment_id`, `razorpay_payment_id` |
| `amount_paise` | `amount` (rupees; `2,499.00`, `Rs 599`, `599` all become 249900 / 59900 / 59900 paise) or `amount_paise` (already paise) |
| `currency` | `currency` |
| status filter | `status`, `payment_status`; only `failed` is imported, the others are counted and reported; a file without a status column is taken to be an export of failed payments |
| `order_id` | `order_id`, `razorpay_order_id` |
| `method` | `method`, `payment_method` |
| `customer_email`, `customer_contact` | `email` / `customer_email`; `contact`, `phone`, `mobile` |
| `customer_name` | `customer_name`, `name`, `customer` |
| `error_code` ... `error_reason` | `error_code` / `error code` / `code`, `error_description`, `error_source`, `error_step`, `error_reason` |
| `has_token` | `token_id` or `token` non-empty |
| `failed_at` | `created_at` / `created` / `date` / `timestamp`: unix seconds, ISO 8601 (an offset is normalised to UTC; naive is taken as UTC) or `DD/MM/YYYY HH:MM:SS` |

Amount rule: a value with a decimal point or a currency mark is rupees; an integer is rupees unless the header is `amount_paise`. Rows with no id, an id that does not start with `pay_`, or an amount that is empty or not a positive number are listed with their line number, never dropped silently. An id already in the database is left as it is (an export re-run is not a redelivery, so no audit row is added for it).

`import-csv --process` then processes every newly imported row (plus any earlier-imported row that has no job yet) and ends with the batch line, the "measured money" figure for a batch: total amount at risk and how many events got a link, are pending, human-queued, stubbed, failed or closed.

## Worked example (verified against a throwaway database)

Run with `DATABASE_URL=sqlite:////tmp/x.db`, no Razorpay or Anthropic keys (fixture links, template nudges), `RAZORPAY_WEBHOOK_SECRET` unset.

```
$ python -m app.main ingest docs/examples/payment_failed_webhook.json --process --execute-now
pay_NfKq2vT8xRb1Zc ingested as attempt 1
pay_NfKq2vT8xRb1Zc card           Rs 2,499.00  INSUFFICIENT_FUNDS (rules)     -> recovery_link        in 48h  job#1   sent https://rzp.io/i/fu5Q1YnE  nudge:template

$ python -m app.main ingest docs/examples/payment_failed_webhook.json --process --execute-now   # the same webhook, redelivered
pay_NfKq2vT8xRb1Zc redelivery as attempt 1
pay_NfKq2vT8xRb1Zc card           Rs 2,499.00  INSUFFICIENT_FUNDS (rules)     -> recovery_link        in 48h  job#1   skipped_duplicate (idempotency key already present; existing job is sent; nothing sent)

$ python -m app.main ingest docs/examples/payment_link_paid_webhook.json
pay_NfKq2vT8xRb1Zc recovered Rs 2,499.00     payment_link.paid via webhook (plink_Ik78ZrxQCVebo8, job#1)

$ python -m app.main ingest docs/examples/payment_link_paid_webhook.json   # redelivered
pay_NfKq2vT8xRb1Zc payment_link.paid redelivered for plink_Ik78ZrxQCVebo8: outcome already recorded, nothing changed

$ python -m app.main show
id  payment_id          method       amount  class               by     action         delay  job   status  link                       nudge     outcome
--  ------------------  ------  -----------  ------------------  -----  -------------  -----  ----  ------  -------------------------  --------  ---------------------
1   pay_NfKq2vT8xRb1Zc  card    Rs 2,499.00  INSUFFICIENT_FUNDS  rules  recovery_link  48h    #1/1  sent    https://rzp.io/i/fu5Q1YnE  template  recovered Rs 2,499.00

$ python -m app.main audit --attempt 1 --no-data
audit trail for attempt 1 (pay_NfKq2vT8xRb1Zc, card, Rs 2,499.00): 14 rows
2026-09-05 13:54:19  ingest    failed payment event received
2026-09-05 13:54:19  classify  INSUFFICIENT_FUNDS (rules): rule insufficient_funds: reason mentions insufficient funds or the description says insufficient, balance or not enough funds
2026-09-05 13:54:19  policy    recovery_link in 48h (max 2 attempts, backoff x1, nudge=yes): Balance replenishes on salary cycles; a 10-minute retry burns an issuer attempt for nothing, so send a link and ask again in 48 hours.
2026-09-05 13:54:19  schedule  scheduled recovery_link (seq 1) for 2026-09-07T13:54:19.358303
2026-09-05 13:54:19  execute   execute_now: running ahead of schedule (was due 2026-09-07T13:54:19.358303)
2026-09-05 13:54:19  execute   sent: payment link plink_Ik78ZrxQCVebo8 created (https://rzp.io/i/fu5Q1YnE)
2026-09-05 13:54:19  nudge     nudge drafted via template for sms; NOT sent: no SMS/email provider in scope (fallback: llm_unavailable->template)
2026-09-05 13:54:20  ingest    redelivery: pay_NfKq2vT8xRb1Zc already ingested as attempt 1; stored row untouched, re-processing so the idempotency key decides
2026-09-05 13:54:20  classify  INSUFFICIENT_FUNDS (rules): rule insufficient_funds: reason mentions insufficient funds or the description says insufficient, balance or not enough funds
2026-09-05 13:54:20  policy    recovery_link in 48h (max 2 attempts, backoff x1, nudge=yes): Balance replenishes on salary cycles; a 10-minute retry burns an issuer attempt for nothing, so send a link and ask again in 48 hours.
2026-09-05 13:54:20  schedule  skipped_duplicate: idempotency key already present
2026-09-05 13:54:21  ingest    payment_link.paid received for plink_Ik78ZrxQCVebo8 (job#1)
2026-09-05 13:54:21  outcome   recovered: payment_link.paid via webhook (plink_Ik78ZrxQCVebo8)
2026-09-05 13:54:22  ingest    payment_link.paid for plink_Ik78ZrxQCVebo8 redelivered: outcome already recorded on job#1 (recovered); nothing changed
```

The paid example matches because its `reference_id` is `executor.reference_id_for(executor.idempotency_key("pay_NfKq2vT8xRb1Zc", 1))`, the first 40 hex characters of the key the executor echoes to Razorpay, and its link id is the one the fixture mints for that reference. Against the real test API the link id in the event is whatever Razorpay assigned; the job is found by either the stored link id or the `reference_id`, so a job whose `sent` write was lost still closes.

The batch entry point, on a second throwaway database:

```
$ python -m app.main import-csv docs/examples/failed_payments_export.csv --process --execute-now
imported 9 failed payments (0 already present, 1 skipped: 1 missing amount)
  line 13: missing amount (pay_NhAl2mN3oP4qR5)
  not imported (status is not failed): captured 1, refunded 1
pay_NhAa1bC2dE3fG4 card           Rs 2,499.00  INSUFFICIENT_FUNDS (rules)     -> recovery_link        in 48h  job#1   sent https://rzp.io/i/ujgULfrY  nudge:template
pay_NhAb2cD3eF4gH5 upi            Rs 1,499.00  INSUFFICIENT_FUNDS (rules)     -> recovery_link        in 48h  job#2   sent https://rzp.io/i/Yyiob5Qq  nudge:template
pay_NhAc3dE4fG5hI6 netbanking       Rs 599.00  ISSUER_DOWN (rules)            -> recovery_link        in 15m  job#3   sent https://rzp.io/i/KaE3hqZ2  nudge:template
pay_NhAe5fG6hI7jK8 card             Rs 899.00  AUTH_ABANDONED (rules)         -> recovery_link        in 10m  job#4   sent https://rzp.io/i/P6MFQmYE  nudge:template
pay_NhAf6gH7iJ8kL9 upi              Rs 459.00  AUTH_ABANDONED (rules)         -> recovery_link        in 10m  job#5   sent https://rzp.io/i/1ZcvZyVD  nudge:template
pay_NhAg7hI8jK9lM0 card           Rs 1,299.00  HARD_DECLINE (rules)           -> nudge_change_method  now     job#6   sent https://rzp.io/i/o9ABWx7T  nudge:template
pay_NhAi9jK0lM1nO2 card          Rs 19,999.00  RISK_BLOCKED (rules)           -> human_queue          now     job#7   human_queue
pay_NhAj0kL1mN2oP3 card             Rs 999.00  NETWORK_TIMEOUT (rules)        -> recovery_link        in 5m   job#8   sent https://rzp.io/i/0luzepfm  nudge:template
pay_NhAk1lM2nO3pQ4 card             Rs 699.00  UNKNOWN (fallback)             -> human_queue          now     job#9   human_queue  [llm_unavailable->human_queue]
batch: 9 events, Rs 28,951.00 at risk -> 7 got a link, 0 pending, 2 human-queued, 0 stubbed, 0 failed, 0 no_action, 0 duplicates

$ python -m app.main resolve --attempt pay_NhAk1lM2nO3pQ4 --recovered 69900 --note "customer paid by bank transfer"
pay_NhAk1lM2nO3pQ4 recovered Rs 699.00 (outcome#3, attempt 9)

$ python -m app.main resolve --attempt pay_NhAk1lM2nO3pQ4 --closed
attempt 9 (pay_NhAk1lM2nO3pQ4) already has an outcome: recovered Rs 699.00 (outcome#3); pass --force to record another
```

`resolve` refuses an attempt whose latest job is not `human_queue`, and one that already has a decided outcome (a recovery, or an earlier `resolve`), unless `--force`. The placeholder `human_queue: parked for a person` outcome the pipeline writes at decision time does not count as decided; it is what `resolve` replaces. The outcome note starts with `resolved by a person`, so `show` and `audit` say who closed it.

## What the receiver was tested against, and what is assumed

`tests/test_ingest.py` covers both payload shapes, `has_token` from `token_id`, the signature (true, false, byte-exact body, `compare_digest` called), the redelivery path (one attempt, one job, one link, row untouched), `payment_link.paid` closing once and matching by `reference_id` when the link id was never stored, unknown events, the CSV mapping (rupees to paise, the paise header, ISO offsets, non-failed rows counted, bad rows reported), the receiver on a real socket (200 / 401 / 400 / health, event id in the audit row), 500 with nothing acknowledged and zero client calls when the database is unopenable, and the four CLI commands end to end. The live receiver has not been exercised against Razorpay's real deliveries (no public URL from the build box).

Assumptions about Razorpay's payloads that could not be verified offline:

- The failed-payment fields are the flat `error_code`, `error_description`, `error_source`, `error_step`, `error_reason` on the payment entity (as in the Payments API), not a nested `error` object.
- `token_id` is present on the entity when a saved card or mandate was used; its absence means no token. The examples omit it.
- `account_id` on the envelope identifies the merchant account (used as `merchant_id`).
- `void@razorpay.com` is the placeholder email for a checkout that collected none.
- The signature is the lowercase hex HMAC-SHA256 of the raw body; the check accepts either case.
- `payment_link.paid` carries `payload.payment_link.entity.reference_id` and `amount_paid` in paise, and `payload.payment.entity` for the paying payment.
- Dashboard CSV headers vary; the tolerant mapping above is a best effort and `parse_payments_csv(...).columns` reports which header was used for each field so a mismatch is visible. Timestamps without an offset are taken as UTC (the dashboard may export IST); this only affects `failed_at`, which no policy decision reads.
