"""Domain models for promise-to-pay (Stage 6 Part B — a commitment, and its fate).

A promise is the one thing in this system a *customer* asserts rather than the
merchant's infrastructure. That makes it the weakest kind of fact here, and the
modelling reflects that:

* `state` is a state machine, not a label. `ALLOWED_PROMISE_TRANSITIONS` declares
  every legal move and `app/ptp/store.py` puts the permitted predecessors into the
  Mongo *filter*, so an illegal move matches zero documents rather than being
  corrected afterwards. Same construction as the event lifecycle in
  `app/models/events.py`, for the same reason;
* `honored` is terminal. Money arriving is the only evidence that settles a
  promise, and it cannot be un-arrived. Every other state is revisitable;
* `follow_up_sent` is *not* a permission to send. It is a record that a send
  already happened, and the validators only constrain its consistency with
  `state`. What actually gates a send is `app/ptp/safety.py` — a follow-up cannot
  be called without an `UnpaidConfirmation`, which cannot be obtained while the
  money is already in;
* there is no field for a payment link, an intervention, an action type, a
  channel, or a message. With `extra="forbid"` a caller cannot add one. A promise
  is a statement about intent; what gets *done* about a broken one is an
  `ExecutionRecord`, written by the existing pipeline under the existing policy
  gate. Two separate records, because they are two separate kinds of fact and
  merging them would create a place where a message could be recorded as sent
  without a verdict having authorized it.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Literal, Optional, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.decision import MONEY_PRECISION

# ---------------------------------------------------------------------------
# The promise lifecycle.
# ---------------------------------------------------------------------------

#: Every state a promise can be in.
#:
#: `reevaluating` is the state of a broken promise that has already been chased:
#: a follow-up went out through the policy gate and the system is waiting again.
#: It is distinct from `broken` precisely so that "needs chasing" and "has been
#: chased" are different values rather than a boolean read alongside a state.
PromiseState = Literal[
    "promised",
    "honored",
    "broken",
    "reevaluating",
]

#: What every promise starts as.
INITIAL_PROMISE_STATE: PromiseState = "promised"

#: Which states each state may move to. The empty set marks a terminal state.
#:
#: Every arc here has exactly one producer in `app/ptp/service.py`, which is the
#: test of whether a state machine is real or decorative:
#:
#: * `promised -> honored`    a verification with outcome `recovered` exists;
#: * `promised -> broken`     the promised date passed with no such verification;
#: * `broken -> reevaluating` a follow-up was authorized and executed;
#: * `broken -> honored`      money arrived while the follow-up was still blocked
#:                            by policy (opt-out, contact cap, or cooldown);
#: * `reevaluating -> honored` money arrived after the follow-up.
#:
#: There is deliberately no arc back to `promised`. A promise names one date, and
#: that date does not move. A customer who commits again produces a *new* promise
#: document, so the record of what was originally agreed survives the renegotiation.
#:
#: There is also no `reevaluating -> broken`. Once chased there is nothing further
#: this system does automatically, so a second "broken" would be a state change
#: with no consequence attached to it. A follow-up whose execution *failed* never
#: reaches `reevaluating` in the first place — see `app/ptp/service.py` — so the
#: retry case does not need this arc either.
ALLOWED_PROMISE_TRANSITIONS: dict[str, frozenset[str]] = {
    "promised": frozenset({"honored", "broken"}),
    "broken": frozenset({"honored", "reevaluating"}),
    "reevaluating": frozenset({"honored"}),
    "honored": frozenset(),
}

#: States nothing can move out of. Derived from the table rather than restated
#: next to it, so the two cannot disagree.
TERMINAL_PROMISE_STATES: frozenset[str] = frozenset(
    state for state, successors in ALLOWED_PROMISE_TRANSITIONS.items() if not successors
)

ALLOWED_PROMISE_STATES: frozenset[str] = frozenset(get_args(PromiseState))

#: The state a promise is in while it is still an open commitment. The one state
#: in which nothing has yet happened to it, which is what `resolved_at` keys off.
OPEN_PROMISE_STATE: PromiseState = "promised"

#: States that mean a follow-up has demonstrably been sent.
REQUIRES_FOLLOW_UP_SENT: frozenset[str] = frozenset({"reevaluating"})

assert set(ALLOWED_PROMISE_TRANSITIONS) == ALLOWED_PROMISE_STATES, (
    "ALLOWED_PROMISE_TRANSITIONS keys "
    f"{sorted(ALLOWED_PROMISE_TRANSITIONS)} do not match PromiseState "
    f"{sorted(ALLOWED_PROMISE_STATES)}"
)
assert all(
    successors <= ALLOWED_PROMISE_STATES
    for successors in ALLOWED_PROMISE_TRANSITIONS.values()
), "ALLOWED_PROMISE_TRANSITIONS names a target that is not a PromiseState"
assert INITIAL_PROMISE_STATE in ALLOWED_PROMISE_STATES
assert OPEN_PROMISE_STATE in ALLOWED_PROMISE_STATES
assert REQUIRES_FOLLOW_UP_SENT <= ALLOWED_PROMISE_STATES
assert INITIAL_PROMISE_STATE not in TERMINAL_PROMISE_STATES, (
    "a promise that starts in a terminal state can never be resolved"
)


def promise_transition_allowed(current: str, target: str) -> bool:
    """Whether a promise may move from `current` to `target`.

    An unknown `current` returns False rather than raising: it means the stored
    value predates this vocabulary, and refusing to move it is the safe answer.
    Self-transitions are False — nothing needs writing, and reporting a no-op as a
    successful transition would overstate what happened.
    """
    return target in ALLOWED_PROMISE_TRANSITIONS.get(current, frozenset())


def states_that_may_become(target: str) -> frozenset[str]:
    """Every state `target` is reachable from.

    This is what the guarded update in `app/ptp/store.py` puts in its filter, so an
    ineligible promise matches no document instead of being corrected afterwards.
    Inverting the table here rather than maintaining a second copy of it means the
    query and the declaration cannot drift apart.
    """
    return frozenset(
        current
        for current, successors in ALLOWED_PROMISE_TRANSITIONS.items()
        if target in successors
    )


def _utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime, to the millisecond.

    Truncated for the same reason the execution and verification records truncate:
    the value goes into BSON, which stores milliseconds, and is read back and
    compared against what the API returned.
    """
    now = datetime.now(timezone.utc)
    return now.replace(microsecond=(now.microsecond // 1000) * 1000)


def today_utc() -> date:
    """The current UTC date, which is what a promised date is compared against.

    One function so the deadline test has a single definition. A promise due
    *today* has not been broken — the day is not over — so the comparison is
    strictly `promised_date < today_utc()`.
    """
    return datetime.now(timezone.utc).date()


def deadline_passed(promised_date: date) -> bool:
    """Whether a promised date is now in the past."""
    return promised_date < today_utc()


# ---------------------------------------------------------------------------
# The promise contract.
# ---------------------------------------------------------------------------


class PromiseToPay(BaseModel):
    """A customer's stated commitment to pay a given amount by a given date.

    Multiple promises can exist for one event — a customer who breaks one and
    commits again produces a second document — but not two for the same date. That
    is a unique index in `app/ptp/store.py`, not a convention.
    """

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(
        ...,
        min_length=1,
        description="The `RevenueEvent.event_id` this commitment is about.",
    )
    promised_amount: float = Field(
        ...,
        gt=0,
        description=(
            "What the customer said they would pay, in major units. Deliberately "
            "not constrained to equal the event's amount at risk: a partial "
            "commitment is a real and common thing to promise, and forcing "
            "equality would make the honest record of one unstorable."
        ),
    )
    promised_date: date = Field(
        ...,
        description=(
            "The date by which they said they would pay. A date, not a datetime — "
            "nobody commits to a millisecond. Stored as an ISO-8601 string because "
            "BSON has no date type; see `app/ptp/store.py`."
        ),
    )
    state: PromiseState = Field(
        default=INITIAL_PROMISE_STATE,
        description=(
            "Where this promise is in its lifecycle. Changed only by a declared "
            "transition — see ALLOWED_PROMISE_TRANSITIONS."
        ),
    )
    created_at: datetime = Field(
        default_factory=_utc_now,
        description="When the commitment was recorded (UTC).",
    )
    resolved_at: Optional[datetime] = Field(
        default=None,
        description=(
            "When the promise last stopped being an open commitment (UTC). None "
            "exactly while `state` is 'promised', set on every transition "
            "thereafter — so on a promise that broke and was later honored this is "
            "the honoring, and `created_at` is still the commitment."
        ),
    )
    follow_up_sent: bool = Field(
        default=False,
        description=(
            "Whether a follow-up has ALREADY been executed for this promise. A "
            "record, not a permission: nothing reads this to decide whether "
            "sending is allowed. That decision belongs to the policy gate, and "
            "reaching the sender at all requires an `UnpaidConfirmation`."
        ),
    )

    @model_validator(mode="after")
    def _resolution_must_match_the_state(self) -> "PromiseToPay":
        """`resolved_at` is set exactly when the promise is no longer open.

        Both directions matter. An open promise carrying a resolution time would
        claim to have been settled while still being counted as outstanding; a
        settled promise without one would lose when it happened, which is the only
        thing that makes 'honored' auditable against the promised date.
        """
        if self.state == OPEN_PROMISE_STATE and self.resolved_at is not None:
            raise ValueError(
                f"state {self.state!r} is still an open commitment and cannot "
                f"carry resolved_at {self.resolved_at!r}"
            )
        if self.state != OPEN_PROMISE_STATE and self.resolved_at is None:
            raise ValueError(
                f"state {self.state!r} means the promise left the open state, so "
                "resolved_at must say when"
            )
        return self

    @model_validator(mode="after")
    def _follow_up_must_be_possible(self) -> "PromiseToPay":
        """A follow-up cannot have been sent on a promise that is still open.

        The deadline has to have passed for a follow-up to be considered at all, and
        a promise whose deadline has passed is no longer `promised`. So
        `follow_up_sent` on an open promise would be evidence of a message sent
        outside the one path that can send them.

        The converse — `honored` with `follow_up_sent` True — is legitimate and
        allowed: it is a customer who paid *after* being chased.
        """
        if self.follow_up_sent and self.state == OPEN_PROMISE_STATE:
            raise ValueError(
                "follow_up_sent is True on a promise still in state "
                f"{OPEN_PROMISE_STATE!r}; a follow-up is only reachable after the "
                "promised date has passed, so this record claims a message sent "
                "outside the gated path"
            )
        if self.state in REQUIRES_FOLLOW_UP_SENT and not self.follow_up_sent:
            raise ValueError(
                f"state {self.state!r} means a follow-up went out, so "
                "follow_up_sent cannot be False"
            )
        return self

    @model_validator(mode="after")
    def _timestamps_must_be_aware(self) -> "PromiseToPay":
        """Reject naive timestamps, as every other stage's record does."""
        if self.created_at.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")
        if self.resolved_at is not None and self.resolved_at.tzinfo is None:
            raise ValueError("resolved_at must be timezone-aware")
        return self

    @field_validator("promised_amount", mode="after")
    @classmethod
    def _round_the_amount(cls, value: float) -> float:
        """Round rather than storing whatever arithmetic produced.

        Money in this system is compared for equality — against the event's amount,
        against what Razorpay reports — so an unrounded float would make a promise
        of 1200.0000000000002 a different number from a promise of 1200.
        """
        return round(value, MONEY_PRECISION)


class PromiseToPayDocument(PromiseToPay):
    """A stored `PromiseToPay`, with its document id."""

    id: str = Field(..., description="MongoDB document id, rendered as a string.")

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> "PromiseToPayDocument":
        """Build a record from a raw MongoDB document."""
        fields = {key: value for key, value in document.items() if key != "_id"}
        return cls(id=str(document["_id"]), **fields)


class PromiseRequest(BaseModel):
    """The body of `POST /promises`.

    Three fields, taken literally. No free text and no Gemini reach this model: a
    promise recorded here is structured data the merchant already has after the
    conversation.

    THIS MODEL IS ALSO WHAT STAGE 10 BUILDS, and the objection that used to be
    written here deserves an answer rather than a deletion. It read: parsing "next
    Tuesday-ish" into a date with an LLM would put a model in the position of
    deciding when money is owed. That is half true, and the half that is true was the
    right thing to worry about.

    What is true: an extracted date does have a consequence. It decides when a
    promise breaks, and therefore when a follow-up is *considered*. A model that
    misreads "Friday" moves that moment.

    What is not true: that this amounts to deciding what is owed, or what happens
    next. `POST /promises/from-text` builds one of these and hands it to the same
    `create_promise` this endpoint calls — so an extracted promise is subject to
    every rule a typed one is. The amount cannot exceed what the event says is at
    risk. The date cannot precede the message or fall beyond a bounded horizon. A
    commitment the model cannot defend produces no promise at all. And the follow-up
    a broken promise might trigger still goes through `authorize_event` and still
    requires an `UnpaidConfirmation`, so the opt-out, the contact cap and the
    cooldown all still apply.

    The worst outcome of a wrong extraction is therefore a contact at the wrong
    *time* — not an unauthorized contact, not a different amount of money moving, and
    not a promise marked `honored` without a verification behind it. That is a real
    cost and it is bounded; the original blanket refusal was cheaper but it also meant
    a merchant had to transcribe every message by hand. See
    `app/models/promise_extraction.py` for the bounds and
    `app/ptp/extraction.py` for the path.
    """

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(..., min_length=1, description="Event the promise is about.")
    promised_amount: float = Field(
        ..., gt=0, description="Amount committed to, in major units."
    )
    promised_date: date = Field(
        ...,
        description=(
            "Date committed to, ISO-8601. A past date is accepted: promises are "
            "recorded by a human after a conversation, sometimes days later, and "
            "refusing to record one that has already lapsed would mean the only "
            "promises the system knows about are the ones nobody had to chase. Stage "
            "10 relies on this too — a date resolved against an old message can be "
            "future relative to that message and past relative to today, and both "
            "readings are correct."
        ),
    )


class FollowUpReport(BaseModel):
    """What happened when a broken promise's follow-up was attempted.

    Present on a check response only when a follow-up was actually attempted, and
    `sent` is False whenever the policy gate refused. A refusal is a normal,
    successful outcome of this stage — the guardrails working — not an error, so it
    is reported here rather than raised.
    """

    model_config = ConfigDict(extra="forbid")

    sent: bool = Field(
        ..., description="Whether a contact was actually executed and recorded."
    )
    policy_verdict_id: str = Field(
        ..., description="The verdict that decided it. Always present: the gate always ran."
    )
    policy_verdict: str = Field(..., description="authorized, blocked, or requires_manual_review.")
    policy_reason: str = Field(..., description="Which check decided, in the policy vocabulary.")
    execution_id: str | None = Field(
        default=None, description="The execution record written, when one was."
    )
    action_type: str | None = Field(
        default=None, description="What kind of action it was, when one happened."
    )
    intervention: str | None = Field(
        default=None, description="The intervention the verdict authorized, when it did."
    )
    detail: str = Field(..., min_length=1, description="One line, in plain words.")


class PromiseCheck(BaseModel):
    """The result of `POST /promises/{event_id}/check`.

    Shaped so the safety property is visible in the response rather than only in
    the code: `payment_rechecked_at` is never null, because the re-check is not
    optional, and `follow_up` is null on every path where the money had already
    arrived.
    """

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(..., description="Event the promise is about.")
    promise_id: str = Field(..., description="The promise that was checked.")
    state_before: PromiseState = Field(..., description="State on entry.")
    state: PromiseState = Field(..., description="State after this check.")
    changed: bool = Field(..., description="Whether this call moved the promise.")
    payment_rechecked_at: datetime = Field(
        ...,
        description=(
            "When the mandatory payment re-check ran. Never null — no path through "
            "this endpoint skips it, including the paths that do nothing else."
        ),
    )
    verifications_examined: int = Field(
        ..., ge=0, description="How many verification records the re-check read."
    )
    recovered_verification_id: str | None = Field(
        default=None,
        description="The verification proving payment, when the re-check found one.",
    )
    deadline_passed: bool = Field(
        ..., description="Whether the promised date is now in the past."
    )
    follow_up: FollowUpReport | None = Field(
        default=None,
        description="Null when no follow-up was attempted, including whenever the money was already in.",
    )
    detail: str = Field(..., min_length=1, description="What this check concluded, in one line.")
