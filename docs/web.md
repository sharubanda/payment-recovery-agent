# The operator view: read-only pages, token-gated actions, proposal apply with evidence

`python -m app.web [--port 8000] [--host 127.0.0.1]` serves a handful of plain HTML pages over the
same SQLite database the CLI writes. Every GET is read-only and anonymous. With `OPERATOR_TOKEN`
set, five POST routes let a person act from the browser; every action is audited under stage
`operator` with the actor's name, and every refusal is a sentence, never a stack trace.

Files: `app/web.py` (routes and HTML), `app/actions.py` (the actions themselves, shared with the
CLI later), additive changes in `app/insights.py` (proposal ids) and `app/merchants.py`
(`write_override`, `rollback_override`, `python -m app.merchants apply|rollback`);
`tests/test_web.py`, `tests/test_actions.py`, `tests/test_merchants.py`.

## Routes

| method | path | what |
|---|---|---|
| GET | `/` | every attempt, `show` columns, `?class=` / `?status=` filters |
| GET | `/attempt/<id>` or `/attempt/pay_...` | event, error object, decisions, jobs, outcomes, audit trail; operator forms when enabled |
| GET | `/human-queue` | parked attempts by expected value; a resolve form per row when enabled |
| GET | `/insights` | `python -m app.insights` as HTML; proposals carry an id and an apply form when enabled |
| GET | `/rule-candidates`, `/policy`, `/reports/failure`, `/reports/simulation`, `/metrics`, `/health` | unchanged, read-only |
| POST | `/action/resolve` | `attempt`, `mode=recovered|closed`, `recovered_paise`, `note` |
| POST | `/action/cancel` | `job`, `reason` |
| POST | `/action/resend` | `job`, `medium=sms|email` |
| POST | `/action/retry` | `job` |
| POST | `/action/apply-proposal` | `proposal` (id), `merchant` (id or `all`) |

Every POST also carries `actor` (required, 1..40 characters), `token`, `back` (the page to return
to; must be a local path) and, on the second step, `confirm=1`. Any other POST path is 405 as
before.

### The two-step confirmation, and why there is still no JavaScript

The first POST of a form (without `confirm=1`) answers 200 with a confirmation page listing the
fields and naming the actor; that page's single button re-POSTs the same fields plus `confirm=1`.
This is the `confirm()` dialog done in HTML, so the CSP stays `default-src 'none'` with no
`script-src` at all, and `tests/test_web.py::test_pages_carry_no_scripts_or_external_assets`
still holds. After a confirmed action the server answers 303 back to `back` with
`?msg=` (success) or `?err=` (refusal) in the query; the page renders it escaped as a flash line.

## The token model

`OPERATOR_TOKEN` (`app/config.py`, or the environment; read through `getattr` so the view runs
without it). Blank is the default and means:

* every `POST /action/*` is 403 with a page saying how to enable actions,
* no form is rendered: the GET pages are byte-for-byte the read-only view they were, plus one
  muted line on the attempt and human-queue pages saying `set OPERATOR_TOKEN ... to enable`.

Set, it means: the forms appear, the operator types the token into a **password field on every
form** (it is never embedded in the page, because anyone who can read the page could then act;
the confirmation page echoes it back as a hidden field, which is the "hidden token field"), the
server compares it with `hmac.compare_digest`, and the `actor` field goes into every audit row.
The human-queue page stops auto-refreshing while actions are on (a refresh would clear a form).

**Why a shared token and not a login.** This is a loopback operator tool for one machine and a
handful of people who already have the database file and the `.env` next to it; a login page
with users, sessions and password storage would add attack surface without adding a boundary,
because whoever can reach the port can already read every row. The token exists so that a stray
form submission, a bookmark, or a colleague's browser tab cannot change state, and so that every
change names a person. A production deployment would need: TLS in front, real authentication
(SSO/OIDC) with per-user identities instead of a typed actor name, CSRF tokens bound to a session
(here the secret token itself is the CSRF defence, since `form-action 'self'` plus a secret the
page never contains means a cross-site form cannot succeed), rate limiting on the token check,
an audit row for refused attempts (today a refusal writes nothing), and the operator view bound
to a private network rather than `--host 0.0.0.0`.

## The actions (`app/actions.py`)

Each function takes a session and returns `{"ok": bool, "message": str, ...}`. On success the
audit trail has one `operator` row (`"<action> by <actor>: ..."`, `data.actor`, `data.action`)
written **before** the state change, plus the rows the underlying path always wrote (`outcome`,
`cancel`, `deliver`, `execute`). A refusal writes nothing.

