"""Persistence for execution records (Stage 5).

Writes to one collection, `executions`, and reads three others — `policy_verdicts`,
`decisions`, `revenue_events` — which it never modifies. An execution is additive to
an event's history, exactly like a verdict is.

**Idempotency lives here, in three layers.** `find_for_verdict` is the pre-flight
check the service uses to return an existing record instead of acting again; the
unique index on `policy_verdict_id` is what makes that correct under a race, because
two concurrent requests both passing the pre-flight will not both insert; and
`app/execution/razorpay.py` derives `reference_id` from the same verdict id, so the
provider refuses a duplicate on the side where the side effect actually happens. The
check-first path is an optimisation for the common case. The index is the guarantee.

**One record per verdict, forever — success or failure.** There is no version field
here, unlike every other stage: a verdict is a single permission, and re-running it
is the thing being prevented rather than a new version of anything. History still
exists, one level up: re-authorizing an event produces a new verdict, and that new
verdict gets its own execution. See `app/execution/service.py` for why a failure is
terminal for its verdict rather than retryable.

**The referential guard re-reads the verdict from the database.** `AuthorizedVerdict`
in `app/models/execution.py` makes an unauthorized verdict unrepresentable in the
executor's signature, which is a claim about code paths. This module makes the
weaker but differently-scoped claim that no row lands in the collection unless the
verdict it names is, at insert time, an existing, current, authorized verdict for
that event whose decision recommends the intervention being recorded. A type cannot
assert that; only a query can.
"""

from __future__ import annotations

import logging
from typing import Any

from bson import ObjectId
from bson.errors import InvalidId
from motor.motor_asyncio import AsyncIOMotorCollection
from pymongo import ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

from app.db import get_database
from app.decision.store import COLLECTION_NAME as DECISION_COLLECTION
from app.models.execution import ExecutionRecord
from app.policy.store import COLLECTION_NAME as VERDICT_COLLECTION
from app.policy.store import EXECUTION_COLLECTION as POLICY_SIDE_EXECUTION_COLLECTION

logger = logging.getLogger(__name__)

COLLECTION_NAME = "executions"

#: `app/policy/store.py` cannot import this module — execution imports policy, so the
#: reverse would be a cycle — and so declares the collection name itself. This
#: asserts the two agree at import time. A silent divergence would make the cooldown
#: measure from an empty collection while executions piled up in another, which is
#: exactly the sort of bug that looks like "the cooldown never triggers".
assert POLICY_SIDE_EXECUTION_COLLECTION == COLLECTION_NAME, (
    f"app.policy.store names the execution collection "
    f"{POLICY_SIDE_EXECUTION_COLLECTION!r}, this module names it {COLLECTION_NAME!r}"
)

#: The idempotency key, as an index. See the module docstring.
VERDICT_INDEX = "uniq_policy_verdict_id"
#: Supports the cooldown lookup and the per-event listing.
EVENT_INDEX = "event_id_executed_at"


class VerdictReferenceError(ValueError):
    """Base for executions whose referenced verdict does not hold up."""


class DanglingVerdictReference(VerdictReferenceError):
    """Raised when an execution points at a verdict that does not exist as claimed."""


class UnauthorizedVerdictReference(VerdictReferenceError):
    """Raised when an execution names a verdict that did not grant permission.

    The loudest error in this stage. Reaching it means something got past
    `require_authorized` — the type-level gate — and tried to write anyway.
    """


class StaleVerdictReference(VerdictReferenceError):
    """Raised when the named verdict has been superseded by a later one.

    The same reasoning as `StaleDecisionReference` in Stage 4, one level along:
    permission granted at v3 and revoked at v4 is revoked. Acting on v3 afterwards
    would execute an authorization the pipeline has since withdrawn.
    """


class InterventionMismatch(VerdictReferenceError):
    """Raised when the recorded intervention is not the one that was authorized.

    Permission is granted for a specific recommendation, not for the event in
    general. Sending a payment link under a verdict that authorized a reminder is a
    different action from the one that was approved.
    """


class DuplicateExecution(RuntimeError):
    """Raised when a verdict already has an execution record.

    Carries the existing record so the caller can return it rather than re-deriving
    it, which is how `POST /execute/{event_id}` answers a repeat request.
    """

    def __init__(self, existing: dict[str, Any]) -> None:
        self.existing = existing
        super().__init__(
            f"verdict {existing.get('policy_verdict_id')} has already been executed "
            f"({existing.get('status')} at {existing.get('executed_at')})"
        )


def collection() -> AsyncIOMotorCollection:
    """Return the executions collection.

    Raises:
        RuntimeError: if MongoDB is not connected.
    """
    return get_database()[COLLECTION_NAME]


async def ensure_indexes() -> None:
    """Create the execution indexes. Idempotent."""
    await collection().create_index(
        [("policy_verdict_id", ASCENDING)],
        unique=True,
        name=VERDICT_INDEX,
    )
    await collection().create_index(
        [("event_id", ASCENDING), ("executed_at", DESCENDING)],
        name=EVENT_INDEX,
    )
    logger.info(
        "Ensured indexes %r/%r on %s", VERDICT_INDEX, EVENT_INDEX, COLLECTION_NAME
    )


async def find_for_verdict(policy_verdict_id: str) -> dict[str, Any] | None:
    """Return the execution record for a verdict, or None.

    The pre-flight idempotency check. Not the guarantee — see the module docstring.
    """
    return await collection().find_one({"policy_verdict_id": policy_verdict_id})


# ---------------------------------------------------------------------------
# The referential guard.
# ---------------------------------------------------------------------------


