# The two LLM call sites

A model is consulted in exactly two places (`app/llm.py`): `classify_unmapped`, for an error description no rule in `app/classify.py` matched, and `draft_nudge`, for the wording of the customer message after a link exists. Everything else is deterministic. Both sites never raise; every failure mode degrades to a fixed fallback with a stable name that the audit trail and the chaos harness assert on. This document states the request each site makes, why the cached part of it is byte-stable, how examples are chosen, what is counted, how language is handled, every rule a model answer must pass, every fallback name, what the model is and is not shown, and what was and was not run.

**Disclosure.** No call was made against the real Anthropic API from this environment: there is no key here and the network is closed. `AnthropicLLM` is exercised by `tests/test_llm.py` against a fake `messages.create` that records its keyword arguments (`test_anthropic_llm_request_shape_and_response_parsing`); the shape below is what that test pins, not what a live response looked like. Latency, token counts and cache hit rates are therefore unknown; the code records them so they can be known once a key is in `.env`.

## 1. The request shape (both sites)

`AnthropicLLM.complete_json(system, user, schema)` makes one `messages.create` call (anthropic 1.4.0):

```python
resp = client.messages.create(
    model=self.model,                         # config.LLM_MODEL, default "claude-opus-5"
    max_tokens=MAX_TOKENS,                    # 512
    system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
    messages=[{"role": "user", "content": user}],
    output_config={"format": {"type": "json_schema", "schema": schema}, "effort": "low"},
)
```

