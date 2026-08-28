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

**Stage 9 — two idempotency keys, two partial indexes.** A `ManualVerification`
carries no `razorpay_event_id`; it carries a `confirmation_id` derived from its
execution. MongoDB treats a missing field as null in a unique index, so the original
non-partial `uniq_razorpay_event_id` would have admitted exactly one manual record
and refused the second with a `DuplicateKeyError` that had nothing to do with a
duplicate. Both indexes are therefore partial on `$exists`, each covering only the
records that have its key. `ensure_indexes` drops and recreates the old
non-partial index when it finds it, which is the one thing in Stage 9 that touches
data written by earlier stages; the reversal is a single `create_index` without the
`partialFilterExpression`.

**The referential guard branches on source, and the manual branch is an allowlist.**
A webhook record must name the link its execution created. A manual record has no
link to name, so the check it gets instead is that the execution is `completed` and
its `action_type` is in `CONTACT_ACTION_TYPES` — stated positively, so that adding a
fourth action type later leaves manual confirmation refusing it until somebody
decides otherwise, rather than silently accepting it.

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
from app.models.execution import CONTACT_ACTION_TYPES
from app.models.verification import (
    MANUAL_SOURCE,
    WEBHOOK_SOURCE,
    VerificationRecord,
)

logger = logging.getLogger(__name__)

COLLECTION_NAME = "verifications"

#: The execution status a verification may report on. A `failed` execution records
#: that no action reached the customer, so there is nothing for either path to
#: verify — and for the link path the model already forbids it a link id.
COMPLETED_EXECUTION_STATUS = "completed"

#: The idempotency key, as an index. See the module docstring.
EVENT_ID_INDEX = "uniq_razorpay_event_id"
#: Supports the per-event listing and the latest-per-event grouping.
VERIFIED_INDEX = "event_id_verified_at"
#: Supports resolving "has this execution already been verified".
EXECUTION_INDEX = "execution_id_verified_at"
#: Stage 9. The manual path's idempotency key, as an index.
CONFIRMATION_ID_INDEX = "uniq_confirmation_id"

#: Restricts each unique index to the records that actually carry its key.
#:
#: Without these the two paths collide: a missing field indexes as null, one null is
#: permitted per unique index, and the second record of the other kind is refused for
#: a duplicate it does not have. `$exists` rather than `$ne: null` because the field
#: is genuinely absent on the other variant — `model_dump` never writes it — so
#: existence is the honest predicate.
RAZORPAY_EVENT_ID_FILTER: dict[str, Any] = {"razorpay_event_id": {"$exists": True}}
CONFIRMATION_ID_FILTER: dict[str, Any] = {"confirmation_id": {"$exists": True}}


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


class NotManuallyConfirmable(VerificationReferenceError):
    """The named execution may not be confirmed by hand. Stage 9.

    Raised for two distinct refusals, both of which are refusals on purpose:

    * the execution produced a Razorpay artifact. Those have a real verification
      channel — a signed webhook about a link the gateway hosts — and letting a
      caller assert payment on one would make that channel optional, which is the
      same as not having it. There is no override;
    * the execution is not `completed`. A failed attempt reached nobody, so there is
      no contact for a customer to have responded to.

    A subclass of `VerificationReferenceError` so the webhook receiver's existing
    handler keeps catching everything the write-time guard can raise.
    """


