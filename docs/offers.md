# Offers, repeat-failer memory, rail preference and shadow mode

Four deterministic rules in `app/offers.py`, applied as post-policy overrides (like
`cadence.subscription_override`; `app/policy.py` is untouched), plus an operating mode in which
nothing leaves the process. Every Razorpay field these rely on is an assumption, listed in section 6;
each is echoed by `FixtureRazorpayClient` and, on a live 400 that names it, dropped with an audit row.
Tests: `tests/test_offers.py` (13), named per section.

## 1. Shadow mode (`PRA_MODE=shadow`)

`offers.shadow_mode()` reads `config.PRA_MODE`, then the env, default `live`. In shadow mode every
stage before the outbound call runs for real (classify, policy, decision, idempotency key, guards,
the atomic pending->executing claim), then:

| where | live | shadow |
|---|---|---|
| `executor.execute_job`, link job | `POST /payment_links` | status `shadow`, `executed_at` set, no link id, no outcome; audit `execute`: `shadow mode: would POST /payment_links with {payload}` (the real payload, PII included, also under `data_json.payload`) |
| halted-subscription link | records the subscription URL | `shadow`, audit `would record the subscription re-authorisation URL ...` |
| the pre-send order check GET | when `CHECK_ORDER_BEFORE_SEND` | skipped: shadow means zero calls |
| nudge (`pipeline.finish_job`) | LLM or template | template only (no model call), `shadow mode: nudge drafted via template ...` |
| delivery | `deliver` rows | one row `shadow mode: nothing delivered; the link was never created` |
| reminders (`cadence.schedule_reminders`) | pending rows | rows with status `shadow`, `executed_at = now`, audit `shadow mode: would schedule reminder n ... and POST /payment_links/{id}/notify_by/{sms|email}` |
| `cadence.execute_reminder` on a pending reminder | `notify_by` | `shadow`, audit `would POST /payment_links/<id>/notify_by/{...}` |
| `ingest._cancel_live_link` | `POST .../cancel` | audit `cancel`: `shadow mode: would POST /payment_links/<id>/cancel ...`; the job is left as it is; `record_order_paid` returns it under `shadow` |
| `poll` | fetches `sent` links | ignores shadow jobs (they have no link id); nothing to ask |
| token retry | `stubbed` | `stubbed` (it never called anything) |

Redelivery is unchanged: the key collides and the second delivery is `skipped_duplicate`. `shadow`
is in `executor._TERMINAL`, so a forced re-execution is refused. `pipeline.shadow_summary(session)`
returns `{jobs, amount_paise, expected_paise, by_action: {action: {jobs, amount_paise, expected_paise,
offers}}, reminders, mode}`, with `expected_paise` from `app.priority.expected_recovery` when that
module imports (else `None`), for the CLI to print.

Tests: `test_shadow_mode_makes_zero_calls_across_process_run_due_poll_and_cancel` (process, run_due
on a pending job, poll, redelivery, `fx.calls == []`, the summary), `test_shadow_cancel_of_a_live_link_is_audited_not_called`.

## 2. The partial-payment offer

Rule: class INSUFFICIENT_FUNDS or LIMIT_EXCEEDED, `amount_paise >= PARTIAL_OFFER_MIN_PAISE` (default
200000 = Rs 2,000), and the first link **expired unpaid** (poll or the `payment_link.expired` webhook,
both through `pipeline.schedule_expiry_followup`): the follow-up (retry_seq 2) is scheduled with
`RecoveryJob.offer = "partial"` (new column) and its payload carries `accept_partial: true` and
`first_min_partial_amount = max(round_to_rupees(PARTIAL_OFFER_MIN_SHARE x amount), 10000)`, never above
the amount (`offers.partial_min_paise`). The `policy` audit row `partial offer: link <plink> expired
unpaid ...` records the minimum and the config. A delivery-failure follow-up (429/transport) is not an
expiry and gets no offer.

The nudge: after `llm.draft_nudge` (either source), `pipeline.attach_nudge` appends one sentence from
`offers.PARTIAL_SENTENCE` in the customer's language (`en`/`hi`/`hinglish`): "You can also pay part of
it now (at least Rs 1,250.00) and the rest later." `app/nudge_templates.py` is unchanged (the
placeholder route would have touched three template sets; one appended sentence is smaller and applies
to the LLM draft too). The sentence is not counted against the SMS budget.

Outcome: `poll` sees status `partially_paid` with `amount_paid > 0` -> outcome `recovered=True`,
`amount_recovered_paise = amount_paid`, note `partially paid via poll (<plink>): X of Y paise`;
`payment_link.paid` with `amount_paid < amount` -> the same with `via webhook`. In both cases the order
is **not** recorded paid and no sibling link is cancelled: the balance is still owed and the link
stays open at Razorpay for it; the link's reminders are voided (the outcome belongs to the link, one
per job). Insights and priority read `outcomes.recovered` / `amount_recovered_paise`, so a partial
payment counts as recovered for the amount paid.

