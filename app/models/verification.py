"""Domain models for verification (Stage 6 Part A — did the money actually arrive?).

Stage 5 records that an action was taken. This module records what came back, and
the boundary it enforces is the mirror image of Stage 5's:

* there is no field for a diagnosis, a decision, an intervention, a policy check,
  or a verdict. With `extra="forbid"` a caller cannot add one. A verification
  states an outcome and nothing else — receiving a webhook must not re-explain why
  the payment failed, re-choose what to do about it, or re-run the policy gate.
  Those stages already ran, are recorded, and are not re-opened by an inbound HTTP
  request from a third party;
* `outcome` is not a free choice. On a webhook record it is derived from the
  Razorpay event name by `OUTCOME_FOR_EVENT`, and the validator rejects any other
  pairing, so a record cannot claim `recovered` on the strength of a
  `payment_link.expired`. On a manual record it is pinned to `recovered` by the
  type itself — see `ManualVerification`;
* `amount_recovered` is zero unless the outcome is `recovered`, and non-zero when
  it is. A recovery of nothing, or an expiry that recovered something, is
  unstorable rather than merely unlikely;
* `amount_mismatch` is re-derived from the two amounts by a validator, exactly as
  Stage 3 re-derives `expected_recovery_value`. It cannot be set to `False` on a
  record whose numbers disagree, which is the whole point of recording it;
* the reference to the `ExecutionRecord` is an ObjectId, checked at write time
  against the stored execution — see `app/webhooks/store.py`. A verification that
  cannot name the exact action it verifies is not evidence about that action.

**Stage 9 — two sources, one collection, no blurring.** A verification is now a
discriminated union on `source`. `WebhookVerification` is Razorpay's signed word
about a payment link and is the only kind that existed before Stage 9.
`ManualVerification` is a merchant's assertion that a receivable arrived after a
contact-type intervention, where there is no gateway artifact for any webhook to
report on.

The two are separated at the *type* level rather than by a flag, for the reason
`AuthorizedVerdict` narrows a verdict and `BaselineComparison` narrows `kind`:
gateway-verified money and merchant-asserted money must never be summable without
the reader having decided to sum them. `source` is required on every new record and
surfaced in `/metrics/summary` and `/metrics/by-intervention`, so a headline number
can always be split back apart.

They share one collection, deliberately. `has_recovered` in
`app/webhooks/service.py` is the single definition of "has this been paid", and
every Stage 7 aggregation reads `verifications`. A second collection would mean a
second read in both places, and a guard assembled twice is a guard that drifts.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Literal, Mapping, Union, get_args

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

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


# ---------------------------------------------------------------------------
# Where the statement came from. Stage 9.
# ---------------------------------------------------------------------------

#: How a recovery was established. The discriminator of the union below, and the
#: field every reader uses to keep gateway-verified money apart from
#: merchant-asserted money.
VerificationSource = Literal["webhook", "manual_confirmation"]

#: Razorpay said so, over a signature-verified webhook, about a link it hosts.
WEBHOOK_SOURCE = "webhook"

#: A human said so, through `POST /executions/{id}/confirm-payment`. There is no
#: gateway artifact behind this and there never can be — the intervention was a
#: contact, not a link — so it is evidence of a different and weaker kind, and the
#: word for that difference is on every record.
MANUAL_SOURCE = "manual_confirmation"

ALLOWED_SOURCES: frozenset[str] = frozenset(get_args(VerificationSource))

assert {WEBHOOK_SOURCE, MANUAL_SOURCE} == ALLOWED_SOURCES

#: Prefix of the manual idempotency key. Namespaced so a `confirmation_id` can
#: never be mistaken for a Razorpay identifier by a human reading the collection.
MANUAL_CONFIRMATION_PREFIX = "manual_conf_"

#: What `confirmed_by` records on a manual confirmation.
#:
#: This build has no authentication on any endpoint, so this names the CHANNEL the
#: assertion arrived through and NOT a verified identity. Saying "merchant" alone
#: would be a claim this system cannot support. The field is server-set — the
#: request model forbids it — because a caller-supplied actor that nothing checks is
#: worse than an honest channel name.
MANUAL_CONFIRMATION_CHANNEL = "merchant_via_api"

#: How far ahead of the recording clock a merchant-asserted `confirmed_at` may sit
#: before it is refused. Sixty seconds, for ordinary clock skew between the caller
#: and this process. Anything further ahead is a confirmation of money that has not
#: arrived yet, which is not a confirmation.
CONFIRMED_AT_FUTURE_TOLERANCE_SECONDS = 60.0


def confirmation_id_for(execution_id: str) -> str:
    """The one confirmation id an execution can ever have.

    Derived from the execution rather than minted, which is what makes "at most one
    manual confirmation per execution" a property of the unique index instead of a
    pre-flight check that can lose a race. Same arrangement as Stage 5 keying
    execution idempotency on `policy_verdict_id`, and for the same reason: the
    caller supplies no key, so the caller cannot supply a second one.
    """
    return f"{MANUAL_CONFIRMATION_PREFIX}{execution_id}"


def source_of(document: Mapping[str, Any]) -> str:
    """Read a stored verification's source, defaulting a pre-Stage-9 record to webhook.

    The single place that default lives. Records written before `source` existed are
    all webhook records by construction — the only writer was `reconcile`, and every
    one of them carries a `razorpay_event_id` — so the default is a statement of
    fact about the existing rows and not a guess. Kept as a read-time shim rather
    than backfilled onto those rows so that Stage 9's only write against existing
    data remains the index migration.
    """
    source = document.get("source")
    return WEBHOOK_SOURCE if source is None else str(source)


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
# The verification contract — what both sources must say.
# ---------------------------------------------------------------------------


class VerificationBase(BaseModel):
    """The fields a verification carries whatever established it.

    Every field here is source-agnostic: which action it concerns, what happened,
    how much, and when this system wrote it down. Nothing in this class names
    Razorpay, and nothing in it names a person — those belong to the two subclasses,
    which is the point of splitting them.

    The three validators below are shared, so the arithmetic guarantees hold
    identically on a manual record and a webhook one. A merchant asserting a
    recovery of zero, or asserting `amount_mismatch=False` over disagreeing
    amounts, is refused by exactly the same code that refuses it of Razorpay.
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
            "this statement is about. There is no companion version field because "
            "executions have none: one execution exists per authorized verdict, "
            "forever, so the id is already exact."
        ),
    )
    outcome: VerificationOutcome = Field(
        ...,
        description=(
            "What happened. Never a free choice: derived from `razorpay_event` on a "
            "webhook record, pinned to 'recovered' by the type on a manual one."
        ),
    )
    amount_recovered: float = Field(
        ...,
        ge=0,
        description=(
            "What arrived, in major units. Zero unless the outcome is 'recovered'. "
            "On a webhook record this is Razorpay's `amount_paid`, never what we "
            "hoped for; on a manual record it is what the merchant states arrived. "
            "Either way it is compared against `amount_expected`, not conflated "
            "with it."
        ),
    )
    amount_expected: float = Field(
        ...,
        gt=0,
        description=(
            "What was owed: the authorized decision's `revenue_at_risk`. Stored "
            "alongside the actual so the comparison behind `amount_mismatch` can be "
            "re-checked from the record itself. Derived from the execution's own "
            "verdict chain in both paths — a caller cannot supply it."
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
        description=(
            "When THIS SYSTEM wrote the record (UTC) — not when Razorpay sent the "
            "webhook, and not when a merchant says the money arrived. Always this "
            "process's own clock, because it is the sort key every reader orders "
            "history by; see `ManualVerification.confirmed_at` for the asserted time."
        ),
    )

    @model_validator(mode="after")
    def _money_must_match_the_outcome(self) -> "VerificationBase":
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
    def _mismatch_must_be_the_truth(self) -> "VerificationBase":
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
    def _verified_at_must_be_aware(self) -> "VerificationBase":
        """Reject a naive timestamp, as every other stage's record does."""
        if self.verified_at.tzinfo is None:
            raise ValueError("verified_at must be timezone-aware")
        return self


class WebhookVerification(VerificationBase):
    """One statement, from Razorpay, about the fate of one executed action.

    Append-only and one per Razorpay event: `razorpay_event_id` is unique-indexed,
    so a re-delivered webhook cannot produce a second record. Razorpay's own event
    id is used rather than one of ours precisely because the duplicate we are
    guarding against is *their* retry of *their* event.

    This is the whole of Stage 6 Part A, unchanged by Stage 9 apart from carrying
    the `source` tag. Link-producing interventions have this path and only this
    path; there is no manual override for them, which is the point of having a real
    verification channel.
    """

    source: Literal["webhook"] = Field(
        default=WEBHOOK_SOURCE,
        description=(
            "Razorpay's signed word. The default because it is the only source that "
            "existed before Stage 9, which is also what makes the read-time default "
            "in `source_of` correct for the records already stored."
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
            "check that could lose a race. Absent from manual records, which is why "
            "that index is partial — see `app/webhooks/store.py`."
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

    @model_validator(mode="after")
    def _outcome_must_follow_the_event(self) -> "WebhookVerification":
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


class ManualVerification(VerificationBase):
    """A merchant's assertion that a receivable arrived. Stage 9.

    The verification path for contact-type interventions. A reminder, an escalating
    sequence and a manual escalation create no Razorpay artifact, so no webhook can
    ever report on them; before this existed, an event routed to one of them was
    structurally unable to show as recovered no matter what the customer did, and
    the metrics said 0% forever.

    Three things this class is careful about, because it is the weaker kind of
    evidence:

    * `outcome` is `Literal["recovered"]` with a default, so this type cannot state
      anything but a recovery. There is no manual channel for asserting an expiry
      or a cancellation, and the type says so rather than a docstring saying so;
    * `confirmation_id` is *derived* from `execution_id` by `confirmation_id_for`
      and re-checked by a validator, so one execution admits exactly one
      confirmation and a re-confirmation collides with the unique index instead of
      appending a second recovery for the same money;
    * `confirmed_by` names the channel, not a person. This build authenticates
      nobody — see `MANUAL_CONFIRMATION_CHANNEL`.

    What it deliberately does not do is pretend to be a webhook. There is no
    synthesised `razorpay_event_id` and no fabricated link id: an asserted recovery
    with gateway-shaped provenance would be indistinguishable from a verified one a
    week later, which is exactly the confusion `source` exists to prevent.
    """

    source: Literal["manual_confirmation"] = Field(
        default=MANUAL_SOURCE,
        description=(
            "A human asserted this. Not verified by any gateway, and reported "
            "separately from webhook recoveries wherever money is totalled."
        ),
    )
    outcome: Literal["recovered"] = Field(
        default=RECOVERED_OUTCOME,
        description=(
            "Always 'recovered'. The manual channel confirms payment and nothing "
            "else; narrowed here so an assertion of failure is unconstructable "
            "rather than merely unroutable."
        ),
    )
    confirmation_id: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description=(
            "THE IDEMPOTENCY KEY for this path: `manual_conf_` followed by the "
            "execution's id, partial-unique-indexed. Derived rather than supplied, "
            "and re-derived by a validator, so 'one confirmation per execution' is "
            "enforced by the database and not by a check above the write."
        ),
    )
    confirmed_by: str = Field(
        ...,
        min_length=1,
        max_length=60,
        description=(
            "The channel the assertion arrived through, NOT a verified identity — "
            "this build authenticates no caller. Server-set; the request model "
            "forbids it, because an unchecked actor name is worse than an honest "
            "channel name."
        ),
    )
    confirmed_at: datetime = Field(
        default_factory=_utc_now,
        description=(
            "When the merchant says the money arrived (UTC). Distinct from "
            "`verified_at`, which is this system's own clock: a receivable is often "
            "confirmed days after it was paid, and letting an asserted timestamp "
            "into the field every reader sorts by would let a backdated "
            "confirmation reorder recovery history."
        ),
    )

    @model_validator(mode="after")
    def _confirmation_id_must_be_derived(self) -> "ManualVerification":
        """The idempotency key is a function of the execution, not an input.

        A caller-chosen key would make the unique index decorative: two keys for one
        execution would both insert, and the same money would be counted twice.
        """
        expected = confirmation_id_for(self.execution_id)
        if self.confirmation_id != expected:
            raise ValueError(
                f"confirmation_id {self.confirmation_id!r} is not the one execution "
                f"{self.execution_id!r} admits ({expected!r}); the key is derived "
                "from the execution and is not a free choice"
            )
        return self

    @model_validator(mode="after")
    def _confirmed_at_must_be_aware_and_not_ahead(self) -> "ManualVerification":
        """Reject a naive asserted time, and one that has not happened yet."""
        if self.confirmed_at.tzinfo is None:
            raise ValueError("confirmed_at must be timezone-aware")
        ceiling = self.verified_at + timedelta(
            seconds=CONFIRMED_AT_FUTURE_TOLERANCE_SECONDS
        )
        if self.confirmed_at > ceiling:
            raise ValueError(
                f"confirmed_at {self.confirmed_at.isoformat()} is more than "
                f"{CONFIRMED_AT_FUTURE_TOLERANCE_SECONDS:.0f}s ahead of "
                f"{self.verified_at.isoformat()}; money that has not arrived yet "
                "cannot be confirmed as arrived"
            )
        return self


#: A verification, of either kind. The annotation every writer and reader uses.
#:
#: Discriminated on `source` rather than left as a plain union so that a raw
#: document resolves to exactly one variant by tag — an untagged union would try
#: `WebhookVerification` first and report a missing `razorpay_event_id` for a
#: perfectly valid manual record.
VerificationRecord = Annotated[
    Union[WebhookVerification, ManualVerification],
    Field(discriminator="source"),
]


class WebhookVerificationDocument(WebhookVerification):
    """A stored `WebhookVerification`, with its document id."""

    id: str = Field(..., description="MongoDB document id, rendered as a string.")


class ManualVerificationDocument(ManualVerification):
    """A stored `ManualVerification`, with its document id."""

    id: str = Field(..., description="MongoDB document id, rendered as a string.")


#: A stored verification of either kind.
VerificationRecordDocument = Annotated[
    Union[WebhookVerificationDocument, ManualVerificationDocument],
    Field(discriminator="source"),
]

_DOCUMENT_ADAPTER: TypeAdapter[
    Union[WebhookVerificationDocument, ManualVerificationDocument]
] = TypeAdapter(VerificationRecordDocument)


def verification_document(
    document: Mapping[str, Any],
) -> Union[WebhookVerificationDocument, ManualVerificationDocument]:
    """Build the right document model from a raw MongoDB document.

    Replaces the `VerificationRecordDocument.from_document` classmethod the single
    model had, because a discriminated union is not a class and cannot carry one.
    The `source` tag is filled in by `source_of`, which is what lets the 42
    pre-Stage-9 records parse without being rewritten.

    Raises:
        pydantic.ValidationError: the document does not satisfy either variant.
    """
    fields = {key: value for key, value in document.items() if key != "_id"}
    fields["source"] = source_of(document)
    return _DOCUMENT_ADAPTER.validate_python({"id": str(document["_id"]), **fields})


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


# ---------------------------------------------------------------------------
# Stage 9 — the manual confirmation request and its receipt.
# ---------------------------------------------------------------------------


class ManualPaymentConfirmation(BaseModel):
    """The body of `POST /executions/{execution_id}/confirm-payment`.

    Two fields, and the list of what is absent is the interesting part. There is no
    `event_id` — it comes from the execution, so a caller cannot attribute a
    payment to a different event than the one the action was taken for. No
    `outcome`, no `amount_expected`, no `amount_mismatch`, no `confirmation_id`, no
    `confirmed_by`, no `source`: every one of those is derived by the server, and
    `extra="forbid"` means an attempt to smuggle one in is a 422 rather than a
    silently ignored key.

    `amount_recovered` is `strict=True`, and it is the only field in this file that
    is. It is the one value in the whole record that cannot be re-derived from
    anything else: `amount_expected` comes from the verdict chain, `event_id` from
    the execution, `confirmation_id` from the execution id, `source` and
    `confirmed_by` are constants. Everything else can be checked against a second
    source; this cannot, so it is checked against nothing and has to arrive exactly
    as sent.

    Without `strict`, Pydantic's lax mode coerces `"100.00"`, `"1e3"`, `" 100 "` and
    `true` into floats, which means a client bug that stringifies or boolean-ifies an
    amount is silently accepted as money. An adversarial probe during Stage 9
    verification wrote a real 100.00 record this way against an expected 2,218.95 —
    correctly, by the rules then in force — which is what prompted this; the incident
    is in `docs/data-corrections.md`. Strict mode still accepts a JSON integer, so a
    whole-rupee `2218` is fine.

    The strictness is here and not on `ManualVerification`. This is the trust
    boundary — the record model's job is the arithmetic invariants, and it also
    parses documents back out of MongoDB, where numeric types are BSON's business
    rather than a caller's.
    """

    model_config = ConfigDict(extra="forbid")

    amount_recovered: float = Field(
        ...,
        gt=0,
        strict=True,
        description=(
            "What the merchant states arrived, in major units. Must be positive: a "
            "confirmation of zero is not a confirmation, and the record model "
            "refuses it too. Compared against the amount the authorized decision "
            "said was at risk, and any difference is recorded as a mismatch rather "
            "than smoothed away. Must be a JSON number, not a quoted one."
        ),
    )
    confirmed_at: datetime | None = Field(
        default=None,
        description=(
            "When the money arrived, if the merchant knows. Defaults to now. Must "
            "be timezone-aware and not in the future. This does NOT become the "
            "record's `verified_at`, which stays this system's own clock."
        ),
    )

    @model_validator(mode="after")
    def _confirmed_at_must_be_aware_and_not_ahead(self) -> "ManualPaymentConfirmation":
        """Reject a naive or future timestamp at the edge, before it reaches the record.

        `ManualVerification` enforces both of these too, and deliberately keeps doing
        so — it is the persistence invariant and must hold against any writer. But a
        record-level `ValidationError` raised from inside the service surfaces as a
        500, because nothing on that path catches it: the value is a client error and
        has to be refused where client input is validated. Both halves are checked
        here so the caller gets a 422 naming the field, which is also what this
        field's own description already promises.

        The ceiling is taken from this process's clock rather than the record's
        `verified_at`, which does not exist yet. The two are microseconds apart and
        the tolerance is sixty seconds, so the same constant governs both checks
        without the edge one being tighter than the invariant it fronts.
        """
        if self.confirmed_at is None:
            return self
        if self.confirmed_at.tzinfo is None:
            raise ValueError(
                "confirmed_at must be timezone-aware; send an offset (e.g. "
                "'2026-08-28T10:30:00+05:30') or omit the field"
            )
        now = _utc_now()
        if self.confirmed_at > now + timedelta(
            seconds=CONFIRMED_AT_FUTURE_TOLERANCE_SECONDS
        ):
            raise ValueError(
                f"confirmed_at {self.confirmed_at.isoformat()} is more than "
                f"{CONFIRMED_AT_FUTURE_TOLERANCE_SECONDS:.0f}s ahead of "
                f"{now.isoformat()}; money that has not arrived yet cannot be "
                "confirmed as arrived"
            )
        return self


class ManualConfirmationAck(BaseModel):
    """What `POST /executions/{execution_id}/confirm-payment` answers with.

    Reports the record and what it changed, and says plainly whether this call did
    the work or found it already done — the same `created` distinction Stage 5's
    execute endpoint carries, for the same reason: a caller has to be able to tell
    "I confirmed it" from "it was already confirmed".
    """

    model_config = ConfigDict(extra="forbid")

    created: bool = Field(
        ...,
        description=(
            "True when this call wrote the record. False when an earlier call "
            "already had, in which case nothing happened this time."
        ),
    )
    verification_id: str = Field(..., description="Id of the stored record.")
    verification: ManualVerificationDocument = Field(
        ...,
        description="The record itself, so `source` is visible in the response.",
    )
    event_status: str | None = Field(
        default=None,
        description="The originating event's status after this call, when known.",
    )
    detail: str = Field(
        ...,
        min_length=1,
        description="One line describing what happened, including any status refusal.",
    )
