# Delivery, reminders and subscriptions

Closes landscape gaps 2, 3 and 4 (`docs/landscape.md`): a recovery is now a delivered campaign
through Razorpay's own channels, with receipts in the audit trail, and subscription failures enter
the same pipeline. No third-party SMS/email provider is involved. Everything below is deterministic;
the LLM is still only consulted for unmapped descriptions and message wording.

## What "delivered" means here (read this first)

Razorpay sends **its own** link message. A Payment Link created with `notify.sms` / `notify.email`
is messaged by Razorpay at creation; `POST /payment_links/{id}/notify_by/{sms|email}` re-sends that
message later. The nudge the agent drafts (`app/llm.py`, stored on the job as `nudge_*`) is **not**
what the customer reads: it is kept for a messaging provider that is not in scope. So a `deliver`
row that says "delivered via Razorpay sms" means "Razorpay accepted a request to message this
customer's phone with its link notification and answered success", not "the customer received our
wording". With `RAZORPAY_NOTIFY_CUSTOMER=0` (the default, see `.env.example`) nothing is delivered
at all and the trail says so.

## The `deliver` stage (`app/cadence.py: record_delivery`)

Runs in `pipeline.finish_job` right after the nudge is drafted, for every link job that becomes
`sent` (created now, or recovered by the reference_id reconcile).

| condition | audit rows (stage `deliver`) |
|---|---|
| `RAZORPAY_NOTIFY_CUSTOMER=1`, customer has phone and/or email | one row per medium: `delivered via Razorpay sms: link plink_… notified at creation (notify.sms=true); receipt {"success": true}` with `data_json` `{medium, link_id, receipt, how}` |
| flag on, no phone and no email | one row: `not delivered: no customer contact or email on the attempt` |
| flag off | one row: `not delivered: RAZORPAY_NOTIFY_CUSTOMER is off; message drafted and stored only` |
| the link is a halted subscription's re-authorisation URL | one row: `not delivered by the agent: … no Payment Link to notify; Razorpay emails its own re-authorisation link when a subscription halts (reported)` |

The receipt for the creation-time send is `{"success": true}` **by assumption**: the create call
was accepted with the notify flags (the response echoes them under `notify`), and that is the shape
`notify_by` answers. Reminders (below) record the `notify_by` response verbatim.

## The reminder cadence (`app/cadence.py`)

After a link job becomes `sent`, one reminder job per offset:

| class | reminders after the send | why |
|---|---|---|
| INSUFFICIENT_FUNDS | +24h, +60h | balance replenishes over days; two touches inside the 72h link |
| AUTH_ABANDONED | +2h, +24h | intent was there minutes ago; a quick second chance, then a next-day one |
| LIMIT_EXCEEDED | +24h | the limit window resets daily |
| HARD_DECLINE | +24h | one reminder to use a different method |
| ISSUER_DOWN | +24h | the outage is over by then |
| NETWORK_TIMEOUT | +24h | same |
| RISK_BLOCKED, UNKNOWN | none | these never reach a sent link (human queue) |

Mechanics, all in `schedule_reminders`:

- Each reminder is a `RecoveryJob` with `action = "reminder"` (`Action.REMINDER`), `parent_job_id` =
  the link job, `retry_seq` = the parent's, and idempotency key
  `sha256(f"{payment_id}:{retry_seq}:reminder:{n}")`, UNIQUE like every job key. A redelivered event
  or a second `finish_job` finds the key present and creates nothing (`skipped_duplicate` audit row).
