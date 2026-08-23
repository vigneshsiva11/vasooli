"""Stage 3 decision logic — pure, deterministic, offline.

No LLM participates in this stage. Choosing between a ₹3 reminder and a ₹20
sequence is arithmetic over a fixed table, not language understanding, and a
model asked to do it could only either reproduce the arithmetic or get it wrong.
The reasoning strings here are assembled from the numbers, not generated.

This module also has no database handle, no Razorpay client, and no import from
`app.policy` or `app.execution`. It takes a diagnosis and an event, and returns
a `Decision`. It cannot authorize one and it cannot act on one.
"""

from __future__ import annotations

import logging

from app.decision.matrix import Candidate, candidates_for, cost_of
from app.models import (
    NO_ACTION_INTERVENTIONS,
    CONFIDENCE_FLOOR,
    Decision,
    DiagnosisRecord,
    MONEY_PRECISION,
    RevenueEvent,
    expected_recovery_value,
)

logger = logging.getLogger(__name__)

#: The currency the matrix costs are denominated in. An event in another currency
#: would compare its own units against rupee costs, so the mismatch is recorded
#: in the reasoning rather than silently ignored. Converting properly needs an FX
#: source, which is out of scope for this stage.
COST_CURRENCY = "INR"

#: Reasoning is capped by the model; truncate before validation rather than
#: losing a correct decision to a verbose assumption string.
_MAX_REASONING = 1200


def _truncate(text: str, limit: int = _MAX_REASONING) -> str:
    """Shorten text to `limit` characters, marking that it was cut."""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _money(value: float) -> str:
    """Format a value for a human-readable reasoning string."""
    return f"{value:,.{MONEY_PRECISION}f}"


def _score(candidate: Candidate, revenue_at_risk: float) -> tuple[Candidate, float, float]:
    """Return a candidate with its cost and ERV."""
    cost = cost_of(candidate.intervention)
    erv = expected_recovery_value(
        revenue_at_risk, candidate.recovery_probability, cost
    )
    return candidate, cost, erv


def evaluate(
    surface: str, root_cause: str, revenue_at_risk: float
) -> list[tuple[Candidate, float, float]]:
    """Score every permitted intervention for a diagnosis, best first.

    Exposed separately from `decide` so the full comparison can be inspected and
    hand-checked, not just the winner.

    Ties break toward the cheaper intervention, then alphabetically, so the same
    inputs always yield the same recommendation.
    """
    scored = [
        _score(candidate, revenue_at_risk)
        for candidate in candidates_for(surface, root_cause)
    ]
    scored.sort(key=lambda item: (-item[2], item[1], item[0].intervention))
    return scored


def _no_action_decision(
    *,
    diagnosis: DiagnosisRecord,
    revenue_at_risk: float,
    intervention: str,
    reasoning: str,
) -> Decision:
    """Assemble a decision that attempts nothing.

    Cost and probability are hard-coded to zero rather than read from anywhere:
    a no-action recommendation must not be able to carry a spend.
    """
    return Decision(
        event_id=diagnosis.event_id,
        diagnosis_id=diagnosis.id,
        diagnosis_version=diagnosis.version,
        recommended_intervention=intervention,  # type: ignore[arg-type]
        estimated_cost=0.0,
        recovery_probability=0.0,
        revenue_at_risk=revenue_at_risk,
        expected_recovery_value=expected_recovery_value(revenue_at_risk, 0.0, 0.0),
        reasoning=_truncate(reasoning),
    )


