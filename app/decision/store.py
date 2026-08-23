"""Persistence for decisions.

Append-only, matching the diagnosis collection: re-deciding an event stores a new
version rather than overwriting the old recommendation, so what was recommended
before a re-diagnosis stays inspectable.

Writes only to the `decisions` collection. The events and diagnoses collections
are read from and never modified — a decision is additive to the history of an
event, exactly as a diagnosis is.
"""

from __future__ import annotations

import logging
from typing import Any

from bson import ObjectId
from bson.errors import InvalidId
from motor.motor_asyncio import AsyncIOMotorCollection
from pymongo import DESCENDING
from pymongo.errors import DuplicateKeyError

from app.db import get_database
from app.diagnosis.store import COLLECTION_NAME as DIAGNOSIS_COLLECTION
from app.models import Decision

logger = logging.getLogger(__name__)

COLLECTION_NAME = "decisions"
VERSION_INDEX = "uniq_event_id_version"

#: Bound on the optimistic-retry loop when allocating a version number.
MAX_VERSION_ATTEMPTS = 5


class DanglingDiagnosisReference(ValueError):
    """Raised when a decision points at a diagnosis that does not exist."""


def collection() -> AsyncIOMotorCollection:
    """Return the decisions collection.

    Raises:
        RuntimeError: if MongoDB is not connected.
    """
    return get_database()[COLLECTION_NAME]


async def ensure_indexes() -> None:
    """Create the unique (event_id, version) index. Idempotent."""
    await collection().create_index(
        [("event_id", 1), ("version", DESCENDING)],
        unique=True,
        name=VERSION_INDEX,
    )
    logger.info(
        "Ensured unique index %r on %s.(event_id, version)",
        VERSION_INDEX,
        COLLECTION_NAME,
    )


async def latest_version(event_id: str) -> int:
    """Return the highest stored version for an event, or 0 if none exist."""
    document = await collection().find_one(
        {"event_id": event_id},
        {"version": 1},
        sort=[("version", DESCENDING)],
    )
    return int(document["version"]) if document else 0


async def _assert_diagnosis_exists(decision: Decision) -> None:
    """Verify the referenced diagnosis exists and belongs to the same event.

    Pydantic can validate that `diagnosis_id` is shaped like an ObjectId, but it
    cannot know whether that document exists — so the referential check lives
    here, at the write boundary, where it is unavoidable. It also compares
    `event_id` and `version`, which catches a decision cross-linked to a
    different event's diagnosis, not merely a missing one.
    """
    try:
        object_id = ObjectId(decision.diagnosis_id)
    except InvalidId as exc:  # pragma: no cover - the model's pattern precedes this
        raise DanglingDiagnosisReference(
            f"diagnosis_id {decision.diagnosis_id!r} is not a valid ObjectId"
        ) from exc

    document = await get_database()[DIAGNOSIS_COLLECTION].find_one(
        {"_id": object_id}, {"event_id": 1, "version": 1}
    )
    if document is None:
        raise DanglingDiagnosisReference(
            f"No diagnosis with id {decision.diagnosis_id!r} exists; refusing to "
            "store a decision that references nothing"
        )
    if document["event_id"] != decision.event_id:
        raise DanglingDiagnosisReference(
            f"Diagnosis {decision.diagnosis_id!r} belongs to event "
            f"{document['event_id']!r}, not {decision.event_id!r}"
        )
    if int(document["version"]) != decision.diagnosis_version:
        raise DanglingDiagnosisReference(
            f"Diagnosis {decision.diagnosis_id!r} is version "
            f"{document['version']}, not {decision.diagnosis_version}"
        )


async def append(decision: Decision) -> tuple[str, int]:
    """Store a decision as the next version for its event.

    Returns:
        The new document's id and its version.

    Raises:
        DanglingDiagnosisReference: if the referenced diagnosis is missing or
            belongs to a different event or version.
    """
    await _assert_diagnosis_exists(decision)

    payload = decision.model_dump()

    for attempt in range(1, MAX_VERSION_ATTEMPTS + 1):
        version = await latest_version(decision.event_id) + 1
        try:
            result = await collection().insert_one({**payload, "version": version})
        except DuplicateKeyError:
            logger.warning(
                "Version %d for event %s was taken (attempt %d/%d); retrying",
                version,
                decision.event_id,
                attempt,
                MAX_VERSION_ATTEMPTS,
            )
            continue
        return str(result.inserted_id), version

    raise RuntimeError(
        f"Could not allocate a decision version for {decision.event_id!r} "
        f"after {MAX_VERSION_ATTEMPTS} attempts"
    )


async def list_decisions(
    event_id: str | None = None, *, history: bool = False
) -> list[dict[str, Any]]:
    """Return stored decisions, newest first.

    Args:
        event_id: Optionally restrict to one event.
        history: When False (the default) only the latest version per event is
            returned, since that is the current recommendation. When True, every
            version is returned so a re-decision can be compared with what it
            replaced.
    """
    query: dict[str, Any] = {"event_id": event_id} if event_id else {}

    if history:
        return (
            await collection()
            .find(query)
            .sort([("decided_at", DESCENDING), ("version", DESCENDING)])
            .to_list(length=None)
        )

    pipeline: list[dict[str, Any]] = []
    if query:
        pipeline.append({"$match": query})
    pipeline += [
        {"$sort": {"event_id": 1, "version": DESCENDING}},
        {"$group": {"_id": "$event_id", "document": {"$first": "$$ROOT"}}},
        {"$replaceRoot": {"newRoot": "$document"}},
        {"$sort": {"decided_at": DESCENDING, "event_id": 1}},
    ]
    return await collection().aggregate(pipeline).to_list(length=None)
