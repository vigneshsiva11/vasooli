"""Policy parameters and the classification of interventions (Stage 4).

Every number here is a ratified policy choice, not a tuning constant. They are
gathered in one module so the whole authority envelope of the agent can be read
at once, and so widening it is a visible one-line diff rather than a change
buried inside branching logic.

Nothing in this module performs I/O, calls an LLM, or executes anything.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Literal, get_args

from app.models import ALLOWED_INTERVENTIONS, NO_ACTION_INTERVENTIONS

# ---------------------------------------------------------------------------
# Economics: the minimum ERV worth acting on.
# ---------------------------------------------------------------------------

#: Below this expected recovery value, acting is not worth the operational
#: overhead even though the arithmetic is positive. Ratified at 25.00 INR.
#:
#: Stage 3 deliberately left this unresolved: it computes whether an action is
#: profitable, which is an arithmetic question, and profitability alone does not
#: make an action worth taking. Judging "worth it" against a threshold is a policy
#: question, so the threshold lives here.
MINIMUM_ERV = 25.0

#: Ratified: zero-cost interventions are EXEMPT from the floor.
#:
#: The floor exists to stop the agent from spending real money chasing trivial
#: upside. A zero-cost intervention (`immediate_retry`, `delayed_retry`) reuses an
#: existing mandate and spends nothing, so there is no downside to weigh against
#: a small gain — its ERV equals its upside and cannot be negative. Applying the
#: floor to it would decline free money.
#:
#: The cost, not the intervention name, is what triggers the exemption: if a retry
#: ever acquires a cost in the matrix, the floor starts applying to it
#: automatically rather than silently continuing to exempt it.
ZERO_COST_EXEMPT_FROM_ERV_FLOOR = True

# ---------------------------------------------------------------------------
# Autonomy: how much money the agent may commit without a human.
# ---------------------------------------------------------------------------

AutonomyTier = Literal["auto", "approval_required", "never_auto"]

#: Amounts strictly below this may be acted on autonomously.
#: Ratified at 5,000 INR, checked against the real event distribution:
#: min 80, median 2,449.75, mean 18,087.17, max 125,000. This leaves the everyday
#: mass of failures (15 of 22 events) inside autonomous reach.
AUTO_AUTHORIZE_BELOW = 5_000.0

#: Amounts at or above this are never acted on autonomously, whatever the ERV.
#: Ratified at 25,000 INR. The four largest events (48,000 / 75,000 / 90,000 /
#: 125,000) sit above it; the gap between the two thresholds captures the
#: mid-sized tail (6,500 / 7,800 / 22,000).
NEVER_AUTO_AT_OR_ABOVE = 25_000.0

#: Thresholds are compared against `Decision.revenue_at_risk`, which Stage 3
#: pins to the event's `amount` in the event's own currency. Non-INR events are
#: not FX-converted anywhere in the pipeline, so a large foreign-currency amount
#: is tiered by its face value. Every event currently in the system is INR; this
#: is recorded as a known limitation rather than papered over with a guessed rate.
TIER_CURRENCY = "INR"

# ---------------------------------------------------------------------------
# Customer protection: how often a person may be contacted.
# ---------------------------------------------------------------------------

#: Interventions that put a message in front of a human being.
#:
#: `payment_method_update_link` is RATIFIED as contact-type, resolving a
#: contradiction between the Stage 4 brief (which named only the three below and
#: explicitly excluded it) and Stage 3's own catalogue, where
#: `app/models/decision.py` groups it under "customer-contact interventions" and
#: prices it at messaging spend — a link has to be delivered to somebody. The
#: catalogue wins: sending someone a link is contacting them, so consent gates it
#: and it consumes one of their three contacts.
#:
#: STILL OPEN: `recovery_payment_link` sits in the same group in
#: `app/models/decision.py`, is likewise priced as messaging spend, and is
#: arguably more clearly outreach — it asks for money rather than repairing a
#: mandate. It remains outside this set pending ratification, so an opted-out
#: customer can still be sent one and it does not count toward the cap. Adding it
#: is a one-line change; the checks below read this set, and no rule hard-codes an
#: intervention name.
CONTACT_INTERVENTIONS: frozenset[str] = frozenset(
    {
        "reminder",
        "escalating_reminder_sequence",
        "manual_escalation",
        "payment_method_update_link",
    }
)

#: Maximum number of authorized contacts for one revenue event, ever.
#: Ratified at 3. Scope is per `event_id` across ALL decision and diagnosis
#: versions: the cap protects a person from being chased about one debt, and
#: re-diagnosing the same failure does not reset how many messages they received.
MAX_CONTACTS_PER_EVENT = 3

#: Minimum gap between authorized contacts for the same event. Ratified at 24h.
COOLDOWN_HOURS = 24
COOLDOWN = timedelta(hours=COOLDOWN_HOURS)

#: Cooldown is measured from the previous verdict's `evaluated_at`, which is when
#: permission was granted, not when a message went out — Stage 4 does not send
#: anything and has no send timestamp to read. Authorization is expected to be
#: followed promptly by execution, so the two are close; once Stage 5 records real
#: send times, this should measure from those instead.
COOLDOWN_MEASURED_FROM = "verdict.evaluated_at"


def is_contact_intervention(intervention: str) -> bool:
    """Whether the intervention puts a message in front of a customer."""
    return intervention in CONTACT_INTERVENTIONS


def tier_for(amount: float) -> AutonomyTier:
    """Return the autonomy tier for an amount at risk.

    Boundaries are half-open and deliberately asymmetric: `AUTO_AUTHORIZE_BELOW`
    is exclusive and `NEVER_AUTO_AT_OR_ABOVE` inclusive, so an amount landing
    exactly on a threshold always falls to the more cautious side.
    """
    if amount >= NEVER_AUTO_AT_OR_ABOVE:
        return "never_auto"
    if amount < AUTO_AUTHORIZE_BELOW:
        return "auto"
    return "approval_required"


def erv_floor_applies(estimated_cost: float) -> bool:
    """Whether the minimum-ERV floor applies to an action of this cost."""
    if ZERO_COST_EXEMPT_FROM_ERV_FLOOR and estimated_cost <= 0:
        return False
    return True


def _validate_parameters() -> None:
    """Reject an internally inconsistent policy configuration at import time.

    The same discipline as the intervention matrix: a policy table that cannot be
    satisfied should fail on the way up, not silently mis-authorize later.
    """
    problems: list[str] = []

    if MINIMUM_ERV < 0:
        problems.append(f"MINIMUM_ERV must not be negative (got {MINIMUM_ERV})")

    if AUTO_AUTHORIZE_BELOW <= 0:
        problems.append(
            f"AUTO_AUTHORIZE_BELOW must be positive (got {AUTO_AUTHORIZE_BELOW})"
        )
    if NEVER_AUTO_AT_OR_ABOVE <= AUTO_AUTHORIZE_BELOW:
        problems.append(
            f"NEVER_AUTO_AT_OR_ABOVE ({NEVER_AUTO_AT_OR_ABOVE}) must exceed "
            f"AUTO_AUTHORIZE_BELOW ({AUTO_AUTHORIZE_BELOW}); otherwise the "
            "approval-required tier is empty and every mid-sized amount is "
            "silently refused rather than reviewed"
        )

    if MAX_CONTACTS_PER_EVENT < 1:
        problems.append(
            f"MAX_CONTACTS_PER_EVENT must be at least 1 (got "
            f"{MAX_CONTACTS_PER_EVENT}); a cap of 0 would block every contact "
            "and should be expressed as an opt-out, not a cap"
        )
    if COOLDOWN_HOURS < 0:
        problems.append(f"COOLDOWN_HOURS must not be negative (got {COOLDOWN_HOURS})")

    unknown = CONTACT_INTERVENTIONS - ALLOWED_INTERVENTIONS
    if unknown:
        problems.append(
            f"CONTACT_INTERVENTIONS names interventions outside the Stage 3 "
            f"catalogue: {sorted(unknown)}"
        )
    overlap = CONTACT_INTERVENTIONS & NO_ACTION_INTERVENTIONS
    if overlap:
        problems.append(
            f"CONTACT_INTERVENTIONS includes no-action variants {sorted(overlap)}; "
            "deciding to do nothing cannot contact anybody"
        )
    if not CONTACT_INTERVENTIONS:
        problems.append(
            "CONTACT_INTERVENTIONS is empty, which would disable the opt-out, "
            "cap and cooldown checks entirely"
        )

    tiers = set(get_args(AutonomyTier))
    reachable = {
        tier_for(AUTO_AUTHORIZE_BELOW - 0.01),
        tier_for(AUTO_AUTHORIZE_BELOW),
        tier_for(NEVER_AUTO_AT_OR_ABOVE),
    }
    if reachable != tiers:
        problems.append(
            f"the thresholds do not reach every tier; reachable={sorted(reachable)} "
            f"declared={sorted(tiers)}"
        )

    if problems:
        raise RuntimeError(
            "Policy parameters are inconsistent:\n  - " + "\n  - ".join(problems)
        )


_validate_parameters()