Tests: `test_partial_min_paise_rounds_to_rupees_and_floors_at_rs_100`,
`test_expired_first_link_makes_the_follow_up_a_partial_offer_with_the_nudge_sentence`,
`test_small_amount_or_other_class_gets_no_partial_offer_on_expiry`,
`test_partial_paid_webhook_records_the_amount_paid_and_leaves_the_order_open`.

## 3. Repeat-failer memory

`offers.customer_history(session, attempt, now, window)`: prior attempts (not this one) of the same
`merchant_id` sharing the customer's phone (exact) or email (case-insensitive), `failed_at` in the
last `REPEAT_FAILER_WINDOW_DAYS` (default 60), counted by the class on their `recovery_decisions` row.
An attempt with no contact has no history. Never across merchants.

`offers.history_override(session, attempt, failure_class, pol, now)` runs in `pipeline._process`
after the subscription override, counting this attempt too, in this order:

| history | result |
|---|---|
| any RISK_BLOCKED | `human_queue`: "repeat-failer memory: N RISK_BLOCKED failure(s) ... a person reviews" |
| 3+ HARD_DECLINE (this one a HARD_DECLINE) | `human_queue`: "repeat hard declines: N HARD_DECLINE failures in 60 days, the instrument is dead, a person reaches out" |
| 3+ INSUFFICIENT_FUNDS + LIMIT_EXCEEDED, link action, amount qualifies (section 2) | the action stands; the **first** link is the partial offer (`offer = "partial"` at seq 1) and the nudge is the partial one |
| a table decision of human_queue / no_action | untouched |

The `policy` audit row `history override: ...` carries `counts`, `window_days`, `table_action` and
`offer`. The outcome note of a parked job is the rationale.

Tests: `test_customer_history_counts_same_merchant_same_contact_in_the_window_only`,
`test_three_soft_failures_make_the_first_link_the_partial_offer`, `test_three_hard_declines_go_to_a_person`
(and that two are not enough), `test_any_risk_block_in_history_parks_everything`.

## 4. Rail preference on HARD_DECLINE

Every HARD_DECLINE / `nudge_change_method` link job gets `offer = "rail_upi"` at schedule time
(`offers.default_offer`, called from `executor.schedule_job`). Its payload carries
`options.checkout.method = {card: false, upi: true, netbanking: true, wallet: true}` and
`notes.preferred_method = "upi"`. The template nudge (not the LLM one, whose validators the appended
text would bypass) ends with "UPI works best." / "UPI सबसे आसान रहेगा।" / "UPI sabse aasaan rahega."

Tests: `test_hard_decline_link_disables_card_prefers_upi_and_the_template_says_so`,
`test_rail_sentence_is_template_only_and_language_aware`.

## 5. The live-400 fallback (both offers)

`executor.execute_job`: when the create loop ends `final_4xx` and the 400's description mentions
`partial`, `options`, `checkout` or `preferred_method` (`offers.is_offer_rejection`), and the payload
had offer fields, one audit row `offer fields refused by Razorpay (...); falling back to a plain link`
(`dropped` lists the fields), `job.offer` is cleared, and the plain payload (`offers.strip_offer_fields`)
is sent under the same `reference_id`. The nudge then carries no offer sentence. Any other 4xx is the
usual final failure. Test: `test_live_400_on_an_offer_field_falls_back_to_a_plain_link_and_is_audited`.

## 6. Razorpay assumptions (unverified offline)

- `accept_partial` (bool) and `first_min_partial_amount` (paise) are valid on a standard Payment Link
  and echoed in the create response; a partial payment leaves the link `partially_paid` with
  `amount_paid` set and the link open for the balance. The fixture does exactly this (`mark_paid`
  with a smaller amount).
- `payment_link.paid` is also emitted for a partial payment with `amount_paid < amount` (the parser
  already accepts status `partially_paid`).
- `options.checkout.method` with per-method booleans restricts the hosted checkout and is echoed under
  `options`; `notes` accepts the extra `preferred_method` key (notes are free-form, this one is safe).
- A 400 refusing any of these names the field in its description. If it does not, the job fails
  `final_4xx` like any bad request and a person sees it; nothing is sent twice.

## 7. New columns, statuses, config, functions

| kind | name |
|---|---|
| job status | `shadow` (`JobStatus.SHADOW`, terminal) |
| column | `recovery_jobs.offer`: `partial` / `rail_upi` / NULL |
| config (`.env.example`) | `PRA_MODE` (`live`/`shadow`), `PARTIAL_OFFER_MIN_PAISE` (200000), `PARTIAL_OFFER_MIN_SHARE` (0.5), `REPEAT_FAILER_WINDOW_DAYS` (60) |
| `schedule_job` / `schedule_followup` | new keyword `offer` |
| `attach_nudge` | new keyword `shadow` |
| `record_order_paid` result | new key `shadow` (job ids whose cancel was a would-have) |
| pipeline | `shadow_summary`, `PARTIAL_NOTE` |
| fixture | echoes `first_min_partial_amount` and `options` |

## 8. What was cut

Pre-emptive subscription recovery (card-expiry parsing, `expiring_instruments`, the scheduler hook and
`docs/examples/subscription_charged_webhook.json`) did not fit the time box and is not shipped; no
column or stub for it was left behind.
