"""Domain models for verification (Stage 6 Part A — did the money actually arrive?).

Stage 5 records that an action was taken. This module records what came back, and
the boundary it enforces is the mirror image of Stage 5's:

* there is no field for a diagnosis, a decision, an intervention, a policy check,
  or a verdict. With `extra="forbid"` a caller cannot add one. A
  `VerificationRecord` states an outcome and nothing else — receiving a webhook
  must not re-explain why the payment failed, re-choose what to do about it, or
  re-run the policy gate. Those stages already ran, are recorded, and are not
  re-opened by an inbound HTTP request from a third party;
* `outcome` is not a free choice. It is derived from the Razorpay event name by
  `OUTCOME_FOR_EVENT`, and the validator rejects any other pairing, so a record
  cannot claim `recovered` on the strength of a `payment_link.expired`;
* `amount_recovered` is zero unless the outcome is `recovered`, and non-zero when
  it is. A recovery of nothing, or an expiry that recovered something, is
  unstorable rather than merely unlikely;
* `amount_mismatch` is re-derived from the two amounts by a validator, exactly as
  Stage 3 re-derives `expected_recovery_value`. It cannot be set to `False` on a
  record whose numbers disagree, which is the whole point of recording it;
* the reference to the `ExecutionRecord` is an ObjectId, checked at write time
  against the stored execution — see `app/webhooks/store.py`. A verification that
  cannot name the exact action it verifies is not evidence about that action.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.decision import MONEY_PRECISION

_OBJECT_ID_PATTERN = r"^[0-9a-fA-F]{24}$"

# ---------------------------------------------------------------------------
# What Razorpay tells us, and what it means.
# ---------------------------------------------------------------------------

#: The Razorpay webhook events this system subscribes to. Payment-link events
#: only: those are the artifacts Stage 5 creates, so they are the only ones this
#: system has a record to reconcile against.
RazorpayLinkEvent = Literal[
    "payment_link.paid",
    "payment_link.expired",
    "payment_link.cancelled",
]

#: What a verification can conclude.
#:
#: `not_recovered` has no producer among the three subscribed events and is
#: therefore not currently constructable — deliberately. It is the general "this
#: attempt yielded nothing" outcome, of which `expired` and `cancelled` are the two
#: specific cases Razorpay actually reports. Keeping the word in the vocabulary
#: without inventing a producer for it is the same choice Stage 4 made with
#: `requires_manual_review`, which has no approver endpoint: the type can say it,
#: and nothing fabricates it.
VerificationOutcome = Literal[
    "recovered",
    "not_recovered",
    "expired",
    "cancelled",
]

#: Which outcome each subscribed event states. The single declaration, so the
#: receiver and the validator cannot disagree about what an event means.
OUTCOME_FOR_EVENT: dict[str, str] = {
    "payment_link.paid": "recovered",
    "payment_link.expired": "expired",
    "payment_link.cancelled": "cancelled",
}

#: The one outcome that means money moved.
RECOVERED_OUTCOME = "recovered"

SUBSCRIBED_EVENTS: frozenset[str] = frozenset(get_args(RazorpayLinkEvent))
ALLOWED_OUTCOMES: frozenset[str] = frozenset(get_args(VerificationOutcome))

#: How far two amounts may differ and still count as equal, in major units. One
#: paise. Not `ERV_TOLERANCE` from Stage 3 despite the identical value: that one
#: bounds a re-derived expected value, this one bounds a comparison between a
#: number we chose and a number a payment gateway reported. Same magnitude, and
#: they would be changed for unrelated reasons.
AMOUNT_TOLERANCE = 0.01

assert set(OUTCOME_FOR_EVENT) == SUBSCRIBED_EVENTS, (
    f"OUTCOME_FOR_EVENT covers {sorted(OUTCOME_FOR_EVENT)}, but the subscribed "
    f"events are {sorted(SUBSCRIBED_EVENTS)}"
)
assert set(OUTCOME_FOR_EVENT.values()) <= ALLOWED_OUTCOMES, (
    "OUTCOME_FOR_EVENT names an outcome outside VerificationOutcome"
)
assert RECOVERED_OUTCOME in ALLOWED_OUTCOMES


def _utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime, to the millisecond.

    Truncated for the same reason `app/models/execution.py` truncates: the value is
    written to BSON, which stores milliseconds, and is compared against values read
    back out. Minting microseconds would mean the timestamp the API returned and the
    timestamp stored were not the same number.
    """
    now = datetime.now(timezone.utc)
    return now.replace(microsecond=(now.microsecond // 1000) * 1000)


def amounts_differ(recovered: float, expected: float) -> bool:
    """Whether two amounts disagree by more than a paise."""
    return abs(round(recovered, MONEY_PRECISION) - round(expected, MONEY_PRECISION)) > (
        AMOUNT_TOLERANCE
    )


# ---------------------------------------------------------------------------
# The verification contract.
# ---------------------------------------------------------------------------


class VerificationRecord(BaseModel):
    """One statement, from Razorpay, about the fate of one executed action.

    Append-only and one per Razorpay event: `razorpay_event_id` is unique-indexed,
    so a re-delivered webhook cannot produce a second record. Razorpay's own event
    id is used rather than one of ours precisely because the duplicate we are
    guarding against is *their* retry of *their* event.
    """

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(
        ...,
        min_length=1,
        description="The `RevenueEvent.event_id` whose recovery this concerns.",
    )
    execution_id: str = Field(
        ...,
        pattern=_OBJECT_ID_PATTERN,
        description=(
            "MongoDB id of the exact `ExecutionRecord` this verifies — the action "
            "that produced the payment link Razorpay is reporting on. There is no "
            "companion version field because executions have none: one execution "
            "exists per authorized verdict, forever, so the id is already exact."
        ),
    )
    razorpay_event_id: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description=(
            "Razorpay's `x-razorpay-event-id` header, unique per event on their "
            "side. THE IDEMPOTENCY KEY: unique-indexed, so an at-least-once "
            "re-delivery is refused by the database and not merely skipped by a "
            "check that could lose a race."
        ),
    )
    razorpay_event: RazorpayLinkEvent = Field(
        ...,
        description="The event name Razorpay sent. Determines `outcome`.",
    )
    razorpay_payment_link_id: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description=(
            "The link the payload named, as it arrived. Matched against the "
            "execution's own recorded link id at write time rather than trusted."
        ),
    )
    outcome: VerificationOutcome = Field(
        ...,
        description="What the event means. Derived from `razorpay_event`.",
    )
    amount_recovered: float = Field(
        ...,
        ge=0,
        description=(
            "What Razorpay confirmed arrived, in major units. Zero unless the "
            "outcome is 'recovered'. Taken from Razorpay's `amount_paid`, never "
            "from what we hoped for — the two are compared, not conflated."
        ),
    )
    amount_expected: float = Field(
        ...,
        gt=0,
        description=(
            "What the link was created for: the authorized decision's "
            "`revenue_at_risk`. Stored alongside the actual so the comparison "
            "behind `amount_mismatch` can be re-checked from the record itself."
        ),
    )
    amount_mismatch: bool = Field(
        ...,
        description=(
            "Whether a recovery came in for something other than the expected "
            "amount. Re-derived by a validator, so it cannot be stored as False "
            "on a record whose numbers disagree."
        ),
    )
    verified_at: datetime = Field(
        default_factory=_utc_now,
        description="When this system processed the webhook (UTC), not when Razorpay sent it.",
    )

    @model_validator(mode="after")
    def _outcome_must_follow_the_event(self) -> "VerificationRecord":
        """The outcome is dictated by the event name, not chosen.

        Without this, a `payment_link.expired` could be stored as `recovered` and
        the event's status would follow it. The outcome is the one field the whole
        stage turns on, so it is the one field with no discretion in it.
        """
        expected = OUTCOME_FOR_EVENT.get(self.razorpay_event)
        if expected is None:  # pragma: no cover - the Literal precedes this
            raise ValueError(
                f"{self.razorpay_event!r} is not a subscribed event; "
                "OUTCOME_FOR_EVENT does not say what it would mean"
            )
        if self.outcome != expected:
            raise ValueError(
                f"{self.razorpay_event!r} means {expected!r}, not {self.outcome!r}; "
                "the outcome is derived from the event and is not a free choice"
            )
        return self

    @model_validator(mode="after")
    def _money_must_match_the_outcome(self) -> "VerificationRecord":
        """A recovery moved money; anything else moved none."""
        if self.outcome == RECOVERED_OUTCOME:
            if self.amount_recovered <= 0:
                raise ValueError(
                    "outcome 'recovered' with amount_recovered "
                    f"{self.amount_recovered!r} claims a recovery of nothing"
                )
        elif self.amount_recovered != 0:
            raise ValueError(
                f"outcome {self.outcome!r} cannot carry amount_recovered "
                f"{self.amount_recovered!r}; only a recovery moves money"
            )
        return self

    @model_validator(mode="after")
    def _mismatch_must_be_the_truth(self) -> "VerificationRecord":
        """Re-derive `amount_mismatch` rather than trusting it.

        Only meaningful on a recovery: on an expiry or cancellation nothing
        arrived, so there is no discrepancy to report and the flag is always False.
        """
        expected = (
            amounts_differ(self.amount_recovered, self.amount_expected)
            if self.outcome == RECOVERED_OUTCOME
            else False
        )
        if self.amount_mismatch != expected:
            raise ValueError(
                f"amount_mismatch is {self.amount_mismatch!r} but "
                f"{self.amount_recovered!r} against an expected "
                f"{self.amount_expected!r} on a {self.outcome!r} outcome makes it "
                f"{expected!r}; the flag is derived, not asserted"
            )
        return self

    @model_validator(mode="after")
    def _verified_at_must_be_aware(self) -> "VerificationRecord":
        """Reject a naive timestamp, as every other stage's record does."""
        if self.verified_at.tzinfo is None:
            raise ValueError("verified_at must be timezone-aware")
        return self


