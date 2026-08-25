"""Domain models for execution (Stage 5 — what was actually done, and when).

The boundary this module enforces:

* `AuthorizedVerdict` is a *type* that only an authorized verdict satisfies. The
  executor takes one of those, not a `PolicyVerdictRecord`, so a blocked or
  review-pending verdict is not merely rejected by a runtime check — it cannot be
  passed in. See the class docstring for what that does and does not buy.
* `intervention` must be one the catalogue says is executable. The three
  `no_action` variants have no mapping in `ACTION_FOR_INTERVENTION`, so an
  `ExecutionRecord` claiming to have executed one is unconstructable.
* `action_type` is not free: it is derived from the intervention by a declared
  table and the validator rejects any other pairing. A payment link recorded as a
  `contact_logged` is not storable.
* The optional fields are populated *exactly* when their action type says they
  should be. A completed payment link without a link id, or a contact record
  carrying a link id, is rejected rather than stored as a half-fact.
* There is no field for whether the money came back. No `paid`, no `recovered`,
  no `amount_received`, no `outcome`, no `success`. `status` is about the API call
  we made, and its docstring says so. With `extra="forbid"`, a caller cannot add
  one — verifying whether an action worked is Stage 6 and needs its own evidence,
  not a boolean we set hopefully at send time.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.models.decision import NO_ACTION_INTERVENTIONS, InterventionName
from app.models.policy import PolicyVerdictRecord

_OBJECT_ID_PATTERN = r"^[0-9a-fA-F]{24}$"

# ---------------------------------------------------------------------------
# What kind of thing was done.
# ---------------------------------------------------------------------------

#: The shape of the action performed, as opposed to the intervention that called
#: for it. Two interventions can share an action type — both payment links produce
#: the same kind of artifact — and the distinction matters to Stage 6, which will
#: verify a generated link differently from a logged contact.
ActionType = Literal[
    "payment_link_generated",
    "retry_simulated",
    "contact_logged",
]

#: Whether the *attempt* succeeded. Not whether the money came back.
#:
#: `completed` means the side effect we intended actually happened: Razorpay
#: accepted the request and returned a link, or the contact record was written.
#: `failed` means it did not. Neither says anything about the customer's response,
#: which is unknowable at this point and is Stage 6's entire subject.
ExecutionStatus = Literal["completed", "failed"]

#: Which action each executable intervention produces. The single place the
#: mapping is declared, so the executor and the validator cannot disagree about
#: what a given intervention should have recorded.
#:
#: The three `no_action` variants are deliberately absent rather than mapped to
#: something inert. Policy refuses to authorize them (`decision_is_actionable`
#: fails, so the verdict is `blocked`), which means one arriving here is not a
#: case to handle gracefully — it is evidence that something upstream is broken,
#: and it should say so loudly.
ACTION_FOR_INTERVENTION: dict[str, str] = {
    # Real Razorpay test-mode payment links.
    "payment_method_update_link": "payment_link_generated",
    "recovery_payment_link": "payment_link_generated",
    # Also real payment links, recorded under their own action type because they
    # are an APPROXIMATION of a retry rather than one. See `app/execution/razorpay.py`.
    "immediate_retry": "retry_simulated",
    "delayed_retry": "retry_simulated",
    # No external call. A structured record of a message we would send.
    "reminder": "contact_logged",
    "escalating_reminder_sequence": "contact_logged",
    "manual_escalation": "contact_logged",
}

#: Action types that produce a Razorpay artifact, and must carry its id and URL
#: when they complete.
LINK_ACTION_TYPES: frozenset[str] = frozenset(
    {"payment_link_generated", "retry_simulated"}
)

#: Action types that record an outbound message, and must carry a channel and a
#: summary when they complete.
CONTACT_ACTION_TYPES: frozenset[str] = frozenset({"contact_logged"})

EXECUTABLE_INTERVENTIONS: frozenset[str] = frozenset(ACTION_FOR_INTERVENTION)

ALLOWED_ACTION_TYPES: frozenset[str] = frozenset(get_args(ActionType))


def _utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime, to the millisecond.

    Truncated for the same reason Stage 4 truncates, and with more at stake here:
    `executed_at` is now the anchor the cooldown is measured from, so it is read
    back out of BSON — which stores milliseconds — and compared against a fresh
    clock. Minting microseconds would mean the value the API returned and the value
    the cooldown later used were not the same number.
    """
    now = datetime.now(timezone.utc)
    return now.replace(microsecond=(now.microsecond // 1000) * 1000)


# ---------------------------------------------------------------------------
# The precondition, as a type.
# ---------------------------------------------------------------------------


class AuthorizedVerdict(PolicyVerdictRecord):
    """A stored verdict that granted permission. The executor's only input type.

    `verdict` and `reason` are narrowed to their single permitting values, so
    validation fails on a `blocked` or `requires_manual_review` document. The
    executor's signature therefore refuses an unauthorized verdict at the type
    level: there is no branch inside it that decides whether to proceed, because
    an instance that should not proceed cannot be constructed.

    What this buys, precisely: any path to execution must go through a
    constructor that re-reads the stored verdict's own `verdict` field. What it
    does not buy: safety against someone bypassing this class entirely and writing
    to the collection directly. That is what `app/execution/store.py`'s write-time
    referential guard is for, and why the audit re-checks every stored execution
    against the verdict it names. Three independent layers, because a type is a
    claim about code paths and not about the database.

    Narrowing `reason` as well as `verdict` is redundant against
    `PolicyVerdict._reason_must_match_verdict`, which already pins the two
    together. Kept because the redundancy is free and it makes the precondition
    legible in one line rather than by following a validator.
    """

    verdict: Literal["authorized"] = Field(
        ...,
        description="Only 'authorized'. Any other value fails validation here.",
    )
    reason: Literal["ok"] = Field(
        ...,
        description="Only 'ok', the sole reason an authorization can carry.",
    )


class NotAuthorized(ValueError):
    """Raised when a verdict cannot be narrowed to an `AuthorizedVerdict`.

    Distinct from Pydantic's `ValidationError` so callers can answer 409 for "this
    verdict does not permit execution" without also swallowing genuine schema
    problems in the same handler.
    """


def require_authorized(document: dict[str, Any]) -> AuthorizedVerdict:
    """Narrow a stored verdict document to `AuthorizedVerdict`, or refuse loudly.

    The one supported way into execution. Raises:
        NotAuthorized: if the verdict is blocked or awaiting review.
    """
    verdict = document.get("verdict")
    if verdict != "authorized":
        raise NotAuthorized(
            f"verdict for event {document.get('event_id')!r} is {verdict!r}, not "
            "'authorized'; execution is not permitted and no record will be written"
        )
    return AuthorizedVerdict.from_document(document)


# ---------------------------------------------------------------------------
# The execution contract.
# ---------------------------------------------------------------------------


class ExecutionRecord(BaseModel):
    """What was done for one authorized verdict, and when.

    One of these exists per verdict at most — enforced by a unique index on
    `policy_verdict_id`, not by hope. Note what is absent: nothing here reports
    whether the customer paid, whether the link was opened, or whether revenue was
    recovered. This record is evidence that an action was taken; evidence that it
    worked is a different kind of fact, arrives later, and belongs to Stage 6.
    """

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(
        ...,
        min_length=1,
        description="The `RevenueEvent.event_id` this action was taken for.",
    )
    policy_verdict_id: str = Field(
        ...,
        pattern=_OBJECT_ID_PATTERN,
        description=(
            "MongoDB id of the exact verdict that authorized this. The "
            "idempotency key: unique across the collection, so a second "
            "execution of the same permission cannot be inserted. Verdicts are "
            "append-only, so pinning the id — not the event — is what stops this "
            "record being read as the execution of some later authorization."
        ),
    )
    policy_verdict_version: int = Field(
        ...,
        ge=1,
        description="Version of that verdict, carried for human readability.",
    )
    intervention: InterventionName = Field(
        ...,
        description=(
            "Copied from the decision the verdict authorized. Verified against "
            "that decision at write time rather than trusted."
        ),
    )
    action_type: ActionType = Field(
        ...,
        description="The shape of what was done. Derived from `intervention`.",
    )
    razorpay_payment_link_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        description="Razorpay's id for the generated link. Link actions only.",
    )
    razorpay_payment_link_url: (
        Annotated[str, StringConstraints(pattern=r"^https://", max_length=500)] | None
    ) = Field(
        default=None,
        description=(
            "The short URL Razorpay returned. Link actions only. Constrained to "
            "https so a record cannot claim a link that was never served securely."
        ),
    )
    contact_channel: str | None = Field(
        default=None,
        min_length=1,
        max_length=40,
        description=(
            "Where the message went. A PLACEHOLDER: the system holds a "
            "`customer_ref` and no address of any kind, so this names the channel "
            "a real integration would use, not one that was used."
        ),
    )
    contact_message_summary: str | None = Field(
        default=None,
        min_length=1,
        max_length=300,
        description=(
            "One line identifying the template, its version, the channel and the "
            "rendered subject. Not the body — see `app/execution/templates.py` for "
            "why the body is reconstructed rather than stored."
        ),
    )
    executed_at: datetime = Field(
        default_factory=_utc_now,
        description=(
            "THE REAL SEND TIMESTAMP (UTC): when the Razorpay call returned, or "
            "when the contact was logged. This is what the cooldown is measured "
            "from, which is why it is minted after the side effect rather than "
            "before it."
        ),
    )
    status: ExecutionStatus = Field(
        ...,
        description=(
            "Whether the attempt itself succeeded. NOT whether money was "
            "recovered — that is unknown here and always will be at this stage."
        ),
    )
    failure_reason: str | None = Field(
        default=None,
        min_length=1,
        max_length=500,
        description="Why the attempt failed. Required when status is 'failed'.",
    )

    @model_validator(mode="after")
    def _intervention_must_be_executable(self) -> "ExecutionRecord":
        """Reject an execution of something that cannot be executed.

        A `no_action` intervention reaching here is a structural error, not an
        edge case: policy blocks it, so its presence means the verdict, the
        decision or the executor disagree about what was authorized.
        """
        if self.intervention in NO_ACTION_INTERVENTIONS:
            raise ValueError(
                f"{self.intervention!r} attempts nothing and cannot be executed; "
                "policy refuses to authorize it, so an execution record for one "
                "means an unauthorized action reached this stage"
            )
        expected = ACTION_FOR_INTERVENTION.get(self.intervention)
        if expected is None:  # pragma: no cover - the Literal precedes this
            raise ValueError(
                f"{self.intervention!r} has no declared action type; it cannot be "
                "executed until ACTION_FOR_INTERVENTION says how"
            )
        if self.action_type != expected:
            raise ValueError(
                f"{self.intervention!r} produces {expected!r}, not "
                f"{self.action_type!r}; the action type is not a free choice"
            )
        return self

    @model_validator(mode="after")
    def _failure_must_be_explained(self) -> "ExecutionRecord":
        """A failure needs a reason; a success must not carry one."""
        if self.status == "failed" and not self.failure_reason:
            raise ValueError(
                "status 'failed' requires a failure_reason; an unexplained "
                "failure is not a record of anything"
            )
        if self.status == "completed" and self.failure_reason is not None:
            raise ValueError(
                f"status 'completed' cannot carry failure_reason "
                f"{self.failure_reason!r}"
            )
        return self

    @model_validator(mode="after")
    def _artifacts_must_match_the_action(self) -> "ExecutionRecord":
        """Each optional field is populated exactly when its action requires it.

        Both directions matter. A completed link action without a link id would
        assert that a link exists while giving nothing to verify against; a
        contact record carrying a link id would attribute an artifact to an action
        that never created one.

        A failed attempt carries neither, because nothing was created. If a
        Razorpay call ever returns a link and *then* fails downstream, that is a
        `completed` execution with a real artifact, not a failure.
        """
        link_fields = {
            "razorpay_payment_link_id": self.razorpay_payment_link_id,
            "razorpay_payment_link_url": self.razorpay_payment_link_url,
        }
        contact_fields = {
            "contact_channel": self.contact_channel,
            "contact_message_summary": self.contact_message_summary,
        }

        wants_link = self.status == "completed" and self.action_type in LINK_ACTION_TYPES
        wants_contact = (
            self.status == "completed" and self.action_type in CONTACT_ACTION_TYPES
        )

        if wants_link:
            missing = sorted(name for name, value in link_fields.items() if not value)
            if missing:
                raise ValueError(
                    f"a completed {self.action_type!r} must record {missing}; "
                    "without them there is no artifact to verify later"
                )
        else:
            present = sorted(name for name, value in link_fields.items() if value)
            if present:
                raise ValueError(
                    f"{present} set on a {self.status} {self.action_type!r}, which "
                    "creates no Razorpay artifact"
                )

        if wants_contact:
            missing = sorted(name for name, value in contact_fields.items() if not value)
            if missing:
                raise ValueError(
                    f"a completed {self.action_type!r} must record {missing}"
                )
        else:
            present = sorted(name for name, value in contact_fields.items() if value)
            if present:
                raise ValueError(
                    f"{present} set on a {self.status} {self.action_type!r}, which "
                    "logs no contact"
                )
        return self

    @model_validator(mode="after")
    def _executed_at_must_be_aware(self) -> "ExecutionRecord":
        """Reject a naive send timestamp.

        Stricter than the other stages' timestamps because this one is arithmetic
        input: the cooldown subtracts it from `now`, and a naive value would raise
        at comparison time inside a policy check rather than here.
        """
        if self.executed_at.tzinfo is None:
            raise ValueError(
                "executed_at must be timezone-aware; the cooldown measures from it"
            )
        return self


class ExecutionRecordDocument(ExecutionRecord):
    """A stored `ExecutionRecord`, with its document id."""

    id: str = Field(..., description="MongoDB document id, rendered as a string.")

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> "ExecutionRecordDocument":
        """Build a record from a raw MongoDB document."""
        fields = {key: value for key, value in document.items() if key != "_id"}
        return cls(id=str(document["_id"]), **fields)
