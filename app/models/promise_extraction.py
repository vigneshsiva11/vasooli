"""Domain models for extracting a promise from free text (Stage 10).

This file is the boundary between what a language model said and what this system
is willing to treat as a commitment. Everything a model produces enters here as a
*proposal* and leaves as either a validated set of values or a refusal with a
reason. Nothing in between.

Three things carry the weight:

* `LLMPromiseProposal` is the only shape a model response can become. It has four
  fields and `extra="forbid"`, so no `event_id`, no `state`, no `follow_up_sent`
  and no action can survive the parse. The model cannot say which event a promise
  attaches to, what state it starts in, or what should be done about it;

* `promised_date` is a **regex-constrained string, not a `date`**. Pydantic's date
  parsing accepts an integer as a Unix timestamp, so a `date`-typed field would
  turn `1234567890` into 2009-02-13 without complaint. A string that must match
  `YYYY-MM-DD` is parsed deliberately in `evaluate_proposal`, where a calendar-
  invalid value like `2026-13-45` becomes a refusal rather than an exception;

* `evaluate_proposal` is pure. No database, no network, no clock of its own — the
  reference time arrives as an argument. That is what lets the adversarial suite
  drive every rejection path with synthetic proposals and zero API calls, which is
  both cheaper and more reproducible than trying to coax a model into misbehaving.

WHAT THIS CANNOT DO, stated plainly rather than implied: an extracted date does
have a consequence. It determines when a promise breaks, and therefore when a
follow-up is *considered*. It cannot authorize one. The follow-up still goes
through `authorize_event` and still requires an `UnpaidConfirmation`, so the worst
outcome of a bad extraction is a contact at the wrong *time* — never an
unauthorized contact, and never a different amount of money moving. The window and
the floor below bound how wrong the timing can get.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal, NamedTuple, Optional, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.decision import MONEY_PRECISION
from app.models.promise import PromiseToPayDocument

# ---------------------------------------------------------------------------
# The bounds. All of them constants, because every one is a ratified number and
# a magic literal buried in a comparison is a number nobody can find later.
# ---------------------------------------------------------------------------

#: Longest customer message accepted. A promise lives in a sentence; anything past
#: this is either a pasted thread or an attempt to bury an instruction in filler.
#: Bounded here rather than only at the prompt because the text is also *stored*.
MAX_RAW_TEXT_CHARS = 2000

#: Longest quote accepted back from the model. It is one span of one sentence.
MAX_QUOTE_CHARS = 200

#: How far ahead a promise may be dated, measured from the message's own timestamp.
#: The bound on how far a misparse can throw a deadline: without it, "next year
#: sometime" resolved into a real date would silently park an event for months.
MAX_PROMISE_HORIZON_DAYS = 90

#: Below this stated confidence, no promise is created.
#:
#: Deliberately higher than anything in Stage 2, and NOT a constant copied from it.
#: Stage 2 has no acting floor at all — it has a ceiling (`LLM_CONFIDENCE_CEILING`,
#: 0.90) and a fallback confidence (0.20), but no threshold below which it declines
#: to produce a diagnosis, because it does not need one: `"unknown"` is a valid safe
#: answer, so its safe default is a *label*.
#:
#: A promise has no equivalent. It either exists or it does not, and the artifact is
#: a dated obligation rather than a descriptive label — so the safe default has to be
#: non-creation, and non-creation needs a threshold. This is new machinery that
#: borrows Stage 2's philosophy, not its numbers.
CONFIDENCE_FLOOR = 0.70

#: Deducted from stated confidence when the model returns a `quote` that does not
#: appear in the message it was given.
#:
#: 0.30 is chosen against the floor rather than picked for feel: a model claiming a
#: quote it cannot support at a typical high confidence of 0.90 lands on 0.60 and is
#: refused, and only a stated 1.00 survives the penalty. A penalty rather than an
#: outright refusal because a mismatch can be cosmetic — a model reformatting
#: "₹5,000" as "5000" is careless, not hallucinating — and Stage 2 already
#: established the preference for discarding a bad part over rejecting a usable
#: whole. The grounding check is a signal, not a security control; the closed
#: schema, the window and the floor are the controls.
UNVERIFIED_QUOTE_PENALTY = 0.30

#: `promised_date` must look exactly like this before anything tries to parse it.
ISO_DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"

#: Length of a `YYYY-MM-DD` string, used as the field's max_length so obvious
#: free text ("next Friday") is refused on size before the pattern is even reached.
ISO_DATE_LENGTH = 10

_WHITESPACE = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# Why an extraction produced no promise.
# ---------------------------------------------------------------------------

#: The closed set of reasons no promise was created. A `Literal`, so a refusal
#: reason invented at a call site fails validation instead of appearing in the audit
#: as a novel category nobody can aggregate.
#:
#: The order is the order the checks run in, which is the order that makes a refusal
#: most informative: structural facts about the response first, then the judgement
#: threshold, then the amount. A response with a date beyond the horizon is reported
#: as such even at high confidence, because "the model named a date three years out"
#: is more useful to an auditor than "confidence was fine".
RefusalReason = Literal[
    "llm_unavailable",
    "unparseable_response",
    "no_commitment_found",
    "unparseable_date",
    "date_before_message",
    "date_beyond_horizon",
    "confidence_below_floor",
    "amount_exceeds_at_risk",
]

ALLOWED_REFUSAL_REASONS: frozenset[str] = frozenset(get_args(RefusalReason))

#: Refusals that happened before any response was validated, so there is no
#: proposal behind them and every extracted field is necessarily null.
PRE_PROPOSAL_REFUSALS: frozenset[str] = frozenset(
    {"llm_unavailable", "unparseable_response"}
)

assert PRE_PROPOSAL_REFUSALS <= ALLOWED_REFUSAL_REASONS


# ---------------------------------------------------------------------------
# What a model is allowed to say.
# ---------------------------------------------------------------------------


class LLMPromiseProposal(BaseModel):
    """One model response, bounded.

    Note what is absent, in the same spirit as `LLMDiagnosisProposal`. There is no
    `event_id`, so the model cannot redirect a promise onto another customer's
    revenue. There is no `state`, so it cannot mint an `honored` promise and skip
    the evidence that state is supposed to represent. There is no `follow_up_sent`,
    no intervention, no channel and no message text. With `extra="forbid"` none of
    them can be introduced by a response either.

    Every field except `confidence` is nullable, and that is the design rather than
    laxity: a non-nullable `promised_date` would force "I'm still thinking about
    it" to produce a fabricated date. Null is how the model says *nothing was
    committed*, which is the answer this stage most needs it to be able to give.
    """

    model_config = ConfigDict(extra="forbid")

    promised_date: Optional[str] = Field(
        default=None,
        max_length=ISO_DATE_LENGTH,
        pattern=ISO_DATE_PATTERN,
        description=(
            "The committed date as YYYY-MM-DD, or null when the message contains no "
            "clear commitment. A string and not a `date`: Pydantic would read the "
            "integer 1234567890 as a Unix timestamp and hand back 2009-02-13, so the "
            "parse is done deliberately in `evaluate_proposal` instead."
        ),
    )
    promised_amount: Optional[float] = Field(
        default=None,
        gt=0,
        strict=True,
        description=(
            "The figure the customer actually stated, or null when they committed to "
            "paying without naming an amount. `strict=True` for the same reason it is "
            "on `ManualPaymentConfirmation.amount_recovered` — this is untrusted input "
            "that becomes money, and lax mode would coerce \"5000\" and `true` into "
            "floats. Strict mode still accepts a JSON integer, so 5000 is fine."
        ),
    )
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description=(
            "How explicit the commitment was, as the model reports it. Not required "
            "to be honest — which is why it is a floor test and not a stored fact "
            "about the promise."
        ),
    )
    quote: Optional[str] = Field(
        default=None,
        max_length=MAX_QUOTE_CHARS,
        description=(
            "The verbatim span the commitment was read from, or null if the model "
            "cannot quote it exactly. Checked against the message in "
            "`evaluate_proposal`: a quote that is not in the text is evidence the "
            "response is not grounded in the input, and costs it "
            "UNVERIFIED_QUOTE_PENALTY of confidence."
        ),
    )


# ---------------------------------------------------------------------------
# Turning a proposal into values, or into a refusal.
# ---------------------------------------------------------------------------


class ExtractionOutcome(NamedTuple):
    """The verdict on one proposal: what may be used, or why nothing may be.

    `accepted` and `refusal_reason` are exact opposites — one is set precisely when
    the other is not — and `assert_consistent` proves it rather than trusting the
    construction sites.
    """

    accepted: bool
    promised_date: date | None
    promised_amount: float | None
    amount_inferred: bool
    confidence: float
    quote: str | None
    quote_verified: bool
    refusal_reason: str | None
    detail: str

    def assert_consistent(self) -> "ExtractionOutcome":
        """Check the invariant every construction site is supposed to maintain."""
        if self.accepted:
            assert self.refusal_reason is None, (
                f"accepted outcome carries refusal_reason {self.refusal_reason!r}"
            )
            assert self.promised_date is not None, "accepted outcome has no date"
            assert self.promised_amount is not None, "accepted outcome has no amount"
        else:
            assert self.refusal_reason in ALLOWED_REFUSAL_REASONS, (
                f"refusal reason {self.refusal_reason!r} is not in the closed set "
                f"{sorted(ALLOWED_REFUSAL_REASONS)}"
            )
        return self


def normalise_for_quote_check(text: str) -> str:
    """Flatten text for substring comparison.

    Whitespace collapsed and case folded, because a model that re-wraps a line or
    capitalises a sentence has still quoted the message. Nothing else is
    normalised: punctuation, digits and currency symbols are left alone, so
    "₹5,000" and "5000" remain different strings and a reformatted amount is
    correctly counted as unverified.
    """
    return _WHITESPACE.sub(" ", text).strip().casefold()


def quote_appears_in(quote: str, raw_text: str) -> bool:
    """Whether a quoted span really occurs in the message it claims to come from."""
    needle = normalise_for_quote_check(quote)
    if not needle:
        return False
    return needle in normalise_for_quote_check(raw_text)


def _refuse(
    reason: RefusalReason,
    detail: str,
    *,
    confidence: float = 0.0,
    quote: str | None = None,
    quote_verified: bool = False,
) -> ExtractionOutcome:
    """Build a refusal. No promise will be created from this."""
    return ExtractionOutcome(
        accepted=False,
        promised_date=None,
        promised_amount=None,
        amount_inferred=False,
        confidence=confidence,
        quote=quote,
        quote_verified=quote_verified,
        refusal_reason=reason,
        detail=detail,
    ).assert_consistent()


def evaluate_proposal(
    proposal: LLMPromiseProposal,
    *,
    raw_text: str,
    received_at: datetime,
    amount_at_risk: float,
    currency: str,
) -> ExtractionOutcome:
    """Decide whether one proposal may become a promise, and with what values.

    Pure: every input is an argument, including the reference time, so this is
    exhaustively testable without a model, a database, or a clock. The adversarial
    suite drives every branch below through synthetic proposals at zero API cost.

    The date window is measured against `received_at`, not against today, and that
    distinction is the whole point of passing the timestamp around. "Friday" in a
    message from three weeks ago means the Friday after that message. Resolving it
    against today would silently move a commitment the customer never made. A
    consequence worth being explicit about: an accepted promise can therefore be
    **already overdue** the moment it is created, which is correct and matches what
    `PromiseRequest` already documents about accepting past dates.

    Args:
        proposal: The validated model response.
        raw_text: The message the model was given, for the quote check.
        received_at: When the message arrived. The reference clock.
        amount_at_risk: The event's `amount`, used both as the inferred default and
            as the ceiling a stated amount may not exceed.
        currency: For the detail line only.
    """
    quote = proposal.quote
    quote_verified = bool(quote) and quote_appears_in(quote, raw_text)

    confidence = proposal.confidence
    if quote and not quote_verified:
        confidence = max(0.0, confidence - UNVERIFIED_QUOTE_PENALTY)

    # --- structural checks on what the model returned ----------------------

    if proposal.promised_date is None:
        return _refuse(
            "no_commitment_found",
            "the message contains no commitment to pay by a specific time, so no "
            "promise was created; nothing was guessed",
            confidence=confidence,
            quote=quote,
            quote_verified=quote_verified,
        )

    try:
        promised_date = date.fromisoformat(proposal.promised_date)
    except ValueError:
        # Shape passed the regex, calendar did not: 2026-13-45 and 2026-02-30.
        return _refuse(
            "unparseable_date",
            f"{proposal.promised_date!r} matches the date pattern but is not a real "
            "calendar date",
            confidence=confidence,
            quote=quote,
            quote_verified=quote_verified,
        )

    message_date = received_at.date()
    horizon = message_date + timedelta(days=MAX_PROMISE_HORIZON_DAYS)

    if promised_date < message_date:
        return _refuse(
            "date_before_message",
            f"{promised_date.isoformat()} is before the message was received "
            f"({message_date.isoformat()}); a commitment cannot be dated earlier "
            "than the conversation that made it",
            confidence=confidence,
            quote=quote,
            quote_verified=quote_verified,
        )

    if promised_date > horizon:
        return _refuse(
            "date_beyond_horizon",
            f"{promised_date.isoformat()} is more than {MAX_PROMISE_HORIZON_DAYS} "
            f"days after the message ({message_date.isoformat()}); beyond that this "
            "is more likely a misparse than a commitment",
            confidence=confidence,
            quote=quote,
            quote_verified=quote_verified,
        )

    # --- the judgement threshold -------------------------------------------

    if confidence < CONFIDENCE_FLOOR:
        penalty_note = (
            f" (stated {proposal.confidence:.2f}, reduced by "
            f"{UNVERIFIED_QUOTE_PENALTY:.2f} because the quoted text does not appear "
            "in the message)"
            if quote and not quote_verified
            else ""
        )
        return _refuse(
            "confidence_below_floor",
            f"confidence {confidence:.2f} is below the {CONFIDENCE_FLOOR:.2f} floor"
            f"{penalty_note}; no promise was created",
            confidence=confidence,
            quote=quote,
            quote_verified=quote_verified,
        )

    # --- the amount --------------------------------------------------------

    amount_inferred = proposal.promised_amount is None
    if amount_inferred:
        promised_amount = round(amount_at_risk, MONEY_PRECISION)
    else:
        assert proposal.promised_amount is not None  # for the type checker
        promised_amount = round(proposal.promised_amount, MONEY_PRECISION)
        if promised_amount > round(amount_at_risk, MONEY_PRECISION):
            # A partial commitment is legitimate and `PromiseToPay` deliberately
            # permits one. An extraction claiming MORE than is at risk is not a
            # partial anything — it is a misparse of a digit or a number the message
            # never contained, and recording it would let model output write a
            # figure nothing corroborates.
            return _refuse(
                "amount_exceeds_at_risk",
                f"extracted amount {promised_amount:,.2f} exceeds the "
                f"{currency} {amount_at_risk:,.2f} at risk on this event; a promise "
                "for more than is owed is a misparse, not a commitment",
                confidence=confidence,
                quote=quote,
                quote_verified=quote_verified,
            )

    stated = (
        f"amount inferred from the event's {currency} {promised_amount:,.2f} at risk "
        "— the customer did not state a figure"
        if amount_inferred
        else f"customer stated {currency} {promised_amount:,.2f}"
    )
    return ExtractionOutcome(
        accepted=True,
        promised_date=promised_date,
        promised_amount=promised_amount,
        amount_inferred=amount_inferred,
        confidence=confidence,
        quote=quote,
        quote_verified=quote_verified,
        refusal_reason=None,
        detail=(
            f"commitment extracted: pay by {promised_date.isoformat()}, {stated} "
            f"(confidence {confidence:.2f})"
        ),
    ).assert_consistent()


# ---------------------------------------------------------------------------
# The audit record. One per attempt, refusals included.
# ---------------------------------------------------------------------------


def _utc_now() -> datetime:
    """Current UTC time, truncated to milliseconds to match what BSON stores."""
    now = datetime.now(timezone.utc)
    return now.replace(microsecond=(now.microsecond // 1000) * 1000)


class PromiseExtraction(BaseModel):
    """What a customer said, and what was made of it.

    One document per *attempt*, in its own collection rather than as fields on
    `PromiseToPay`. The decisive reason is refusals: a refused extraction has no
    promise to hang off, so an audit field on the promise record would silently lose
    exactly the cases where the guardrail worked — the non-committal message, the
    injection attempt. Those are the records worth keeping.

    Three supporting reasons. `PromiseToPay` keeps `extra="forbid"` and its exact
    Stage 6 shape, so no existing promise record and no existing reader changes. One
    promise can have several extraction attempts behind it, which is 1:N and does not
    fit a field. And unbounded untrusted customer text stays out of the domain
    record entirely.

    There is deliberately **no unique index** on this collection — see
    `app/ptp/extraction_store.py`. Two identical submissions are two attempts, and
    both of them happened. Idempotency belongs on the promise, where
    `uniq_event_promised_date` already provides it.
    """

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(
        ..., min_length=1, description="The event this message was about."
    )
    raw_text: str = Field(
        ...,
        min_length=1,
        max_length=MAX_RAW_TEXT_CHARS,
        description=(
            "What the customer actually said, stored verbatim and untouched. The "
            "point of this record: an auditor can read the message and the extraction "
            "side by side. Never interpreted as an instruction by anything that reads "
            "it back."
        ),
    )
    received_at: datetime = Field(
        ...,
        description=(
            "When the message arrived. THE REFERENCE CLOCK — every relative date in "
            "the message was resolved against this, not against the extraction time, "
            "so the two are stored separately and neither is derived from the other."
        ),
    )
    accepted: bool = Field(
        ...,
        description="Whether a defensible commitment was found. False is a normal outcome.",
    )
    promise_id: Optional[str] = Field(
        default=None,
        description=(
            "The promise this extraction produced. Null on every refusal. Null on an "
            "accepted extraction means one of two things, and neither is silently "
            "repaired: the linking write did not complete, or promise creation was "
            "refused downstream because a different amount was already promised for "
            "that date."
        ),
    )
    promised_date: Optional[str] = Field(
        default=None,
        description=(
            "The accepted date, ISO-8601. A string for the same reason "
            "`app/ptp/store.py` stores one: BSON has no date type, and midnight-UTC "
            "would invent a time nobody promised."
        ),
    )
    promised_amount: Optional[float] = Field(
        default=None, gt=0, description="The accepted amount, in major units."
    )
    amount_inferred: bool = Field(
        default=False,
        description=(
            "True when the customer committed without naming a figure and the event's "
            "full amount at risk was used. Recorded because 'they promised to pay' and "
            "'they promised to pay ₹5,000' are different facts and the difference must "
            "not be lost once the promise exists."
        ),
    )
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description=(
            "The confidence the floor was tested against — after any quote penalty, "
            "not as stated. What actually decided the outcome."
        ),
    )
    confidence_floor: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description=(
            "The floor in force when this ran. Stored rather than looked up at read "
            "time so a later change to CONFIDENCE_FLOOR cannot retroactively make a "
            "past refusal look arbitrary."
        ),
    )
    quote: Optional[str] = Field(
        default=None,
        max_length=MAX_QUOTE_CHARS,
        description="The span the model said it read the commitment from.",
    )
    quote_verified: bool = Field(
        default=False,
        description="Whether that span actually appears in `raw_text`.",
    )
    refusal_reason: Optional[RefusalReason] = Field(
        default=None,
        description="Why no promise was created. Null exactly when `accepted` is True.",
    )
    llm_model: Optional[str] = Field(
        default=None,
        description=(
            "Which model answered. Provenance, carried here rather than on the "
            "promise: which model ran is a fact about how the record was produced, "
            "not part of the commitment."
        ),
    )
    raw_response: Optional[str] = Field(
        default=None,
        max_length=MAX_RAW_TEXT_CHARS,
        description=(
            "What the model actually returned, truncated. Kept so a refusal can be "
            "audited against the response that caused it rather than against a "
            "summary of it."
        ),
    )
    extracted_at: datetime = Field(
        default_factory=_utc_now, description="When this attempt ran (UTC)."
    )

    @model_validator(mode="after")
    def _acceptance_and_refusal_are_exclusive(self) -> "PromiseExtraction":
        """`refusal_reason` is set precisely when `accepted` is False.

        Both directions. An accepted extraction carrying a refusal reason would be
        unreadable; a refused one without a reason would record that the guardrail
        fired without recording which guardrail, which is the only useful part.
        """
        if self.accepted and self.refusal_reason is not None:
            raise ValueError(
                f"accepted extraction carries refusal_reason "
                f"{self.refusal_reason!r}; the two are exclusive"
            )
        if not self.accepted and self.refusal_reason is None:
            raise ValueError(
                "a refused extraction must say which check refused it; "
                f"allowed reasons are {sorted(ALLOWED_REFUSAL_REASONS)}"
            )
        return self

    @model_validator(mode="after")
    def _accepted_extractions_carry_values(self) -> "PromiseExtraction":
        """An accepted extraction has both values; a refused one has neither.

        The second half matters as much as the first. A refused record holding a
        date would read as a commitment this system declined to honour, when in fact
        it is a commitment this system declined to *believe*.
        """
        if self.accepted:
            if self.promised_date is None or self.promised_amount is None:
                raise ValueError(
                    "an accepted extraction must carry both promised_date and "
                    "promised_amount; this one is missing "
                    + (
                        "promised_date"
                        if self.promised_date is None
                        else "promised_amount"
                    )
                )
        else:
            if self.promised_date is not None or self.promised_amount is not None:
                raise ValueError(
                    "a refused extraction cannot carry extracted values; nothing "
                    "was accepted, so there is nothing to record"
                )
            if self.promise_id is not None:
                raise ValueError(
                    f"a refused extraction cannot name a promise, but this one names "
                    f"{self.promise_id!r}"
                )
            if self.amount_inferred:
                raise ValueError(
                    "amount_inferred is True on a refused extraction; no amount was "
                    "accepted, so none was inferred"
                )
        return self

    @model_validator(mode="after")
    def _pre_proposal_refusals_have_no_extracted_detail(self) -> "PromiseExtraction":
        """A refusal from before validation cannot report a quote or a confidence.

        `llm_unavailable` and `unparseable_response` both happen with no
        `LLMPromiseProposal` in hand. A stored quote or a non-zero confidence on one
        of those would be a number with no source.
        """
        if self.refusal_reason in PRE_PROPOSAL_REFUSALS:
            if self.quote is not None or self.quote_verified:
                raise ValueError(
                    f"refusal {self.refusal_reason!r} happened before any response "
                    "was validated, so there is no quote to record"
                )
            if self.confidence != 0.0:
                raise ValueError(
                    f"refusal {self.refusal_reason!r} happened before any response "
                    f"was validated, so confidence cannot be {self.confidence}"
                )
        return self

    @model_validator(mode="after")
    def _quote_verification_requires_a_quote(self) -> "PromiseExtraction":
        """`quote_verified` cannot be True without a quote to have verified."""
        if self.quote_verified and not self.quote:
            raise ValueError("quote_verified is True but no quote was recorded")
        return self

    @model_validator(mode="after")
    def _timestamps_must_be_aware(self) -> "PromiseExtraction":
        """Reject naive timestamps, as every other stage's record does."""
        if self.received_at.tzinfo is None:
            raise ValueError("received_at must be timezone-aware")
        if self.extracted_at.tzinfo is None:
            raise ValueError("extracted_at must be timezone-aware")
        return self

    @field_validator("promised_amount", mode="after")
    @classmethod
    def _round_the_amount(cls, value: float | None) -> float | None:
        """Round for the same reason `PromiseToPay` does: money is compared for equality."""
        return None if value is None else round(value, MONEY_PRECISION)


