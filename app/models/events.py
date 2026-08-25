"""Domain models for revenue-at-risk events (Stage 1 — Ingestion)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Optional, get_args

from pydantic import BaseModel, ConfigDict, Field

#: Where in the merchant's revenue flow the money is at risk.
Surface = Literal["payment", "checkout", "subscription", "receivable"]

# ---------------------------------------------------------------------------
# The event lifecycle (added in Stage 6 — RATIFIED 2026-08-25).
# ---------------------------------------------------------------------------

#: Every lifecycle state an event can be in. A `Literal` rather than a free string
#: because Stage 6 is the first stage that *changes* this field, and a status that
#: can hold any value cannot be reasoned about: "is this event still collectable"
#: would become a question about spelling.
EventStatus = Literal[
    "at_risk",
    "awaiting_promise",
    "recovered",
    "recovery_failed",
]

#: What every event starts as. Written by `$setOnInsert` at ingestion and never
#: rewritten by it — see `app/ingestion/store.py:INSERT_ONLY_FIELDS`.
INITIAL_EVENT_STATUS: EventStatus = "at_risk"

#: Which states each state may move to. The empty set marks a terminal state.
#:
#: `recovered` is terminal, and that is the load-bearing entry. Razorpay guarantees
#: at-least-once delivery and explicitly does *not* guarantee ordering, so a
#: `payment_link.expired` webhook can arrive after the `payment_link.paid` webhook
#: for the same link. Without this table that late arrival would move a paid event
#: to `recovery_failed` — the system would forget money it had already confirmed,
#: and the only evidence would be a status field that had quietly gone backwards.
#:
#: `recovery_failed` is *not* terminal. A link that expired or was cancelled is a
#: dead end for that attempt, not for the debt: re-authorizing the event produces a
#: new verdict and a new attempt, and a customer can still pay late.
ALLOWED_STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    "at_risk": frozenset({"awaiting_promise", "recovered", "recovery_failed"}),
    "awaiting_promise": frozenset({"recovered", "recovery_failed"}),
    "recovery_failed": frozenset({"awaiting_promise", "recovered"}),
    "recovered": frozenset(),
}

#: States nothing can move out of. Derived from the table, not restated alongside
#: it, so the two cannot disagree.
TERMINAL_EVENT_STATUSES: frozenset[str] = frozenset(
    status
    for status, successors in ALLOWED_STATUS_TRANSITIONS.items()
    if not successors
)

ALLOWED_EVENT_STATUSES: frozenset[str] = frozenset(get_args(EventStatus))

# The table must cover the vocabulary exactly, in both directions. A status in the
# Literal but missing from the table would be a state with undefined exits; a
# target in the table but missing from the Literal would be a transition to a state
# that cannot be stored.
assert set(ALLOWED_STATUS_TRANSITIONS) == ALLOWED_EVENT_STATUSES, (
    "ALLOWED_STATUS_TRANSITIONS keys "
    f"{sorted(ALLOWED_STATUS_TRANSITIONS)} do not match EventStatus "
    f"{sorted(ALLOWED_EVENT_STATUSES)}"
)
assert all(
    successors <= ALLOWED_EVENT_STATUSES
    for successors in ALLOWED_STATUS_TRANSITIONS.values()
), "ALLOWED_STATUS_TRANSITIONS names a target that is not an EventStatus"
assert INITIAL_EVENT_STATUS in ALLOWED_EVENT_STATUSES


def transition_allowed(current: str, target: str) -> bool:
    """Whether an event may move from `current` to `target`.

    An unknown `current` returns False rather than raising: it means the stored
    value predates this vocabulary, and refusing to move it is the safe answer.
    Self-transitions are False — nothing needs writing, and reporting a no-op as a
    successful transition would overstate what happened.
    """
    return target in ALLOWED_STATUS_TRANSITIONS.get(current, frozenset())


def statuses_that_may_become(target: str) -> frozenset[str]:
    """Every state `target` is reachable from.

    This is what the guarded update in `app/webhooks/store.py` puts in its filter,
    so an ineligible event matches no document instead of being corrected
    afterwards. Inverting the table here rather than maintaining a second copy of
    it means the query and the declaration cannot drift apart.
    """
    return frozenset(
        current
        for current, successors in ALLOWED_STATUS_TRANSITIONS.items()
        if target in successors
    )


def _utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


class RevenueEvent(BaseModel):
    """A single unit of revenue the merchant is at risk of losing.

    This is the system's normalised input shape: every downstream stage reads
    events in this form regardless of which surface produced them.
    """

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(
        ...,
        min_length=1,
        description="Upstream identifier for the event, e.g. a Razorpay payment id.",
    )
    surface: Surface = Field(
        ...,
        description="Which revenue surface the risk originated on.",
    )
    amount: float = Field(
        ...,
        gt=0,
        description="Amount at risk, in major currency units.",
    )
    currency: str = Field(
        default="INR",
        min_length=3,
        max_length=3,
        description="ISO 4217 currency code.",
    )
    raw_failure_reason: Optional[str] = Field(
        default=None,
        description="Verbatim upstream failure text, if the surface supplied one.",
    )
    customer_ref: str = Field(
        ...,
        min_length=1,
        description="Merchant-side reference for the customer who owes the money.",
    )
    created_at: datetime = Field(
        default_factory=_utc_now,
        description="When the event entered the system (UTC).",
    )
    status: EventStatus = Field(
        default=INITIAL_EVENT_STATUS,
        description=(
            "Lifecycle state of the event. Set once at ingestion and thereafter "
            "changed only by a declared transition — see ALLOWED_STATUS_TRANSITIONS."
        ),
    )


class RevenueEventRecord(RevenueEvent):
    """A stored `RevenueEvent`, carrying its MongoDB document id."""

    id: str = Field(..., description="MongoDB document id, rendered as a string.")

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> "RevenueEventRecord":
        """Build a record from a raw MongoDB document."""
        fields = {key: value for key, value in document.items() if key != "_id"}
        return cls(id=str(document["_id"]), **fields)


class EventCreatedResponse(BaseModel):
    """Acknowledgement returned once an event has been persisted."""

    id: str = Field(..., description="MongoDB document id of the inserted event.")
    event_id: str = Field(..., description="Echo of the upstream event identifier.")