- The client is `anthropic.Anthropic(timeout=config.LLM_TIMEOUT_SECONDS, max_retries=1)` (`.env`: `LLM_TIMEOUT_SECONDS=8`), constructed on first use so importing the module needs no key.
- No `thinking`, no `temperature`, no assistant prefill; the test asserts `"thinking" not in kw and "temperature" not in kw`. The model runs with its defaults apart from `effort: "low"`.
- The whole system text is **one** block with `cache_control: {"type": "ephemeral"}`; the user turn is plain text and carries only the per-event fields.
- `output_config.format` is a JSON schema with `additionalProperties: False` and a closed `enum` on the class (`CLASSIFY_SCHEMA`) or the channel (`NUDGE_SCHEMA`). Numeric bounds are not in the schema (`test_classify_schema_is_closed_and_bound_free`: the API's structured-output schema does not support `minimum`/`maximum`), so `confidence` in 0..1 is enforced by pydantic after the fact.
- The response's first `text` block is the answer; a leading `thinking` block is skipped. `resp.model`, `stop_reason` and `usage` (`input_tokens`, `output_tokens`, `cache_read_input_tokens`) are read defensively, missing values count as 0 (`test_anthropic_llm_usage_defaults_to_zero_when_the_response_has_none`).
- `MAX_TOKENS = 512`: a classification is one line and a nudge is at most a short email. A `stop_reason` of `max_tokens` is treated as truncation and rejected, not parsed.

Exceptions propagate out of `complete_json`; the two callers own the degrade decision (`test_anthropic_llm_errors_propagate_to_the_caller_which_degrades`).

### One call, at most one repair

`_Call.ask_validated` makes one call, parses the answer, and on a `_Rejected` answer makes **exactly one** repair call whose user turn is the original user turn plus `Your previous answer was rejected: <why>.\nIt was:\n<raw>\n\nReturn only the JSON object matching the schema.` A second rejection is final. A refusal or truncation (`_Stopped`) and any exception from the client end the interaction without a repair (`test_timeout_falls_to_human_queue_after_one_call`; `test_bad_json_gets_one_repair_call_then_falls_to_human_queue`). The system block is identical on the repair call; only the user turn grows.

## 2. The cached prefix and why it is byte-stable

Prompt caching is a prefix match: the system block is served from cache only if it is byte-identical to a recent request's. Both system texts are therefore built so that nothing per-event, per-process or per-time can reach them.

**`CLASSIFY_SYSTEM`** is `build_classify_system(FEW_SHOT_EXAMPLES)`, computed once at import: the constant `_CLASSIFY_RULES` (the taxonomy, one line per class, the rules of thumb), then, when examples exist, a fixed sentence introducing the labelled examples, then one line per example `- "<description>" -> <CLASS>`. `build_classify_system` is a pure function of its input; `FEW_SHOT_EXAMPLES` is a deterministic selection (section 3) from a file that is part of the repository. Two processes with the same checkout and the same `LLM_FEW_SHOT_PER_CLASS` therefore produce the same bytes (`test_classify_system_prompt_is_byte_stable_and_has_examples_for_every_class`: the prompt built from the fixture in file order equals the one built from the fixture reversed equals the module constant), and within one process every call sends the same system text while the user turn differs (`test_cached_prompts_carry_no_pii_and_only_the_user_turn_varies`).

Exact size of the cached block for `classify_unmapped`, from this checkout with the default `LLM_FEW_SHOT_PER_CLASS=2`:

```
$ python -c "from app.llm import CLASSIFY_SYSTEM; print(len(CLASSIFY_SYSTEM), len(CLASSIFY_SYSTEM.encode()))"
3635 3635
```

3635 characters, 3635 bytes (pure ASCII). Its token count was not measured (that needs the token-counting endpoint, which needs a key); by a rough four-characters-per-token estimate it is on the order of 900 tokens, which is above the 512-token minimum a prefix must reach to be cached on `claude-opus-5`. That minimum is model-dependent and higher on some other models, so `LLM_MODEL` changes can silently turn caching off; the only proof of a hit is a non-zero `cache_read_input_tokens` in `usage_log`.

**`NUDGE_SYSTEM`** is a module-level f-string interpolating three constants (`SMS_MAX_CHARS`, `SMS_MAX_CHARS_UNICODE`, `SUBJECT_MAX_CHARS`) and nothing else; it carries the language rules, the hard rules and the answer shape. It is 1851 characters, 1861 bytes (it contains the Devanagari words आप and तुम in the register rule). That is likely below the cacheable minimum on `claude-opus-5`, so the nudge block should be expected **not** to cache; the marker costs nothing and the block is still stable, so it will cache if it ever grows past the minimum. Neither system text carries a name, contact, email, ten-digit number or `@` (`test_cached_prompts_carry_no_pii_and_only_the_user_turn_varies`).

What would break the prefix: editing `_CLASSIFY_RULES` or `NUDGE_SYSTEM` (intended), editing or re-labelling the first cases per class in `tests/fixtures/failure_cases.json`, changing `LLM_FEW_SHOT_PER_CLASS`, or deploying without the fixture file (section 3). `LLM_MODEL` does not change the bytes but caches are per model.

## 3. Few-shot selection and `LLM_FEW_SHOT_PER_CLASS`

`select_few_shot_examples(cases, per_class)`: for each class in `FailureClass` declaration order, take the cases whose `expected_class` is that class, **sort them by `id`** (string order), and keep the first `per_class` whose `error_description` is non-empty after whitespace normalisation (internal newlines and runs of spaces collapse to one space). Never random, never dependent on file order. UNKNOWN cases are included on purpose so the model sees what must stay UNKNOWN. A class with fewer usable cases than `per_class` contributes fewer lines; an unknown `expected_class` is ignored (`test_few_shot_selection_skips_empty_descriptions_and_honours_per_class`).

`per_class` is `FEW_SHOT_PER_CLASS = max(0, int(_setting("LLM_FEW_SHOT_PER_CLASS", "2")))`, read **once at import**. `_setting` looks at `config.<NAME>` first (not declared in `app/config.py` today), then the environment (which includes `.env`, because `config.py` runs `load_dotenv` at import), then the default. `0` renders the rules with no examples (`build_classify_system([])` is the rules text alone). The source file is `tests/fixtures/failure_cases.json` (`FEW_SHOT_FIXTURE`); if it cannot be read, a warning is logged and the prompt carries no examples rather than failing (`_load_few_shot`), so a packaged deploy without `tests/` still classifies, uncalibrated.

With the shipped fixture and the default 2 per class, `FEW_SHOT_EXAMPLES` has 16 entries, in this order: two each for INSUFFICIENT_FUNDS (`IF-01`, `IF-02`: "Your payment could not be completed due to insufficient funds..." and "Transaction failed as the account has insufficient balance..."), ISSUER_DOWN, AUTH_ABANDONED, HARD_DECLINE, RISK_BLOCKED (including the ISO 8583 "59 Suspected fraud"), NETWORK_TIMEOUT, LIMIT_EXCEEDED, and UNKNOWN (`UK-01`, `UK-02`: two bare "declined by the bank" messages). The test pins the first example to `IF-01` and the second-to-last to `UK-01`.

## 4. Usage accounting

Every call to the client, successful or not, appends one dict to `llm.usage_log` and emits one INFO line on the `app.llm` logger (`_Call.ask`, in a `finally`):

```
{"call_site": "classify_unmapped" | "draft_nudge", "client": "anthropic" | "scripted" | "faulting",
 "model": <served model, or the configured one before any answer>, "attempt": 1 | 2,
 "latency_ms": <this call>, "stop_reason": "end_turn" | "max_tokens" | "refusal" | "",
 "error": None | "<ExceptionType>", "input_tokens": n, "output_tokens": n, "cache_read_input_tokens": n}
```

```
llm call site=classify_unmapped client=anthropic model=claude-test-served attempt=1 latency_ms=12 input_tokens=1200 output_tokens=40 cache_read_input_tokens=1100 stop_reason=end_turn error=-
```

The list is capped at `USAGE_LOG_MAX = 1000` entries, oldest dropped (`test_usage_log_is_capped`). `last_call_usage()` returns a **copy** of the newest entry or `None`; `clear_usage_log()` empties it. Test doubles report zero tokens (`test_usage_log_records_every_call_with_zero_tokens_for_doubles`). The pipeline does **not** persist any of this: `recovery_decisions` stores `llm_model`, `llm_latency_ms` (summed over the attempt and its repair), `confidence`, `classified_by`, `llm_used` and `fallback_taken`; the `nudge` audit row stores `source`, `fallback_taken` and `reason`. Token counts live only in process memory and the log line until the pipeline is changed to write them (ARCHITECTURE lists this under "What I'd rebuild"). `Classification.llm_latency_ms` and `llm_model` are set even on a fallback, provided at least one call was made (`test_bad_json_gets_one_repair_call_then_falls_to_human_queue`), so a slow failure is visible as such.

## 5. Site 1: `classify_unmapped`

Input: the attempt. The user turn (`_classify_user_content`) is exactly these lines, and nothing else:

```
Failed payment error object:
error_code: <error_code>
error_source: <error_source>
error_step: <error_step>
error_reason: <error_reason>
error_description: <error_description>
payment_method: <method>
amount: Rs 2,499.00 (249900 paise)
```

Output schema: `{"failure_class": <enum of FailureClass values>, "confidence": number, "rationale": string}`, all required, no extras. Parsing (`_parse_classification`): `json.loads` -> must be an object -> pydantic `_ClassifyAnswer` with `extra="forbid"`, `failure_class: FailureClass`, `confidence` in `[0, 1]`. A `failure_class` outside the enum is `llm_hallucinated_class`; any other schema problem (range, missing field, extra field, not an object) is `llm_bad_json` (`test_hallucinated_class_is_rejected_not_coerced`, `test_schema_violations_fall_to_human_queue`). A valid but low-confidence answer is returned as-is with `source="llm"`: the 0.7 gate is applied by `policy.decide`, so the trail shows the model's real number (`test_low_confidence_is_reported_honestly_not_rewritten`).

Result on success: `Classification(failure_class, reason=rationale, source="llm", confidence, llm_model, llm_latency_ms)`. Result on any failure: `Classification(UNKNOWN, source="fallback", confidence=0.0, fallback_taken=<kind>->human_queue, reason=<why>)`, which the confidence gate turns into `human_queue`.

| `fallback_taken` | when |
|---|---|
| `llm_unavailable->human_queue` | `get_client()` is `None`: no `ANTHROPIC_API_KEY`, or the `unknown_error_code` fault is active |
| `llm_timeout->human_queue` | the client raised `TimeoutError` or `anthropic.APITimeoutError` (one call, no repair) |
| `llm_error->human_queue` | any other exception from the client: `RateLimitError` (one call, no retry loop), other `APIStatusError`, `APIConnectionError`, a client bug, a double that ran dry; also an unexpected crash anywhere in the function |
| `llm_bad_json->human_queue` | malformed JSON, not an object, or a schema violation other than the class, after the repair call failed too |
| `llm_hallucinated_class->human_queue` | `failure_class` not in the enum, after the repair |
| `llm_refusal->human_queue` | `stop_reason == "refusal"` |
| `llm_truncated->human_queue` | `stop_reason == "max_tokens"` |

Tests: `test_scripted_happy_path_reports_llm_source_model_and_latency`, `test_bad_json_repaired_on_second_call_is_accepted`, `test_sdk_timeout_and_api_errors_map_to_documented_fallbacks`, `test_refusal_and_truncation_stop_reasons_fall_back`, `test_no_client_means_llm_unavailable_fallback`, `test_classifier_never_raises_on_a_broken_client`.

## 6. Site 2: `draft_nudge`

Input: the attempt, the failure class, the action, the link URL (or `None`), an optional `language`. Before any model call:

1. `RISK_BLOCKED`, `UNKNOWN`, and the actions `human_queue`, `no_action`, `token_retry` are **neutral** (`nudge_templates.is_neutral`): the customer gets the template's "our team will get in touch" message with no link, `source="template"`, `fallback_taken=None`, and the model is not called (`test_risk_blocked_and_human_queue_never_call_the_model`).
2. No client -> `llm_unavailable->template`.
3. Otherwise the channel is decided here, not by the model: `sms` when the attempt has a `customer_contact`, else `email` (`channel_for`).

The user turn (`_nudge_user_content`):

```
channel: sms
language: hi (Hindi, Devanagari script)
customer_first_name: Priya
order_id: order_NfKp9wQ2sYd7Lm
amount: Rs 2,499.00
what_happened: the payment did not go through because the account did not have enough balance at the time
what_happened_in_hi: खाते में पर्याप्त राशि न होने से पूरा नहीं हो सका
what_to_do: open the link and complete the payment
link: https://rzp.io/i/wZMGIy1X
```

`what_happened` is always the English `plain_phrase`; `what_happened_in_<lang>` is the same class phrase in the chosen language (the same string the template uses, so model and template say the same thing about the same failure, `test_plain_phrase_matches_what_the_template_says`). `what_to_do` is `use a different card or payment method ...` for `nudge_change_method`, else `open the link and complete the payment`. With no link the line reads `link: (none: tell the customer to retry from the checkout page)`.

Output schema: `{"channel": "sms" | "email", "subject": string, "body": string}`, all required, no extras. After the schema, `_nudge_parser` applies every one of these rules, collects every failure, and rejects with all of them joined by `; ` (so the repair turn names each problem):

| rule | check |
|---|---|
| channel | `channel` equals the one decided by `channel_for` |
| body present | body non-empty after strip |
| the amount, verbatim | `format_rupees(amount_paise)` (e.g. `Rs 2,499.00`) appears in the body |
| no other amount | no rupee amount whose digits differ appears in subject or body: `Rs`, `Rs.`, `INR`, `₹`, `रु` followed by digits (`_AMOUNT_RE`); `₹2,499` next to `Rs 2,499.00` is a different rendering of the same number and is still rejected as "a second amount" (`test_other_amounts_and_promises_are_rejected_then_template`, `test_same_amount_repeated_or_in_the_subject_is_not_a_different_amount`) |
| the link, verbatim | when a link was given, it appears in the body |
| SMS length | `len(body) <= sms_limit_for(body)`: 300 when every character is Latin-1, else 200 (section 7) |
| email subject | non-empty and at most 60 characters (`SUBJECT_MAX_CHARS`) |
| no risk words | none of `fraud`, `risk`, `suspicious`, `suspect`, `blocked`, `blacklist` in the lower-cased subject+body (substring match: `unblocked` trips it) |
| no promises | none of `refund`, `guarantee`, `within 24 hours`, `रिफंड`, `रिफ़ंड`, `गारंटी`, `24 घंटे`, `24 ghante` |
| no emoji | no code point at or above U+1F000, and none in U+2600..U+27BF |
| script matches language | `hi` requires at least one Devanagari character; `en` and `hinglish` require none. Only presence is checked: a mostly English body with one Devanagari word passes as Hindi |

On success the subject is forced to `""` for SMS and `Nudge(channel, subject, body, source="llm")` is returned. On failure the template answers **in the same language** (`_nudge_fallback` passes `lang` through), with `source="template"`, `fallback_taken=<kind>->template`, and `reason` carrying the detail so a bug is distinguishable from a network error in the `nudge` audit row (`fallback: llm_output_rejected->template` is what the trail shows).

| `fallback_taken` | when |
|---|---|
| `llm_unavailable->template` | no client |
| `llm_timeout->template` | `TimeoutError` / `APITimeoutError` |
| `llm_error->template` | any other client exception, or a crash in the function |
| `llm_bad_json->template` | malformed JSON, not an object, or a schema violation (an unknown channel such as `whatsapp`, a missing field, an extra field); this is also what the `llm_hallucinated_class` fault produces at this site, because a classification object fails the nudge schema |
| `llm_output_rejected->template` | any content rule above, after the repair |
| `llm_refusal->template` | `stop_reason == "refusal"` |
| `llm_truncated->template` | `stop_reason == "max_tokens"` |
| `template_error(<ExceptionType>)->neutral` | `template_nudge` itself crashed (it is wrapped; the neutral English email is returned); only reachable through a broken attempt object |

Tests: `test_draft_nudge_happy_path_includes_link_and_amount`, `test_draft_nudge_email_channel_when_no_contact`, `test_draft_nudge_rejects_bad_output_then_uses_template` (link missing, wrong amount, too long, wrong channel, forbidden word, emoji, malformed, schema), `test_draft_nudge_repair_can_succeed`, `test_draft_nudge_timeout_and_errors_use_template`, `test_draft_nudge_without_client_uses_template`, `test_draft_nudge_never_raises`, `test_language_rules_are_enforced_then_the_template_answers_in_the_same_language`, `test_latin_sms_keeps_the_300_budget_and_unicode_gets_200`.

Where it runs: `pipeline.attach_nudge`, after `execute_job` returned a `sent` link job (so the real `short_url` is in the body) and never before, so message generation cannot block a recovery; the call is synchronous and bounded by `LLM_TIMEOUT_SECONDS` times the SDK's one retry, per call, at most two calls. The result is stored on the job (`nudge_channel`, `nudge_subject`, `nudge_body`, `nudge_source`) and never sent.

## 7. Language

**Supported:** `en` (Indian English), `hi` (Hindi, Devanagari), `hinglish` (romanised Hindi, Latin script). `normalise_language` lower-cases, turns `_` into `-`, applies the alias table (`english`, `en-in` -> `en`; `hindi`, `hi-in`, `hin` -> `hi`; `romanised-hindi`, `romanized-hindi`, `hi-latn` -> `hinglish`), then accepts a supported code or the part before the first `-` (`en-GB` -> `en`); anything else is `None`, never an error.

**Resolution order** (`preferred_language(attempt, language)`): the explicit `language` argument (the pipeline passes `attempt.customer_language`, else the merchant file's `nudge_language`, see docs/merchants.md), then `attempt.customer_language`, then `NUDGE_LANGUAGE_DEFAULT` (a declared `config` attribute first, then the environment/`.env`, default `en`), then `en`; an unsupported value at any level falls through to the next (`test_preferred_language_resolution_order`, `test_language_resolution_explicit_then_attempt_then_default`). Two facts about how this reaches the pipeline: `PaymentAttempt` has **no** `customer_language` column, so on a real row that level is always empty; and `pipeline.attach_nudge` calls `draft_nudge` without a `language`. In the pipeline as shipped, therefore, every message is in `NUDGE_LANGUAGE_DEFAULT`; the per-attempt and per-call levels exist for callers and tests. The language rules live in the cached system block; the user turn only names the language (`test_draft_nudge_in_hindi_tells_the_model_and_accepts_a_devanagari_body`), and the same language reaches the template on fallback.

**SMS length, and why 300 and 200.** A message made only of characters an SMS carrier can encode in GSM-7 costs 160 characters per segment, 153 when concatenated, so 300 characters is about two segments. Any character outside Basic Latin / Latin-1 (Devanagari, the rupee sign `₹`, a curly quote) forces UCS-2, 70 characters per segment, 67 concatenated, so a Devanagari body is capped at 200 characters, about three segments. `is_latin_script` is `all(ord(ch) <= 0xFF)`, and `sms_limit_for(body)` returns 300 or 200 on that basis; one curly quote in an otherwise Latin body switches the budget (`test_sms_limit_follows_the_script`). This is why amounts render as `Rs 2,499.00` and never `₹`: the sign alone would push every SMS into UCS-2. Longer messages cost more and can arrive as several parts out of order on some handsets. The template tries `sms`, then `sms_compact` (no greeting), then `sms_minimal` (no cause) against the budget its script allows, and only then hard-cuts (`test_sms_compacts_by_dropping_greeting_then_cause_before_the_hard_cut`); the model is told the same limits in `NUDGE_SYSTEM` and rejected if it exceeds them.

**Rendered templates** (from this checkout, `template_nudge` with the example attempt, INSUFFICIENT_FUNDS, `recovery_link`, link `https://rzp.io/i/wZMGIy1X`; the same message the model's fallback would produce):

Hindi SMS, 182 characters (budget 200):

```
नमस्ते Priya, ऑर्डर order_NfKp9wQ2sYd7Lm के लिए आपका Rs 2,499.00 का भुगतान खाते में पर्याप्त राशि न होने से पूरा नहीं हो सका। इस सुरक्षित लिंक से पूरा करें: https://rzp.io/i/wZMGIy1X
```

Hinglish SMS, 241 characters (budget 300):

```
Namaste Priya, order order_NfKp9wQ2sYd7Lm ke liye aapka Rs 2,499.00 ka payment poora nahi ho saka kyunki us samay account mein paryapt balance nahi tha. Aap is secure link se ise ek minute mein poora kar sakte hain: https://rzp.io/i/wZMGIy1X
```

For comparison, the English SMS for the same inputs is 230 characters. A Hindi email for HARD_DECLINE / `nudge_change_method` (no phone on the attempt) renders subject `ज़रूरी: Rs 2,499.00 का भुगतान पूरा नहीं हुआ` and a five-paragraph body ending `धन्यवाद।`. English is the fallback per template piece, not per message: a language lacking a phrase for one class gets that phrase in English and everything else in its own language (`test_english_is_the_fallback_per_piece_when_a_language_lacks_a_class`). Every class x channel x language renders within budget with the amount, order id and first name present and no forbidden word (`tests/test_nudge_templates.py::test_every_class_channel_language_renders_within_budget`).

## 8. Faults and client selection

`get_client()` order: an active `llm_timeout`, `llm_bad_json` or `llm_hallucinated_class` fault -> `FaultingLLM(fault)`; an active `unknown_error_code` fault -> `None` (the case "no rule matches and there is no model to ask"); `config.llm_live()` (a non-empty `ANTHROPIC_API_KEY`) -> `AnthropicLLM()`; else `None`. Faults beat keys, so the chaos harness never reaches the network (`test_get_client_honours_faults_over_config`, `test_unknown_error_code_fault_forces_no_client_even_with_a_key`).

`FaultingLLM` fails **every** call the same way, so the one repair fails too: `llm_timeout` raises `TimeoutError`; `llm_bad_json` answers a truncated object; `llm_hallucinated_class` answers `CUSTOMER_MOVED_ABROAD` at confidence 0.97. Per site (`test_each_llm_fault_produces_its_documented_fallback`):

| fault | `classify_unmapped` | `draft_nudge` | model calls per site |
|---|---|---|---|
| `llm_timeout` | `llm_timeout->human_queue` | `llm_timeout->template` | 1 (an exception is not repairable) |
| `llm_bad_json` | `llm_bad_json->human_queue` | `llm_bad_json->template` | 2 |
| `llm_hallucinated_class` | `llm_hallucinated_class->human_queue` | `llm_bad_json->template` | 2 |
| `unknown_error_code` | `llm_unavailable->human_queue` | `llm_unavailable->template` | 0 |

`ScriptedLLM` is the test double: it answers a list in order, raises an item that is an exception, returns an `LLMResponse` item verbatim (to script a refusal or truncation), and records every `(system, user, schema)` it was given. `docs/failure_report.md` sections 1-3 and 7 are the harness output for these faults.

## 9. What the model is shown, and what it is not

- **Classification** sends the six error fields, the payment method and the amount. It does not send the customer's name, contact or email, the order id, the payment id or the merchant id (`test_classification_prompt_carries_error_fields_but_no_pii` checks that the name, first name, phone and email are absent from system and user text). `error_description` is text relayed from a bank and is sent as-is; the system block tells the model to treat it as data, not instructions.
- **Nudge** sends the channel, the language, the customer's **first name only** (`first_name`), the order id, the formatted amount, the class phrase in English and in the language, the action phrase and the link URL. It does not send the phone number or the email address: the channel is chosen in-process from whether a contact exists (`test_draft_nudge_happy_path_includes_link_and_amount` asserts `CONTACT not in user and EMAIL not in user`).
- The **repair turn** quotes the model's own rejected output back to it, so whatever the model wrote in the first answer is sent again; nothing new from the attempt is added.
- Neither system block contains anything about any customer (section 2).
- The **audit trail** stores the drafted body, subject and the rejection reason (`nudge` row `data_json`), and the classification rationale (`llm` row). Those rows are what the operator view shows.

## 10. Configuration

| key | default | read by |
|---|---|---|
| `ANTHROPIC_API_KEY` | empty (no model) | `config.llm_live` |
| `LLM_MODEL` | `claude-opus-5` | `AnthropicLLM.model` |
| `LLM_TIMEOUT_SECONDS` | `8` | `anthropic.Anthropic(timeout=...)`, with `max_retries=1` |
| `LLM_MIN_CONFIDENCE` | `0.7` (code constant, not env) | `policy.decide` |
| `LLM_FEW_SHOT_PER_CLASS` | `2` | `llm.FEW_SHOT_PER_CLASS` at import; not declared in `config.py`, read through `_setting` |
| `NUDGE_LANGUAGE_DEFAULT` | `en` | `nudge_templates.preferred_language`; same lookup |

`MAX_TOKENS` (512), `USAGE_LOG_MAX` (1000), `effort: "low"`, the SMS budgets (300 / 200) and the subject limit (60) are code constants.

## 11. Recording and replaying real calls

The disclosure at the top stands until someone with a key records the model path. `app/llm_cassette.py` makes that one command: `CassetteLLM` wraps any `LLMClient`, and `get_client()` applies it after faults are settled (a fault still beats it, `test_get_client_off_is_the_old_behaviour_and_faults_beat_the_cassette`).

**Modes** (`LLM_CASSETTE_MODE`: a declared `config` attribute first, then the environment, default `off`):

| mode | `get_client()` returns | on each call |
|---|---|---|
| `off` | the live `AnthropicLLM` or `None`, unchanged | passthrough |
| `record` | `CassetteLLM(AnthropicLLM())` when a key exists, else `None` (nothing to record) | calls the model, appends one record to the cassette, returns the response unchanged; an exception is not recorded and propagates as before |
| `replay` | `CassetteLLM(NullLLM())`, key or no key | answers from the cassette by key; a request with no record raises `CassetteMiss`, which the call sites turn into `llm_cassette_miss->human_queue` / `llm_cassette_miss->template`. `NullLLM` raises on any call, so a replay cannot reach the network even on a bug (`test_replay_miss_is_its_own_fallback_at_both_sites_and_never_reaches_the_network`) |

An unknown mode is a `ValueError` from `get_client()`, which the sites' outer `try` turns into `llm_error->human_queue` / `->template` with the reason naming `LLM_CASSETTE_MODE`.

**File** (`LLM_CASSETTE_PATH`, default `tests/fixtures/llm_cassettes/default.jsonl`; a relative value is relative to the repository root): JSON Lines, one record per call including repair calls, appended, last record per key wins on replay:

```
{"key": <sha256 of (site, system, user, schema JSON with sorted keys)>, "site": "classify_unmapped" | "draft_nudge",
 "client": "anthropic" | "scripted", "model": <served model>,
 "request": {"system": ..., "user": ..., "schema": {...}},
 "response": {"text", "model", "latency_ms", "stop_reason", "input_tokens", "output_tokens", "cache_read_input_tokens"},
 "recorded_at": <UTC ISO 8601>}
```

The key is exact on purpose: both system texts are byte-stable (section 2) and the user turn is a pure function of the event fields, so replaying the same event hits, and any edit to a prompt, the few-shot selection or the schema misses (a person decides) rather than replaying a stale answer. `client` is the inner client's name, so a cassette says where it came from.

**The three commands:**

```
LLM_CASSETTE_MODE=record ANTHROPIC_API_KEY=... python scripts/eval_llm.py --record   # 1. record: the UNKNOWN-by-design cases through the real model
LLM_CASSETTE_MODE=replay python -m app.cli process ...                              # 2. replay: the same answers in the pipeline, no key, no network
python scripts/eval_llm.py --write docs/llm_eval.md                                 # 3. evaluate offline from the cassette
```

`scripts/eval_llm.py` runs `classify_unmapped` over the UNKNOWN-by-design cases of `tests/fixtures/failure_cases.json` (the only cases a production event would send to the model; `--include-mapped` scores every case to measure agreement with the rules' labels, at the cost of 103 more recordings). Per case it reports the label, the model's class and confidence, and whether policy would hand the event to a person (confidence below 0.7, an UNKNOWN answer, or a fallback); overall it reports agreement with the labels, that gate rate, and the average latency and token counts **as recorded in the cassette** (the wall-clock a replay measures is meaningless). The amount in every eval prompt is a fixed `Rs 2,499.00`, since the fixture has none.

**PII.** The cassette stores the prompts verbatim. By design they carry no contact details: the classification prompt has no customer field at all, and the nudge prompt carries the first name only (section 9). `test_recorded_prompts_carry_no_contact_details` records both sites for an attempt with a name, phone and email and asserts that the phone, the email, the full name, the payment id and the merchant id are absent from the file. A recorded cassette is still a transcript of bank error text and customer first names: keep it out of a public repository unless that is acceptable.

**What the repository ships with: no recorded cassette.** `tests/fixtures/llm_cassettes/default.jsonl` does not exist (`test_default_cassette_is_not_shipped`), `python scripts/eval_llm.py` prints `no cassette recorded yet; run with --record and a key` and exits 0, and `--write` produces a page that says exactly that and claims nothing about the model. The only cassette in the tree, `tests/fixtures/llm_cassettes/synthetic.jsonl`, is written by `ScriptedLLM` in record mode (`tests/test_llm_cassette.py::write_synthetic_cassette`); every record says `"client": "scripted"`, the eval labels it SYNTHETIC, and it exists to prove record -> replay -> eval works, not to say anything about a model. If the UNKNOWN cases in the fixture change, the keys change; regenerate it with that function.