class PromiseExtractionDocument(PromiseExtraction):
    """A stored `PromiseExtraction`, with its document id."""

    id: str = Field(..., description="MongoDB document id, rendered as a string.")

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> "PromiseExtractionDocument":
        """Build a record from a raw MongoDB document."""
        fields = {key: value for key, value in document.items() if key != "_id"}
        return cls(id=str(document["_id"]), **fields)


# ---------------------------------------------------------------------------
# The HTTP surface.
# ---------------------------------------------------------------------------


class PromiseFromTextRequest(BaseModel):
    """The body of `POST /promises/from-text`.

    An alternative input path to `POST /promises`, not a replacement for it. What
    happens after a promise exists is identical either way, because both endpoints
    reach the same `create_promise`.

    `received_at` is validated here, on the request model, rather than inside the
    service. That is a lesson from Stage 9: a `ValidationError` raised from a record
    model inside a service function surfaces as an HTTP 500, because nothing on that
    path catches it. Client-input invariants have to be enforced where FastAPI
    validates the body, or a bad request is reported as a server fault.
    """

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(
        ..., min_length=1, description="Event the message is about."
    )
    raw_text: str = Field(
        ...,
        min_length=1,
        max_length=MAX_RAW_TEXT_CHARS,
        description=(
            "The customer's message, verbatim. Bounded because it is stored and "
            "because a promise lives in a sentence: past this length the content is "
            "either a pasted thread or filler around an instruction."
        ),
    )
    received_at: Optional[datetime] = Field(
        default=None,
        description=(
            "When the message arrived, with a timezone offset. Every relative date in "
            "the message is resolved against THIS, not against today — so a message "
            "recorded a week late still yields the date the customer meant. Defaults "
            "to now when omitted."
        ),
    )

    @field_validator("raw_text", mode="after")
    @classmethod
    def _must_not_be_only_whitespace(cls, value: str) -> str:
        """A message of spaces is not a message.

        `min_length` alone would accept "   ", which reaches the model as an empty
        prompt and spends a real API call on nothing.
        """
        if not value.strip():
            raise ValueError("raw_text is only whitespace; there is nothing to extract")
        return value

    @field_validator("received_at", mode="after")
    @classmethod
    def _must_be_aware_and_not_future(cls, value: datetime | None) -> datetime | None:
        """A naive or future `received_at` is refused, at the edge, with a 422.

        Naive because the whole point of this field is resolving dates against it,
        and an offsetless timestamp does not identify an instant. Future because it
        is a record of something that has already happened, and a message from
        tomorrow would move the entire date window forward with it.
        """
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError(
                "received_at must include a timezone offset; a naive timestamp does "
                "not identify an instant to resolve 'Friday' against"
            )
        if value > _utc_now():
            raise ValueError(
                f"received_at {value.isoformat()} is in the future; a message cannot "
                "have arrived before it was sent"
            )
        return value