- `scheduled_at` = the link's `executed_at` + offset, moved out of the 21:00-09:00 IST quiet hours
  by `scheduling.next_allowed_send`; the move is in `schedule_note` ("reminder 2: +60h after send;
  quiet hours: 8 Sep 09:00 IST").
- An offset at or after `RECOVERY_LINK_EXPIRY_HOURS` (also after the quiet-hours move) is skipped with
  an audit row. The expiry is computed from the send time, not read back from the link.
- The weekly contact cap counts reminders (`scheduling.CONTACT_ACTIONS`): a reminder that would
  exceed `MAX_CONTACTS_PER_CUSTOMER_PER_WEEK` is parked `human_queue` with the reason, at schedule
  time and again at execution. With the default cap of 3, a link plus two reminders is exactly the
  budget for one customer per week.
- `REMINDERS_ENABLED=0` creates no reminder jobs (one audit row says so).
- A halted subscription's re-authorisation URL gets no reminders: there is no Payment Link id for
  `notify_by` to re-send.

Execution (`execute_reminder`, reached through `executor.execute_job` from `scheduler.run_due`):

1. already executed -> refused, nothing sent (same terminal-status guard as every job);
2. the order was paid elsewhere -> `no_action` (reminders are also voided outright by the
   paid-elsewhere stop, see below);
3. the link is no longer open (parent not `sent`, its outcome written, or Razorpay's fetch says
   `paid` / `expired` / `cancelled`) -> `no_action`, audited;
4. `RAZORPAY_NOTIFY_CUSTOMER=0` -> `no_action: notifications off`, audited; nothing is called;
5. the sweep runs inside quiet hours -> the reminder moves to the next 09:00 IST and stays pending;
6. the contact cap is hit -> `human_queue` with the reason;
7. otherwise one `POST /payment_links/{id}/notify_by/{medium}` per medium the customer has (sms
   when there is a phone, email when there is an address). Each answer is a `deliver` row with the
   receipt; the job ends `sent` if any medium succeeded, `failed` (`<kind>: …` in `last_error`, no
   follow-up chain) if every medium was refused.

A reminder writes no `outcomes` row: the outcome belongs to the link. When the link closes, the
remaining reminders are voided `no_action` with the reason: `payment.captured` / `order.paid` /
`payment_link.paid` (through `record_order_paid`, reported under `reminders_voided`),
`payment_link.expired`, and the poll seeing `paid` / `expired` / `cancelled`.

### Reminders are hidden from plain job queries

Every reader that takes "the attempt's latest job" as its status (the CLI totals and `show`, the
operator view, the insights report) would otherwise see a pending or voided reminder where the sent
link is. So `app/models.py` attaches an ORM loader criterion: a `SELECT` of `RecoveryJob`, including
the `PaymentAttempt.jobs` relationship, excludes `action = "reminder"` unless the statement carries
`execution_options(include_reminders=True)` (`models.with_reminders(stmt)`). The scheduler
(`due_jobs`, `next_due`), the contact cap, the paid-elsewhere stop and `cadence` itself ask for them.
To render reminders, call `pipeline.reminders_for(session, attempt)` (an alias of
`cadence.reminders_for`); `cadence.get_job(session, id)` fetches one by id. Column refreshes are
never filtered, so an in-session reminder object stays usable. The invariant every test and the
chaos harness assert, "one LINK per payment", is unchanged: a reminder never creates a link.

## Subscriptions (`app/ingest.py: parse_subscription_event`)

`subscription.pending` and `subscription.halted` webhooks are ingested like `payment.failed`: the
failed charge becomes a `PaymentAttempt` with `has_token = True` (a mandate exists, whatever its
state), the payment entity's `method` (else `emandate`), its `error_*` fields, its notes, and three
new columns: `subscription_id`, `subscription_status` (`pending` | `halted`) and `subscription_url`
(the subscription's `short_url`). `order_id` is the payment's, else the subscription id, so a later
`order.paid` / `payment.captured` still matches. Redelivery is idempotent through the payment id
and the job key, exactly as for `payment.failed`. Examples:
`docs/examples/subscription_halted_webhook.json`, `docs/examples/subscription_pending_webhook.json`.

The two subscription rules live in `app/cadence.py: subscription_override` (never in `app/policy.py`):

- **pending** (Razorpay is still retrying the charge itself, T+1 / T+2 / T+3): classified and
  decided as usual, then the action is downgraded to `human_queue` with the reason
  `subscription pending: Razorpay retry in progress` and no new link, unless the class is
  HARD_DECLINE or RISK_BLOCKED (their normal action already stops the retrying; a HARD_DECLINE gets
  its change-method link). The outcome note carries the same reason.
- **halted** (Razorpay's retries are exhausted; the customer must re-authorise): the recovery link
  is the subscription's own `short_url`. `schedule_job` marks the job `link_source =
  "subscription_url"`; `execute_job` records that URL as the link with **no create call** and the
  audit row `sent: subscription re-authorisation URL from the payload; no Payment Link created`.
  A table decision of `token_retry` is overridden to `recovery_link` now (a halted mandate cannot be
  charged). `razorpay_link_id` stays empty, so the link poll never fetches it; the loop closes when
  the order is paid another way or a person resolves it (`subscription.activated` is not ingested).

## New columns, actions, config

- `payment_attempts.subscription_id`, `.subscription_status`, `.subscription_url` (nullable).
- `recovery_jobs.parent_job_id` (FK to `recovery_jobs.id`), `.link_source`
  (`payment_link` | `subscription_url`).
- `Action.REMINDER = "reminder"`; audit stage `deliver`.
- `REMINDERS_ENABLED` (default 1). `RAZORPAY_NOTIFY_CUSTOMER` gates every delivery, as before.
- `RazorpayClient.notify_payment_link(link_id, medium)` on the live, fixture and faulting clients;
  the fixture keeps `notifications[link_id]` and `mark_expired()` for tests.

## Razorpay assumptions (unverified offline)

1. `POST /payment_links/{id}/notify_by/{sms|email}` takes no body and answers `{"success": true}`;
   any non-2xx is a `RazorpayError` (fixture: 400 for an unknown id, a medium other than sms/email,
   or a link that is paid/expired/cancelled).
2. A link created with `notify.sms` / `notify.email` is messaged by Razorpay at creation; the
   creation-time receipt `{"success": true}` is derived from the accepted create call, not from a
   separate response.
3. `subscription.pending` / `subscription.halted` envelopes carry `payload.subscription.entity`
   (`id`, `plan_id`, `customer_id`, `status`, `paid_count`, `remaining_count`, `current_start`,
   `current_end`, `short_url`, `notes`) and `payload.payment.entity` with the failed payment's
   `error_*` fields; the payment entity is required (it is the dedup key and the classifier input).
4. The subscription's `short_url` is a hosted page on which the customer can re-authorise a halted
   mandate; Razorpay also emails a card-change link on halt (reported in `docs/landscape.md`).
5. Real-API field validation is stricter than the fixture's; every assumption above costs at most a
   failed reminder or a parked job, never a second link or a charge.

## Verification

- `pytest`: 558 tests at the time of writing (was 466), including `tests/test_cadence.py`.
- `rm -f recovery.db; python -m app.main demo; rm -f recovery.db`: totals unchanged
  (`22 attempts -> human_queue 5, sent 15, stubbed 2; 15 payment links created`).
- `python scripts/chaos.py --all --write docs/failure_report.md` twice: `9 faults: 9 PASS, 0 FAIL`,
  identical output; the report now shows the `deliver` row and the reminder schedule rows in each
  nudge-probe trail.
