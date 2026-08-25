"""Persistence for verification records (Stage 6 Part A).

Owns the `verifications` collection. Reads `executions` to resolve a Razorpay
payment link back to the action that created it, and performs the one deliberate
mutation this project makes to a `RevenueEvent` after ingestion: its `status`.

**Idempotency is the unique index on `razorpay_event_id`.** The pre-flight lookup
in `find_by_razorpay_event_id` is an optimisation for the common case; the index is
what makes a concurrent re-delivery safe. This mirrors Stage 5's arrangement around
`policy_verdict_id`, with one difference worth stating: there the third layer was
Razorpay refusing a duplicate `reference_id` on the side where the side effect
happens. There is no third layer here, because the "side effect" is somebody else's
retry and we do not control it. Two layers, and the index is the one that holds.

**The status update is guarded by the transition table, in the query.** The filter
names the states the target is reachable from, so an ineligible event matches zero
documents rather than being written and corrected. Razorpay does not guarantee
ordering, so `payment_link.expired` genuinely can arrive after `payment_link.paid`
for the same link; the filter is what makes that harmless. Checking the current
status in Python and then writing would leave the race open in exactly the window
that matters.

**It is a `$set`, and that is not a violation of Stage 1's discipline.**
`app/ingestion/store.py` keeps `status` in `INSERT_ONLY_FIELDS` so that
*re-ingesting* an event cannot rewind it. That protects against an upstream
re-delivery, which carries no information about recovery. This module is the
opposite case: a deliberate, evidenced lifecycle transition, written once per
verification, from a source that does know.
"""

from __future__ import annotations

import logging
from typing import Any, NamedTuple

from bson import ObjectId
from bson.errors import InvalidId
from motor.motor_asyncio import AsyncIOMotorCollection
from pymongo import ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

from app.db import get_database
from app.execution.store import COLLECTION_NAME as EXECUTION_COLLECTION
from app.ingestion.store import COLLECTION_NAME as EVENT_COLLECTION
from app.models.events import statuses_that_may_become, transition_allowed
from app.models.verification import VerificationRecord

logger = logging.getLogger(__name__)

COLLECTION_NAME = "verifications"

#: The idempotency key, as an index. See the module docstring.
EVENT_ID_INDEX = "uniq_razorpay_event_id"
#: Supports the per-event listing and the latest-per-event grouping.
VERIFIED_INDEX = "event_id_verified_at"
#: Supports resolving "has this execution already been verified".
EXECUTION_INDEX = "execution_id_verified_at"


class VerificationReferenceError(ValueError):
    """Base for verifications whose referenced execution does not hold up."""


class DanglingExecutionReference(VerificationReferenceError):
    """The verification names an execution that does not exist as claimed."""


class LinkMismatch(VerificationReferenceError):
    """The named execution did not create the payment link being reported on.

    The loudest error in this stage. A verification is only evidence about an action
    if it is about *that* action's artifact; attaching Razorpay's word about link A
    to the execution that created link B would credit a recovery to the wrong
    attempt, and the record would look perfectly well-formed afterwards.
    """


class DuplicateVerification(RuntimeError):
    """Raised when this Razorpay event has already been recorded.

    Carries the existing document so the receiver can answer with it instead of
    re-deriving one, which is how a re-delivery is acknowledged.
    """

    def __init__(self, existing: dict[str, Any]) -> None:
        self.existing = existing
        super().__init__(
            f"Razorpay event {existing.get('razorpay_event_id')} has already been "
            f"recorded ({existing.get('outcome')} at {existing.get('verified_at')})"
        )


class StatusTransition(NamedTuple):
    """The result of attempting one event-status transition.

    `changed` False with `refused` False means the event was already in the target
    state — a re-delivery that got past the record-level dedup, or two links for one
    event both resolving to `recovered`. `refused` True means the transition is not
    in the table from where the event actually is, which is the out-of-order case
    the guard exists for.
    """

    event_id: str
    target: str
    changed: bool
    refused: bool
    current: str | None

    @property
    def detail(self) -> str:
        """One line describing what happened, for the log and the response."""
        if self.current is None:
            return f"event {self.event_id!r} not found; status unchanged"
        if self.changed:
            return f"event {self.event_id!r} status -> {self.target!r}"
        if self.refused:
            return (
                f"event {self.event_id!r} is {self.current!r}, which does not permit "
                f"a move to {self.target!r}; status left alone"
            )
        return f"event {self.event_id!r} was already {self.target!r}"