async def _assert_verdict_authorizes(record: ExecutionRecord) -> None:
    """Verify the named verdict exists, is current, authorized, and matches.

    Re-reads from the database rather than trusting the `AuthorizedVerdict` the
    caller already holds. That redundancy is the point: the type proves something
    about the call path, this proves something about the row about to be written.

    Raises:
        DanglingVerdictReference: missing verdict, or wrong event or version.
        UnauthorizedVerdictReference: the verdict did not grant permission.
        StaleVerdictReference: a later verdict exists for this event.
        InterventionMismatch: the intervention is not the authorized one.
    """
    try:
        object_id = ObjectId(record.policy_verdict_id)
    except InvalidId as exc:  # pragma: no cover - the model's pattern precedes this
        raise DanglingVerdictReference(
            f"policy_verdict_id {record.policy_verdict_id!r} is not a valid ObjectId"
        ) from exc

    verdicts = get_database()[VERDICT_COLLECTION]
    document = await verdicts.find_one(
        {"_id": object_id},
        {"event_id": 1, "version": 1, "verdict": 1, "reason": 1, "decision_id": 1},
    )
    if document is None:
        raise DanglingVerdictReference(
            f"No policy verdict with id {record.policy_verdict_id!r} exists; refusing "
            "to record an execution that nothing authorized"
        )
    if document["event_id"] != record.event_id:
        raise DanglingVerdictReference(
            f"Verdict {record.policy_verdict_id!r} belongs to event "
            f"{document['event_id']!r}, not {record.event_id!r}"
        )
    if int(document["version"]) != record.policy_verdict_version:
        raise DanglingVerdictReference(
            f"Verdict {record.policy_verdict_id!r} is version {document['version']}, "
            f"not {record.policy_verdict_version}"
        )
    if document["verdict"] != "authorized":
        raise UnauthorizedVerdictReference(
            f"Verdict {record.policy_verdict_id!r} for event {record.event_id!r} is "
            f"{document['verdict']!r} ({document.get('reason')!r}), not 'authorized'; "
            "refusing to record an execution of an action that was not permitted"
        )

    newest = await verdicts.find_one(
        {"event_id": record.event_id},
        {"version": 1},
        sort=[("version", DESCENDING)],
    )
    if newest is not None and int(newest["version"]) > record.policy_verdict_version:
        raise StaleVerdictReference(
            f"Verdict v{record.policy_verdict_version} for event "
            f"{record.event_id!r} has been superseded by v{newest['version']}; "
            "refusing to execute a permission that is no longer current"
        )

    decision = await get_database()[DECISION_COLLECTION].find_one(
        {"_id": ObjectId(document["decision_id"])}, {"recommended_intervention": 1}
    )
    if decision is None:
        raise DanglingVerdictReference(
            f"Verdict {record.policy_verdict_id!r} references decision "
            f"{document['decision_id']!r}, which no longer exists; the authorized "
            "intervention cannot be confirmed"
        )
    if decision["recommended_intervention"] != record.intervention:
        raise InterventionMismatch(
            f"Verdict {record.policy_verdict_id!r} authorized "
            f"{decision['recommended_intervention']!r}, not {record.intervention!r}"
        )


async def insert(record: ExecutionRecord) -> str:
    """Store an execution record.

    Args:
        record: A validated record. Its `executed_at` should already be the real
            send time — this function does not stamp one.

    Returns:
        The new document's id.

    Raises:
        VerdictReferenceError: any of its subclasses, if the guard refuses.
        DuplicateExecution: if this verdict already has a record. Carries it.
    """
    await _assert_verdict_authorizes(record)

    try:
        result = await collection().insert_one(record.model_dump())
    except DuplicateKeyError:
        existing = await find_for_verdict(record.policy_verdict_id)
        if existing is None:  # pragma: no cover - would mean the index lied
            raise
        logger.warning(
            "Execution for verdict %s lost the insert race; returning the record "
            "that won",
            record.policy_verdict_id,
        )
        raise DuplicateExecution(existing) from None

    logger.info(
        "Recorded %s execution of %s for event %s (verdict %s)",
        record.status,
        record.intervention,
        record.event_id,
        record.policy_verdict_id,
    )
    return str(result.inserted_id)


# ---------------------------------------------------------------------------
# Reading back.
# ---------------------------------------------------------------------------


async def list_executions(
    event_id: str | None = None,
    *,
    history: bool = False,
    status: str | None = None,
) -> list[dict[str, Any]]:
    """Return stored executions, newest first.

    Args:
        event_id: Optionally restrict to one event.
        history: When False (the default) only the most recent execution per event
            is returned, since that is what was last done. When True, every
            execution is returned — an event accumulates one per authorized verdict,
            so the history is where a failed attempt followed by a successful
            re-authorization is visible.
        status: Optionally restrict to 'completed' or 'failed'.
    """
    query: dict[str, Any] = {}
    if event_id:
        query["event_id"] = event_id
    if status:
        query["status"] = status

    if history:
        return (
            await collection()
            .find(query)
            .sort([("executed_at", DESCENDING), ("event_id", ASCENDING)])
            .to_list(length=None)
        )

    pipeline: list[dict[str, Any]] = []
    if query:
        pipeline.append({"$match": query})
    pipeline += [
        {"$sort": {"event_id": ASCENDING, "executed_at": DESCENDING}},
        {"$group": {"_id": "$event_id", "document": {"$first": "$$ROOT"}}},
        {"$replaceRoot": {"newRoot": "$document"}},
        {"$sort": {"executed_at": DESCENDING, "event_id": ASCENDING}},
    ]
    return await collection().aggregate(pipeline).to_list(length=None)
