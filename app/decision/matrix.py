"""The bounded intervention matrix, and the assumptions behind every number.

Two tables, split by what each fact actually depends on:

* `INTERVENTIONS` holds cost. What it costs to send a payment link does not
  depend on why the payment failed, so cost belongs to the intervention.
* `INTERVENTION_MATRIX` holds probability, per `(surface, root_cause)` pair.
  How often a retry succeeds *does* depend on why it failed — a delayed retry
  after `insufficient_funds` is a bet that the balance arrives, while the same
  retry after `issuer_declined` is a bet that the issuer changes its mind. One
  of those is much more likely than the other.

HONESTY ABOUT THE NUMBERS
=========================
Every probability below is a calibrated estimate reasoned from payment-domain
priors. None is measured. We have no historical recovery outcomes yet, so
nothing here has been fitted to data, and the ERVs computed from them are
therefore ordinal guidance ("this option beats that one") far more than they are
cash forecasts. Stage 6 records actual outcomes; these constants are what should
be replaced first once it has collected enough of them.

Costs are in the event's currency unit, assumed INR — see `engine.py` for the
consequence of that assumption on a non-INR event.

This module is validated at import time. If a root cause exists without a
mapping, or a mapping names an intervention outside the catalogue, the process
fails to start rather than silently defaulting.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models import (
    ALLOWED_INTERVENTIONS,
    ALLOWED_ROOT_CAUSES,
    NO_ACTION_INTERVENTIONS,
    NON_RECOVERABLE_ROOT_CAUSES,
)


@dataclass(frozen=True)
class InterventionSpec:
    """What an intervention costs, and what it is."""

    name: str
    estimated_cost: float
    summary: str


@dataclass(frozen=True)
class Candidate:
    """One option for a given root cause, with its assumed success rate."""

    intervention: str
    recovery_probability: float
    assumption: str


# ---------------------------------------------------------------------------
# Costs.
# ---------------------------------------------------------------------------

INTERVENTIONS: dict[str, InterventionSpec] = {
    "immediate_retry": InterventionSpec(
        name="immediate_retry",
        estimated_cost=0.0,
        summary="Re-attempt the charge now, on the existing authorization.",
    ),
    "delayed_retry": InterventionSpec(
        name="delayed_retry",
        estimated_cost=0.0,
        summary="Re-attempt the charge after a wait, on the existing mandate.",
    ),
    "payment_method_update_link": InterventionSpec(
        name="payment_method_update_link",
        estimated_cost=5.0,
        summary="Ask the customer to supply a working payment instrument.",
    ),
    "recovery_payment_link": InterventionSpec(
        name="recovery_payment_link",
        estimated_cost=5.0,
        summary="Send a link back to the abandoned cart at the same price.",
    ),
    "reminder": InterventionSpec(
        name="reminder",
        estimated_cost=3.0,
        summary="A single nudge about an outstanding invoice.",
    ),
    "escalating_reminder_sequence": InterventionSpec(
        name="escalating_reminder_sequence",
        estimated_cost=20.0,
        summary="A multi-touch sequence over several days, increasing in firmness.",
    ),
    "manual_escalation": InterventionSpec(
        name="manual_escalation",
        estimated_cost=50.0,
        summary="Hand to a human collections owner. Priced as staff time.",
    ),
    "no_action": InterventionSpec(
        name="no_action",
        estimated_cost=0.0,
        summary="Attempt nothing. Recovery is inappropriate or unmapped.",
    ),
    "no_action_low_confidence": InterventionSpec(
        name="no_action_low_confidence",
        estimated_cost=0.0,
        summary="Attempt nothing: the diagnosis is not trustworthy enough to act on.",
    ),
    "no_action_negative_erv": InterventionSpec(
        name="no_action_negative_erv",
        estimated_cost=0.0,
        summary="Attempt nothing: every available option costs more than it recovers.",
    ),
}


def _no_action(reason: str) -> tuple[Candidate, ...]:
    """A mapping that attempts nothing. Zero probability, zero cost."""
    return (Candidate("no_action", 0.0, reason),)


# ---------------------------------------------------------------------------
# Probabilities, per (surface, root_cause).
# ---------------------------------------------------------------------------

INTERVENTION_MATRIX: dict[tuple[str, str], tuple[Candidate, ...]] = {
    # -- payment ------------------------------------------------------------
    ("payment", "insufficient_funds"): (
        Candidate(
            "delayed_retry",
            0.35,
            "Balances are often topped up within days; a free retry captures that "
            "without asking the customer for anything.",
        ),
        Candidate(
            "payment_method_update_link",
            0.20,
            "Asking for a different card can work, but a customer short of funds on "
            "one instrument is frequently short on all of them.",
        ),
    ),
    ("payment", "card_expired"): (
        Candidate(
            "payment_method_update_link",
            0.45,
            "The blocker is a stale credential and nothing else, so a customer who "
            "still wants the product can fix it in one step. Retrying cannot help: "
            "the expiry date will not change.",
        ),
    ),
    ("payment", "issuer_declined"): (
        Candidate(
            "delayed_retry",
            0.15,
            "A bare issuer decline rarely reverses on its own — retrying mostly "
            "re-collects the same refusal — but it is free, so any chance is upside.",
        ),
        Candidate(
            "payment_method_update_link",
            0.30,
            "A different instrument bypasses the declining issuer entirely, which is "
            "twice as likely to work as asking the same issuer again.",
        ),
    ),
    ("payment", "temporary_processing_error"): (
        Candidate(
            "immediate_retry",
            0.65,
            "By construction the failure was transient and the customer's intent is "
            "still fresh, which makes this the highest-probability case in the matrix.",
        ),
        Candidate(
            "delayed_retry",
            0.45,
            "Also free and also likely, but waiting loses some customers who have "
            "moved on, so it should lose to an immediate attempt.",
        ),
    ),
    ("payment", "suspected_fraud"): _no_action(
        "Recovering suspected-fraud revenue is not a goal; pursuing it converts a "
        "blocked loss into a chargeback and a compliance problem."
    ),
    ("payment", "unknown"): _no_action(
        "Clearing the confidence floor while still not knowing the cause is not a "
        "basis for spending money or contacting a customer."
    ),
    # -- checkout -----------------------------------------------------------
    # One intervention exists for this surface — the cart is unpaid, so there is
    # nothing to retry. The causes differ only in how likely a link is to convert.
    ("checkout", "technical_error"): (
        Candidate(
            "recovery_payment_link",
            0.35,
            "Intent was demonstrated and only the mechanism failed, so this is the "
            "strongest abandonment case: a working link removes the sole obstacle.",
        ),
    ),
    ("checkout", "payment_method_unavailable"): (
        Candidate(
            "recovery_payment_link",
            0.25,
            "Intent was real, but the link only converts if the customer finds an "
            "instrument we accept, which not all will.",
        ),
    ),
    ("checkout", "checkout_friction"): (
        Candidate(
            "recovery_payment_link",
            0.20,
            "A link skips part of the flow that caused the drop-off, though whatever "
            "frustrated the customer may still be waiting for them.",
        ),
    ),
    ("checkout", "price_sensitivity"): (
        Candidate(
            "recovery_payment_link",
            0.08,
            "The objection is the price, and this link re-presents the same price. "
            "Deliberately low: a discount would convert far better, but discounting "
            "is not in the catalogue and adding it is a revenue-policy decision, "
            "not a decision-engine one.",
        ),
    ),
    ("checkout", "low_purchase_intent"): (
        Candidate(
            "recovery_payment_link",
            0.05,
            "A browser who never intended to buy does not become a buyer because of "
            "a link. Low enough that on small carts the messaging cost exceeds the "
            "expected return, which is the intended outcome.",
        ),
    ),
    ("checkout", "unknown"): _no_action(
        "No cause identified, and every checkout intervention costs money to send."
    ),
    # -- subscription -------------------------------------------------------
    ("subscription", "mandate_expired"): (
        Candidate(
            "payment_method_update_link",
            0.45,
            "The mandate must be re-authorised by the customer; no retry can "
            "substitute for that. Comparable to a card expiry.",
        ),
    ),
    ("subscription", "card_expired"): (
        Candidate(
            "payment_method_update_link",
            0.45,
            "Same shape as the one-off card expiry, and a subscriber has already "
            "shown ongoing intent.",
        ),
    ),
    ("subscription", "insufficient_funds"): (
        Candidate(
            "delayed_retry",
            0.35,
            "Standard dunning behaviour: wait for the salary cycle and retry free.",
        ),
        Candidate(
            "payment_method_update_link",
            0.20,
            "Escalating to ask for a new instrument is heavier than waiting, and no "
            "more likely to succeed.",
        ),
    ),
    ("subscription", "issuer_declined"): (
        Candidate(
            "delayed_retry",
            0.15,
            "Free, but an issuer that declined a recurring debit tends to keep "
            "declining it.",
        ),
        Candidate(
            "payment_method_update_link",
            0.30,
            "Routing around the issuer is the more plausible fix.",
        ),
    ),
    ("subscription", "dunning_exhausted"): (
        Candidate(
            "manual_escalation",
            0.25,
            "Every automated attempt has already failed, so by definition the cheap "
            "options are spent. A human sometimes recovers these; the low rate plus "
            "the high cost of staff time means small subscriptions should not "
            "qualify, which the ERV comparison enforces.",
        ),
    ),
    ("subscription", "voluntary_churn"): _no_action(
        "The customer chose to leave. Win-back is a different product from revenue "
        "recovery and is out of scope for this build."
    ),
    ("subscription", "mandate_revoked"): _no_action(
        "The customer withdrew authorisation. Charging around a revoked mandate is "
        "not something to optimise."
    ),
    ("subscription", "unknown"): _no_action(
        "No cause identified; do not spend against a subscription we cannot explain."
    ),
    # -- receivable ---------------------------------------------------------
    ("receivable", "genuine_delay"): (
        Candidate(
            "reminder",
            0.55,
            "The counterparty accepts the debt and intends to pay, so a single nudge "
            "usually suffices. Cheapest option that works.",
        ),
        Candidate(
            "escalating_reminder_sequence",
            0.65,
            "Persistence adds a modest lift over one nudge. Costs seven times as "
            "much, so it should only win on invoices large enough to justify it — "
            "the crossover against a single reminder sits near 170 currency units.",
        ),
    ),
    ("receivable", "non_responsive"): (
        Candidate(
            "escalating_reminder_sequence",
            0.35,
            "Silence has already defeated one channel; repetition sometimes breaks "
            "through, but a party ignoring invoices often keeps ignoring them.",
        ),
        Candidate(
            "manual_escalation",
            0.55,
            "A human who can telephone, or invoke the contract, is markedly more "
            "effective against deliberate silence than more automated email.",
        ),
    ),
    ("receivable", "payment_dispute"): _no_action(
        "A disputed invoice is a commercial disagreement, not a collections "
        "problem. Dunning it damages the relationship and prejudices the dispute."
    ),
    ("receivable", "unknown"): _no_action(
        "No cause identified; every receivable intervention has a contact cost."
    ),
}


# ---------------------------------------------------------------------------
# Import-time integrity checks.
# ---------------------------------------------------------------------------


def _validate_matrix() -> None:
    """Fail at import if the matrix is incomplete or inconsistent.

    Stage 2's root causes and this matrix are separate tables that must agree.
    Checking that here means adding a root cause without deciding what to do
    about it breaks startup, rather than quietly falling through to a default.
    """
    problems: list[str] = []

    catalogue_names = set(INTERVENTIONS)
    if catalogue_names != ALLOWED_INTERVENTIONS:
        problems.append(
            f"INTERVENTIONS does not match the InterventionName Literal; "
            f"only in catalogue: {sorted(catalogue_names - ALLOWED_INTERVENTIONS)}, "
            f"only in Literal: {sorted(ALLOWED_INTERVENTIONS - catalogue_names)}"
        )

    for name, spec in INTERVENTIONS.items():
        if spec.name != name:
            problems.append(f"INTERVENTIONS[{name!r}] has mismatched name {spec.name!r}")
        if spec.estimated_cost < 0:
            problems.append(f"{name!r} has negative cost {spec.estimated_cost}")
        if name in NO_ACTION_INTERVENTIONS and spec.estimated_cost != 0.0:
            problems.append(f"{name!r} attempts nothing but costs {spec.estimated_cost}")

    # Every root cause the diagnosis stage can emit must have a mapping.
    for surface, root_causes in ALLOWED_ROOT_CAUSES.items():
        for root_cause in sorted(root_causes):
            key = (surface, root_cause)
            candidates = INTERVENTION_MATRIX.get(key)
            if not candidates:
                problems.append(f"no intervention mapping for {surface}/{root_cause}")
                continue

            for candidate in candidates:
                if candidate.intervention not in INTERVENTIONS:
                    problems.append(
                        f"{surface}/{root_cause} names unknown intervention "
                        f"{candidate.intervention!r}"
                    )
                if not 0.0 <= candidate.recovery_probability <= 1.0:
                    problems.append(
                        f"{surface}/{root_cause} -> {candidate.intervention} has "
                        f"probability {candidate.recovery_probability} outside [0, 1]"
                    )
                if not candidate.assumption.strip():
                    problems.append(
                        f"{surface}/{root_cause} -> {candidate.intervention} has no "
                        "documented assumption"
                    )

            # A cause diagnosis calls unrecoverable must not be mapped to an
            # action here. The engine blocks these anyway; disagreement between
            # the two tables would be a silent trap, so it is an error instead.
            if root_cause in NON_RECOVERABLE_ROOT_CAUSES:
                actionable = [
                    c.intervention
                    for c in candidates
                    if c.intervention not in NO_ACTION_INTERVENTIONS
                ]
                if actionable:
                    problems.append(
                        f"{surface}/{root_cause} is non-recoverable but maps to "
                        f"{actionable}"
                    )

    # No mapping for a pair diagnosis cannot produce.
    for surface, root_cause in INTERVENTION_MATRIX:
        if root_cause not in ALLOWED_ROOT_CAUSES.get(surface, frozenset()):
            problems.append(
                f"matrix maps {surface}/{root_cause}, which is not a valid root "
                "cause for that surface"
            )

    if problems:
        raise RuntimeError(
            "Intervention matrix failed validation:\n  - " + "\n  - ".join(problems)
        )


_validate_matrix()


def candidates_for(surface: str, root_cause: str) -> tuple[Candidate, ...]:
    """Return the permitted interventions for one diagnosis shape.

    Raises:
        KeyError: if the pair has no mapping. Unreachable in practice — the
            import-time check guarantees coverage of every valid pair — but it
            fails loudly rather than inventing a default if that ever changes.
    """
    return INTERVENTION_MATRIX[(surface, root_cause)]


def cost_of(intervention: str) -> float:
    """Return an intervention's fixed cost."""
    return INTERVENTIONS[intervention].estimated_cost