| function / route | precondition | refused when |
|---|---|---|
| `resolve(session, attempt_id, *, recovered_paise \| closed, note, actor, force=False)` | latest job is `human_queue`; no decided outcome yet | attempt missing; neither/both of recovered/closed; amount <= 0; not human-queued (`force` overrides, as the CLI's `--force`); already resolved or recovered |
| `cancel_link(session, job_id, actor, rz_client=None, reason="")` | job is `sent`, a link action, has a link id, no outcome | not such a job; Razorpay refuses the cancel (the job stays `sent`, a `cancel` audit row says why). Never mints a second link |
| `resend_notification(session, job_id, medium, actor, rz_client=None)` | sent open Payment Link; `RAZORPAY_NOTIFY_CUSTOMER=1`; merchant file not `notify_customer: false`; the customer has that contact; `scheduling.contact_cap` allows another send | any of those fails (the cap's own sentence is the message); Razorpay refuses `notify_by` |
| `retry_now(session, job_id, actor, rz_client=None)` | job is `pending` | anything else. Runs `executor.execute_job` with every guard intact (order paid elsewhere, idempotency, backoff) and no sleeps |
| `apply_proposal(session, proposal_id, actor, merchant_id=None)` | see below | see below |

`resolve` re-implements the checks in `app/main.py:cmd_resolve` (they are inline there and print
to stderr); the CLI should call `actions.resolve` when it is next touched. The same holds for a
future `pra cancel` / `pra resend` / `pra retry-now`: call these functions, print `message`.

Known limit: an operator resend is not a job row, so it does not count against the weekly
contact cap for later sends (a reminder job does). The cap still refuses the resend itself.

## Proposal apply with evidence

`app/insights.py` proposals now carry a stable id, `<CLASS>-<from>-to-<to>` with the bucket
labels stripped of comparison signs (`INSUFFICIENT_FUNDS-48h-to-24h`, `ISSUER_DOWN-1h-to-over48h`),
the two buckets and their `n`; `insights.proposal_by_id(report, id)` finds one. The id is shown
on `/insights` and in the text report is unchanged.

Applying means writing `classes.<CLASS>.delay_seconds = <upper bound of the target bucket>`
(`insights.bucket_delay_seconds`; 72h for `>48h`) into `merchants/<merchant_id>.json`, or
`merchants/_default.json` for `all`, through `merchants.write_override`:

```
python -m app.merchants apply --proposal INSUFFICIENT_FUNDS-48h-to-24h --merchant merchant_acme --actor asha
python -m app.merchants apply --proposal INSUFFICIENT_FUNDS-48h-to-24h --all --actor asha
python -m app.merchants rollback --merchant merchant_acme --class INSUFFICIENT_FUNDS
```

or the apply button on `/insights` (`POST /action/apply-proposal`).

Rules, in the order they are checked:

1. The report is **recomputed at apply time**, scoped to the merchant when one is named. An id
   that is not a current proposal is refused ("the evidence may have changed").
2. Both buckets need `n >= 30` (`insights.MIN_SAMPLES`) **whatever `--min-samples` the page was
   rendered with**: a lower threshold is fine for reading, not for writing.
3. `write_override` validates the merged file through the same schema a hand-edited file passes
   (`ClassOverride` / `MerchantPolicy`: only `delay_seconds`, `max_attempts`, `nudge`, `action`;
   `action` only `human_queue`; the pinned classes; the ranges). Nothing is written when it fails.
4. **Evidence is mandatory.** `write_override(..., evidence="")` is refused. The proposal's
   evidence string (id, both `n`, the proposal text) is stored in the file under `_history`:

   ```json
   "_history": [{"when": "...Z", "actor": "asha", "class": "INSUFFICIENT_FUNDS",
                 "fields": {"delay_seconds": 86400}, "previous": null, "proposal_n": [40, 45],
                 "evidence": "insights proposal INSUFFICIENT_FUNDS-48h-to-24h (n=40 vs n=45, ...): PROPOSAL: ..."}]
   ```

   The loader ignores `_history` (and still rejects any other unknown key).
5. **Conservative-only still applies to manual writes.** `merchants.less_conservative_than_table`
   names a shorter delay, more attempts, or a nudge the table does not send; such a write is
   refused unless it carries `proposal_n` with both `n >= 30`, which only `apply_proposal`
   supplies. A more conservative manual write (a longer delay, `action: human_queue`) needs only
   evidence and an actor. `RISK_BLOCKED` / `UNKNOWN` cannot be moved at all (schema).
6. Every apply writes an `operator` audit row (attempt id none) with the proposal id, the file,
   the fields and the evidence, and the running process reloads the registry so `/policy?merchant=`
   shows the new table at once.

`rollback_override(merchant_id, class_name)` pops the most recent `_history` entry for that
class and restores its `previous` (deleting the class entry when there was none); it is refused
when there is nothing to roll back. A file written by `_default.json` still layers under a
merchant's own file, so rolling back the merchant does not undo an `--all` apply: roll that back
with `--all`.

## Cut in this round (time box)

* No CLI wiring of `pra resolve/cancel/resend/retry-now` onto `app/actions.py` (`app/main.py`
  belongs to another agent); the functions and their result dicts are ready for it.
* No rollback button in the browser (CLI only), no audit row for refused POSTs, no rate limit on
  the token check, no per-action confirmation text beyond the field table.
* An operator resend does not create a job row, so it is not counted by the weekly contact cap
  for later sends.
