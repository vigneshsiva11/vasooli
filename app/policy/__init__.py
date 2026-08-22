"""Stage 4 — Policy.

A deterministic authorization gate. Every proposed action is checked against
amount-based autonomy tiers, contact caps, cooldown periods, and opt-out status.
No LLM output reaches execution without passing through here.

Nothing in this package may call an LLM.
"""
