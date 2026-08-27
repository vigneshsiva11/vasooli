"""Domain models for diagnosis (Stage 2 — why revenue is at risk).

The bounded root-cause vocabulary lives here, beside the model that enforces it,
so there is exactly one place a category can be added. Nothing in this module
describes an action, a cost, or a recovery attempt — diagnosis explains, and stops.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.models.events import Surface

# ---------------------------------------------------------------------------
# Bounded root-cause vocabulary, one closed set per surface.
# ---------------------------------------------------------------------------

PaymentRootCause = Literal[
    "insufficient_funds",
    "card_expired",
    "issuer_declined",
    "temporary_processing_error",
    "suspected_fraud",
    "unknown",
]

CheckoutRootCause = Literal[
    "price_sensitivity",
    "payment_method_unavailable",
    "checkout_friction",
    "technical_error",
    "low_purchase_intent",
    "unknown",
]

SubscriptionRootCause = Literal[
    "mandate_expired",
    "mandate_revoked",
    "card_expired",
    "insufficient_funds",
    "issuer_declined",
    "voluntary_churn",
    "dunning_exhausted",
    "unknown",
]

ReceivableRootCause = Literal[
    "payment_dispute",
    "genuine_delay",
    "non_responsive",
    "unknown",
]

#: The single source of truth for what a root cause may be, per surface. Both the
#: `Diagnosis` validator and the Gemini response schema are derived from this, so
#: the LLM's allowed vocabulary cannot drift from what storage will accept.
ALLOWED_ROOT_CAUSES: dict[str, frozenset[str]] = {
    "payment": frozenset(get_args(PaymentRootCause)),
    "checkout": frozenset(get_args(CheckoutRootCause)),
    "subscription": frozenset(get_args(SubscriptionRootCause)),
    "receivable": frozenset(get_args(ReceivableRootCause)),
}

#: Fallback used whenever a root cause cannot be established or fails validation.
UNKNOWN_ROOT_CAUSE = "unknown"

#: Root causes where attempting recovery is the wrong move regardless of amount.
#: Chasing payment on a disputed invoice, or on a customer who deliberately
#: cancelled, damages the relationship rather than recovering revenue.
NON_RECOVERABLE_ROOT_CAUSES: frozenset[str] = frozenset(
    {
        "suspected_fraud",
        "payment_dispute",
        "voluntary_churn",
        "mandate_revoked",
    }
)

#: Cap on stored evidence, so a prompt-injected model response cannot use the
#: evidence list as a place to dump arbitrary text into our database.
MAX_EVIDENCE_ITEMS = 6

EvidenceItem = Annotated[
    str,
    StringConstraints(min_length=1, max_length=240, strip_whitespace=True),
]

#: Which path produced a diagnosis. Recorded for auditability: a reviewer can tell
#: at a glance whether a classification came from deterministic rules or from Gemini.
DiagnosisMethod = Literal["rules", "llm", "fallback"]


def _utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


def is_recoverable(root_cause: str) -> bool:
    """Return whether a root cause is worth attempting recovery on.

    Deliberately a deterministic table lookup rather than a model judgement: "is
    this worth chasing" is the first question that shades into economics, and the
    LLM is not permitted to answer it.
    """
    return root_cause not in NON_RECOVERABLE_ROOT_CAUSES


# ---------------------------------------------------------------------------
# The diagnosis contract.
# ---------------------------------------------------------------------------


class Diagnosis(BaseModel):
    """Why a `RevenueEvent` is at risk — an explanation, and nothing else.

    There is deliberately no field here for a proposed action, an amount to
    charge, a recipient, or a cost. `extra="forbid"` means one cannot be added by
    a caller or by a model response either.
    """

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(
        ...,
        min_length=1,
        description="The `RevenueEvent.event_id` this diagnosis explains.",
    )
    surface: Surface = Field(
        ...,
        description="Copied from the event; determines the valid root-cause set.",
    )
    root_cause: str = Field(
        ...,
        description="One of the fixed values allowed for this surface.",
    )
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="How much to trust this classification.",
    )
    evidence: list[EvidenceItem] = Field(
        default_factory=list,
        max_length=MAX_EVIDENCE_ITEMS,
        description="Short factual observations supporting the root cause.",
    )
    recoverable: bool = Field(
        ...,
        description="Whether attempting recovery on this event is appropriate.",
    )
    diagnosed_at: datetime = Field(
        default_factory=_utc_now,
        description="When the diagnosis was produced (UTC).",
    )

    @model_validator(mode="after")
    def _root_cause_must_be_allowed_for_surface(self) -> "Diagnosis":
        """Reject any root cause outside this surface's closed set.

        This is the structural boundary: an invalid category cannot exist as a
        `Diagnosis` object, so it cannot reach storage regardless of which code
        path or model produced it.
        """
        allowed = ALLOWED_ROOT_CAUSES.get(self.surface)
        if allowed is None:  # pragma: no cover - Surface literal prevents this
            raise ValueError(f"No root-cause set defined for surface {self.surface!r}")
        if self.root_cause not in allowed:
            raise ValueError(
                f"root_cause {self.root_cause!r} is not allowed for surface "
                f"{self.surface!r}; allowed values are {sorted(allowed)}"
            )
        return self

    @model_validator(mode="after")
    def _suspected_fraud_is_never_recoverable(self) -> "Diagnosis":
        """Make a recoverable fraud diagnosis unconstructable."""
        if self.root_cause == "suspected_fraud" and self.recoverable:
            raise ValueError(
                "root_cause 'suspected_fraud' requires recoverable=False"
            )
        return self


class DiagnosisRecord(Diagnosis):
    """A stored `Diagnosis`, with its document id and append-only version."""

    id: str = Field(..., description="MongoDB document id, rendered as a string.")
    version: int = Field(
        ...,
        ge=1,
        description="1 for the first diagnosis of an event, incrementing thereafter.",
    )
    method: DiagnosisMethod = Field(
        ...,
        description="Which path produced this diagnosis.",
    )
    llm_model: str | None = Field(
        default=None,
        max_length=120,
        description=(
            "The model identifier that produced this diagnosis, or None when no "
            "model was called. `method` says whether an LLM was involved; this "
            "says which one. Provenance only — not a reproducibility guarantee, "
            "since a provider can change behaviour behind a stable model name."
        ),
    )

    @model_validator(mode="after")
    def _llm_model_only_when_a_model_answered(self) -> "DiagnosisRecord":
        """Keep `llm_model` and `method` from disagreeing about what happened.

        A rules-path record naming a model would be a false provenance claim, which
        is worse than no claim at all. Note the converse is deliberately allowed:
        `method="llm"` with `llm_model=None` is how records written before this field
        existed read back, and silently inventing a name for them would be a lie.
        """
        if self.method == "rules" and self.llm_model is not None:
            raise ValueError(
                "method 'rules' means no model was called, so llm_model must be None; "
                f"got {self.llm_model!r}"
            )
        return self

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> "DiagnosisRecord":
        """Build a record from a raw MongoDB document."""
        fields = {key: value for key, value in document.items() if key != "_id"}
        return cls(id=str(document["_id"]), **fields)


class LLMDiagnosisProposal(BaseModel):
    """The only shape a Gemini response is allowed to take.

    Note what is absent. There is no `event_id`, no `surface`, and no
    `diagnosed_at` — the caller sets those from the event, so the model cannot
    influence which event a diagnosis attaches to. There is no `recoverable`
    either: that is a deterministic lookup. And there is no action field of any
    kind, with `extra="forbid"` ensuring one cannot be introduced.

    A model response is a *proposal*. It becomes a `Diagnosis` only after the
    caller validates its root cause against the surface's closed set.
    """

    model_config = ConfigDict(extra="forbid")

    root_cause: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="Proposed root cause; validated against the allowed set by the caller.",
    )
    confidence: float = Field(..., ge=0.0, le=1.0)
    evidence: list[EvidenceItem] = Field(
        default_factory=list,
        max_length=MAX_EVIDENCE_ITEMS,
    )