def collection() -> AsyncIOMotorCollection:
    """Return the verifications collection.

    Raises:
        RuntimeError: if MongoDB is not connected.
    """
    return get_database()[COLLECTION_NAME]


async def ensure_indexes() -> None:
    """Create the verification indexes. Idempotent."""
    await collection().create_index(
        [("razorpay_event_id", ASCENDING)],
        unique=True,
        name=EVENT_ID_INDEX,
    )
    await collection().create_index(
        [("event_id", ASCENDING), ("verified_at", DESCENDING)],
        name=VERIFIED_INDEX,
    )
    await collection().create_index(
        [("execution_id", ASCENDING), ("verified_at", DESCENDING)],
        name=EXECUTION_INDEX,
    )
    logger.info(
        "Ensured indexes %r/%r/%r on %s",
        EVENT_ID_INDEX,
        VERIFIED_INDEX,
        EXECUTION_INDEX,
        COLLECTION_NAME,
    )


# ---------------------------------------------------------------------------
# Matching a Razorpay link back to what created it.
# ---------------------------------------------------------------------------


async def find_execution_by_link_id(link_id: str) -> dict[str, Any] | None:
    """Return the execution that created a Razorpay payment link, or None.

    Restricted to `completed` executions: a `failed` one records that no artifact
    was created, and the model enforces that it carries no link id, so a failure
    matching here would mean the record contradicts itself.

    Returns None rather than raising. An unmatched link is an ordinary situation —
    the test account can hold links this system never created — and the caller's job
    is to log it and acknowledge, not to invent a match.
    """
    return await get_database()[EXECUTION_COLLECTION].find_one(
        {"razorpay_payment_link_id": link_id, "status": "completed"}
    )


async def find_by_razorpay_event_id(razorpay_event_id: str) -> dict[str, Any] | None:
    """Return the verification recorded for a Razorpay event id, or None."""
    return await collection().find_one({"razorpay_event_id": razorpay_event_id})


async def list_for_event(event_id: str) -> list[dict[str, Any]]:
    """Return every verification for one event, newest first.

    The read behind promise-to-pay's payment re-check, so it deliberately returns
    everything rather than the latest: `honored` turns on whether a recovery exists
    at all, not on whether the most recent webhook happened to be one.
    """
    return (
        await collection()
        .find({"event_id": event_id})
        .sort("verified_at", DESCENDING)
        .to_list(length=None)
    )


# ---------------------------------------------------------------------------
# The referential guard.
# ---------------------------------------------------------------------------


async def _assert_execution_matches(record: VerificationRecord) -> None:
    """Verify the named execution exists, is for this event, and made this link.

    Re-reads from the database rather than trusting whatever the reconciler
    assembled, on the same reasoning as Stage 5's write-time guard: the reconciler
    looked the execution up by link id a moment ago, and this asserts that the row
    about to be written still says so.

    Raises:
        DanglingExecutionReference: missing execution, or a different event.
        LinkMismatch: the execution did not create the link being reported on.
    """
    try:
        object_id = ObjectId(record.execution_id)
    except InvalidId as exc:  # pragma: no cover - the model's pattern precedes this
        raise DanglingExecutionReference(
            f"execution_id {record.execution_id!r} is not a valid ObjectId"
        ) from exc

    document = await get_database()[EXECUTION_COLLECTION].find_one(
        {"_id": object_id},
        {"event_id": 1, "razorpay_payment_link_id": 1, "status": 1, "action_type": 1},
    )
    if document is None:
        raise DanglingExecutionReference(
            f"No execution with id {record.execution_id!r} exists; refusing to "
            "record a verification of an action that is not on file"
        )
    if document["event_id"] != record.event_id:
        raise DanglingExecutionReference(
            f"Execution {record.execution_id!r} belongs to event "
            f"{document['event_id']!r}, not {record.event_id!r}"
        )
    stored_link = document.get("razorpay_payment_link_id")
    if stored_link != record.razorpay_payment_link_id:
        raise LinkMismatch(
            f"Execution {record.execution_id!r} created link {stored_link!r}, but "
            f"this verification reports on {record.razorpay_payment_link_id!r}; "
            "refusing to attribute one link's outcome to another link's action"
        )


