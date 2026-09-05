# Landscape: failed-payment recovery products, and where this agent sits

Researched 2026-09-05 for the Razorpay AI Buildathon (Track 03) panel. Every product section relies on the vendor's public pages and press coverage as found through web search; most vendor sites (razorpay.com, stripe.com, chargebee.com, hyperswitch.io, paddle.com, cashfree.com, payu.com) were unreachable from the research box, so where a claim rests only on a search snippet or a third-party write-up it is marked *reported*. Nothing below was tested. Numbers are the vendors' own and are quoted, not endorsed.

What this agent does, for reference: takes a `payment.failed` event, classifies the cause with thirteen ordered deterministic rules (a model only for descriptions no rule matches, gated at 0.7 confidence), decides from an eight-row policy table (action, delay, backoff, max attempts, stop rules), recovers through a fresh Razorpay Payment Link under a DB-UNIQUE idempotency key echoed as `reference_id`, drafts (but does not send) a customer nudge, and writes an append-only audit row per stage. Token retries are stubbed. See `README.md` and `ARCHITECTURE.md`.

## Stripe: Smart Retries and Adaptive Acceptance

*What.* Smart Retries re-attempts failed subscription invoices in Stripe Billing on a per-payment schedule instead of a fixed one; Adaptive Acceptance retries certain network declines in real time, before the customer sees the decline, by varying routing and ISO message fields. Stripe reports Adaptive Acceptance recovered "$6 billion in falsely declined transactions in 2024".
*Intelligence.* Machine learning trained across the Stripe network; the Smart Retries model is reported to ingest 500+ attributes in five categories (customer, card, issuer, merchant, time). Hard-decline codes are not retried even when scheduled (reported).
*Signals and channels.* Card network responses, issuer behaviour, the customer's cross-Stripe history; up to 8 retries in a window of 1 week to 2 months (reported); works with Card Account Updater, dunning emails and the hosted customer portal for card updates.
*Sources.* [How we built it: Smart Retries](https://stripe.com/blog/how-we-built-it-smart-retries), [AI enhancements to Adaptive Acceptance](https://stripe.com/blog/ai-enhancements-to-adaptive-acceptance), [Smart Retries docs](https://docs.stripe.com/billing/revenue-recovery/smart-retries), [Churnkey summary](https://churnkey.co/blog/stripe-smart-retries) (third party).

## Chargebee: Smart Dunning and Revive

*What.* Smart Dunning is the configurable dunning engine (retry cadence, reminder emails, end-of-dunning action such as cancel or unpaid). Revive is a newer ML retry mode layered on it.
*Intelligence.* Smart Dunning is rules keyed on gateway error type: hard declines wait for a payment-method update, soft declines are retried at a time chosen from error category and recovery odds, up to 12 retries (or up to 5 on merchant-chosen days). Revive is reported to read "200+ signals" per failure (issuer behaviour, time zones, funding cycles, LTV, tenure, billing history).
*Signals and channels.* Gateway error category plus full billing context; email reminders run on a separate track from retries; supports Razorpay as one of 40+ gateways (reported), which makes it the closest incumbent for a Razorpay subscription merchant.
*Sources.* [Smart and manual dunning](https://www.chargebee.com/docs/payments/2.0/kb/payments/smart-and-manual-dunning-management), [Payment Retries: Revive + Smart Dunning](https://www.chargebee.com/payments/retries-and-dunning/).

## Recurly: Intelligent Retries

*What.* ML-scheduled retries for declined recurring card payments inside Recurly Billing, with a "Recovered revenue" report. Recurly reports $1.3 billion recovered for merchants and a custom per-merchant retry model that adds an average 7% lift (both vendor figures).
*Intelligence.* A model over transaction attributes, processor response codes and the customer's payment history; soft declines only, hard declines are not retried automatically (reported).
*Signals and channels.* Bounded at 20 total attempts or 60 days from invoice creation; direct debit (ACH, SEPA) excluded (reported). Dunning emails and account-updater integrations sit alongside.
*Sources.* [Intelligent retries docs](https://docs.recurly.com/docs/retry-logic), [Understanding Intelligent Retries](https://recurly.com/blog/product-perspectives-understanding-intelligent-retries/), [Product page](https://recurly.com/product/intelligent-retries/).

## Churn Buster

*What.* A dunning add-on for Stripe Billing (and other billing systems) that runs branded, escalating email campaigns over several weeks with a one-click card-update path, plus "Enhanced Retries" that position extra retries inside the campaign.
*Intelligence.* Campaign logic and retry placement; no ML claim found. Positions itself as a premium service for higher-MRR merchants.
*Signals and channels.* Email, SMS, additional notification channels (reported); retries against the card on file via the billing platform.
*Sources.* [Retries doc](https://churnbuster.io/docs/retries/), [Stripe Billing + Churn Buster](https://churnbuster.io/docs/stripe-billing-churn-buster).

## Gravy

*What.* A human-staffed recovery service: full-time US-based "retention specialists" contact the customer within hours of a failure. Gravy reports over $1 billion returned to clients and recovery rates "up to 80%, averaging around 50%" (vendor figures, unverified).
*Intelligence.* People, working from the merchant's brand script; not software-first.
*Signals and channels.* Email, text and phone calls; serves SaaS, subscription boxes, course creators, nonprofits.
*Sources.* [Gravy for SaaS](https://www.gravysolutions.io/saas), [TechCrunch on the $4.5M raise](https://techcrunch.com/2021/02/17/gravy-raises-4-5m-for-its-service-that-helps-subscription-businesses-recover-failed-payments).

## Butter Payments

*What.* Passive-churn recovery that plugs into the merchant's billing/processor stack and re-schedules retries. Butter reports 20 to 200% lift in recovered payments and a 9% average churn reduction (vendor figures).
*Intelligence.* A patented ML approach (reported) that classifies the root cause of each failure (expired card, insufficient funds, technical fault) and picks retry timing per transaction with "as few retries as possible".
*Signals and channels.* Transaction and processor data; brand-specific strategies; customer outreach is not the headline feature.
*Sources.* [Payment Recovery](https://www.butterpayments.com/payment-recovery/), [ML for payment recovery](https://www.butterpayments.com/resources/blog/how-to-use-machine-learning-to-recover-failed-recurring-payments).

## Paddle Retain (formerly ProfitWell Retain)

*What.* Payment Recovery inside Paddle Billing (also sold standalone for Stripe, Braintree, Chargebee, Recurly, Zuora): retries plus notifications over a 30-day dunning window, a no-login payment-update form, and automatic pause or cancel when dunning is exhausted. Paddle reports a "50%+ recovery rate" and 17% lower involuntary churn (vendor figures).
*Intelligence.* "Tactical Retries" using 15+ factors including payment method type, customer location, failure code and time of day (reported); retries 1 to 4 typically within 10 to 12 days, 5 to 7 by around day 20 (reported).
*Signals and channels.* Email, in-app, SMS; Apple Pay, Google Pay, PayPal and card on the update form.
*Sources.* [Payment Recovery developer docs](https://developer.paddle.com/concepts/retain/payment-recovery-dunning/), [Retry cadence help article](https://www.paddle.com/help/profitwell-metrics/retain/how-it-works/retain-payment-recovery-how-it-works-retry-cadence).

## Baremetrics Recover

*What.* Dunning for Stripe, Braintree and Recurly merchants: a 30-day email and SMS sequence, in-app reminders and paywalls, a hosted card-capture form, pre-expiry card reminders, and analytics. Baremetrics reports $1.35 million reclaimed across 148 customers in one month (vendor figure).
*Intelligence.* Rules: soft declines retried at "optimal times, such as around payday" (reported); no ML claim found.
*Signals and channels.* Email, SMS, in-app; two-way card write-back to the processor.
*Sources.* [Recover feature page](https://baremetrics.com/features/recover), [What is Recover?](https://help.baremetrics.com/en/articles/5380360-what-is-recover).

## Razorpay: Agent Studio's Subscription Recovery Agent (FTX 2026)

*What.* One of eight agents Razorpay launched on 12 March 2026 with Agent Studio, built on the Claude Agent SDK. It "analyzes the reason for failure, applies intelligent retry logic, and triggers targeted customer nudges", escalating to an ElevenLabs voice call in English or Hindi when retries are insufficient (reported). Siblings include Dispute Responder, Abandoned Cart Conversion, RTO Shield and Cashflow Forecaster.
*Intelligence.* LLM agent; retry logic details are not public. Razorpay's guardrails post describes merchant-approved scopes, a review-first mode that holds actions for merchant approval, escalation to the merchant on WhatsApp, platform-side scope checks, and a full per-action audit trail on a dashboard (reported).
*Signals and channels.* Razorpay's own subscription and payment data; voice, WhatsApp, plus whatever nudge channels the platform supports. Critics (MediaNama) raised dark-pattern and pricing questions.
*Sources.* [Agent Studio](https://razorpay.com/agent-studio/), [Newsroom launch post](https://razorpay.com/newsroom/razorpay-launches-the-worlds-first-ai-native-agent-studio-for-payments-at-ftx26-powered-by-anthropics-claude/), [Principles, guardrails and merchant control](https://razorpay.com/blog/razorpay-agent-studio-principles-guardrails-and-merchant-control/), [ElevenLabs case study](https://elevenlabs.io/blog/razorpay), [MediaNama](https://www.medianama.com/2026/03/223-razorpay-launches-ai-agent-studio-questions-loom-dark-patterns-price-discrimination/).

## Razorpay: platform features this agent can build on

*Subscriptions auto-retry.* For a failed subscription charge Razorpay itself retries on T+1, T+2, T+3, then moves the subscription to `halted` and fires `subscription.halted`; Razorpay emails the customer a card-change link, and a successful card change re-activates the subscription. Halted subscriptions cannot be updated by API (reported). This is the one piece of native cause-agnostic retry on the platform.
*Optimizer (Smart Router, Downtime Router).* Enterprise routing across 100+ providers using a random-forest model over 150+ parameters; creates a 20-minute temporary downtime for a gateway whose success rate drops and routes to the next priority; can be set to retry through an alternative provider only after the preferred one fails. Razorpay reports "up to 10%" success-rate lift. This is a first-attempt and same-session optimisation, not post-failure recovery.
*Magic Checkout abandoned-cart webhook.* Emits the customer's contact when they drop off at checkout, for retargeting including a Razorpay WhatsApp flow; e-commerce focused.
*Payment Links.* Create, resend (`POST /payment_links/{id}/notify_by/{sms|email}`), `notify.sms` / `notify.email`, `reminder_enable`, and account-level reminder schedules (up to 3 reminders at set intervals). This is exactly the surface this agent already uses.
*Sources.* [Subscriptions payment retries](https://razorpay.com/docs/payments/subscriptions/payment-retries/), [Subscription states](https://razorpay.com/docs/payments/subscriptions/states/), [Optimizer docs](https://razorpay.com/docs/payments/optimizer/), [Dynamic Routing](https://razorpay.com/docs/payments/optimizer/dynamic-routing/), [Optimizer AI/ML routing blog](https://razorpay.com/blog/boost-payments-success-rates-with-optimizers-ai-ml-routing/), [Abandoned cart webhook](https://razorpay.com/docs/payments/magic-checkout/abandoned-cart/), [Payment Link reminders](https://razorpay.com/docs/payments/payment-links/reminders/), [Payment Links APIs](https://razorpay.com/docs/api/payments/payment-links/).

## Juspay Hyperswitch: Smart Retries and Revenue Recovery

*What.* Open-source payment orchestration. Smart Retries re-attempt a failed card payment on the same or an alternative processor in-session; Revenue Recovery is a newer module for recurring payments that schedules retries per failure through a dashboard, with "hard-decline switch" support in recent releases (reported).
*Intelligence.* Error normalisation across PSP codes into retryable / non-retryable categories, then rules; Revenue Recovery is reported as an "intelligent retry engine" over 20+ transaction parameters. 3DS transactions failing for business reasons are not retried; only technical failures; recommended to start with one retry; enablement is via support (from the docs repo).
*Signals and channels.* Normalised error code, processor health, 3DS step-up (retrying a non-3DS decline with 3DS when the error suggests it). No customer-facing channel; it is infrastructure.
*Sources.* [Smart retries product page](https://hyperswitch.io/products/smart-retries), [Smart retries docs (GitHub)](https://github.com/juspay/hyperswitch-docs/blob/main/features/smart-retries.md), [Revenue recovery](https://hyperswitch.io/revenue-recovery), [Error normalisation blog](https://hyperswitch.io/blog/part-1-3-error-normalization-and-smart-retries-how-we-turn-noisy-psp-codes-into-decisions).

## Cashfree

*What.* Native dunning for mandates (UPI Autopay, cards, wallets, bank debits) with a fixed-schedule "Smart Retry Logic" (e.g. retry in 3 days, then 5), escalating message workflows, in-app blocks and reporting (reported from Cashfree's own blog). In late August 2026 Cashfree launched Relay, AI agents that "retry failed payments, follow up on abandoned carts, manage failed subscriptions and file disputes", free at launch with outcome-based pricing planned (reported, press coverage).
*Intelligence.* Rules for dunning; Relay is LLM-agent based; details of its retry logic are not public.
*Signals and channels.* Voice and text nudges (reported), discounts to convert at-risk carts (reported).
*Sources.* [Cashfree on dunning](https://www.cashfree.com/blog/what-is-dunning-managment/), [Relay overview docs](https://www.cashfree.com/docs/tools-ai/relay/overview), [Business Standard on Relay](https://www.business-standard.com/companies/news/cashfree-rolls-out-ai-agents-for-merchants-to-automate-payment-operations-126083000455_1.html).

## PayU

*What.* "Instant Retry" re-routes a declined transaction through a pre-configured alternative provider in the same session when the decline is technical or financial, with merchant-configurable criteria by decline reason, card country and similar; a patented dynamic routing engine watches bank-route health and steers traffic around failing routes (reported, PayU Global corporate site; PayU India blog posts echo the routing claim). No post-session dunning product found.
*Intelligence.* Rules configured in a "Decision Engine"; route health monitoring.
*Signals and channels.* Decline reason, route health; no customer channel.
*Sources.* [Instant Retry](https://corporate.payu.com/payment-optimization/instant-retry-feature/), [Routing engine](https://corporate.payu.com/payment-optimization/smart-routing-engine/), [PayU India on reducing failures](https://payu.in/blog/how-to-reduce-transaction-failures/).

## What they have that this agent does not

"In scope" means: achievable by a merchant-side agent that only holds Razorpay API keys and its own database, without being the gateway.

| capability | who has it | in scope? | how it would be added here |
|---|---|---|---|
| Real token / mandate re-charge | Stripe, Chargebee, Recurly, Paddle, Razorpay Subscriptions auto-retry | Yes, first | Replace the stub in `app/executor.py:execute_job` for `Action.TOKEN_RETRY` with Orders API + recurring Payments API (`POST /orders`, then `POST /payments/create/recurring` with `token` and `customer_id`; endpoint names reported, unverified offline), a mandate/token status check before the debit, and the same `reference_id`/`receipt` idempotency echo. Deterministic. |
| ML-timed retries (per-card optimal window) | Stripe, Recurly, Chargebee Revive, Paddle, Butter, Hyperswitch RR | Partly | Not the model: no cross-merchant data. In scope is a per-class delay learned from this merchant's own `outcomes` (a bandit over class x delay bucket) that can only move inside bounds the table sets, and only after `outcomes` has real rows. `app/policy.py:delay_for` takes an optional learned override; the table stays the guardrail. Model, bounded by deterministic rules. |
| Real-time in-session retry / re-route / 3DS step-up | Stripe Adaptive Acceptance, Optimizer, Hyperswitch, PayU Instant Retry | No | Happens inside the gateway before `payment.failed` exists. The merchant-side equivalent is the rail-switch action cut from the timebox: for `HARD_DECLINE` and `LIMIT_EXCEEDED` create the Payment Link with `options.checkout.method` restricting to UPI/netbanking (Payment Links API supports customising methods, reported). Deterministic, `app/executor.py:build_payment_link_payload`. |
| Card account updater / network token refresh | Stripe, Recurly, Baremetrics (via processor), Cashfree (reported) | No | Issuer and network service; Razorpay handles RBI network tokenisation on its side and exposes no merchant-side updater API that the research found. Best available proxy is Razorpay's own card-change email for halted subscriptions. |
| Real customer delivery: email, SMS, WhatsApp, in-app, voice | All dunning vendors; Razorpay agent (voice), Cashfree Relay | Yes | Turn on `RAZORPAY_NOTIFY_CUSTOMER` for Razorpay-delivered SMS/email on the link, add `POST /payment_links/{id}/notify_by/sms|email` for re-nudges, and put an SMS/WhatsApp provider behind the stored `nudge_*` fields with a `delivered` stage in `audit_log`. Consent/DND handling required. Deterministic. |
| Multi-step campaign with escalating messages over 14 to 30 days | Chargebee, Churn Buster, Paddle, Baremetrics, Gravy | Yes | The policy table has `max_attempts` and `backoff` already; add a `campaign` column (message template per attempt) in `app/policy.py` and let `app/scheduler.py` mint later `retry_seq` rows as re-nudges on the same link (Razorpay `reminder_enable` covers up to 3). The one-link-per-payment rule holds. Deterministic. |
| Pre-emptive expiry / pre-dunning reminders | Baremetrics, Recurly, Chargebee | Yes | A new entry point in `app/ingest.py` for `token` / `subscription.charged` events with card expiry, and a `PRE_EXPIRY` class with `nudge_change_method` at T-14d. Needs token expiry from `GET /customers/{id}/tokens` (reported). Deterministic. |
| Subscription lifecycle actions at end of dunning (pause, cancel, mark unpaid) | Chargebee, Paddle, Recurly, Stripe Billing | Yes | On the stop rule (`no_action: max attempts reached`) call `POST /subscriptions/{id}/pause` or `/cancel` when the attempt carries a `subscription_id`; add that column to `payment_attempts` from the `subscription.halted` webhook. Deterministic, `app/pipeline.py:finish_job`. |
| Hosted card-update page with no login | Paddle, Baremetrics, Churn Buster, Razorpay Subscriptions email | Partly | Razorpay's own card-change link for halted subscriptions is the platform's version; the agent can surface it in the nudge if the URL is available from the subscription entity (unverified). For one-off payments the Payment Link already is the hosted page. |
| Merchant-configurable policy and dashboard | Chargebee, Recurly, Hyperswitch RR, Razorpay Agent Studio | Yes | A per-merchant override layer on `POLICY` (YAML or a `policy_overrides` table) validated against the same schema, rendered by `policy_table()`; the read-only `app/web.py` grows a queue and an edit form. Deterministic. |
| Recovered-revenue reporting and A/B measurement | Stripe, Recurly, Baremetrics, Paddle | Yes | `outcomes` already holds `amount_recovered_paise`; add a `report` CLI over class x action x attempt and a holdout flag on `recovery_jobs` so a fraction of events run the T+1h baseline for real. Deterministic. |
| Cross-merchant network signals (issuer behaviour, customer history elsewhere) | Stripe, Recurly, Chargebee, Optimizer | No | Only the gateway has them. Out of scope by construction. |
| Human voice outreach | Gravy (people), Razorpay agent and Cashfree Relay (synthetic voice) | Partly | Possible as a channel behind the human queue, not automatic: a person in `resolve` picks "call". Synthetic outbound voice in a money path is deliberately not in the table. |
| Approval-first mode for sensitive actions | Razorpay Agent Studio (review-first, WhatsApp escalation) | Yes | Already partly present (RISK_BLOCKED and UNKNOWN go to a person). Add a `REQUIRE_APPROVAL_ABOVE_PAISE` gate in `app/policy.py:decide` that routes high-value links to `human_queue` and a `resolve --approve` path that executes the pending job. Deterministic. |

## What this agent does that they do not, or do not expose

- **Cause-specific policy as a readable table.** Every vendor above says it "retries based on error type"; none publishes the table. Here the eight rows (class, action, delay, backoff, max attempts, nudge, rationale) are in `app/policy.py`, rendered into the README, and the audit row for each decision quotes the rationale string verbatim. A payments reviewer can veto a row; a prompt cannot be vetoed.
- **Classification a reviewer can read.** Thirteen ordered rules with names; `Classification.reason` is `rule <name>: <what it checks>`. Butter and Chargebee Revive classify with a model; Hyperswitch normalises codes but the mapping is internal. Here the rules are scored on a 118-case labelled set (`make eval`) with a pinned regression floor, and a generic "declined by the bank" is UNKNOWN by design rather than a guess.
- **Idempotency in layers, and a chaos harness that proves it.** DB-UNIQUE key inserted before the call, atomic `pending -> executing` claim, the key echoed as Razorpay `reference_id`, and a reconcile-by-`reference_id` step before any follow-up. `make chaos` injects eight faults at the client boundary (LLM timeout, bad JSON, hallucinated class, 429, 5xx, duplicate delivery, unknown code, DB outage) and asserts one-link-per-payment. Vendors presumably have this internally; none of them shows it, and a merchant cannot test it.
- **Deterministic first, model second, human third.** The model is called only for unmapped descriptions and message wording, validated against a closed enum, gated at 0.7, and every failure mode lands in the human queue with a named fallback (`llm_timeout->human_queue`). The Razorpay and Cashfree agents are LLM-first; the dunning vendors are ML-first for timing. Here no money action depends on a model answer.
- **Never retry a risk flag; never silently debit an empty account.** `RISK_BLOCKED` is terminal and `INSUFFICIENT_FUNDS` never token-retries even when a token exists. These are policy invariants with tests, not model tendencies.
- **Open source, runnable offline, honest about what is real.** `make demo` runs with no keys against an in-memory Payment Links fixture; every stub, fixture and unmeasured number is labelled in the README. Hyperswitch is the only other open-source entry, and it is a gateway, not a recovery agent.
- **Test-mode-only by construction.** A non-`rzp_test_` key is refused at startup. No commercial product needs this, but it is why a panel can run the code.

Fair caveats: this agent has no production traffic, no delivery channel, no real token retry, no per-merchant configuration, and its recovery numbers come from a hand-specified simulation. The vendors above have years of outcome data; this agent has an audit table waiting for some.

## Prioritised gaps to close

1. **Real token / mandate re-charge behind the existing stub.** Every competitor's core action is the silent retry; without it the agent recovers only what a customer clicks.
2. **Actual customer delivery with receipts in the trail.** A drafted nudge that is never sent recovers nothing; Razorpay's own `notify` and `notify_by` endpoints make this a configuration change plus one audit stage.
3. **Subscription lifecycle hooks (`subscription.halted` in, pause/cancel out).** Track 03 is subscription revenue; the agent currently sees only one-off `payment.failed` events and never closes the loop on the subscription.
4. **Multi-step campaign column in the policy table.** Every dunning vendor runs 14 to 30 days of escalating contact; the agent stops after one link and up to three re-sends of it.
5. **Per-merchant policy overrides and a recovered-revenue report.** Chargebee, Recurly and Agent Studio all expose configuration and a dashboard; the read-only operator view is not yet either.
6. **Bounded, outcome-learned delays per class.** ML retry timing is the industry's headline feature; a bandit that may only move inside the table's bounds captures the part a merchant-side agent can honestly learn from its own outcomes.
