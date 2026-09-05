# Narration, scene by scene

Read at a natural pace (about 150 words a minute). One clip per scene; the scene numbers match
`scripts/demo_video.py`. Total is about 4 minutes 40 seconds, leaving room for the open and close on camera.

## Scene 1, on camera, 0:00 to 0:30 (76 words)

A payment fails. Most merchants retry everything an hour later. That is wrong for every cause. Insufficient funds: an hour later the balance is still empty; two days later, after salary, it is not. An issuer outage: fifteen minutes is exactly right. A stolen card: a retry is fraud-adjacent, never. Abandoned at OTP: do not retry, send the customer back to checkout. I built an agent that decides per cause what to do, when, and when to stop. And it cannot double-charge.

## Scene 2, doctor (30 words)

First, the health check. Twelve checks, including a synthetic failed payment pushed through the real pipeline on an in-memory database. If this is green, the agent can run.

## Scene 3, shadow import (58 words)

Before an agent touches a customer, a merchant wants to see the plan. This is shadow mode on a dashboard export. Every stage runs: classification, the policy table, the idempotency key, the timing rules, the reminders. Nothing leaves the process. Each line is a would-be action, and the payload it would have sent is in the audit trail.

## Scene 4, plan (35 words)

The plan, totalled: how many links would have gone out, for how much, and the expected recovery. That number is a simulation prior until the merchant's own outcomes reach thirty per cause, and it says so.

## Scene 5 and 6, live demo (150 words)

Now the live run. Twenty-two realistic Razorpay failure events. Each one is classified by thirteen deterministic rules on Razorpay's error object: code, source, step, reason, description. Eight causes, including limit-exceeded, which is neither an empty account nor a dead card. No model is involved for a known cause. Where a rule fires you see "rules"; where nothing matched you see "fallback", because with no model key configured those go to a person. That is the only place a model classifies, and below point seven confidence a person decides anyway. The action column is a table, not a prompt: forty-eight hours for insufficient funds, shifted to the second of next month if that lands in the salary wait; fifteen minutes with backoff for an issuer outage; a human queue for a risk block. Every link is a real Razorpay Payment Link in test mode, because you cannot re-charge a failed card without a saved token. The outcome column closes the loop.

## Scene 7, audit trail (40 words)

One event, end to end: ingested, classified with the rule that fired, decided with the rationale, scheduled under its idempotency key, executed, the customer message drafted and stored, the outcome. The execute row even admits the demo ran ahead of schedule.

## Scene 8 and 9, paid elsewhere (60 words)

A real payment-failed webhook payload goes in and gets its link. Then the thing every dunning tool gets wrong: the customer paid the order another way. The captured webhook arrives, the pending job is voided, the live link is cancelled at Razorpay, the reminders are voided, and the outcome is closed. Nothing is ever sent twice.

## Scene 10, chaos (65 words)

Failure recovery is scored, so it is tested. Nine faults injected at the client boundary, each asserting an invariant, and the script exits non-zero if any fails. A model timeout: human queue, zero links. Razorpay rate-limiting: backoff, exactly one link. The same event delivered twice: one job, one link, the second skipped by the unique key. The report is committed and reproduces byte for byte.

## Scene 11, simulation (70 words)

The number, labelled honestly. Five hundred synthetic failure events with a hand-specified recovery model: a simulation, not production data. The policy table recovers one hundred and fifty-seven payments against seventy-five for retry-everything, with zero wasted attempts against one hundred and forty-six, zero risk-block violations against twenty-five, and zero duplicate links against seventeen. The comparison holds across five seeds. The ordering is the claim; the absolute rate is not.

## Scene 12, queue, then on camera close (85 words)

The human queue is ordered by expected recovery, so a person works the money first. Everything learns the way a finance team would accept: recovery rates by cause and delay with confidence intervals, and a table change is proposed only with thirty samples on both sides; a person applies it, with the evidence stored and a rollback. Built in a timebox with Claude Code against a spec I wrote. Six hundred and twenty-four tests, nine faults, one honest number. Next: real token retries, and the webhook receiver taking live deliveries.
