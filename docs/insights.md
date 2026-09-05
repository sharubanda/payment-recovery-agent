# Insights: learning from outcomes without changing behaviour silently

Two commands read the audit tables back and turn them into evidence for a person. Neither
one writes to the agent's behaviour: `app/policy.py` (the policy table) and `app/classify.py`
(the ordered rules) are code-reviewed files, and every change to them goes through a pull
request with fixture cases. "AI judgment" here means statistics with sample sizes and
intervals, and a human approving every change.

    python -m app.insights        [--write docs/insights_report.md] [--min-samples N] [--merchant ID]
    python -m app.rule_candidates [--write docs/rule_candidates.md] [--min-occurrences N]

Both are also served read-only by the operator view: `GET /insights` and `GET /rule-candidates`
on `python -m app.web` (same numbers, HTML tables, GET query filters only).

## What `insights` measures

From `payment_attempts`, `recovery_decisions`, `recovery_jobs` and `outcomes`, one pass over
the attempts (optionally one merchant), taking each attempt's latest decision and latest job:

| section | numbers |
|---|---|
| per failure class | attempts, sent links, stubbed token retries, human-queued (and share), recovered count and amount, recovery rate with a 95% Wilson interval, average time to recovery (`recovered_at - executed_at` of the recovering job) |
| by delay bucket | for sent links only, the recovery rate + interval per bucket of the delay the job was *scheduled* with: `now`, `<=15m`, `<=1h`, `<=24h`, `<=48h`, `>48h`; `*` marks the bucket the policy table uses today |
| LLM usage | decisions, how many consulted a model (`llm_used`), the share, decisions by `classified_by`, every `fallback_taken` by name and count, median and p95 `llm_latency_ms` |
| money by merchant | at risk (sum of failed amounts), recovered, open (link sent, unpaid), parked (human queue) |
| proposals | one verdict per class, see below, plus a "rules gap" flag |

Definitions that matter:

* **rate = recovered / sent links.** A stubbed token retry cannot recover in test mode (no
  real tokens), so it is not in the denominator; it is counted separately.
* **The bucket is the scheduled delay, not when the job ran.** `demo --execute-now` runs a
  48h job immediately and it still counts as `<=48h`, because the thing under test is the delay
  in the policy table. The delay is read from the decision row for a first attempt and from
  `scheduled_at - created_at` (rounded to the minute) for follow-ups.
* **Human-queue share > 30% flags a rules gap** ("rules gap: see propose-rules") except for a
  class the policy sends to a person by design (RISK_BLOCKED). UNKNOWN is the gap by
  definition: every UNKNOWN in the queue is a description neither the rules nor the model resolved.

## The interval, and why

Every rate is printed with a **95% Wilson score interval**, written by hand in
`insights.wilson_interval` (stdlib only, no scipy):

    centre = (p + z²/2n) / (1 + z²/n)
    half   = z · sqrt(p(1-p)/n + z²/4n²) / (1 + z²/n)
    z = 1.96

Wilson rather than the normal ("Wald") approximation because the numbers in this database are
small. Wald says 3 of 3 is "100% ± 0%" and 0 of 5 is "0% ± 0%", both nonsense; Wilson says
3 of 3 is 100% (44%-100%) and 0 of 5 is 0% (0%-43%), and never leaves [0, 1]. With n = 0 the
function returns (0, 1): no trials means the rate could be anything, never a point at zero.
The test file pins the interval against known values (5/10, 50/100, 0/30, 30/30, 1/1).

## The proposal rule

For each class with an automated first attempt, `compare_buckets` takes the bucket the policy
table uses and every other bucket that has data, and:

* proposes a change **only** when both buckets have `n >= --min-samples` (default 30) **and**
  their 95% intervals do not overlap (the competitor's lower bound is above the current
  bucket's upper bound);
* reports "evidence supports the current bucket" when every comparable bucket is clearly worse;
* says **`insufficient evidence (n=..)`** for everything else: too few samples in either bucket,
  overlapping intervals, or no other bucket ever tried. The text always carries the n so the
  reader knows how far from a decision the data is.

RISK_BLOCKED and UNKNOWN have no automated retry to tune, so they get "not applicable".

## Why nothing auto-applies

A proposal is a line of text. It is printed, and with `--write` it goes into a Markdown file.
It is never fed back into `app/policy.py`: that table is code-reviewed, and the review is
where a person weighs the interval against what the numbers cannot see (a merchant's salary
cycle, an issuer's incident that week, the double-charge risk of a shorter delay). This report
is evidence for that reviewer, **not a bandit**. There is no explore/exploit loop, no
per-class weight that drifts, and the agent never simulates its own evidence: the only
synthetic-outcome helper (`simulate_outcomes`) lives in `tests/test_insights.py`.

The same gate applies to the classifier. `rule_candidates` groups `llm`/`fallback` decisions by
a normalised description signature (lowercase, ids stripped, digits -> `#`, whitespace
collapsed) plus `(error_code, error_source, error_step, error_reason)`. A group becomes a
candidate at `--min-occurrences` (default 3) decisions when the model's classifications agree
on one class with mean confidence >= 0.85; a fallback-only group becomes "recurring unmapped:
needs a human label". Each candidate prints the signature, count, proposed class (or
`unlabelled`), up to three example descriptions (descriptions only, never a name, contact or
email), the structured fields, and a ready-to-paste `Rule(...)` stub for `app/classify.py` with
the note "review and add tests/fixtures/failure_cases.json cases before adding". Groups that
recur but disagree, or agree with low confidence, are listed as rejected with the reason.
`classify.py` is never modified (a test checks its hash before and after a run).

## `python -m app.insights` on the demo database

22 seed events, `demo` run with the fixture Razorpay client and no LLM key (every UNKNOWN
falls back to a person), the fixture paying every other link. Small n everywhere, so every
tunable class says insufficient evidence. That is the honest result, and
`tests/test_insights.py::test_demo_database_is_honest_about_small_n` asserts it.

```
payment-recovery-agent insights
  database   : sqlite:///./recovery.db
  generated  : 2026-09-05T14:30:34Z
  min-samples: 30
  Every number below is an observation with a sample size; nothing here changes the agent. The policy table (app/policy.py) is code-reviewed; this report is evidence for the reviewer, not a bandit.

== per failure class (22 attempts, 15 outcome rows)
class               attempts  sent  stubbed  human  human%  recovered        amount  rate (95% Wilson)    avg time-to-recovery  flag
------------------  --------  ----  -------  -----  ------  ---------  ------------  -------------------  --------------------  ----------------------------
INSUFFICIENT_FUNDS         3     3        0      0      0%          2  Rs 15,498.00  67% (21%-94%) n=3    0s
ISSUER_DOWN                3     2        1      0      0%          1     Rs 799.00  50% (9%-91%) n=2     0s
AUTH_ABANDONED             4     4        0      0      0%          2  Rs 33,998.00  50% (15%-85%) n=4    0s
HARD_DECLINE               3     3        0      0      0%          1     Rs 399.00  33% (6%-79%) n=3     0s
RISK_BLOCKED               2     0        0      2    100%          0       Rs 0.00  -                    -
NETWORK_TIMEOUT            2     1        1      0      0%          1     Rs 199.00  100% (21%-100%) n=1  0s
LIMIT_EXCEEDED             2     2        0      0      0%          1  Rs 22,999.00  50% (9%-91%) n=2     0s
UNKNOWN                    3     0        0      3    100%          0       Rs 0.00  -                    -                     rules gap: see propose-rules
  rate = recovered / sent links (a stubbed token retry cannot recover in test mode, so it is not in the denominator); interval = 95% Wilson score. * marks the bucket the policy table uses today.

== recovery rate by scheduled delay bucket (sent links only)
class               policy bucket  policy delay  now                <=15m                 <=1h  <=24h              <=48h               >48h
------------------  -------------  ------------  -----------------  --------------------  ----  -----------------  ------------------  ----
INSUFFICIENT_FUNDS  <=48h          48h           -                  -                     -     -                  *67% (21%-94%) n=3  -
ISSUER_DOWN         <=15m          15m           -                  *50% (9%-91%) n=2     -     -                  -                   -
AUTH_ABANDONED      <=15m          10m           -                  *50% (15%-85%) n=4    -     -                  -                   -
HARD_DECLINE        now            now           *33% (6%-79%) n=3  -                     -     -                  -                   -
NETWORK_TIMEOUT     <=15m          5m            -                  *100% (21%-100%) n=1  -     -                  -                   -
LIMIT_EXCEEDED      <=24h          24h           -                  -                     -     *50% (9%-91%) n=2  -                   -

== LLM usage
decisions  model consulted  share  rules  llm  fallback  latency median  latency p95
---------  ---------------  -----  -----  ---  --------  --------------  -----------
       22                0     0%     19    0         3  -               -
fallback_taken                count
----------------------------  -----
llm_unavailable->human_queue      3

== money by merchant
merchant         attempts         at risk     recovered  open (sent, unpaid)  parked (human queue)
---------------  --------  --------------  ------------  -------------------  --------------------
merchant_acme           8    Rs 62,092.00  Rs 50,497.00          Rs 7,498.00           Rs 1,599.00
merchant_bolt           7    Rs 39,093.00   Rs 9,198.00          Rs 5,397.00          Rs 24,498.00
merchant_zenith         7    Rs 26,603.00  Rs 14,197.00         Rs 11,458.00             Rs 948.00
TOTAL                  22  Rs 1,27,788.00  Rs 73,892.00         Rs 24,353.00          Rs 27,045.00

== proposals (never applied)
  A change is proposed only when the policy bucket and a competing bucket both have n >= 30 and their 95% intervals do not overlap. Proposals are printed, never applied.
  INSUFFICIENT_FUNDS   insufficient evidence (n=3 in the policy bucket <=48h, need 30; no other bucket tried)
  ISSUER_DOWN          insufficient evidence (n=2 in the policy bucket <=15m, need 30; no other bucket tried)
  AUTH_ABANDONED       insufficient evidence (n=4 in the policy bucket <=15m, need 30; no other bucket tried)
  HARD_DECLINE         insufficient evidence (n=3 in the policy bucket now, need 30; no other bucket tried)
  RISK_BLOCKED         no automated retry in the policy table (human decides); nothing to tune
  NETWORK_TIMEOUT      insufficient evidence (n=1 in the policy bucket <=15m, need 30; no other bucket tried)
  LIMIT_EXCEEDED       insufficient evidence (n=2 in the policy bucket <=24h, need 30; no other bucket tried)
  UNKNOWN              no automated retry in the policy table (human decides); nothing to tune
  rules gap (human-queue share > 30%): UNKNOWN -> python -m app.rule_candidates
  0 proposal(s); 6 class(es) with insufficient evidence.
```
