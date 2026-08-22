"""Stage 6 — Verification (inbound webhooks).

Receives and signature-verifies Razorpay webhooks, then reconciles them against
executed actions to confirm whether the money actually came back.
"""
