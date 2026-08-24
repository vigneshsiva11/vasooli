"""Policy parameters and the classification of interventions (Stage 4).

Every number here is a ratified policy choice, not a tuning constant. They are
gathered in one module so the whole authority envelope of the agent can be read
at once, and so widening it is a visible one-line diff rather than a change
buried inside branching logic.

These are the values in force *now*. `current_rulebook()` snapshots them into a
`Rulebook`, whose fingerprint every verdict records, and `app/policy/rulebook.py`
archives the sets that have been superseded. Amending anything below therefore
changes the fingerprint, which is what lets the audit tell an old verdict judged
under an older rulebook apart from one that would be decided differently today.

Nothing in this module performs I/O, calls an LLM, or executes anything.
"""

from __future__ import annotations

from datetime import timedelta
from typing import get_args

from app.models import ALLOWED_INTERVENTIONS, NO_ACTION_INTERVENTIONS
from app.models.policy import POLICY_CHECKS, REASON_PRECEDENCE, REASON_VERDICT
from app.policy.rulebook import (
    SUPERSEDED_RULEBOOKS,
    AutonomyTier,
    Rulebook,
    fingerprint_of,
)

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

# `AutonomyTier` is defined in `app/policy/rulebook.py`, beside the `tier_for`
# that returns it, and re-exported here so this module still reads as the one
# place the authority envelope is described.

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
#: Both payment links are RATIFIED as contact-type, on the principle that any link
#: sent to a customer is outreach. This resolves a contradiction between the
#: Stage 4 brief — which named only the three below and explicitly excluded
#: `payment_method_update_link` — and Stage 3's own catalogue, where
#: `app/models/decision.py` groups both links under "customer-contact
#: interventions" and prices them at messaging spend, because a link has to be
#: delivered to somebody. The catalogue wins: sending someone a link is contacting
#: them, so consent gates it and it consumes one of their three contacts.
#:
#: This set and the catalogue's own grouping now agree completely. Every
#: non-zero-cost intervention whose cost is messaging spend is here; the only
#: interventions outside it are the two free retries, which touch the payment rail
#: rather than the person, and the three ways of doing nothing.
CONTACT_INTERVENTIONS: frozenset[str] = frozenset(
    {
        "reminder",
        "escalating_reminder_sequence",
        "manual_escalation",
        "payment_method_update_link",
        "recovery_payment_link",
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


# ---------------------------------------------------------------------------
# The rulebook in force.
# ---------------------------------------------------------------------------


def current_rulebook() -> Rulebook:
    """Snapshot every ratified parameter as it stands right now.

    Reads the module globals at call time rather than closing over them at import,
    so a caller that installs a different value for a test gets a rulebook — and
    therefore a fingerprint — that reflects it.

    The four tables pulled in from `app/models` are ratified policy too: which
    interventions mean "do nothing", which checks make up the trail, which failure
    outranks which, and whether a given failure blocks or routes for review. They
    are hashed alongside the numbers, because a rulebook that disagreed about any
    of them would reach different verdicts from identical inputs.
    """
    return Rulebook(
        minimum_erv=MINIMUM_ERV,
        zero_cost_exempt_from_erv_floor=ZERO_COST_EXEMPT_FROM_ERV_FLOOR,
        auto_authorize_below=AUTO_AUTHORIZE_BELOW,
        never_auto_at_or_above=NEVER_AUTO_AT_OR_ABOVE,
        tier_currency=TIER_CURRENCY,
        contact_interventions=frozenset(CONTACT_INTERVENTIONS),
        max_contacts_per_event=MAX_CONTACTS_PER_EVENT,
        cooldown_hours=COOLDOWN_HOURS,
        cooldown_measured_from=COOLDOWN_MEASURED_FROM,
        no_action_interventions=frozenset(NO_ACTION_INTERVENTIONS),
        policy_checks=tuple(POLICY_CHECKS),
        reason_precedence=tuple(REASON_PRECEDENCE),
        reason_verdict=tuple(sorted(REASON_VERDICT.items())),
        note="in force",
    )


def current_fingerprint() -> str:
    """The fingerprint of the rulebook in force right now."""
    return fingerprint_of(current_rulebook())


def rulebook_registry() -> dict[str, Rulebook]:
    """Every rulebook this build can identify, keyed by fingerprint.

    The current one plus the archive. A verdict whose fingerprint is absent from
    this mapping was judged by a rulebook this build has no record of, which the
    audit reports rather than papering over — an unidentifiable rulebook means the
    verdict can only be re-derived against the present, and that has to be said
    out loud rather than assumed away.
    """
    registry = {rulebook.fingerprint: rulebook for rulebook in SUPERSEDED_RULEBOOKS}
    current = current_rulebook()
    # Last, so that a rulebook which is both archived and in force (as happens
    # while a test has an older parameter set installed) resolves to the live one.
    registry[current.fingerprint] = current
    return registry


# ---------------------------------------------------------------------------
# Convenience predicates against the rulebook in force.
#
# The rulebook's own methods are what the engine consults, since a verdict must be
# re-derivable under the rules of its own time. These module-level wrappers exist
# for callers that legitimately mean "under today's policy" — chiefly
# `app.policy.store`, which is deciding what to count right now.
# ---------------------------------------------------------------------------


def is_contact_intervention(intervention: str) -> bool:
    """Whether the intervention puts a message in front of a customer, today."""
    return current_rulebook().is_contact(intervention)


def tier_for(amount: float) -> AutonomyTier:
    """Return the autonomy tier for an amount at risk, under today's thresholds."""
    return current_rulebook().tier_for(amount)


def erv_floor_applies(estimated_cost: float) -> bool:
    """Whether today's minimum-ERV floor applies to an action of this cost."""
    return current_rulebook().erv_floor_applies(estimated_cost)


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

    if problems:
        # Raised before anything below builds a `Rulebook`, whose own structural
        # checks would otherwise fire first and report one problem instead of all
        # of them.
        raise RuntimeError(
            "Policy parameters are inconsistent:\n  - " + "\n  - ".join(problems)
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

    # The archive has to stay coherent with the present, or the fingerprint stops
    # meaning anything. Both checks below catch a real mistake in amending policy.
    archived: dict[str, str] = {}
    for rulebook in SUPERSEDED_RULEBOOKS:
        existing = archived.get(rulebook.fingerprint)
        if existing is not None:
            problems.append(
                f"two superseded rulebooks have the same fingerprint "
                f"{rulebook.fingerprint}: {existing!r} and {rulebook.note!r}; they "
                "describe one parameter set, not two"
            )
        archived[rulebook.fingerprint] = rulebook.note

    current = current_rulebook()
    if current.fingerprint in archived:
        problems.append(
            f"the rulebook in force has fingerprint {current.fingerprint}, which "
            f"the archive lists as superseded ({archived[current.fingerprint]!r}). "
            "Either an amendment was archived but never applied to the parameters "
            "above, or one was reverted without removing its archive entry"
        )

    if problems:
        raise RuntimeError(
            "Policy parameters are inconsistent:\n  - " + "\n  - ".join(problems)
        )


_validate_parameters()