async def insert(record: VerificationRecord) -> str:
    """Store a verification record.

    Returns:
        The new document's id.

    Raises:
        VerificationReferenceError: any subclass, if the guard refuses.
        DuplicateVerification: if this Razorpay event is already recorded.
    """
    await _assert_execution_matches(record)

    try:
        result = await collection().insert_one(record.model_dump())
    except DuplicateKeyError:
        existing = await find_by_razorpay_event_id(record.razorpay_event_id)
        if existing is None:  # pragma: no cover - would mean the index lied
            raise
        logger.warning(
            "Verification for Razorpay event %s lost the insert race; returning the "
            "record that won",
            record.razorpay_event_id,
        )
        raise DuplicateVerification(existing) from None

    logger.info(
        "Recorded %s for event %s from Razorpay event %s (link %s, %.2f recovered)",
        record.outcome,
        record.event_id,
        record.razorpay_event_id,
        record.razorpay_payment_link_id,
        record.amount_recovered,
    )
    return str(result.inserted_id)


# ---------------------------------------------------------------------------
# The one deliberate mutation of an event.
# ---------------------------------------------------------------------------


async def transition_event_status(*, event_id: str, target: str) -> StatusTransition:
    """Move an event to `target`, but only from a state the table permits.

    The permitted predecessors go into the *filter*, not into an `if` above the
    write, so the check and the write are one atomic operation. That is what makes a
    late-arriving `payment_link.expired` harmless against an event already marked
    `recovered`: it matches no document.

    Never raises on a refusal. An out-of-order webhook is expected behaviour, not an
    error, and the caller has to acknowledge the delivery either way.
    """
    events = get_database()[EVENT_COLLECTION]
    eligible = sorted(statuses_that_may_become(target))

    result = await events.update_one(
        {"event_id": event_id, "status": {"$in": eligible}},
        {"$set": {"status": target}},
    )
    if result.modified_count:
        outcome = StatusTransition(
            event_id=event_id, target=target, changed=True, refused=False, current=target
        )
        logger.info("%s", outcome.detail)
        return outcome

    # Nothing moved. Find out why, so the log says which of the three cases it was.
    document = await events.find_one({"event_id": event_id}, {"status": 1})
    if document is None:
        outcome = StatusTransition(
            event_id=event_id,
            target=target,
            changed=False,
            refused=False,
            current=None,
        )
        logger.warning("%s", outcome.detail)
        return outcome

    current = str(document.get("status"))
    if current == target:
        outcome = StatusTransition(
            event_id=event_id,
            target=target,
            changed=False,
            refused=False,
            current=current,
        )
        logger.info("%s", outcome.detail)
        return outcome

    assert not transition_allowed(current, target), (
        f"event {event_id!r} is {current!r} and the table permits {target!r}, yet the "
        "guarded update matched nothing — the filter and the table have diverged"
    )
    outcome = StatusTransition(
        event_id=event_id, target=target, changed=False, refused=True, current=current
    )
    logger.warning("%s", outcome.detail)
    return outcome


async def current_event_status(event_id: str) -> str | None:
    """Return an event's stored status, or None if the event is absent."""
    document = await get_database()[EVENT_COLLECTION].find_one(
        {"event_id": event_id}, {"status": 1}
    )
    return None if document is None else str(document.get("status"))


# ---------------------------------------------------------------------------
# Reading back.
# ---------------------------------------------------------------------------


async def list_verifications(
    event_id: str | None = None,
    *,
    history: bool = False,
    outcome: str | None = None,
) -> list[dict[str, Any]]:
    """Return stored verifications, newest first.

    Args:
        event_id: Optionally restrict to one event.
        history: When False (the default) only the most recent verification per
            event is returned. When True, every one is returned — an event can
            accumulate several, since each authorized attempt gets its own link and
            each link can report paid, expired or cancelled.
        outcome: Optionally restrict to one outcome value.
    """
    query: dict[str, Any] = {}
    if event_id:
        query["event_id"] = event_id
    if outcome:
        query["outcome"] = outcome

    if history:
        return (
            await collection()
            .find(query)
            .sort([("verified_at", DESCENDING), ("event_id", ASCENDING)])
            .to_list(length=None)
        )

    pipeline: list[dict[str, Any]] = []
    if query:
        pipeline.append({"$match": query})
    pipeline += [
        {"$sort": {"event_id": ASCENDING, "verified_at": DESCENDING}},
        {"$group": {"_id": "$event_id", "document": {"$first": "$$ROOT"}}},
        {"$replaceRoot": {"newRoot": "$document"}},
        {"$sort": {"verified_at": DESCENDING, "event_id": ASCENDING}},
    ]
    return await collection().aggregate(pipeline).to_list(length=None)
