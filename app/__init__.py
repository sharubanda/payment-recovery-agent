"""payment-recovery-agent: cause-aware recovery of failed Razorpay payments.

Pipeline (see ARCHITECTURE.md):
    failed payment event
      -> classify.py   deterministic rules on Razorpay's error object (no LLM)
      -> llm.py        ONLY for descriptions no rule matched (schema-validated, fallbacks)
      -> policy.py     deterministic table: failure class -> action + delay + stop rules
      -> executor.py   Razorpay Payment Links (test mode), idempotency-keyed
      -> outcomes      recovered / not / human-queued, all in the audit trail
"""
