"""Domain models for decision (Stage 3 — what we would recommend, and why).

The boundary this module enforces:

* `recommended_intervention` is a `Literal`. An intervention that is not in the
  fixed catalogue cannot be represented, so it cannot be stored.
* `expected_recovery_value` is re-derived from the other three numbers by a
  validator. The ERV cannot be set to a flattering value independently of the
  cost and probability it is supposed to follow from.
* There is no field for authorization, approval, execution, or a payment
  reference — and `extra="forbid"` means one cannot be added by a caller. A
  `Decision` can say "this is what I would do"; it has no vocabulary for
  "this was allowed" or "this was done". Those belong to Stage 4 and Stage 5.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ---------------------------------------------------------------------------
# The bounded intervention catalogue.
# ---------------------------------------------------------------------------

#: Every action the system is permitted to recommend. Adding a capability means
#: editing this Literal, which is a reviewable change to a type — not something
#: that can happen at runtime, and not something an LLM can influence, since no
#: LLM participates in this stage at all.
InterventionName = Literal[
    # Retries — free, because they reuse an existing mandate or authorization.
    "immediate_retry",
    "delayed_retry",
    # Customer-contact interventions — cost is the messaging spend.
    "payment_method_update_link",
    "recovery_payment_link",
    "reminder",
    "escalating_reminder_sequence",
    # Human time.
    "manual_escalation",
    # The three ways of deciding to do nothing, kept distinct so that "we chose
    # not to chase this" is separable from "nothing applied" when Stage 6 asks
    # what happened to the money.
    "no_action",
    "no_action_low_confidence",
    "no_action_negative_erv",
]

ALLOWED_INTERVENTIONS: frozenset[str] = frozenset(get_args(InterventionName))

#: Interventions that attempt nothing. All three must carry zero cost and zero
#: probability, which the `Decision` validators enforce.
NO_ACTION_INTERVENTIONS: frozenset[str] = frozenset(
    {
        "no_action",
        "no_action_low_confidence",
        "no_action_negative_erv",
    }
)

#: Below this confidence, no paid or customer-contacting intervention may be
#: recommended, regardless of ERV. Stage 2 deliberately left "is this diagnosis
#: trustworthy enough to act on" to the decision layer; this is that answer.
#:
#: Ratified at 0.5. Worth knowing when re-tuning: observed confidences are
#: bimodal — rules-based diagnoses land at 0.88-0.97, LLM/fallback unknowns at
#: 0.10-0.20 — so any floor between 0.21 and 0.88 behaves identically on current
#: data. The value matters as calibration drifts, not today.
CONFIDENCE_FLOOR = 0.5

#: Tolerance when re-deriving ERV, since the stored value is rounded to paise.
ERV_TOLERANCE = 0.01

#: Money values are rounded to two decimal places before storage.
MONEY_PRECISION = 2

_OBJECT_ID_PATTERN = r"^[0-9a-fA-F]{24}$"


def _utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


def expected_recovery_value(
    revenue_at_risk: float,
    recovery_probability: float,
    estimated_cost: float,
) -> float:
    """Compute ERV: what the attempt is worth, net of what it costs.

    Defined once, here, so the engine that picks an intervention and the
    validator that checks the stored result cannot disagree about the formula.
    """
    return round(
        revenue_at_risk * recovery_probability - estimated_cost,
        MONEY_PRECISION,
    )


# ---------------------------------------------------------------------------
# The decision contract.
# ---------------------------------------------------------------------------


class Decision(BaseModel):
    """A recommendation: the best-scoring intervention for one diagnosis.

    Note what is absent, deliberately. No `authorized`, no `approved`, no
    `executed`, no `status`, no payment-link id, no recipient. This stage
    recommends; it has no way to express that anything was permitted or done.
    """

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(
        ...,
        min_length=1,
        description="The `RevenueEvent.event_id` this recommendation concerns.",
    )
    diagnosis_id: str = Field(
        ...,
        pattern=_OBJECT_ID_PATTERN,
        description=(
            "MongoDB id of the exact diagnosis document this was decided from. "
            "Diagnosis is append-only, so pinning the id — not just the event — "
            "is what makes a decision reproducible after a re-diagnosis."
        ),
    )
    diagnosis_version: int = Field(
        ...,
        ge=1,
        description="Version of that diagnosis, carried for human readability.",
    )
    recommended_intervention: InterventionName = Field(
        ...,
        description="One of the fixed catalogue values. Nothing else is representable.",
    )
    estimated_cost: float = Field(
        ...,
        ge=0.0,
        description="Fixed cost of the intervention, in the event's currency.",
    )
    recovery_probability: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Calibrated estimate — not a measured rate. See the matrix.",
    )
    revenue_at_risk: float = Field(
        ...,
        ge=0.0,
        description="The linked event's amount.",
    )
    expected_recovery_value: float = Field(
        ...,
        description="revenue_at_risk * recovery_probability - estimated_cost.",
    )
    reasoning: str = Field(
        ...,
        min_length=1,
        max_length=1200,
        description="Human-readable account of why this intervention won.",
    )
    decided_at: datetime = Field(
        default_factory=_utc_now,
        description="When the recommendation was produced (UTC).",
    )

    @model_validator(mode="after")
    def _erv_must_follow_from_its_inputs(self) -> "Decision":
        """Reject a stored ERV that does not match the formula.

        Without this, `expected_recovery_value` would be an ordinary float that
        any caller could set to anything — and since Stage 4 will authorize
        spending based on it, a wrong ERV is the most consequential thing that
        could be smuggled through this model.
        """
        expected = expected_recovery_value(
            self.revenue_at_risk,
            self.recovery_probability,
            self.estimated_cost,
        )
        if abs(self.expected_recovery_value - expected) > ERV_TOLERANCE:
            raise ValueError(
                f"expected_recovery_value {self.expected_recovery_value} does not "
                f"match {self.revenue_at_risk} * {self.recovery_probability} - "
                f"{self.estimated_cost} = {expected}"
            )
        return self

    @model_validator(mode="after")
    def _no_action_attempts_nothing(self) -> "Decision":
        """A no-action recommendation cannot carry a cost or a success chance."""
        if self.recommended_intervention in NO_ACTION_INTERVENTIONS:
            if self.estimated_cost != 0.0 or self.recovery_probability != 0.0:
                raise ValueError(
                    f"{self.recommended_intervention!r} must have "
                    f"estimated_cost=0 and recovery_probability=0; got "
                    f"cost={self.estimated_cost}, p={self.recovery_probability}"
                )
        return self

    @model_validator(mode="after")
    def _real_intervention_must_be_able_to_succeed(self) -> "Decision":
        """Reject recommending an action that is assumed never to work."""
        if (
            self.recommended_intervention not in NO_ACTION_INTERVENTIONS
            and self.recovery_probability <= 0.0
        ):
            raise ValueError(
                f"{self.recommended_intervention!r} has recovery_probability "
                f"{self.recovery_probability}; recommending an action with no "
                "chance of success is never correct — use no_action instead"
            )
        return self


class DecisionRecord(Decision):
    """A stored `Decision`, with its document id and append-only version."""

    id: str = Field(..., description="MongoDB document id, rendered as a string.")
    version: int = Field(
        ...,
        ge=1,
        description="1 for the first decision on an event, incrementing thereafter.",
    )

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> "DecisionRecord":
        """Build a record from a raw MongoDB document."""
        fields = {key: value for key, value in document.items() if key != "_id"}
        return cls(id=str(document["_id"]), **fields)