def decide(*, diagnosis: DiagnosisRecord, event: RevenueEvent) -> Decision:
    """Recommend the best-scoring intervention for one diagnosis version.

    The gates apply in this order, and the order is load-bearing:

    1. `recoverable=False` blocks unconditionally. Nothing downstream can
       overturn it — not a large amount, not a high ERV.
    2. Confidence below `CONFIDENCE_FLOOR` blocks any paid or contacting
       intervention. Acting on an untrustworthy explanation is worse than
       waiting, however attractive the arithmetic looks.
    3. Only then is the matrix consulted and ERV compared.
    4. A winning ERV below zero blocks: the cheapest correct action is to keep
       the money we would have spent chasing.

    Args:
        diagnosis: The exact stored diagnosis version this decision is made from.
        event: The event that diagnosis explains, read for `amount`.

    Returns:
        A recommendation. Never an authorization.
    """
    if diagnosis.event_id != event.event_id:
        raise ValueError(
            f"Diagnosis is for event {diagnosis.event_id!r} but event is "
            f"{event.event_id!r}; refusing to decide across a mismatched pair"
        )

    revenue_at_risk = round(event.amount, MONEY_PRECISION)
    provenance = (
        f"Decided from diagnosis v{diagnosis.version} "
        f"({diagnosis.surface}/{diagnosis.root_cause}, confidence "
        f"{diagnosis.confidence:.2f}, method {diagnosis.method})."
    )

    currency_note = ""
    if event.currency.upper() != COST_CURRENCY:
        currency_note = (
            f" NOTE: event is denominated in {event.currency.upper()} while "
            f"intervention costs are in {COST_CURRENCY}; the cost term is not "
            "converted, so this ERV understates or overstates cost accordingly."
        )
        logger.warning(
            "Event %s is in %s but intervention costs are in %s; ERV cost term "
            "is not FX-converted",
            event.event_id,
            event.currency,
            COST_CURRENCY,
        )

    # 1. Hard block: diagnosis says this is not worth recovering at all.
    if not diagnosis.recoverable:
        return _no_action_decision(
            diagnosis=diagnosis,
            revenue_at_risk=revenue_at_risk,
            intervention="no_action",
            reasoning=(
                f"{provenance} Diagnosis marks this event as not recoverable, which "
                "is an unconditional block: no amount at risk and no expected value "
                "can override it. No intervention was scored."
            ),
        )

    # 2. Confidence floor: do not spend against an explanation we distrust.
    if diagnosis.confidence < CONFIDENCE_FLOOR:
        return _no_action_decision(
            diagnosis=diagnosis,
            revenue_at_risk=revenue_at_risk,
            intervention="no_action_low_confidence",
            reasoning=(
                f"{provenance} Confidence {diagnosis.confidence:.2f} is below the "
                f"{CONFIDENCE_FLOOR:.2f} floor required to recommend a paid or "
                f"customer-contacting intervention, so {_money(revenue_at_risk)} "
                "remains at risk deliberately: the diagnosis is not trustworthy "
                "enough to act on. Re-diagnose to obtain a firmer explanation."
            ),
        )

    # 3. Score every permitted option for this exact root cause.
    scored = evaluate(diagnosis.surface, diagnosis.root_cause, revenue_at_risk)
    best_candidate, best_cost, best_erv = scored[0]

    # The matrix itself may map a cause to no-action — the ratified handling of
    # `unknown` on every surface, for instance.
    if best_candidate.intervention in NO_ACTION_INTERVENTIONS:
        return _no_action_decision(
            diagnosis=diagnosis,
            revenue_at_risk=revenue_at_risk,
            intervention=best_candidate.intervention,
            reasoning=(
                f"{provenance} The intervention matrix maps "
                f"{diagnosis.surface}/{diagnosis.root_cause} to no action. "
                f"{best_candidate.assumption}"
            ),
        )

    comparison = "; ".join(
        f"{candidate.intervention} ERV {_money(erv)} "
        f"(= {_money(revenue_at_risk)} × {candidate.recovery_probability:.2f} "
        f"− {_money(cost)})"
        for candidate, cost, erv in scored
    )

    # 4. Refuse to recommend spending more than we expect to recover.
    if best_erv < 0:
        return _no_action_decision(
            diagnosis=diagnosis,
            revenue_at_risk=revenue_at_risk,
            intervention="no_action_negative_erv",
            reasoning=_truncate(
                f"{provenance} Every available option costs more than it is "
                f"expected to recover on {_money(revenue_at_risk)} at risk. "
                f"Scored: {comparison}. Best ERV {_money(best_erv)} is negative, so "
                f"the economically correct action is to spend nothing.{currency_note}"
            ),
        )

    alternatives = len(scored) - 1
    if alternatives:
        choice = (
            f"Chose {best_candidate.intervention} over "
            f"{alternatives} alternative{'s' if alternatives > 1 else ''}."
        )
    else:
        choice = (
            f"{best_candidate.intervention} is the only intervention mapped to this "
            "root cause."
        )

    return Decision(
        event_id=diagnosis.event_id,
        diagnosis_id=diagnosis.id,
        diagnosis_version=diagnosis.version,
        recommended_intervention=best_candidate.intervention,  # type: ignore[arg-type]
        estimated_cost=best_cost,
        recovery_probability=best_candidate.recovery_probability,
        revenue_at_risk=revenue_at_risk,
        expected_recovery_value=best_erv,
        reasoning=_truncate(
            f"{provenance} Scored: {comparison}. {choice} "
            f"Assumption behind {best_candidate.recovery_probability:.2f}: "
            f"{best_candidate.assumption} Probabilities are calibrated estimates, "
            f"not measured rates.{currency_note}"
        ),
    )
