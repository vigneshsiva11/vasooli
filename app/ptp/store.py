"""Persistence for promises to pay (Stage 6 Part B).

Three things worth reading before the code:

* **state transitions are enforced by the query, not by a check before it.**
  `apply_transition` puts `states_that_may_become(target)` into the Mongo *filter*,
  so an illegal move matches zero documents and writes nothing. A read-then-write
  guard would leave a window in which two concurrent checks both saw `broken` and
  both moved on. Same construction as `app/webhooks/store.py:transition_event_status`,
  and for the same reason;

* **one promise per event per date.** `uniq_event_promised_date` is a unique index,
  so a duplicate `POST /promises` is refused by the database rather than by a
  lookup that can lose a race. Multiple promises for one event are allowed and
  expected — a customer who breaks one and commits again produces a second
  document, and the original stays exactly as it was recorded;

* **`promised_date` is stored as an ISO-8601 string.** BSON has no date type, only
  datetime, and storing a bare date as midnight-UTC would silently invent a time
  that nobody promised and that shifts across timezones. A string round-trips
  exactly, sorts correctly, and range-queries correctly — which is what a real
  scheduled sweep would need.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, NamedTuple

from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorCollection
from pymongo import ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

from app.db import get_database
from app.models.events import TERMINAL_EVENT_STATUSES
from app.models.promise import (
    OPEN_PROMISE_STATE,
    PromiseState,
    PromiseToPay,
    promise_transition_allowed,
    states_that_may_become,
    today_utc,
)

logger = logging.getLogger(__name__)

COLLECTION_NAME = "promises"
EVENT_COLLECTION = "events"

#: One promise per event per promised date. THE IDEMPOTENCY KEY.
DATE_INDEX = "uniq_event_promised_date"
#: Latest-per-event reads, which is what `GET /promises` does by default.
CREATED_INDEX = "event_id_created_at"
#: What a real scheduled sweep would use: every open promise whose date has passed.
SWEEP_INDEX = "state_promised_date"


class PromiseError(ValueError):
    """Base class for refusals to write a promise."""


class EventNotFound(PromiseError):
    """Raised when a promise names an event that does not exist."""


class EventSettled(PromiseError):
    """Raised when a promise names an event that has already reached a terminal status.

    Distinct from `DuplicatePromise`: that one means "you already told us this", this
    one means "there is nothing left to tell us". The only terminal status is
    `recovered`, so in practice this fires when somebody records a promise for money
    that Razorpay has already confirmed arriving.
    """


class PromiseNotFound(PromiseError):
    """Raised when a check is requested for an event with no promise."""


class DuplicatePromise(RuntimeError):
    """Raised when a promise already exists for this event and date.

    Carries the existing document so the caller can decide whether this was an
    idempotent retry of the same commitment or a genuine conflict with a different
    amount — a distinction the database cannot make for us.
    """

    def __init__(self, existing: dict[str, Any]) -> None:
        self.existing = existing
        super().__init__(
            f"a promise already exists for event {existing.get('event_id')!r} on "
            f"{existing.get('promised_date')!r} for "
            f"{existing.get('promised_amount')!r}"
        )


class PromiseTransition(NamedTuple):
    """The outcome of one attempted state change, and why it went that way."""

    promise_id: str
    target: str
    changed: bool
    refused: bool
    current: str | None

    @property
    def detail(self) -> str:
        if self.changed:
            return f"promise moved to {self.target!r}"
        if self.current is None:
            return f"promise {self.promise_id} no longer exists"
        if self.current == self.target:
            return f"promise was already {self.target!r}"
        return (
            f"promise is {self.current!r}, which does not permit a move to "
            f"{self.target!r}"
        )


def collection() -> AsyncIOMotorCollection:
    """Return the promises collection."""
    return get_database()[COLLECTION_NAME]


async def ensure_indexes() -> None:
    """Create the indexes this stage relies on. Idempotent."""
    await collection().create_index(
        [("event_id", ASCENDING), ("promised_date", ASCENDING)],
        name=DATE_INDEX,
        unique=True,
    )
    await collection().create_index(
        [("event_id", ASCENDING), ("created_at", DESCENDING)],
        name=CREATED_INDEX,
    )
    await collection().create_index(
        [("state", ASCENDING), ("promised_date", ASCENDING)],
        name=SWEEP_INDEX,
    )
    logger.info(
        "Ensured indexes on '%s': %s (unique), %s, %s",
        COLLECTION_NAME,
        DATE_INDEX,
        CREATED_INDEX,
        SWEEP_INDEX,
    )


def encode(promise: PromiseToPay) -> dict[str, Any]:
    """Render a promise for storage, with `promised_date` as an ISO string."""
    document = promise.model_dump()
    document["promised_date"] = promise.promised_date.isoformat()
    return document


def decode_date(value: Any) -> date:
    """Read a stored `promised_date` back into a `date`.

    Tolerates a datetime as well as a string so that a document written by some
    other tool — or by an earlier version of this code — still reads, rather than
    failing the whole list request.
    """
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    return date.fromisoformat(str(value))


# ---------------------------------------------------------------------------
# Referential guards.
# ---------------------------------------------------------------------------

#: Event statuses that cannot carry a new promise.
#:
#: Derived from the ratified status table rather than listed, so it cannot disagree
#: with it. Today that is `recovered` alone: you cannot promise to pay money that
#: has already arrived, and a promise against a terminal event could never be
#: resolved because nothing will move that event again.
#:
#: THERE IS NO SURFACE RESTRICTION HERE, and that is a corrected decision rather
#: than an omission. Promise-to-pay is motivated by receivables, and an earlier
#: version of this file refused every other surface on that basis. That restriction
#: made the `honored` state unreachable in production: honoring a promise requires a
#: `VerificationRecord`, Part A only writes those for Razorpay payment-link events,
#: and no receivable root cause maps to a link-producing intervention — see the
#: receivable block of `app/decision/matrix.py`, where every candidate is a contact.
#: So a receivable-only promise could be broken but never honored. Restricting the
#: surface would have made the safety-critical path dead code, which is a worse
#: outcome than allowing a promise against a failed card payment that somebody has
#: said they will settle.
NON_PROMISABLE_STATUSES: frozenset[str] = TERMINAL_EVENT_STATUSES


async def assert_event_promisable(event_id: str) -> dict[str, Any]:
    """Return the event this promise is about, or refuse to record the promise.

    Raises:
        EventNotFound: no such event. A promise against nothing is not a promise.
        EventSettled: the event has reached a terminal status.
    """
    event = await get_database()[EVENT_COLLECTION].find_one({"event_id": event_id})
    if event is None:
        raise EventNotFound(
            f"no event with event_id {event_id!r}; a promise must be about "
            "revenue this system already knows is at risk"
        )
    status = event.get("status")
    if status in NON_PROMISABLE_STATUSES:
        raise EventSettled(
            f"event {event_id!r} is {status!r}, which is terminal; there is nothing "
            "left to promise to pay and no transition could ever resolve the promise"
        )
    return event


# ---------------------------------------------------------------------------
# Writes.
# ---------------------------------------------------------------------------


async def insert(promise: PromiseToPay) -> str:
    """Persist a new promise and return its document id.

    Raises:
        EventNotFound / EventSettled: the referential guard refused.
        DuplicatePromise: one already exists for this event and date.
    """
    await assert_event_promisable(promise.event_id)

    try:
        result = await collection().insert_one(encode(promise))
    except DuplicateKeyError as exc:
        existing = await find_for_date(promise.event_id, promise.promised_date)
        if existing is None:  # pragma: no cover - the index just said otherwise
            raise RuntimeError(
                "the unique index rejected this promise but no existing document "
                f"was found for event {promise.event_id!r} on "
                f"{promise.promised_date.isoformat()}"
            ) from exc
        raise DuplicatePromise(existing) from exc

    logger.info(
        "Recorded promise for event %r: %.2f by %s (state %r)",
        promise.event_id,
        promise.promised_amount,
        promise.promised_date.isoformat(),
        promise.state,
    )
    return str(result.inserted_id)


async def apply_transition(
    *,
    promise_id: str,
    target: PromiseState,
    resolved_at: datetime,
    follow_up_sent: bool | None = None,
) -> PromiseTransition:
    """Move one promise to `target`, if and only if its current state permits it.

    The permitted predecessors go into the filter, so this is a single atomic
    operation and not a check followed by a write. A promise that is not eligible
    matches nothing, `changed` comes back False, and `refused` says so — the caller
    reports it rather than retrying, because an ineligible transition is
    information, not a failure.

    `follow_up_sent` is written in the *same* update as the state, never separately:
    `reevaluating` is invalid without it, so two writes would leave a window in
    which a stored promise violated its own model.
    """
    eligible = sorted(states_that_may_become(target))
    changes: dict[str, Any] = {"state": target, "resolved_at": resolved_at}
    if follow_up_sent is not None:
        changes["follow_up_sent"] = follow_up_sent

    result = await collection().update_one(
        {"_id": ObjectId(promise_id), "state": {"$in": eligible}},
        {"$set": changes},
    )
    if result.matched_count:
        logger.info("Promise %s state -> %r", promise_id, target)
        return PromiseTransition(
            promise_id=promise_id,
            target=target,
            changed=True,
            refused=False,
            current=target,
        )

    document = await collection().find_one({"_id": ObjectId(promise_id)})
    current = None if document is None else document.get("state")

    if current is not None and current != target:
        # The filter and the declaration must agree. If this ever fires, the table
        # says the move is legal but the query that encodes the table did not match,
        # which means one of them has been edited without the other.
        assert not promise_transition_allowed(current, target), (
            f"promise {promise_id} is {current!r} and ALLOWED_PROMISE_TRANSITIONS "
            f"permits {current!r} -> {target!r}, but the guarded update matched "
            "nothing; the filter and the transition table disagree"
        )
        logger.info(
            "Refused promise %s transition %r -> %r", promise_id, current, target
        )

    return PromiseTransition(
        promise_id=promise_id,
        target=target,
        changed=False,
        refused=current is not None and current != target,
        current=current,
    )


# ---------------------------------------------------------------------------
# Reads.
# ---------------------------------------------------------------------------


async def find_by_id(promise_id: str) -> dict[str, Any] | None:
    """Return one promise by document id."""
    return await collection().find_one({"_id": ObjectId(promise_id)})


async def find_for_date(event_id: str, promised_date: date) -> dict[str, Any] | None:
    """Return the promise for this event and date, if there is one."""
    return await collection().find_one(
        {"event_id": event_id, "promised_date": promised_date.isoformat()}
    )


async def find_latest(event_id: str) -> dict[str, Any] | None:
    """Return the most recently recorded promise for an event.

    "Most recent" is by `created_at`, not by `promised_date`: the promise that is
    current is the one the customer made last, even if they committed to an earlier
    date than a promise they made before it.
    """
    return await collection().find_one(
        {"event_id": event_id}, sort=[("created_at", DESCENDING)]
    )


async def list_promises(
    event_id: str | None = None,
    *,
    history: bool = False,
    state: str | None = None,
) -> list[dict[str, Any]]:
    """Return stored promises, newest first.

    The same latest-per-event / full-history pair every other stage's list endpoint
    offers. `state` filters inside the pipeline rather than after it, because
    unlike the execution list there is no "the latest one is the current state"
    reading to protect: promises are independent commitments, so a caller asking
    for the broken ones wants all of them.
    """
    query: dict[str, Any] = {}
    if event_id is not None:
        query["event_id"] = event_id
    if state is not None:
        query["state"] = state

    if history:
        cursor = collection().find(query).sort("created_at", DESCENDING)
        return await cursor.to_list(length=None)

    pipeline: list[dict[str, Any]] = []
    if query:
        pipeline.append({"$match": query})
    pipeline += [
        {"$sort": {"event_id": ASCENDING, "created_at": DESCENDING}},
        {"$group": {"_id": "$event_id", "latest": {"$first": "$$ROOT"}}},
        {"$replaceRoot": {"newRoot": "$latest"}},
        {"$sort": {"created_at": DESCENDING}},
    ]
    return await collection().aggregate(pipeline).to_list(length=None)


async def count_open_overdue() -> int:
    """How many promises a scheduled sweep would have work to do on.

    Not used by any endpoint. Present because it is the query the scheduler this
    stage deliberately does not build would run, and having it here documents the
    shape of that missing piece more precisely than a comment about it would.
    """
    return await collection().count_documents(
        {
            "state": OPEN_PROMISE_STATE,
            "promised_date": {"$lt": today_utc().isoformat()},
        }
    )