class PromiseFromTextResponse(BaseModel):
    """What `POST /promises/from-text` reports.

    Shaped so the demo moment is in the response and not only in the database:
    `raw_text` comes back beside `quote`, `promised_amount_inferred` and the
    resulting promise, so one call shows what the customer said and what was made
    of it.

    A refusal is a 200 with `commitment_found: false`, not an error. Declining to
    invent a commitment is this stage working, exactly as a `blocked` policy verdict
    is the gate working — the same convention `FollowUpReport(sent=False, ...)` uses.
    """

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(..., description="Event the message was about.")
    commitment_found: bool = Field(
        ...,
        description=(
            "Whether a defensible commitment was extracted. False means no promise "
            "exists and nothing was guessed."
        ),
    )
    created: bool = Field(
        ...,
        description=(
            "Whether this call wrote a NEW promise. False with "
            "`commitment_found: true` means an identical promise already existed and "
            "is being returned unchanged — the idempotency the unique index provides."
        ),
    )
    extraction_id: str = Field(
        ...,
        description="The audit record for this attempt. Written whether or not a promise was.",
    )
    raw_text: str = Field(..., description="What the customer said, echoed back verbatim.")
    received_at: datetime = Field(
        ..., description="The reference clock relative dates were resolved against."
    )
    promise: Optional[PromiseToPayDocument] = Field(
        default=None,
        description=(
            "The promise, when one exists. An ordinary `PromiseToPay` — identical in "
            "shape and behaviour to one recorded through `POST /promises`, because it "
            "was written by the same function."
        ),
    )
    promised_amount_inferred: bool = Field(
        ...,
        description=(
            "True when the customer committed without naming a figure and the event's "
            "full amount at risk was used. Stated explicitly rather than left for the "
            "caller to notice."
        ),
    )
    confidence: float = Field(
        ..., ge=0.0, le=1.0, description="The confidence the floor was tested against."
    )
    confidence_floor: float = Field(
        ..., ge=0.0, le=1.0, description="The floor that was applied."
    )
    quote: Optional[str] = Field(
        default=None, description="The span the model read the commitment from."
    )
    quote_verified: bool = Field(
        ..., description="Whether that span really appears in the message."
    )
    refusal_reason: Optional[RefusalReason] = Field(
        default=None, description="Which check declined, when one did."
    )
    llm_model: Optional[str] = Field(
        default=None, description="Which model answered."
    )
    detail: str = Field(
        ..., min_length=1, description="What happened, in one line of plain words."
    )
