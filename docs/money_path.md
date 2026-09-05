# The money path: stopping, expiring, and timing a recovery

This document covers the three behaviours that decide whether and when a recovery link goes out after the policy table has said "send one": the stop when the order is paid another way, the follow-up when a sent link expires unpaid, and the timing rules (salary window, quiet hours, contact cap) that move or park a link job. The idempotency layers themselves are in the README ("Idempotency, or why a redelivery cannot send twice") and ARCHITECTURE D4; this document assumes them.

Every claim below is checkable in `app/executor.py`, `app/pipeline.py`, `app/ingest.py`, `app/scheduling.py`, `app/config.py`, `app/razorpay_client.py` and `scripts/chaos.py`. Where a test covers a behaviour it is named; where none does, that is said. Nothing here was run against the real Razorpay API: the fixture client stood in for every call, and the shapes it answers with are assumptions listed in section 5.

## 1. Stop when the order is paid another way

A recovery link is a second way to pay the same order. If the customer pays the original order some other way (a checkout retry, a second attempt from the merchant's page), every pending recovery is noise and every live link is a double-payment risk. The stop has four parts: the event that says the order is paid, the columns that record it, the guards that read those columns before anything goes out, and the cancel of a link that already went out.

### 1.1 The events: `payment.captured` and `order.paid`

`app/ingest.py:ingest_event` handles two more webhook events besides `payment.failed`, `payment_link.paid` and `payment_link.expired`: `payment.captured` and `order.paid` (`ORDER_PAID_EVENTS`). Both are parsed by `parse_payment_captured` into `{event, order_id, payment_id, amount_paise, paid_at}`:

- `payment.captured`: `payload.payment.entity`; `status` must be `captured` when present, else `IngestError`; `order_id` comes from the payment entity's `order_id`. A captured payment with no `order_id` parses to `order_id: None` and matches nothing (audited under stage `ingest` with `matched: False`).
- `order.paid`: `payload.order.entity` supplies the order id; `payload.payment.entity`, when present, supplies the paying payment (`payment_id` is `"?"` without it); the amount falls back to the order's `amount_paid` or `amount`.
- `paid_at` is the payment entity's `created_at` (unix), else the envelope's `created_at`, else `None` (the handler then uses its own clock).
- A payload with neither `payload.payment.entity` nor `payload.order.entity` is an `IngestError` (HTTP 400 from `serve-webhook`; `tests/test_ingest.py` line 330 checks that a malformed `order.paid` is a 400, not a 200).

`docs/examples/payment_captured_webhook.json` is a `payment.captured` envelope for the same order as `payment_failed_webhook.json` (`order_NfKp9wQ2sYd7Lm`), paid by `pay_NfKr8mW4vQz3Xd` over UPI.

The parsed dict goes to `record_order_paid` (section 1.4). `close_paid_link` (the `payment_link.paid` handler) also calls `record_order_paid` after writing its recovered outcome, with `quiet=True`, so a sibling attempt for the same order cannot send another link and any sibling link still live is cancelled.

### 1.2 The columns: `order_paid_at` and `paid_by_payment_id`

`PaymentAttempt` (`app/models.py`) carries two nullable columns: `order_paid_at` (DateTime) and `paid_by_payment_id` (String). `executor.mark_order_paid(session, order_id, payment_id, paid_at, now)` sets both on **every** attempt row sharing `order_id` whose `order_paid_at` is still `None`; rows already marked keep their first record ("the earliest capture is the truth"). It commits and returns every row for the order, marked or not.

`executor.paid_order(session, attempt)` is the read side: `(paid_at, payment_id)` from the attempt's own columns, else from any other attempt row with the same `order_id` that is marked (a stale `payment.failed` for an order can arrive after its capture and land on a fresh row), else `None`. An attempt with no `order_id` is never considered paid. `payment_id` falls back to `"?"` when the column is empty.

### 1.3 The guards: schedule time and execute time, zero outbound calls

**Schedule time** (`executor.schedule_job`). The decision chain is, in order: action `human_queue` -> status `human_queue`; action `no_action` -> status `no_action`; `retry_seq > max_attempts` -> `no_action` (stop rule); `paid_order(...)` not `None` -> status `no_action` with `last_error = "order already paid by <payment_id>"` (`order_paid_reason`); otherwise `pending`, with the timing rules of section 3 applied to link jobs. The row is still inserted under its idempotency key, so the trail shows why nothing went out. The audit row reads `no_action: order already paid by <id>; nothing scheduled, nothing will be sent` with `order_paid_by` in `data_json`, and `schedule_job` writes the outcome itself: not recovered, amount 0, note `order paid elsewhere: no recovery needed` (`ORDER_PAID_NOTE`). `pipeline._process` sees a parked status and calls `_close`, which writes nothing because an outcome already exists (one outcome per job). This applies to token retries as well as links: the guard sits before the action check.

**Execute time** (`executor.execute_job`, "guard 5"). After the terminal-status re-read (which now includes `cancelled`) and the `human_queue` / `no_action` shortcuts, and **before** the token-retry branch, `paid_order` is read again. A paid order ends the job through `_finish_order_paid`: status `no_action`, `last_error` as above, audit `no_action: order already paid by <id> (recorded at ingest); nothing sent, nothing charged`, and an outcome with `ORDER_PAID_NOTE`. No Razorpay call is made: the guard runs before the atomic `pending -> executing` claim and before `build_payment_link_payload`. The comment on the guard says why it is above the token branch: charging a token for an order the customer has since paid is the double charge the agent exists to avoid.

Both guards are exercised by the chaos scenario (section 1.7): after the capture voided the pending job, `scheduler.run_due` at the due time executes nothing and `execute_job` forced on the job is refused with zero create calls and zero links; a stale `payment.failed` for the same order is refused at schedule with an outcome and zero calls. There is no unit test of either guard in `tests/`; the scenario under `tests/test_chaos.py::test_each_fault_passes_its_invariant[order_paid_elsewhere]` is the coverage.

### 1.4 What `record_order_paid` does to jobs that already exist

`ingest.record_order_paid(session, paid, rz_client, event_id, now, source, quiet)`:

1. No `order_id` -> one audit row (`attempt_id` None), `{"matched": False}`.
2. `mark_order_paid` on every attempt sharing the order. No attempts -> one audit row, `matched: False`.
3. For each attempt, its jobs are split into `pending` (status `pending`) and `live` (status `sent`, has `razorpay_link_id`, action in `LINK_ACTIONS`, and no `outcomes` row yet). A stubbed token retry, a failed job, a human-queued job or a job in `executing` is in neither list and is left alone.
4. Nothing pending and nothing live: one audit row saying either `already recorded, nothing pending, no live link; nothing changed` (when the attempt was already marked by a different payment id, or any job already carries the `order already paid by` reason) or `nothing pending and no live link for this attempt; recorded, nothing to stop`. With `quiet=True` the second form is skipped.
5. Each pending job becomes `no_action` with `executed_at = now` and `last_error = order already paid by <id>`, an audit row under stage `schedule` (`no_action: job#N (seq S) voided before execution, order already paid by <id>; nothing will be sent`), and an outcome with `ORDER_PAID_NOTE`.
6. Each live link is cancelled (section 1.5).

The return value is `{event, matched, order_id, payment_id, attempt_ids, voided, cancelled, parked, already_recorded}`; `main.py:_ingest_line` prints it as `order <id> paid elsewhere by <pay>: voided pending job(s) #..; cancelled live link job(s) #..; PARKED job(s) #.. (cancel failed; a person cancels the link)`.

Redelivery of the same capture is idempotent by construction: the second call finds nothing pending and nothing live, marks `already_recorded`, and makes no cancel call and no second outcome. The chaos scenario checks this (`redelivered payment.captured: recorded as such, no second cancel call, no second outcome`).

### 1.5 Cancelling a link that was already sent

`ingest._cancel_live_link` calls `rz_client.cancel_payment_link(job.razorpay_link_id)` once and reads the answer's `status`:

- **Answer is a dict with `status == "cancelled"`** (or a dict with no `status`): job status `cancelled` (`JobStatus.CANCELLED`, new in this build), `executed_at` kept (or set to `now` if it was empty), `last_error = "order already paid by <id>; link <plink> cancelled at Razorpay"`, one audit row under the new stage `cancel` (`cancelled: payment link <plink> cancelled at Razorpay because the order already paid by <id>; the customer cannot pay twice`), and an outcome: not recovered, amount 0, note `order paid elsewhere: no recovery needed (link <plink> cancelled)`.
- **Anything else** (a `RazorpayError`, a client without `cancel_payment_link`, a client bug, or an answer whose `status` is not `cancelled`): job status `human_queue`, `last_error = "order already paid by <id>, but cancelling live link <plink> failed (<error>); a person cancels it at Razorpay before the customer can pay twice"`, an audit row under `cancel` (`human_queue: ...`), and an outcome whose note is `human_queue: <that last_error>`. The link stays live at Razorpay and the trail says so. Nothing is retried automatically.

`cancelled` is in `executor._TERMINAL`, so `execute_job` refuses a cancelled job before any call; `pipeline.open_link_jobs` (what `poll` asks about) selects only `sent` jobs without an outcome, so a cancelled link is not polled. `poll_outcomes` has its own `cancelled` branch for a link cancelled by someone else (from the dashboard, say): it writes a not-recovered outcome `payment link cancelled (<plink>)` and schedules no follow-up ("a cancel is a decision, not a lapse"), but the job's status stays `sent` on that path; only `_cancel_live_link` writes `cancelled`.

`FaultingRazorpayClient` passes cancels through to the inner client (only creates are faulted), which is why the chaos scenario uses its own `_CancelRefused` wrapper for step 4.

### 1.6 The optional live check: `CHECK_ORDER_BEFORE_SEND`

The webhook is the primary record. As belt and braces, `execute_job` can ask Razorpay whether the order already has a captured payment before creating a link: `GET /orders/{order_id}/payments` (`list_order_payments` on every client). The switch is `config.CHECK_ORDER_BEFORE_SEND` (`.env`: `auto` by default) read through `config.check_order_before_send()`: `1`/`true`/`yes`/`on` forces it on, `0`/`false`/`no`/`off` forces it off, anything else means `razorpay_live()`, i.e. on with real test keys and off against the fixture. When forced on against the fixture, the fixture answers from its own memory (`FixtureRazorpayClient.mark_order_paid`), never from the links it holds, because a recovery link creates its own order at Razorpay and paying it is not a payment on the original order.

Where it runs: after the atomic `pending -> executing` claim, only when the attempt has an `order_id`, and before the follow-up landed-check and the create call. `_captured_payment_on_order` returns the first item whose `status` lower-cases to `captured`:

- **Found**: `mark_order_paid(session, order_id, pid, now, now=now)` (note: `order_paid_at` is set to the check time, not the capture time, because the payments list is not read for one), an audit row `order check: GET /orders/<id>/payments shows a captured payment <pid>; recorded on the attempt, nothing will be sent`, then `_finish_order_paid(..., how="found by the order check before sending")`: `no_action`, outcome, zero create calls.
- **The call failed** (`RazorpayError`, a client without the method, a client bug): an audit row `order check: GET /orders/<id>/payments could not answer (<error>); proceeding, the payment.captured webhook is the primary record`, and the send proceeds.
- **No captured payment**: an audit row `order check: no captured payment on order <id>; sending`, and the send proceeds.

So "zero calls" in the paid case means zero *create* calls; the one GET is itself a call and is audited. Coverage: chaos step (2) with `CHECK_ORDER_BEFORE_SEND=1` and the fixture's memory primed (`order check: one GET /orders/{id}/payments, a captured payment found, no_action, zero creates, zero links`). There is no unit test of the failed-check or no-payment branches.

### 1.7 The `order_paid_elsewhere` chaos scenario and its invariants

`scripts/chaos.py:scenario_order_paid_elsewhere` (the ninth entry in `app/faults.py:FAULTS`) runs four steps on a throwaway SQLite file with the fixture client and a fixed clock, and `docs/failure_report.md` section 9 is its output:

1. **Paid between decision and execution.** An INSUFFICIENT_FUNDS UPI event is scheduled at T0 for T0+48h. A `payment.captured` for the order at T0+30m matches by `order_id`, voids the pending job (`no_action`, `last_error` names the paying payment), records `order_paid_at = T0+30m` and `paid_by_payment_id` on the attempt, and writes the outcome. `run_due` at T0+48h executes nothing; `execute_job` forced on the job is refused; zero create calls, zero links, zero fixture calls at all. Then a stale `payment.failed` for the same order at T0+1h is refused at schedule (`no_action`, outcome, zero calls).
2. **The pre-send order check.** With `CHECK_ORDER_BEFORE_SEND` forced to `1` and `fixture.mark_order_paid` primed, an AUTH_ABANDONED event executed now makes exactly one `list_order_payments` call, finds the captured payment, ends `no_action`, records `paid_by_payment_id` and the outcome; zero creates.
3. **Paid after the link was sent.** A HARD_DECLINE event (delay 0) sends a link at T0. A capture at T0+2h cancels it: exactly one `cancel_payment_link` call, the fixture's link is `cancelled`, the job is `cancelled` with the paying payment in `last_error`, the outcome note names the cancelled link, `open_link_jobs` is empty and `poll` has nothing to ask. A redelivered capture makes no second cancel call and no second outcome.
4. **The cancel is refused.** A NETWORK_TIMEOUT UPI event sends a link; the capture is ingested through a wrapper whose cancel answers HTTP 502. One cancel attempt; the job is `human_queue`, `last_error` says the link is still live and why; the outcome starts `human_queue` and says a person cancels it; the fixture still holds the link as `created`.

The scenario's stated invariant (`chaos.INVARIANT["order_paid_elsewhere"]`): paid before execution -> job `no_action`, outcome written, zero links, zero create calls; paid after the link was sent -> exactly one cancel call, link cancelled, job cancelled, outcome written; cancel refused -> job `human_queue` with the reason, never a silent live link. `tests/test_chaos.py::test_each_fault_passes_its_invariant` is parametrised over `faults.FAULTS`, so this scenario runs under pytest; `tests/test_chaos.py` lines 93-98 also run the demo with `PRA_FAULTS=order_paid_elsewhere`, where `main.py:_pay_elsewhere` pays every order that just got a link (payment id `pay_elsewhere<attempt id>`) so every link is cancelled again.

### 1.8 Worked example (real output, throwaway database)

Run from this environment with `DATABASE_URL=sqlite:////tmp/mp.db`, no keys, and the database deleted afterwards. Timestamps are the wall clock at the time of the run.

```
$ python -m app.main ingest docs/examples/payment_failed_webhook.json --process --execute-now
pay_NfKq2vT8xRb1Zc ingested as attempt 1
pay_NfKq2vT8xRb1Zc card           Rs 2,499.00  INSUFFICIENT_FUNDS (rules)     -> recovery_link        in 48h  job#1   sent https://rzp.io/i/fu5Q1YnE  nudge:template

$ python -m app.main ingest docs/examples/payment_captured_webhook.json
order order_NfKp9wQ2sYd7Lm paid elsewhere by pay_NfKr8mW4vQz3Xd: PARKED job(s) #1 (cancel failed; a person cancels the link)

$ python -m app.main show
id  payment_id          method       amount  class               by     action         delay  job   status       link                       nudge     outcome
--  ------------------  ------  -----------  ------------------  -----  -------------  -----  ----  -----------  -------------------------  --------  -----------
1   pay_NfKq2vT8xRb1Zc  card    Rs 2,499.00  INSUFFICIENT_FUNDS  rules  recovery_link  48h    #1/1  human_queue  https://rzp.io/i/fu5Q1YnE  template  human_queue

$ python -m app.main audit --attempt 1 --no-data
audit trail for attempt 1 (pay_NfKq2vT8xRb1Zc, card, Rs 2,499.00): 10 rows
2026-09-05 14:47:17  ingest    failed payment event received
2026-09-05 14:47:17  classify  INSUFFICIENT_FUNDS (rules): rule insufficient_funds: reason mentions insufficient funds or the description says insufficient, not enough funds, low balance, balance too low, credit limit, or the Hinglish paryapt/aparyapt
2026-09-05 14:47:17  policy    recovery_link in 48h (max 2 attempts, backoff x1, nudge=yes): Balance replenishes on salary cycles; a 10-minute retry burns an issuer attempt for nothing, so send a link and ask again in 48 hours.
2026-09-05 14:47:17  schedule  scheduled recovery_link (seq 1) for 2026-09-07T14:47:17.519369
2026-09-05 14:47:17  execute   execute_now: running ahead of schedule (was due 2026-09-07T14:47:17.519369)
2026-09-05 14:47:17  execute   sent: payment link plink_Ik78ZrxQCVebo8 created (https://rzp.io/i/fu5Q1YnE)
2026-09-05 14:47:17  nudge     nudge drafted via template for sms; NOT sent: no SMS/email provider in scope (fallback: llm_unavailable->template)
2026-09-05 14:47:18  ingest    payment.captured: order order_NfKp9wQ2sYd7Lm paid by pay_NfKr8mW4vQz3Xd; 0 pending job(s) to void, 1 live link(s) to cancel
2026-09-05 14:47:18  cancel    human_queue: order already paid by pay_NfKr8mW4vQz3Xd, but cancelling live link plink_Ik78ZrxQCVebo8 failed (Razorpay HTTP 400 BAD_REQUEST_ERROR: The id provided does not exist: plink_Ik78ZrxQCVebo8); a person cancels it at Razorpay before the customer can pay twice
2026-09-05 14:47:18  outcome   not recovered: human_queue: order already paid by pay_NfKr8mW4vQz3Xd, but cancelling live link plink_Ik78ZrxQCVebo8 failed (Razorpay HTTP 400 BAD_REQUEST_ERROR: The id provided does not exist: plink_Ik78ZrxQCVebo8); a person cancels it at Razorpay before the customer can pay twice
```

What this shows, and what it does not. The capture matched the attempt by `order_id`, found one live link, attempted exactly one cancel, and, because the cancel failed, parked the job `human_queue` with the reason in `last_error` and in the outcome rather than leaving the link silently live: this is the cancel-refused path of section 1.5, exactly as `docs/failure_report.md` step (4) shows it. The cancel failed here for a reason specific to running without keys across two processes: `FixtureRazorpayClient` is in-memory, the second `ingest` process has forgotten the link the first one created, and `cmd_ingest` does not call `main.py:_fixture_rehydrate` (only `poll`, `mark-paid` and `demo` do), so the fixture answered the cancel with its "does not exist" 400. In one process (the chaos scenario, or `serve-webhook` handling both events) the same two payloads take the cancel-succeeded path: job `cancelled`, outcome `order paid elsewhere: no recovery needed (link ... cancelled)`. Against real test keys the answer would come from the API, whose cancel endpoint shape is an assumption (section 5). The `show` row reads `human_queue` in both the `status` and `outcome` columns; the link URL is still shown because the link is still live.

## 2. Expired links trigger the next attempt

A sent link that is still open is never re-issued: the customer holds it, Razorpay's `reminder_enable` re-nudges when notifications are on, and a second live link for one order is a double-payment risk. A link that **expired unpaid** is different: the customer never used it, so the class's next attempt is minted from the expiry time, until the stop rule closes the payment.

### 2.1 Two ways the agent learns a link expired

- **`poll`** (`pipeline.poll_outcomes`): for every `sent` job with a link id and no outcome, `fetch_payment_link`; a link whose `status` is `expired` gets a not-recovered outcome `payment link expired (<plink>)` and then `schedule_expiry_followup(..., now=_expiry_time(link, now))`, where `_expiry_time` reads the link's `expired_at`, else `expire_by` (unix), else `now`, and never returns a time later than `now`. Covered by `tests/test_pipeline.py::test_poll_records_paid_and_expired_and_leaves_open_links_alone` for the outcome; that test does not assert the follow-up job.
- **The `payment_link.expired` webhook** (`ingest.parse_payment_link_expired` -> `close_expired_link`): the link entity's `id` must start with `plink_`, `status` must be `expired` when present, and `expired_at` is read from the entity's `expired_at`, else `expire_by`, else the envelope's `created_at`. The job is found by `razorpay_link_id`, or by `reference_id` as a prefix of the stored `idempotency_key` (a link whose id was never written because of a crash between the create call and the `sent` write is still matched, and its id is then recorded). A link matching no job is audited and ignored; a job that already has an outcome (poll got there first, or a redelivery) gets an audit row and nothing else. Otherwise: an outcome `payment link expired (<plink>)` dated `min(expired_at, now)`, then `schedule_expiry_followup` with the same clamped time. There is no test of `close_expired_link` or `parse_payment_link_expired` in `tests/`; the worked example in 2.4 is the only exercise of this path in this build.

`docs/examples/payment_link_expired_webhook.json` is the expiry of the link the failed example creates: its `reference_id` `21d31059fd47dea46255bb9b176e3188c470ce25` is `sha256("pay_NfKq2vT8xRb1Zc:1")[:40]`, and `plink_Ik78ZrxQCVebo8` is the id the fixture derives from that reference.

### 2.2 When `retry_seq + 1` is scheduled

`pipeline.schedule_expiry_followup(session, attempt, job, now=expiry_time)`:

1. No `RecoveryDecision` behind the job -> a `policy` audit row saying a person decides; returns `None`.
2. Any job for the attempt already `pending` -> a `policy` audit row (`follow-up job#N (seq S) is already pending, nothing more minted`); returns `None`. Only status `pending` is checked; a job left `executing` by a crash does not block a follow-up.
3. Otherwise `schedule_followup(session, attempt, decision, now=expiry_time, trigger="link expired (<plink>)")`.

`schedule_followup` takes `seq = next_retry_seq` (one past the highest sequence used for the attempt), asks `policy.decide(failure_class, has_token, retry_seq=seq, reachable)` and passes the result to `schedule_job(..., retry_seq=seq, now=expiry_time)`. The delay is the class's `base * backoff ** (seq - 1)` counted from the expiry time, not from when the agent noticed, and `schedule_job` applies the order-paid guard (section 1.3), the salary window and quiet hours, and the contact cap (section 3) to the new row exactly as to a first attempt. The `policy` audit row carries `trigger` and `retry_seq` in `data_json` and reads `follow-up seq 2: recovery_link in 48h (...)`. The delivery-failure branch of `schedule_followup` (`delivery_retry`) does not apply here: it keys on the last `failed` job's failure kind, and an expired link's job is `sent`.

### 2.3 When the stop rule closes it, and what is never re-issued

`policy.decide` returns `no_action` with a rationale starting `stop rule: max attempts reached` once `retry_seq > max_attempts`. `schedule_job` then inserts a `no_action` row (audit `no_action: nothing will be sent`) and `schedule_followup` closes it with an outcome whose note is `no_action: <that rationale>`. With the table as shipped: INSUFFICIENT_FUNDS, AUTH_ABANDONED, LIMIT_EXCEEDED and NETWORK_TIMEOUT (link path) get one follow-up (max 2); ISSUER_DOWN (link path) gets two (max 3); HARD_DECLINE (max 1) gets none, so its first expiry closes the payment. A confidence gate is not re-applied on a follow-up: the class cleared it at seq 1.

Never re-issued: a `sent` link that is still open (`poll` audits `link <plink> still open: status <s>` and does nothing); a paid link; a link cancelled by `record_order_paid` (job `cancelled`, outcome written) or seen `cancelled` by `poll` (outcome, no follow-up); a link whose attempt already has a pending follow-up; a link whose job has no decision row; and any link once `paid_order` is set, because the follow-up's `schedule_job` is refused `no_action` with the order-paid reason.

### 2.4 Worked example (real output, throwaway database)

`DATABASE_URL=sqlite:////tmp/mp2.db`, deleted afterwards:

```
$ python -m app.main ingest docs/examples/payment_failed_webhook.json --process --execute-now
pay_NfKq2vT8xRb1Zc ingested as attempt 1
pay_NfKq2vT8xRb1Zc card           Rs 2,499.00  INSUFFICIENT_FUNDS (rules)     -> recovery_link        in 48h  job#1   sent https://rzp.io/i/fu5Q1YnE  nudge:template

$ python -m app.main ingest docs/examples/payment_link_expired_webhook.json
pay_NfKq2vT8xRb1Zc not recovered: payment link expired (plink_Ik78ZrxQCVebo8, job#1); follow-up job#2 pending at 2026-09-07 14:48Z

$ python -m app.main show
id  payment_id          method       amount  class               by     action         delay  job   status   link  nudge  outcome
--  ------------------  ------  -----------  ------------------  -----  -------------  -----  ----  -------  ----  -----  -------------------------------------------
1   pay_NfKq2vT8xRb1Zc  card    Rs 2,499.00  INSUFFICIENT_FUNDS  rules  recovery_link  48h    #2/2  pending  -     -      payment link expired (plink_Ik78ZrxQCVebo8)

$ python -m app.main audit --attempt 1 --no-data
audit trail for attempt 1 (pay_NfKq2vT8xRb1Zc, card, Rs 2,499.00): 11 rows
2026-09-05 14:48:17  ingest    failed payment event received
2026-09-05 14:48:17  classify  INSUFFICIENT_FUNDS (rules): rule insufficient_funds: reason mentions insufficient funds or the description says insufficient, not enough funds, low balance, balance too low, credit limit, or the Hinglish paryapt/aparyapt
2026-09-05 14:48:17  policy    recovery_link in 48h (max 2 attempts, backoff x1, nudge=yes): Balance replenishes on salary cycles; a 10-minute retry burns an issuer attempt for nothing, so send a link and ask again in 48 hours.
2026-09-05 14:48:17  schedule  scheduled recovery_link (seq 1) for 2026-09-07T14:48:17.279675
2026-09-05 14:48:17  execute   execute_now: running ahead of schedule (was due 2026-09-07T14:48:17.279675)
2026-09-05 14:48:17  execute   sent: payment link plink_Ik78ZrxQCVebo8 created (https://rzp.io/i/fu5Q1YnE)
2026-09-05 14:48:17  nudge     nudge drafted via template for sms; NOT sent: no SMS/email provider in scope (fallback: llm_unavailable->template)
2026-09-05 14:48:18  ingest    payment_link.expired received for plink_Ik78ZrxQCVebo8 (job#1)
2026-09-05 14:48:18  outcome   not recovered: payment link expired (plink_Ik78ZrxQCVebo8)
2026-09-05 14:48:18  policy    follow-up seq 2: recovery_link in 48h (max 2 attempts, backoff x1, nudge=yes): Balance replenishes on salary cycles; a 10-minute retry burns an issuer attempt for nothing, so send a link and ask again in 48 hours.
2026-09-05 14:48:18  schedule  scheduled recovery_link (seq 2) for 2026-09-07T14:48:18.314114
```

The example payload's `expired_at` is `1788862476`, which is 2026-09-08 10:14:36 UTC, later than the run's clock, so `close_expired_link` clamped the expiry time to `now` (`min(when, now)`) and the 48-hour class delay counts from the run time: seq 2 is due 2026-09-07 14:48Z, which is 20:18 IST on the 7th, outside quiet hours and outside the salary wait, so no `schedule_note`. Ingested after the real expiry time, the follow-up would be due 48 hours after 10:14:36 UTC on the 8th. When seq 2's link expires in turn, seq 3 exceeds `max_attempts` 2 and the payment closes `no_action`. The `show` row now shows job `#2/2` pending with no link, and the outcome column shows the latest outcome, which belongs to job 1.

## 3. Timing: salary window, quiet hours, contact cap

All three live in `app/scheduling.py` as pure functions of `now`, and all three are applied in one place, `executor.schedule_job`, to **link jobs only** (`action` in `LINK_ACTIONS` = `recovery_link`, `nudge_change_method`). A token retry is silent and is not timed or capped. Times are naive UTC as everywhere else; IST is a fixed `+05:30` (`IST_OFFSET`), no daylight saving. Function-level coverage is `tests/test_scheduling.py`; there is no test that goes through `schedule_job` and asserts the shifted `scheduled_at` or the `schedule_note` on the row.

### 3.1 The salary window (INSUFFICIENT_FUNDS only)

Rule (`next_salary_window`): if the send time, converted to IST, falls on the **24th or later** of a month (`SALARY_CYCLE_FROM_DAY = 24`, so the 24th to the 31st), the send moves to day `config.SALARY_WINDOW_DAY` of the **next** month at **10:00 IST** (`SALARY_WINDOW_HOUR`). Any other day is returned unchanged, including the 1st to the 23rd and the window day itself. `SALARY_WINDOW_DAY` (`.env`, default `2`) is clamped to 1..28 so every month has the day. December and February are ordinary months (27 Dec -> 2 Jan, 24 Feb -> 2 Mar). The result is never earlier than the input (`max(now, shifted)`). It is applied first, then quiet hours; 10:00 IST is outside quiet hours so the second step never moves a salary-window send.

Computed from this checkout:

| send time (UTC) | in IST | result |
|---|---|---|
| 2026-09-23 12:00 | 23 Sep 17:30 | unchanged |
| 2026-09-24 04:00 | 24 Sep 09:30 | 2026-10-02 04:30 (2 Oct 10:00 IST) |
| 2026-09-27 06:30 | 27 Sep 12:00 | 2026-10-02 04:30 (2 Oct 10:00 IST) |
| 2026-12-28 06:30 | 28 Dec 12:00 | 2027-01-02 04:30 (2 Jan 10:00 IST) |

This is a **heuristic**, and the module says so: most Indian salaried accounts are credited in the last days of a month or the first days of the next, so a balance that was short on the 27th is most likely to be there on the 2nd. It is not a fact about any customer, which is why it is one number in config and one function. Tests: `tests/test_scheduling.py` (`test_salary_window_is_never_earlier_than_now`, `test_salary_window_day_is_clamped_to_a_day_every_month_has`, `test_adjust_applies_salary_window_then_quiet_hours_for_insufficient_funds`, plus the unnamed-in-this-list cases at lines 18-42 covering the 23rd, the 24th, February and December).

### 3.2 Quiet hours, 21:00 to 09:00 IST

Rule (`within_quiet_hours`, `next_allowed_send`): a customer message is not sent when the IST hour is `>= 21` or `< 9`. 21:00:00 IST is quiet; 09:00:00 IST is allowed (`test_quiet_hours_boundaries_are_inclusive_at_21_and_exclusive_at_9`). A send inside the window moves to the next 09:00 IST: the same morning for 00:00-08:59, the next morning for 21:00-23:59. UTC midnight is 05:30 IST and therefore quiet (`test_utc_midnight_is_5_30am_ist_and_therefore_quiet`). Applied to every link job of every class, after the salary window (`adjust_send_time`), at schedule time, for first attempts and follow-ups alike.

What is shifted: the `scheduled_at` of a link job, at the moment `schedule_job` writes it. What is not shifted: a token retry (nothing in `scheduling.py` touches it: it is silent); a job already written (the rule is not re-checked when `run-due` executes; `run-due` only asks whether `scheduled_at <= now`); and anything run with `--execute-now`, which runs ahead of schedule by design and audits `execute_now: running ahead of schedule (was due <shifted time>)`.

The one gap found while writing this section has since been fixed: `pipeline._process` used to run a freshly scheduled job immediately whenever the class delay was 0, which let a HARD_DECLINE at 02:00 IST send at 02:00 even though quiet hours had moved it to 09:00. It now runs immediately only when `--execute-now` is passed or the adjusted `scheduled_at` is already due; otherwise `run-due` sends it at 09:00 IST (`tests/test_money_path.py::test_quiet_hours_are_honoured_on_the_process_path_for_a_delay_zero_class`).

### 3.3 The contact cap

Rule (`contact_sends`, `contact_cap`): count the `recovery_jobs` rows whose action is a link action, whose status is `sent` **or `cancelled`** (a cancelled link still reached the customer), whose `executed_at` is not null and lies in `(now - 7 days, now]`, across every attempt that shares the customer's phone (`customer_contact`, exact string after stripping) or email (`customer_email`, compared lower-cased). `stubbed`, `pending`, `failed` and `human_queue` rows do not count (`test_cancelled_links_count_but_token_retries_and_pending_jobs_do_not`). If that count is `>= config.MAX_CONTACTS_PER_CUSTOMER_PER_WEEK` (`.env`, default `3`; clamped at 0), the job is not scheduled: `schedule_job` writes it as `human_queue` with `scheduled_at = now` and `last_error = "contact cap: N link/nudge send(s) to <who> in the last 7 days (max M per week); a person decides rather than the agent sending a (N+1)th"`, audits `human_queue: <that reason>; nothing will be sent`, and `pipeline` closes it with an outcome `human_queue: <that reason>`. An attempt with neither phone nor email is not capped (`test_no_contact_means_no_cap`); a cap of 0 parks the first send (`test_cap_of_zero_parks_the_first_send`). The window is measured at scheduling `now`, not at the (possibly shifted) send time: a job scheduled on the 27th for the 2nd is judged against the week before the 27th. The cap runs after the salary-window/quiet-hours shift in the code, but since it only reads `now` the order does not change the answer.

### 3.4 How the three show up in the trail and in `show`

- `recovery_jobs.schedule_note` (new column, String(160)): `salary window: 2 Oct 10:00 IST`, `quiet hours: 6 Sep 09:00 IST`, or both joined with `; `; `None` when nothing moved. The format is `format_ist`: day without a leading zero, `%b %H:%M IST`.
- Audit rows at schedule time: the usual `scheduled <action> (seq N) for <iso>` (with `schedule_note` and `delay_seconds` in `data_json`), plus, when the time moved, a second `schedule` row `send time moved from <iso> to <iso> (<note>)` with `from`, `to`, `note` and `failure_class` in `data_json`. A capped job writes `human_queue: contact cap: ...; nothing will be sent` instead of `scheduled ...`.
- `process` / `ingest --process` output (`main.py:_event_line`): `in 48h -> 2 Oct 10:00 IST (salary window)`.
- `show` (`main.py:_show`), the `delay` column: `48h -> salary window: 2 Oct 10:00 IST`; a capped job shows `human_queue` in `status` and `outcome`.
- The `ingest` line for an expiry shows the follow-up's note: `follow-up job#2 pending at <ts> (<note>)`.
- `data_json` on the `schedule` row of a paid order carries `order_paid_by`.

### 3.5 Why the simulation is unaffected

`scripts/simulate.py` imports `app.executor` only for `idempotency_key` and `app.policy` for `decide`; it never calls `schedule_job` and never imports `app.scheduling`. Its recovery model is a per-class probability keyed on the policy's delay (`INSUFFICIENT_FUNDS link 48h 0.42 ... link + nudge on the salary cycle` in its table), not on wall-clock time, and the timing rules change `scheduled_at`, never `delay_seconds` or the class. So `docs/simulation_report.md` is the same before and after this change, and it says nothing about what a salary window, quiet hours or a contact cap would do to recovery: those effects are not modelled.

## 4. What is new in this build

| kind | name | where | meaning |
|---|---|---|---|
| job status | `cancelled` | `taxonomy.JobStatus`, `executor._TERMINAL` | a sent link cancelled at Razorpay because the order was paid another way; terminal |
| audit stage | `cancel` | `ingest._cancel_live_link` | one row per cancel attempt, `cancelled: ...` or `human_queue: ...` |
| column | `payment_attempts.order_paid_at` | `models.PaymentAttempt` | when the order was recorded paid (capture time from the webhook; check time from the order check) |
| column | `payment_attempts.paid_by_payment_id` | `models.PaymentAttempt` | the paying payment; `"?"` shown when unknown |
| column | `recovery_jobs.schedule_note` | `models.RecoveryJob` | why `scheduled_at` is not `now + delay` |
| `last_error` prefix | `order already paid by <id>` | `executor.ORDER_PAID_PREFIX` | on a job voided or refused for a paid order; `_parked_note` and `record_order_paid` read it |
| outcome note | `order paid elsewhere: no recovery needed` (`(link <plink> cancelled)` appended after a cancel) | `executor.ORDER_PAID_NOTE` | |
| outcome note | `payment link expired (<plink>)` | `pipeline.poll_outcomes`, `ingest.close_expired_link` | followed by the next attempt or the stop rule |
| outcome note | `payment link cancelled (<plink>)` | `pipeline.poll_outcomes` | a link cancelled by someone else; no follow-up |
| config | `CHECK_ORDER_BEFORE_SEND` | `config`, default `auto` | `GET /orders/{id}/payments` before a create; `auto` = on with real test keys |
| config | `SALARY_WINDOW_DAY` | `config`, default `2`, clamped 1..28 | the day of the next month INSUFFICIENT_FUNDS sends move to |
| config | `MAX_CONTACTS_PER_CUSTOMER_PER_WEEK` | `config`, default `3` | link/nudge sends per phone or email in a rolling 7 days |
| constants | `QUIET_START_HOUR = 21`, `QUIET_END_HOUR = 9`, `SALARY_CYCLE_FROM_DAY = 24`, `SALARY_WINDOW_HOUR = 10`, `CONTACT_WINDOW = 7 days` | `scheduling` | not configurable from `.env` |
| events | `payment.captured`, `order.paid` | `ingest.ORDER_PAID_EVENTS` | -> `record_order_paid` |
| event | `payment_link.expired` | `ingest.EVENT_LINK_EXPIRED` | -> `close_expired_link` |
| client method | `cancel_payment_link(link_id)` | all three clients | `POST /payment_links/{id}/cancel` |
| client method | `list_order_payments(order_id)` | all three clients | `GET /orders/{id}/payments` |
| fixture helper | `FixtureRazorpayClient.mark_order_paid(order_id, payment_id, amount, now)` | fixture only | primes `list_order_payments` |
| fault | `order_paid_elsewhere` | `faults.FAULTS`, `chaos.SCENARIOS`, `main._pay_elsewhere` | the ninth fault |
| pipeline function | `schedule_expiry_followup` | `pipeline` | the next attempt after an expiry, or the stop rule |
| executor functions | `paid_order`, `mark_order_paid`, `order_paid_reason`, `_finish_order_paid`, `_captured_payment_on_order` | `executor` | |

## 5. Assumptions about Razorpay

None of these were verified against the API from this environment (no keys, no network to the docs); each is implemented in `FixtureRazorpayClient` as stated and named in its docstring.

- **Cancel endpoint.** `POST /payment_links/{id}/cancel` with an empty JSON body (`{}` is sent, with `Content-Type: application/json`) answers the link entity with `status: "cancelled"` and `cancelled_at` set. The fixture also assumes: cancelling a paid, partially paid or expired link is a 400 (`only a created link can be cancelled`); cancelling an already-cancelled link is idempotent; an unknown id is a 400 `does not exist`. If the real answer has a different `status` word, `_cancel_live_link` treats it as a failed cancel and parks the job: a wrong assumption costs one human review, never a silent live link.
- **Order payments.** `GET /orders/{order_id}/payments` answers `{"entity": "collection", "count": n, "items": [payment entities]}`; the live client also accepts a `payments` key. A payment with `status == "captured"` (case-insensitive) means the order is paid. If the call fails or the shape is unexpected, the executor audits it and sends anyway: the webhook is the primary record.
- **`payment.captured` payload.** `payload.payment.entity` with `id` (`pay_`), `status: "captured"`, `order_id`, `amount` (paise) and `created_at` (unix); the envelope's `created_at` is the fallback timestamp. Only `id`, `status`, `order_id`, `amount` and `created_at` are read.
- **`order.paid` payload.** `payload.order.entity` with `id` and `amount_paid` / `amount`, and `payload.payment.entity` for the paying payment. This shape is remembered, not confirmed.
- **`payment_link.expired` payload.** `payload.payment_link.entity` with `id` (`plink_`), `status: "expired"`, `reference_id`, and `expired_at` (unix) once expired, with `expire_by` as the fallback. `docs/examples/payment_link_expired_webhook.json` is written to this assumption.
- **Uniqueness and lookup of `reference_id`** (the base of every cancel that matches by reference) are the assumptions already stated in the README and ARCHITECTURE D4; they are unchanged here.
