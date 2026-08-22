"""Stage 5 — Execution.

Carries out actions the policy gate has already approved: generating a Razorpay
test-mode payment link, scheduling a retry, logging a reminder.

Entry points here accept an *authorized* action only.
"""