class VerificationRecordDocument(VerificationRecord):
    """A stored `VerificationRecord`, with its document id."""

    id: str = Field(..., description="MongoDB document id, rendered as a string.")

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> "VerificationRecordDocument":
        """Build a record from a raw MongoDB document."""
        fields = {key: value for key, value in document.items() if key != "_id"}
        return cls(id=str(document["_id"]), **fields)


class WebhookAck(BaseModel):
    """What `POST /webhooks/razorpay` answers with.

    Razorpay treats any non-2xx as a delivery failure and retries with backoff for
    24 hours, then disables the endpoint. So every authentic request is
    acknowledged — including ones this system could do nothing with — and the body
    reports which of those happened. `received: true` means "we have it", never
    "we acted on it".
    """

    model_config = ConfigDict(extra="forbid")

    received: bool = Field(default=True, description="Always true on a 2xx.")
    razorpay_event_id: str = Field(..., description="Echo of the header, for tracing.")
    razorpay_event: str = Field(..., description="Echo of the event name.")
    processed: bool = Field(
        ...,
        description="Whether this call produced a new VerificationRecord.",
    )
    detail: str = Field(
        ...,
        min_length=1,
        description="Why, in one line — duplicate, unmatched link, ignored event, or recorded.",
    )
    verification_id: str | None = Field(
        default=None,
        description="The record's id, when one was created or already existed.",
    )
    event_status: str | None = Field(
        default=None,
        description="The originating event's status after this webhook, when known.",
    )