class DuplicateVerification(RuntimeError):
    """Raised when this Razorpay event, or this confirmation, is already recorded.

    Carries the existing document so the receiver can answer with it instead of
    re-deriving one, which is how a re-delivery is acknowledged. Stage 9 reuses it
    unchanged for the manual path, where the duplicate being reported is a second
    confirmation of the same execution rather than a re-delivered webhook.
    """

    def __init__(self, existing: dict[str, Any]) -> None:
        self.existing = existing
        razorpay_event_id = existing.get("razorpay_event_id")
        label = (
            f"Razorpay event {razorpay_event_id}"
            if razorpay_event_id
            else f"confirmation {existing.get('confirmation_id')}"
        )
        super().__init__(
            f"{label} has already been recorded "
            f"({existing.get('outcome')} at {existing.get('verified_at')})"
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


def _same_filter(stored: Any, wanted: dict[str, Any]) -> bool:
    """Whether a stored `partialFilterExpression` matches the one declared above.

    MongoDB hands the expression back as an ordered mapping rather than a plain
    dict, so the values are normalised one level deep — which is as deep as these
    filters go. A false negative here is harmless: the index is dropped and rebuilt.
    """
    if stored is None:
        return False
    normalised = {
        key: dict(value) if hasattr(value, "items") else value
        for key, value in stored.items()
    }
    return normalised == wanted


async def _drop_if_not_partial(name: str, wanted: dict[str, Any]) -> bool:
    """Drop a unique index that predates its partial filter. Returns whether it went.

    `create_index` will not silently change an existing index's options — it raises
    `IndexOptionsConflict` — so the old non-partial `uniq_razorpay_event_id` has to be
    dropped before the partial one can be built. The rebuild is a real uniqueness
    check: if the collection somehow held duplicates, `create_index` fails and the
    startup log says uniqueness is not enforced, rather than the constraint quietly
    disappearing.
    """
    information = await collection().index_information()
    existing = information.get(name)
    if existing is None:
        return False
    if _same_filter(existing.get("partialFilterExpression"), wanted):
        return False
    logger.warning(
        "Index %r on %s has partialFilterExpression %r, not %r — dropping and "
        "rebuilding it so both verification sources can coexist (Stage 9 migration)",
        name,
        COLLECTION_NAME,
        existing.get("partialFilterExpression"),
        wanted,
    )
    await collection().drop_index(name)
    return True


async def ensure_indexes() -> None:
    """Create the verification indexes. Idempotent."""
    migrated = await _drop_if_not_partial(EVENT_ID_INDEX, RAZORPAY_EVENT_ID_FILTER)
    await collection().create_index(
        [("razorpay_event_id", ASCENDING)],
        unique=True,
        name=EVENT_ID_INDEX,
        partialFilterExpression=RAZORPAY_EVENT_ID_FILTER,
    )
    await collection().create_index(
        [("event_id", ASCENDING), ("verified_at", DESCENDING)],
        name=VERIFIED_INDEX,
    )
    await collection().create_index(
        [("execution_id", ASCENDING), ("verified_at", DESCENDING)],
        name=EXECUTION_INDEX,
    )
    await collection().create_index(
        [("confirmation_id", ASCENDING)],
        unique=True,
        name=CONFIRMATION_ID_INDEX,
        partialFilterExpression=CONFIRMATION_ID_FILTER,
    )
    logger.info(
        "Ensured indexes %r/%r/%r/%r on %s%s",
        EVENT_ID_INDEX,
        VERIFIED_INDEX,
        EXECUTION_INDEX,
        CONFIRMATION_ID_INDEX,
        COLLECTION_NAME,
        " (rebuilt the Razorpay key index as partial)" if migrated else "",
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


async def find_by_confirmation_id(confirmation_id: str) -> dict[str, Any] | None:
    """Return the manual confirmation recorded under this id, or None. Stage 9.

    The counterpart pre-flight lookup to `find_by_razorpay_event_id`, and an
    optimisation in exactly the same way: the partial unique index is what makes a
    concurrent second confirmation safe.
    """
    return await collection().find_one({"confirmation_id": confirmation_id})


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
    """Verify the named execution exists, is for this event, and admits this record.

    Re-reads from the database rather than trusting whatever the caller assembled,
    on the same reasoning as Stage 5's write-time guard: the caller looked the
    execution up a moment ago, and this asserts that the row about to be written
    still says so.

    Two branches after the shared checks, because the two sources are evidence about
    different things. A webhook record must name the link its execution created. A
    manual record has no link, so what it must satisfy instead is the positive
    allowlist: `completed`, and an `action_type` in `CONTACT_ACTION_TYPES`.

    Raises:
        DanglingExecutionReference: missing execution, or a different event.
        LinkMismatch: the execution did not create the link being reported on.
        NotManuallyConfirmable: the execution is not a completed contact action.
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

    if record.source == WEBHOOK_SOURCE:
        stored_link = document.get("razorpay_payment_link_id")
        if stored_link != record.razorpay_payment_link_id:
            raise LinkMismatch(
                f"Execution {record.execution_id!r} created link {stored_link!r}, but "
                f"this verification reports on {record.razorpay_payment_link_id!r}; "
                "refusing to attribute one link's outcome to another link's action"
            )
        return

    assert record.source == MANUAL_SOURCE, (
        f"source {record.source!r} has no branch in the write-time guard; a new "
        "verification source was added without deciding what it must prove"
    )

    action_type = document.get("action_type")
    if action_type not in CONTACT_ACTION_TYPES:
        raise NotManuallyConfirmable(
            f"Execution {record.execution_id!r} is a {action_type!r}, and only "
            f"{sorted(CONTACT_ACTION_TYPES)} may be confirmed by hand. A "
            "link-producing action is verified by a signed Razorpay webhook about "
            "the link it created; there is no manual override for that path"
        )
    execution_status = document.get("status")
    if execution_status != COMPLETED_EXECUTION_STATUS:
        raise NotManuallyConfirmable(
            f"Execution {record.execution_id!r} is {execution_status!r}, not "
            f"{COMPLETED_EXECUTION_STATUS!r}; nothing reached the customer, so there "
            "is no contact for a payment to have been a response to"
        )


async def insert(record: VerificationRecord) -> str:
    """Store a verification record.

    Returns:
        The new document's id.

    Raises:
        VerificationReferenceError: any subclass, if the guard refuses.
        DuplicateVerification: if this Razorpay event, or this execution's one
            permitted confirmation, is already recorded.
    """
    await _assert_execution_matches(record)

    try:
        result = await collection().insert_one(record.model_dump())
    except DuplicateKeyError:
        existing = (
            await find_by_razorpay_event_id(record.razorpay_event_id)
            if record.source == WEBHOOK_SOURCE
            else await find_by_confirmation_id(record.confirmation_id)
        )
        if existing is None:  # pragma: no cover - would mean the index lied
            raise
        logger.warning(
            "Verification for %s lost the insert race; returning the record that won",
            record.razorpay_event_id
            if record.source == WEBHOOK_SOURCE
            else record.confirmation_id,
        )
        raise DuplicateVerification(existing) from None

    if record.source == WEBHOOK_SOURCE:
        logger.info(
            "Recorded %s for event %s from Razorpay event %s (link %s, %.2f recovered)",
            record.outcome,
            record.event_id,
            record.razorpay_event_id,
            record.razorpay_payment_link_id,
            record.amount_recovered,
        )
    else:
        # Deliberately says "asserted", not "confirmed". This line is what an
        # operator reading the log sees, and the distinction the `source` field keeps
        # in the data is worth keeping in the prose too.
        logger.info(
            "Recorded MANUALLY ASSERTED %s for event %s from %s (%s, %.2f against an "
            "expected %.2f) — no gateway verified this",
            record.outcome,
            record.event_id,
            record.confirmation_id,
            record.confirmed_by,
            record.amount_recovered,
            record.amount_expected,
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
    source: str | None = None,
) -> list[dict[str, Any]]:
    """Return stored verifications, newest first.

    Args:
        event_id: Optionally restrict to one event.
        history: When False (the default) only the most recent verification per
            event is returned. When True, every one is returned — an event can
            accumulate several, since each authorized attempt gets its own link and
            each link can report paid, expired or cancelled.
        outcome: Optionally restrict to one outcome value.
        source: Optionally restrict to `webhook` or `manual_confirmation`. Records
            written before Stage 9 carry no `source` field and are all webhook
            records, so the webhook filter matches a missing field too — the same
            default `source_of` applies when they are read into a model.
    """
    query: dict[str, Any] = {}
    if event_id:
        query["event_id"] = event_id
    if outcome:
        query["outcome"] = outcome
    if source == WEBHOOK_SOURCE:
        query["source"] = {"$in": [WEBHOOK_SOURCE, None]}
    elif source:
        query["source"] = source

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
